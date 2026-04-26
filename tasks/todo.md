# Pending real-world verification

These items can only be confirmed by observing the live system over a normal
operating cycle — they're listed here so they don't get lost after PR #7 merges.

## From PR #7 (SoC-reset + SmartShunt patches)

- [ ] **Next engine run**: confirm alternator charge is visible in aggregate
      `/Dc/0/Current` and in VRM (it should show as a positive current into
      the bank, matching SmartShunt LFP). Before the patch this was always
      ~0 because Orion DC-DC isn't summed by the upstream driver.
- [ ] **Next full charge cycle (with shore power)**: confirm the 30-min
      `SWITCH_TO_FLOAT_WAIT_FOR_SEC` hold actually runs at 28.4V CVL, and that
      both Boven and Onder reach SoC = 100% (the JK BMS internal SoC reset
      should trigger now that `SOC-100% Volt` is at 3.545V).
- [ ] **SmartShunt LFP (instance 277)**: confirm its own SoC syncs to 100%
      during the same cycle (charged-detection requires sustained 28.4V +
      tail current — should now have enough hold time).
- [ ] **Verify-patches boot hook**: after the next Cerbo reboot, confirm
      `/data/var/zwartewater-patches.status` is updated with a fresh
      timestamp and shows `OK:`.
