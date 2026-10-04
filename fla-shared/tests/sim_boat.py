"""SimBoat — a crude simulation of Zwartewater's DC system for scenario tests.

It stands in for DbusMonitor, the temp battery subprocess, the aggregate driver
and the clock, so a scenario can run the REAL service tick, Takeover, relay
control and reconnect hold end to end, including a reboot in the middle.

The physics is deliberately rough (first-order voltage ramps, fixed loads). It
only has to reproduce the behaviours that matter for safety:

  - ESS "Optimized" feeds the boat from the DVCC-selected battery, even on
    shore power; "Keep batteries charged" charges it to the selected BMS's CVL.
  - With relay 2 open the Orion draws from the Trojans to charge the LFPs.
  - DVCC pointing at a BMS that is not running = "BMS lost": no charging.
  - A reboot kills the processes, closes relay 2 and keeps the settings.

Numbers come from the 2026-10-03 incident logs. Not a battery model.
"""

LOAD_A = 30.0          # boat AC load seen from the DC side
ORION_A = 16.0         # Orion DC-DC draw from the Trojans while relay 2 is open
CHARGER_A = 60.0       # Quattro charge current into the Trojans
ESS_KEEP_CHARGED = 9
TEMP_INSTANCE, AGGREGATE_INSTANCE = 100, 99
MAX_SIM_HOURS = 12     # a scenario running longer than this is stuck in a hold


class SimReboot(BaseException):
    """Power cut. A BaseException so it passes through `except Exception`, like
    a real one. Python still runs `finally` blocks on the way out, which a real
    power cut does not: scenarios call SimBoat.reboot() only after it has
    propagated, so those blocks see the pre-reboot state (relay still open)."""


