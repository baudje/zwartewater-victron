#!/usr/bin/env python3
"""Generate voltage profile diagrams for the hybrid LFP+FLA system."""

import matplotlib.pyplot as plt
import numpy as np
import os

OUT_DIR = os.path.dirname(__file__)

# --- Trojan L16H-AC OCV vs SoC (per cell, from Trojan SoC table) ---
TROJAN_SOC =  [0,     10,    20,    30,    40,    50,    60,    70,    80,    90,    100]
TROJAN_CELL = [1.895, 1.918, 1.943, 1.969, 1.993, 2.017, 2.040, 2.062, 2.083, 2.103, 2.122]
FLA_CELLS = 12

# --- EVE MB31 LFP OCV vs SoC (per cell, typical LiFePO4 curve) ---
LFP_SOC =  [0,    5,    10,   20,   30,   40,   50,   60,   70,   80,   90,   95,   100]
LFP_CELL = [2.50, 3.05, 3.15, 3.22, 3.25, 3.27, 3.28, 3.29, 3.30, 3.32, 3.35, 3.40, 3.55]
LFP_CELLS = 8


def voltage_profile():
    """Main diagram: OCV curves for both chemistries showing the operating zones."""

    fig, ax = plt.subplots(figsize=(14, 9))

    lfp_soc = np.array(LFP_SOC)
    lfp_pack = np.array(LFP_CELL) * LFP_CELLS
    fla_soc = np.array(TROJAN_SOC)
    fla_pack = np.array(TROJAN_CELL) * FLA_CELLS

    # Zone backgrounds
    ax.axhspan(25.5, 28.4, color='#2196F3', alpha=0.06, zorder=0)
    ax.axhspan(22.5, 25.5, color='#FF9800', alpha=0.06, zorder=0)
    ax.axhspan(28.4, 32.5, color='#d32f2f', alpha=0.04, zorder=0)

    # OCV curves
    ax.plot(lfp_soc, lfp_pack, color='#2196F3', linewidth=2.5, marker='o', markersize=4,
            label='LFP open-circuit voltage (8 cells)', zorder=5)
    ax.plot(fla_soc, fla_pack, color='#FF9800', linewidth=2.5, marker='s', markersize=4,
            label='FLA open-circuit voltage (12 cells, Trojan SoC table)', zorder=5)

    # Shaded gap
    fla_interp = np.interp(lfp_soc, fla_soc, fla_pack)
    ax.fill_between(lfp_soc, fla_interp, lfp_pack, alpha=0.06, color='#9C27B0',
                    where=lfp_pack > fla_interp, zorder=1)

    # Key voltage lines
    ax.axhline(y=28.4, color='#2196F3', linestyle='--', linewidth=1.2, alpha=0.7)
    ax.annotate('LFP daily charge target  28.4V (3.55V/cell)',
                xy=(102, 28.4), fontsize=8, color='#2196F3', va='center')

    ax.axhline(y=29.2, color='#d32f2f', linestyle=':', linewidth=1, alpha=0.6)
    ax.annotate('LFP absolute max  29.2V (3.65V/cell)',
                xy=(102, 29.2), fontsize=8, color='#d32f2f', va='center')

    ax.axhline(y=29.64, color='#FF9800', linestyle='--', linewidth=1, alpha=0.5)
    ax.annotate('FLA absorption  29.64V (2.47V/cell)',
                xy=(102, 29.64), fontsize=8, color='#e65100', va='center')

    ax.axhline(y=31.5, color='#FF9800', linestyle='--', linewidth=1, alpha=0.5)
    ax.annotate('FLA equalisation  31.5V (2.625V/cell)',
                xy=(102, 31.5), fontsize=8, color='#e65100', va='center')

    ax.axhline(y=22.4, color='#2196F3', linestyle=':', linewidth=0.8, alpha=0.4)
    ax.annotate('LFP cutoff  22.4V (2.80V/cell)',
                xy=(102, 22.4), fontsize=8, color='#2196F3', alpha=0.7, va='center')

    fla_100_v = TROJAN_CELL[-1] * FLA_CELLS
    ax.axhline(y=fla_100_v, color='#FF9800', linestyle=':', linewidth=0.8, alpha=0.5)
    ax.annotate(f'FLA 100% OCV  {fla_100_v:.1f}V (2.122V/cell)',
                xy=(102, fla_100_v), fontsize=8, color='#FF9800', alpha=0.8, va='center')

    # Crossover annotation
    lfp_fine = np.linspace(0, 100, 500)
    lfp_pack_fine = np.interp(lfp_fine, lfp_soc, lfp_pack)
    cross_idx = np.argmin(np.abs(lfp_pack_fine - fla_100_v))
    cross_soc = lfp_fine[cross_idx]

    ax.annotate(f'FLA starts discharging\nLFP ~{cross_soc:.0f}% SoC, ~{fla_100_v:.1f}V',
                xy=(cross_soc, fla_100_v),
                xytext=(cross_soc + 15, fla_100_v + 1.5),
                fontsize=9, fontweight='bold', color='#e65100',
                arrowprops=dict(arrowstyle='->', color='#e65100', lw=1.5),
                ha='center', zorder=10)

    # Zone labels
    ax.text(60, 27.2,
            'LFP handles all cycling\nFLA stays at ~100% SoC (float charged by LFP)',
            ha='center', va='center', fontsize=10, color='#1565C0', fontweight='bold', alpha=0.7,
            bbox=dict(boxstyle='round,pad=0.3', facecolor='white', alpha=0.8, edgecolor='none'))

    ax.text(60, 24.0,
            'LFP depleted — FLA takes over discharge\nKeeps DC bus alive if BMS disconnects LFPs',
            ha='center', va='center', fontsize=10, color='#e65100', fontweight='bold', alpha=0.7,
            bbox=dict(boxstyle='round,pad=0.3', facecolor='white', alpha=0.8, edgecolor='none'))

    ax.text(60, 30.5,
            'FLA CHARGE / EQUALISATION ZONE\nRelay must be open — LFPs on Orion',
            ha='center', va='center', fontsize=10, color='#c62828', fontweight='bold', alpha=0.6,
            bbox=dict(boxstyle='round,pad=0.3', facecolor='white', alpha=0.8, edgecolor='none'))

    ax.set_xlim(-2, 140)
    ax.set_ylim(21.5, 32.5)
    ax.set_xlabel('State of Charge (%)', fontsize=11)
    ax.set_ylabel('Pack Voltage (V)', fontsize=11)
    ax.set_title('Hybrid LFP + FLA Open-Circuit Voltage Profiles — Zwartewater 24V System',
                 fontsize=13, fontweight='bold')
    ax.legend(loc='upper left', fontsize=10)
    ax.grid(True, alpha=0.2)
    ax.set_xticks(range(0, 101, 10))

    plt.tight_layout()
    path = os.path.join(OUT_DIR, 'voltage-profiles.png')
    fig.savefig(path, dpi=150, bbox_inches='tight')
    plt.close()
    print(f'Saved: {path}')


