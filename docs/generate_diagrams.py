#!/usr/bin/env python3
"""Generate voltage profile diagrams for the hybrid LFP+FLA system."""

import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
import os

OUT_DIR = os.path.dirname(__file__)


def voltage_profile():
    """Main diagram: hybrid system voltage zones with both battery profiles."""

    fig, ax = plt.subplots(figsize=(14, 8))

    # --- Zone backgrounds ---
    # Normal parallel operation (relay closed)
    ax.axhspan(22, 28.4, color='#1a472a', alpha=0.15, zorder=0)
    # LFP absolute max zone (danger if relay closed)
    ax.axhspan(28.4, 29.2, color='#8B6914', alpha=0.10, zorder=0)
    # FLA absorption zone (relay MUST be open)
    ax.axhspan(29.2, 30.2, color='#8B4513', alpha=0.10, zorder=0)
    # FLA equalisation zone (relay MUST be open)
    ax.axhspan(30.2, 32.5, color='#8B0000', alpha=0.10, zorder=0)

    # --- LFP voltage vs SoC curve (8 cells) ---
    # EVE MB31 typical discharge curve (flat in the middle)
    lfp_soc = np.array([0, 2, 5, 10, 20, 30, 40, 50, 60, 70, 80, 90, 95, 98, 100])
    lfp_cell = np.array([2.50, 2.80, 3.05, 3.20, 3.24, 3.26, 3.27, 3.28, 3.29, 3.30, 3.32, 3.35, 3.40, 3.48, 3.55])
    lfp_pack = lfp_cell * 8

    ax.plot(lfp_soc, lfp_pack, color='#2196F3', linewidth=2.5, label='LFP pack (8 cells)', zorder=5)
    ax.fill_between(lfp_soc, lfp_pack, alpha=0.08, color='#2196F3', zorder=1)

    # --- FLA voltage vs SoC curve (12 cells) ---
    # Trojan L16H-AC typical at rest (open-circuit)
    fla_soc = np.array([0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100])
    fla_cell = np.array([1.75, 1.88, 1.95, 2.00, 2.03, 2.06, 2.09, 2.12, 2.15, 2.18, 2.13])
    # Under charge (higher voltage due to charge acceptance)
    fla_cell_charge = np.array([1.75, 1.95, 2.02, 2.07, 2.10, 2.13, 2.16, 2.19, 2.23, 2.30, 2.42])
    fla_pack_charge = fla_cell_charge * 12

    ax.plot(fla_soc, fla_pack_charge, color='#FF9800', linewidth=2.5, label='FLA pack (12 cells, under charge)', zorder=5)
    ax.fill_between(fla_soc, fla_pack_charge, alpha=0.08, color='#FF9800', zorder=1)

    # --- Key voltage lines ---
    ax.axhline(y=28.4, color='#2196F3', linestyle='--', linewidth=1, alpha=0.7)
    ax.annotate('LFP daily charge  28.4V (3.55V/cell)', xy=(102, 28.4),
                fontsize=8, color='#2196F3', va='center')

    ax.axhline(y=29.2, color='#2196F3', linestyle=':', linewidth=1, alpha=0.7)
    ax.annotate('LFP absolute max  29.2V (3.65V/cell)', xy=(102, 29.2),
                fontsize=8, color='#d32f2f', va='center')

    ax.axhline(y=29.64, color='#FF9800', linestyle='--', linewidth=1, alpha=0.7)
    ax.annotate('FLA absorption  29.64V (2.47V/cell)', xy=(102, 29.64),
                fontsize=8, color='#FF9800', va='center')

    ax.axhline(y=31.5, color='#FF9800', linestyle='--', linewidth=1, alpha=0.7)
    ax.annotate('FLA equalisation  31.5V (2.625V/cell)', xy=(102, 31.5),
                fontsize=8, color='#FF9800', va='center')

    ax.axhline(y=22.4, color='#2196F3', linestyle=':', linewidth=1, alpha=0.4)
    ax.annotate('LFP min  22.4V (2.80V/cell)', xy=(102, 22.4),
                fontsize=8, color='#2196F3', alpha=0.7, va='center')

    ax.axhline(y=27.0, color='#FF9800', linestyle=':', linewidth=1, alpha=0.4)
    ax.annotate('FLA float  27.0V (2.25V/cell)', xy=(102, 27.0),
                fontsize=8, color='#FF9800', alpha=0.7, va='center')

    # --- Zone labels ---
    ax.text(50, 25.5, 'NORMAL OPERATION\nRelay closed — both banks on bus\nLFP cycles, FLA stays near 100%',
            ha='center', va='center', fontsize=10, color='#1a472a', fontweight='bold', alpha=0.8)

    ax.text(50, 29.65, 'FLA ABSORPTION — relay open, LFPs on Orion',
            ha='center', va='center', fontsize=9, color='#8B4513', fontweight='bold', alpha=0.8)

    ax.text(50, 31.5, 'FLA EQUALISATION — relay open, LFPs on Orion',
            ha='center', va='center', fontsize=9, color='#8B0000', fontweight='bold', alpha=0.8)

    # --- Relay annotation ---
    ax.annotate('RELAY OPENS',
                xy=(95, 28.4), xytext=(80, 30.5),
                fontsize=9, fontweight='bold', color='#d32f2f',
                arrowprops=dict(arrowstyle='->', color='#d32f2f', lw=1.5),
                ha='center')

    # --- Formatting ---
    ax.set_xlim(-2, 140)
    ax.set_ylim(21, 33)
    ax.set_xlabel('State of Charge (%)', fontsize=11)
    ax.set_ylabel('Pack Voltage (V)', fontsize=11)
    ax.set_title('Hybrid LFP + FLA Voltage Profiles — Zwartewater 24V System', fontsize=13, fontweight='bold')
    ax.legend(loc='lower right', fontsize=10)
    ax.grid(True, alpha=0.2)
    ax.set_xticks(range(0, 101, 10))

    plt.tight_layout()
    path = os.path.join(OUT_DIR, 'voltage-profiles.png')
    fig.savefig(path, dpi=150, bbox_inches='tight')
    plt.close()
    print(f'Saved: {path}')