class SimBoat:
    def __init__(self, tmpdir, ess_state=10, bus_voltage=26.9):
        self.t = 1_000_000.0
        self._t0 = self.t
        self.tmpdir = tmpdir
        # persistent settings (survive a reboot)
        self.battery_service = "com.victronenergy.battery/277"
        self.bms_instance = AGGREGATE_INSTANCE
        self.max_charge_voltage = 32.0
        self.ess_state = ess_state
        self.shared_voltage_sense = 1
        # volatile state
        self.relay = 1
        self.temp_cvl = None          # temp battery CVL; None = subprocess not running
        self.aggregate_running = True
        self.ac = True
        self.ess_writable = True
        self._systemcalc_down = False
        self._last_cvl = 27.0
        # plant
        self.v_trojan = self.v_lfp = bus_voltage   # 28.4 = LFP bank in absorption
        self.i_trojan = 0.0
        self.trojan_soc = 100.0
        self._tail = 40.0             # absorption tail current, decays at the target
        # what the scenarios assert on
        self.isolated_discharge_s = 0.0   # relay open AND Trojans discharging > 5A
        self.bms_lost_s = 0.0             # DVCC selects a BMS that is not running
        self.relay_open_s = 0.0
        self.svs_at_high_cvl_s = 0.0      # isolated, CVL above LFP-safe, Quattro on the late sense voltage
        self.lfp_overvoltage_s = 0.0      # relay closed while DVCC's CVL is above LFP-safe 28.4V
        self.min_v_trojan = self.v_trojan
        self.peak_v_trojan = self.v_trojan
        self.events = []                  # optional hooks: fn(sim) called every step

    # ---- clock (patched in for time.time / time.monotonic / time.sleep) ----
    def now(self):
        return self.t

    def sleep(self, seconds):
        remaining = float(seconds)
        while remaining > 0:
            dt = min(5.0, remaining)
            self._step(dt)
            remaining -= dt
        if self.t - self._t0 > MAX_SIM_HOURS * 3600:
            raise AssertionError("simulation ran %dh — stuck in a hold?" % MAX_SIM_HOURS)

    # ---- plant ----
    def _cvl(self):
        """Charge voltage limit DVCC currently gets, or None when BMS is lost."""
        if self.bms_instance == TEMP_INSTANCE:
            cvl = self.temp_cvl
        elif self.bms_instance == AGGREGATE_INSTANCE:
            cvl = 27.0 if self.aggregate_running else None
        else:
            cvl = 27.0
        return None if cvl is None else min(cvl, self.max_charge_voltage)

    def _step(self, dt):
        self.t += dt
        # While systemcalc restarts nothing re-evaluates DVCC: the chargers keep
        # their last limits. (The handoff stops the aggregate ~4 min before the
        # selection moves to the temp battery; the boat raised no BMS alarm in
        # that window on 2026-10-03 or 2026-10-04.)
        cvl = self._last_cvl if self._systemcalc_down else self._cvl()
        self._last_cvl = cvl
        if cvl is None:
            self.bms_lost_s += dt
        minutes = dt / 60.0
        if self.relay == 1:
            self.i_trojan = 0.0   # the LFP bank carries the cycling
            if cvl is not None and cvl > 28.45:
                self.lfp_overvoltage_s += dt
            self.v_trojan = self.v_lfp
        else:
            self.relay_open_s += dt
            if cvl is not None and cvl > 28.45 and self.shared_voltage_sense:
                self.svs_at_high_cvl_s += dt
            # On the Orion a full LFP bank barely moves (28.84V -> 28.13V in 11
            # hours on 2026-10-04).
            if self.v_lfp > 27.0:
                self.v_lfp = max(27.0, self.v_lfp - 0.065 * minutes / 60)
            inverting = (not self.ac) or (self.ess_state != ESS_KEEP_CHARGED
                                           and self.trojan_soc > 20.0)
            charging = self.ac and cvl is not None and not inverting
            if inverting:
                self.i_trojan = -(LOAD_A + ORION_A)
                self.v_trojan -= (3.0 if self.v_trojan > 27.5 else
                                  0.6 if self.v_trojan > 24.6 else 0.005) * minutes
                self.trojan_soc -= -self.i_trojan * dt / 3600 / 435 * 100
            elif charging and self.v_trojan < cvl - 0.05:
                self.i_trojan = CHARGER_A - ORION_A
                self.v_trojan = min(cvl, self.v_trojan + 1.0 * minutes)
                self._tail = 40.0
            elif charging and self.v_trojan > cvl + 0.05:
                self.i_trojan = 0.0   # CVL lowered: surface charge relaxes fast
                self.v_trojan = max(cvl, self.v_trojan - 30.0 * minutes)
            elif charging:
                self._tail = max(5.0, self._tail - 1.0 * minutes)
                self.i_trojan = self._tail
            else:                     # BMS lost, AC present: only the Orion draws
                self.i_trojan = -ORION_A
                self.v_trojan -= 0.2 * minutes
            if self.i_trojan < -5.0:
                self.isolated_discharge_s += dt
        self.min_v_trojan = min(self.min_v_trojan, self.v_trojan)
        self.peak_v_trojan = max(self.peak_v_trojan, self.v_trojan)
        for hook in list(self.events):
            hook(self)

    def reboot(self):
        """Cerbo power cycle: processes die, relay 2 boot-closes, the aggregate
        driver comes back with the OS, settings and /data files persist."""
        self.temp_cvl = None
        self.relay = 1
        self.aggregate_running = True
        self.events = []
        self._systemcalc_down = False
        self._step(1.0)

    # ---- DbusMonitor surface ----
    def get_trojan_voltage(self): return round(self.v_trojan, 2)
    def get_lfp_voltage(self): return round(self.v_lfp, 2)
    def get_trojan_current(self): return round(self.i_trojan, 1)
    def get_lfp_current(self): return ORION_A if self.relay == 0 else 0.0
    def get_trojan_soc(self): return self.trojan_soc
    def get_lfp_soc(self): return 99.0
    def get_battery_temperature(self): return 25.0
    def get_relay_state(self): return self.relay

    def set_relay(self, state):
        self.relay = state
        return True

    def get_battery_service_setting(self): return self.battery_service

    def set_battery_service_setting(self, value):
        self.battery_service = value
        return True

    def get_bms_instance(self): return self.bms_instance

    def set_bms_instance(self, instance):
        self.bms_instance = instance
        return True

    def get_dvcc_max_charge_voltage(self): return self.max_charge_voltage

    def set_dvcc_max_charge_voltage(self, voltage):
        self.max_charge_voltage = voltage
        return True

    def get_ess_state(self): return self.ess_state

    def set_ess_state(self, state):
        if not self.ess_writable:
            return False
        self.ess_state = state
        return True

    def get_shared_voltage_sense(self): return self.shared_voltage_sense

    def set_shared_voltage_sense(self, value):
        self.shared_voltage_sense = value
        return True

    def _alive(self, instance):
        return (self.temp_cvl is not None if instance == TEMP_INSTANCE
                else self.aggregate_running if instance == AGGREGATE_INSTANCE else True)

    def restart_systemcalc(self, system_timeout=300, system_poll=1.0, should_abort=None):
        self._systemcalc_down = True
        try:
            self.sleep(240)   # the v3.80 restart takes ~4 minutes (incident log)
        finally:
            self._systemcalc_down = False
        return True

    def wait_for_service_instance(self, instance, prefix="com.victronenergy.battery",
                                  timeout_seconds=120, poll_interval=0.5, should_abort=None):
        return "sim/%d" % instance if self._alive(instance) else None

    def wait_for_bms_selection(self, battery_service, bms_instance,
                               timeout_seconds=5, poll_interval=0.5):
        return (self.battery_service == battery_service
                and self.bms_instance == bms_instance and self._alive(bms_instance))

    def invalidate_services(self):
        pass

    # ---- fakes for the temp battery subprocess and the aggregate driver ----
    def temp_battery_class(sim):
        class SimTempBattery:
            def __init__(self, device_instance=TEMP_INSTANCE, trojan_instance=279):
                pass

            def register(self, charge_voltage, charge_current, discharge_current=0):
                sim.temp_cvl = charge_voltage
                return True

            def attach(self):
                return True

            def set_charge_voltage(self, voltage):
                if sim.temp_cvl is not None:
                    sim.temp_cvl = voltage

            def update_voltage_current(self, voltage, current):
                pass

            def deregister(self):
                sim.temp_cvl = None
        return SimTempBattery

    def aggregate_driver(sim):
        class SimAggregateDriver:
            @staticmethod
            def stop():
                sim.aggregate_running = False
                return True

            @staticmethod
            def start():
                sim.aggregate_running = True
                return True
        return SimAggregateDriver