def _plot_sequence(ax, title, high_voltage, high_label,
                   t_charge_end, t_relay_open=0):
    """Plot a charge/EQ sequence on a single axes.

    The float reduction + voltage matching is ONE phase in the code:
    wait_for_match() sets CVL to float, then checks delta every 30s.
    Relay closes the moment delta < 1V as Trojan drops from high voltage.
    """
    orion_v = 27.2  # Orion holds LFP at ~27.2V
    float_v = 27.0
    settle_v = 26.5  # Resting voltage after reconnect

    # Time for Trojan to drop from high voltage to within 1V of LFP
    match_time = 0.8 * (high_voltage - orion_v)  # ~2h for absorption, ~3.5h for EQ

    # --- Charging phase (relay already open) ---
    if t_relay_open > 0:
        # Phase 1: shared charging (charge service only)
        t_p1 = np.linspace(0, t_relay_open, 50)
        v_bus_p1 = 25.5 + (28.4 - 25.5) * (1 - np.exp(-t_p1 / 1.5))
        ax.plot(t_p1, v_bus_p1, color='#FF9800', linewidth=2)
        ax.plot(t_p1, v_bus_p1, color='#2196F3', linewidth=2)  # Same — both on bus
        ax.axvspan(0, t_relay_open, color='#1a472a', alpha=0.08)
        ax.text(t_relay_open / 2, high_voltage + 1.0, 'Phase 1\nShared charging',
                ha='center', fontsize=9, color='#1a472a', fontweight='bold')
        # Disconnect
        ax.plot([t_relay_open, t_relay_open + 0.05], [28.4, 28.4], color='#FF9800', linewidth=2)
        ax.plot([t_relay_open, t_relay_open + 0.05], [28.4, orion_v], color='#2196F3', linewidth=2)

    t_charge_start = t_relay_open + 0.05
    t_charge_abs = t_charge_start + t_charge_end

    # --- High-voltage charging (absorption or EQ) ---
    t_hv = np.linspace(t_charge_start, t_charge_abs, 60)
    v_trojan_hv = 28.4 + (high_voltage - 28.4) * (1 - np.exp(-(t_hv - t_charge_start) / 0.5))
    v_lfp_hv = orion_v * np.ones(60)

    ax.plot(t_hv, v_trojan_hv, color='#FF9800', linewidth=2)
    ax.plot(t_hv, v_lfp_hv, color='#2196F3', linewidth=2)
    ax.axvspan(t_charge_start, t_charge_abs, color='#8B0000' if high_voltage > 30 else '#8B4513',
               alpha=0.08)
    ax.text((t_charge_start + t_charge_abs) / 2, high_voltage + 1.0,
            f'{high_label}\n{high_voltage}V',
            ha='center', fontsize=9,
            color='#8B0000' if high_voltage > 30 else '#8B4513', fontweight='bold')

    # --- Float + voltage matching (ONE phase in code) ---
    # CVL drops to float, Trojan voltage decays from high_voltage toward float
    # Relay closes when Trojan drops to within 1V of LFP (orion_v)
    t_match = np.linspace(t_charge_abs, t_charge_abs + match_time, 80)
    # Trojan: exponential decay from high_voltage toward float_v
    tau = match_time / 3  # Time constant
    v_trojan_match = float_v + (high_voltage - float_v) * np.exp(-(t_match - t_charge_abs) / tau)
    v_lfp_match = orion_v * np.ones(80)

    ax.plot(t_match, v_trojan_match, color='#FF9800', linewidth=2)
    ax.plot(t_match, v_lfp_match, color='#2196F3', linewidth=2)
    ax.axvspan(t_charge_abs, t_charge_abs + match_time, color='#4a148c', alpha=0.05)
    ax.text((t_charge_abs + t_charge_abs + match_time) / 2, high_voltage + 1.0,
            'CVL → float (27V)\nWaiting for delta < 1V',
            ha='center', fontsize=9, color='#4a148c', fontweight='bold')

    # Find where Trojan crosses orion_v + 1.0 (delta = 1V threshold)
    threshold = orion_v + 1.0  # 28.2V
    cross_idx = np.argmin(np.abs(v_trojan_match - threshold))
    t_cross = t_match[cross_idx]
    v_cross = v_trojan_match[cross_idx]

    # --- Relay closes at delta < 1V ---
    t_reconnect = t_cross
    ax.axvline(x=t_reconnect, color='#2e7d32', linestyle='--', linewidth=1.5, alpha=0.7)
    ax.text(t_reconnect, high_voltage + 1.8, 'RELAY CLOSES\n(delta < 1V)',
            ha='center', fontsize=8, color='#2e7d32', fontweight='bold')

    # Delta annotation at reconnect point
    ax.annotate('', xy=(t_reconnect - 0.1, v_cross),
                xytext=(t_reconnect - 0.1, orion_v),
                arrowprops=dict(arrowstyle='<->', color='#2e7d32', lw=1.5))
    ax.text(t_reconnect - 0.3, (v_cross + orion_v) / 2, '~1V',
            fontsize=9, color='#2e7d32', fontweight='bold', ha='right', va='center')

    # After reconnect: voltages converge — Trojan drops, LFP rises, meet in middle
    equilibrium_v = (v_cross + orion_v) / 2  # They meet halfway
    t_settle = np.linspace(t_reconnect, t_reconnect + 1.5, 30)
    v_trojan_settle = equilibrium_v + (v_cross - equilibrium_v) * np.exp(-(t_settle - t_reconnect) / 0.15)
    v_lfp_settle = equilibrium_v + (orion_v - equilibrium_v) * np.exp(-(t_settle - t_reconnect) / 0.15)

    ax.plot(t_settle, v_trojan_settle, color='#FF9800', linewidth=2)
    ax.plot(t_settle, v_lfp_settle, color='#2196F3', linewidth=2)
    ax.axvspan(t_reconnect, t_reconnect + 1.5, color='#1a472a', alpha=0.08)
    ax.text(t_reconnect + 0.75, high_voltage + 1.0, 'Normal\noperation',
            ha='center', fontsize=9, color='#1a472a', fontweight='bold')

    # Relay open marker
    ax.axvline(x=t_relay_open, color='#d32f2f', linestyle='--', linewidth=1.5, alpha=0.7)
    ax.text(t_relay_open, high_voltage + 1.8, 'RELAY\nOPENS',
            ha='center', fontsize=8, color='#d32f2f', fontweight='bold')

    # LFP safe line
    ax.axhline(y=28.4, color='#2196F3', linestyle=':', linewidth=0.8, alpha=0.5)
    ax.annotate('LFP safe max 28.4V', xy=(0.1, 28.55), fontsize=7, color='#2196F3', alpha=0.7)

    # Legend
    ax.plot([], [], color='#FF9800', linewidth=2, label='Trojan FLA (bus voltage)')
    ax.plot([], [], color='#2196F3', linewidth=2, label='LFP (on Orion when relay open)')

    ax.set_ylabel('Voltage (V)', fontsize=11)
    ax.set_ylim(24.5, high_voltage + 2.5)
    ax.set_title(title, fontsize=12, fontweight='bold')
    ax.legend(loc='center right', fontsize=9)
    ax.grid(True, alpha=0.2)


def charge_sequence():
    """Diagram: FLA charge and EQ sequences showing voltage over time."""

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 10))

    _plot_sequence(ax1,
                   title='FLA Charge Sequence (Trojan SoC < 85%)',
                   high_voltage=29.64,
                   high_label='Phase 2-3\nFLA absorption',
                   t_charge_end=3.5,
                   t_relay_open=4)  # Phase 1 shared charging first

    _plot_sequence(ax2,
                   title='FLA Equalisation Sequence (every 90 days)',
                   high_voltage=31.5,
                   high_label='Equalisation',
                   t_charge_end=2.5,
                   t_relay_open=0)  # Relay opens immediately

    ax2.set_xlabel('Time (hours)', fontsize=11)

    plt.tight_layout()
    path = os.path.join(OUT_DIR, 'charge-sequences.png')
    fig.savefig(path, dpi=150, bbox_inches='tight')
    plt.close()
    print(f'Saved: {path}')


if __name__ == '__main__':
    voltage_profile()
    charge_sequence()