def charge_sequence():
    """Diagram: FLA charge/EQ sequence showing voltage over time."""

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 10), sharex=True)

    # --- FLA Charge Sequence (top) ---
    # Time segments (hours)
    t_phase1 = np.linspace(0, 4, 50)       # Phase 1: shared charging
    t_disconnect = np.array([4, 4.1])       # Relay opens
    t_phase23 = np.linspace(4.1, 8, 50)    # Phase 2-3: FLA absorption
    t_float = np.linspace(8, 9, 20)        # Float reduction
    t_match = np.linspace(9, 12, 40)       # Voltage matching
    t_reconnect = np.array([12, 12.1])      # Relay closes
    t_normal = np.linspace(12.1, 13, 10)   # Normal operation

    # Bus voltage (what Quattro charges to)
    bus_p1 = 26.0 + (28.4 - 26.0) * (1 - np.exp(-t_phase1 / 1.5))
    bus_disc = np.array([28.4, 28.4])
    bus_p23 = 28.4 + (29.64 - 28.4) * (1 - np.exp(-(t_phase23 - 4.1) / 0.3))
    bus_float = 29.64 - (29.64 - 27.0) * np.linspace(0, 1, 20)
    bus_match = 27.0 * np.ones(40) - 0.3 * np.exp(-np.linspace(0, 5, 40))
    bus_recon = np.array([26.7, 26.8])
    bus_norm = 26.8 * np.ones(10)

    # Trojan voltage (follows bus when connected, IS the bus in phase 2-3)
    trojan_p1 = bus_p1
    trojan_disc = bus_disc
    trojan_p23 = bus_p23
    trojan_float = bus_float
    trojan_match = bus_match
    trojan_recon = bus_recon
    trojan_norm = bus_norm

    # LFP voltage
    lfp_p1 = bus_p1  # Same as bus (parallel)
    lfp_disc = np.array([28.4, 27.5])  # Drops to Orion voltage
    lfp_p23 = 27.5 - 0.3 * np.exp(-np.linspace(0, 3, 50)) + 0.3  # Orion holds ~27.2-27.5V
    lfp_float = np.linspace(27.2, 27.0, 20)
    lfp_match = 27.0 * np.ones(40) - 0.1 * np.exp(-np.linspace(0, 5, 40))  # Converging
    lfp_recon = np.array([26.9, 26.8])
    lfp_norm = 26.8 * np.ones(10)

    # Plot Trojan
    for t, v in [(t_phase1, trojan_p1), (t_disconnect, trojan_disc), (t_phase23, trojan_p23),
                 (t_float, trojan_float), (t_match, trojan_match), (t_reconnect, trojan_recon),
                 (t_normal, trojan_norm)]:
        ax1.plot(t, v, color='#FF9800', linewidth=2)
    ax1.plot([], [], color='#FF9800', linewidth=2, label='Trojan FLA')

    # Plot LFP
    for t, v in [(t_phase1, lfp_p1), (t_disconnect, lfp_disc), (t_phase23, lfp_p23),
                 (t_float, lfp_float), (t_match, lfp_match), (t_reconnect, lfp_recon),
                 (t_normal, lfp_norm)]:
        ax1.plot(t, v, color='#2196F3', linewidth=2)
    ax1.plot([], [], color='#2196F3', linewidth=2, label='LFP (on Orion when relay open)')

    # Phase backgrounds
    ax1.axvspan(0, 4, color='#1a472a', alpha=0.08)
    ax1.axvspan(4, 8, color='#8B4513', alpha=0.08)
    ax1.axvspan(8, 9, color='#FFD700', alpha=0.08)
    ax1.axvspan(9, 12, color='#4a148c', alpha=0.05)
    ax1.axvspan(12, 13, color='#1a472a', alpha=0.08)

    # Phase labels
    ax1.text(2, 30.5, 'Phase 1\nShared\ncharging', ha='center', fontsize=9, color='#1a472a', fontweight='bold')
    ax1.text(6, 30.5, 'Phase 2-3\nFLA absorption\n29.64V', ha='center', fontsize=9, color='#8B4513', fontweight='bold')
    ax1.text(8.5, 30.5, 'Float\n27V', ha='center', fontsize=9, color='#8B6914', fontweight='bold')
    ax1.text(10.5, 30.5, 'Voltage\nmatching', ha='center', fontsize=9, color='#4a148c', fontweight='bold')
    ax1.text(12.5, 30.5, 'Normal', ha='center', fontsize=9, color='#1a472a', fontweight='bold')

    # Relay markers
    ax1.axvline(x=4, color='#d32f2f', linestyle='--', linewidth=1.5, alpha=0.7)
    ax1.text(4, 31.2, 'RELAY\nOPENS', ha='center', fontsize=8, color='#d32f2f', fontweight='bold')
    ax1.axvline(x=12, color='#2e7d32', linestyle='--', linewidth=1.5, alpha=0.7)
    ax1.text(12, 31.2, 'RELAY\nCLOSES', ha='center', fontsize=8, color='#2e7d32', fontweight='bold')

    # LFP safe line
    ax1.axhline(y=28.4, color='#2196F3', linestyle=':', linewidth=0.8, alpha=0.5)
    ax1.annotate('LFP max 28.4V', xy=(0.2, 28.55), fontsize=7, color='#2196F3', alpha=0.7)

    ax1.set_ylabel('Voltage (V)', fontsize=11)
    ax1.set_ylim(25.5, 31.5)
    ax1.set_title('FLA Charge Sequence', fontsize=12, fontweight='bold')
    ax1.legend(loc='lower right', fontsize=9)
    ax1.grid(True, alpha=0.2)

    # --- FLA Equalisation Sequence (bottom) ---
    t_eq_start = np.array([0, 0.1])
    t_eq = np.linspace(0.1, 2.5, 50)
    t_eq_float = np.linspace(2.5, 3, 20)
    t_eq_match = np.linspace(3, 6, 40)
    t_eq_recon = np.array([6, 6.1])
    t_eq_norm = np.linspace(6.1, 7, 10)

    # Trojan during EQ
    trojan_start = np.array([28.4, 28.4])
    trojan_eq = 28.4 + (31.5 - 28.4) * (1 - np.exp(-(t_eq - 0.1) / 0.5))
    trojan_eq_float = 31.5 - (31.5 - 27.0) * np.linspace(0, 1, 20)
    trojan_eq_match = 27.0 * np.ones(40) - 0.3 * np.exp(-np.linspace(0, 5, 40))
    trojan_eq_recon = np.array([26.7, 26.8])
    trojan_eq_norm = 26.8 * np.ones(10)

    # LFP during EQ (on Orion the whole time)
    lfp_start = np.array([28.4, 27.5])
    lfp_eq = 27.5 - 0.2 * np.exp(-np.linspace(0, 3, 50)) + 0.2
    lfp_eq_float = np.linspace(27.3, 27.0, 20)
    lfp_eq_match = 27.0 * np.ones(40) - 0.1 * np.exp(-np.linspace(0, 5, 40))
    lfp_eq_recon = np.array([26.9, 26.8])
    lfp_eq_norm = 26.8 * np.ones(10)

    for t, v in [(t_eq_start, trojan_start), (t_eq, trojan_eq), (t_eq_float, trojan_eq_float),
                 (t_eq_match, trojan_eq_match), (t_eq_recon, trojan_eq_recon), (t_eq_norm, trojan_eq_norm)]:
        ax2.plot(t, v, color='#FF9800', linewidth=2)
    ax2.plot([], [], color='#FF9800', linewidth=2, label='Trojan FLA')

    for t, v in [(t_eq_start, lfp_start), (t_eq, lfp_eq), (t_eq_float, lfp_eq_float),
                 (t_eq_match, lfp_eq_match), (t_eq_recon, lfp_eq_recon), (t_eq_norm, lfp_eq_norm)]:
        ax2.plot(t, v, color='#2196F3', linewidth=2)
    ax2.plot([], [], color='#2196F3', linewidth=2, label='LFP (on Orion)')

    # Phase backgrounds
    ax2.axvspan(0, 0.1, color='#d32f2f', alpha=0.08)
    ax2.axvspan(0.1, 2.5, color='#8B0000', alpha=0.08)
    ax2.axvspan(2.5, 3, color='#FFD700', alpha=0.08)
    ax2.axvspan(3, 6, color='#4a148c', alpha=0.05)
    ax2.axvspan(6, 7, color='#1a472a', alpha=0.08)

    ax2.text(1.3, 32.5, 'Equalisation\n31.5V', ha='center', fontsize=9, color='#8B0000', fontweight='bold')
    ax2.text(2.75, 32.5, 'Float\n27V', ha='center', fontsize=9, color='#8B6914', fontweight='bold')
    ax2.text(4.5, 32.5, 'Voltage matching', ha='center', fontsize=9, color='#4a148c', fontweight='bold')
    ax2.text(6.5, 32.5, 'Normal', ha='center', fontsize=9, color='#1a472a', fontweight='bold')

    ax2.axvline(x=0, color='#d32f2f', linestyle='--', linewidth=1.5, alpha=0.7)
    ax2.text(0, 33.2, 'RELAY\nOPENS', ha='center', fontsize=8, color='#d32f2f', fontweight='bold')
    ax2.axvline(x=6, color='#2e7d32', linestyle='--', linewidth=1.5, alpha=0.7)
    ax2.text(6, 33.2, 'RELAY\nCLOSES', ha='center', fontsize=8, color='#2e7d32', fontweight='bold')

    ax2.axhline(y=28.4, color='#2196F3', linestyle=':', linewidth=0.8, alpha=0.5)
    ax2.annotate('LFP max 28.4V', xy=(0.2, 28.55), fontsize=7, color='#2196F3', alpha=0.7)

    ax2.set_xlabel('Time (hours)', fontsize=11)
    ax2.set_ylabel('Voltage (V)', fontsize=11)
    ax2.set_ylim(25.5, 33.5)
    ax2.set_title('FLA Equalisation Sequence', fontsize=12, fontweight='bold')
    ax2.legend(loc='lower right', fontsize=9)
    ax2.grid(True, alpha=0.2)

    plt.tight_layout()
    path = os.path.join(OUT_DIR, 'charge-sequences.png')
    fig.savefig(path, dpi=150, bbox_inches='tight')
    plt.close()
    print(f'Saved: {path}')


if __name__ == '__main__':
    voltage_profile()
    charge_sequence()
