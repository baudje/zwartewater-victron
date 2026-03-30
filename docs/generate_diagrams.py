#!/usr/bin/env python3
"""Generate voltage profile diagrams for the hybrid LFP+FLA system."""

import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
import os

OUT_DIR = os.path.dirname(__file__)

# --- Trojan L16H-AC OCV vs SoC (per cell, from Trojan SoC table) ---
# Standard flooded lead-acid specific gravity / OCV relationship
TROJAN_SOC =  [0,    10,   20,   30,   40,   50,   60,   70,   80,   90,   100]
TROJAN_CELL = [1.895, 1.918, 1.943, 1.969, 1.993, 2.017, 2.040, 2.062, 2.083, 2.103, 2.122]
FLA_CELLS = 12  # 4x 6V batteries, 3 cells each

# --- EVE MB31 LFP OCV vs SoC (per cell, typical LiFePO4 curve) ---
LFP_SOC =  [0,    5,    10,   20,   30,   40,   50,   60,   70,   80,   90,   95,   100]
LFP_CELL = [2.50, 3.05, 3.15, 3.22, 3.25, 3.27, 3.28, 3.29, 3.30, 3.32, 3.35, 3.40, 3.55]
LFP_CELLS = 8


def voltage_profile():
    """Main diagram: OCV curves for both chemistries showing the operating zones."""

    fig, ax = plt.subplots(figsize=(14, 9))

    # Convert to pack voltages
    lfp_soc = np.array(LFP_SOC)
    lfp_pack = np.array(LFP_CELL) * LFP_CELLS
    fla_soc = np.array(TROJAN_SOC)
    fla_pack = np.array(TROJAN_CELL) * FLA_CELLS

    # --- Zone backgrounds ---
    # LFP-only discharge zone (bus voltage above FLA OCV)
    ax.axhspan(25.5, 28.4, color='#2196F3', alpha=0.06, zorder=0)
    # FLA takeover zone (bus voltage drops to FLA OCV range)
    ax.axhspan(22.5, 25.5, color='#FF9800', alpha=0.06, zorder=0)
    # FLA charge voltages (relay must be open)
    ax.axhspan(28.4, 32.5, color='#d32f2f', alpha=0.04, zorder=0)

    # --- Plot OCV curves ---
    ax.plot(lfp_soc, lfp_pack, color='#2196F3', linewidth=2.5, marker='o', markersize=4,
            label='LFP open-circuit voltage (8 cells)', zorder=5)
    ax.plot(fla_soc, fla_pack, color='#FF9800', linewidth=2.5, marker='s', markersize=4,
            label='FLA open-circuit voltage (12 cells, Trojan SoC table)', zorder=5)

    # --- Shaded gap between curves ---
    # Interpolate FLA to LFP SoC points for fill
    fla_interp = np.interp(lfp_soc, fla_soc, fla_pack)
    ax.fill_between(lfp_soc, fla_interp, lfp_pack, alpha=0.06, color='#9C27B0',
                    where=lfp_pack > fla_interp, zorder=1)

    # --- Key voltage lines ---
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

    # --- FLA 100% OCV reference ---
    fla_100_v = TROJAN_CELL[-1] * FLA_CELLS  # 25.46V
    ax.axhline(y=fla_100_v, color='#FF9800', linestyle=':', linewidth=0.8, alpha=0.5)
    ax.annotate(f'FLA 100% OCV  {fla_100_v:.1f}V (2.122V/cell)',
                xy=(102, fla_100_v), fontsize=8, color='#FF9800', alpha=0.8, va='center')

    # --- Crossover annotation ---
    # Find where LFP curve crosses FLA 100% OCV
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

    # --- Zone labels ---
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

    # --- Formatting ---
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


def charge_sequence():
    """Diagram: FLA charge/EQ sequence showing voltage over time."""

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 10), sharex=False)

    # --- FLA Charge Sequence (top) ---
    t_phase1 = np.linspace(0, 4, 50)
    t_disconnect = np.array([4, 4.1])
    t_phase23 = np.linspace(4.1, 8, 50)
    t_float = np.linspace(8, 9, 20)
    t_match = np.linspace(9, 12, 40)
    t_reconnect = np.array([12, 12.1])
    t_normal = np.linspace(12.1, 13, 10)

    # Trojan voltage (bus voltage during charge)
    trojan_p1 = 25.5 + (28.4 - 25.5) * (1 - np.exp(-t_phase1 / 1.5))
    trojan_disc = np.array([28.4, 28.4])
    trojan_p23 = 28.4 + (29.64 - 28.4) * (1 - np.exp(-(t_phase23 - 4.1) / 0.3))
    trojan_float = 29.64 - (29.64 - 27.0) * np.linspace(0, 1, 20)
    trojan_match = 27.0 - 0.3 * (1 - np.exp(-np.linspace(0, 4, 40)))  # Settling toward ~26.7
    trojan_recon = np.array([26.7, 26.7])
    trojan_norm = 26.7 * np.ones(10)

    # LFP voltage
    lfp_p1 = trojan_p1  # Same as bus (parallel)
    lfp_disc = np.array([28.4, 27.2])  # Drops to Orion voltage
    lfp_p23 = 27.2 * np.ones(50)  # Orion holds ~27.2V steady
    lfp_float = 27.2 * np.ones(20)  # LFP stays on Orion, voltage steady
    lfp_match = 27.2 - 1.0 * (1 - np.exp(-np.linspace(0, 2.5, 40)))  # Orion off, LFP settles
    lfp_recon = np.array([26.2, 26.7])  # Jumps to bus voltage on reconnect
    lfp_norm = 26.7 * np.ones(10)

    for t, v in [(t_phase1, trojan_p1), (t_disconnect, trojan_disc), (t_phase23, trojan_p23),
                 (t_float, trojan_float), (t_match, trojan_match), (t_reconnect, trojan_recon),
                 (t_normal, trojan_norm)]:
        ax1.plot(t, v, color='#FF9800', linewidth=2)
    ax1.plot([], [], color='#FF9800', linewidth=2, label='Trojan FLA (bus voltage)')

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

    ax1.text(2, 30.5, 'Phase 1\nShared charging\n(both on bus)', ha='center', fontsize=9,
             color='#1a472a', fontweight='bold')
    ax1.text(6, 30.5, 'Phase 2-3\nFLA absorption\n29.64V', ha='center', fontsize=9,
             color='#8B4513', fontweight='bold')
    ax1.text(8.5, 30.5, 'Float\n27V', ha='center', fontsize=9, color='#8B6914', fontweight='bold')
    ax1.text(10.5, 30.5, 'Voltage\nmatching', ha='center', fontsize=9, color='#4a148c', fontweight='bold')
    ax1.text(12.5, 30.5, 'Normal', ha='center', fontsize=9, color='#1a472a', fontweight='bold')

    ax1.axvline(x=4, color='#d32f2f', linestyle='--', linewidth=1.5, alpha=0.7)
    ax1.text(4, 31.2, 'RELAY\nOPENS', ha='center', fontsize=8, color='#d32f2f', fontweight='bold')
    ax1.axvline(x=12, color='#2e7d32', linestyle='--', linewidth=1.5, alpha=0.7)
    ax1.text(12, 31.2, 'RELAY CLOSES\n(delta < 1V)', ha='center', fontsize=8,
             color='#2e7d32', fontweight='bold')

    # Show delta < 1V at reconnect
    ax1.annotate('', xy=(12, 26.7), xytext=(12, 26.2),
                 arrowprops=dict(arrowstyle='<->', color='#2e7d32', lw=1.5))
    ax1.text(12.3, 26.45, '<1V', fontsize=8, color='#2e7d32', fontweight='bold')

    ax1.axhline(y=28.4, color='#2196F3', linestyle=':', linewidth=0.8, alpha=0.5)
    ax1.annotate('LFP safe max 28.4V', xy=(0.2, 28.55), fontsize=7, color='#2196F3', alpha=0.7)

    ax1.set_ylabel('Voltage (V)', fontsize=11)
    ax1.set_ylim(25, 31.5)
    ax1.set_title('FLA Charge Sequence (Trojan SoC < 85%)', fontsize=12, fontweight='bold')
    ax1.legend(loc='center right', fontsize=9)
    ax1.grid(True, alpha=0.2)

    # --- FLA Equalisation Sequence (bottom) ---
    t_eq_start = np.array([0, 0.1])
    t_eq = np.linspace(0.1, 2.5, 50)
    t_eq_float = np.linspace(2.5, 3, 20)
    t_eq_match = np.linspace(3, 6, 40)
    t_eq_recon = np.array([6, 6.1])
    t_eq_norm = np.linspace(6.1, 7, 10)

    trojan_start = np.array([28.4, 28.4])
    trojan_eq = 28.4 + (31.5 - 28.4) * (1 - np.exp(-(t_eq - 0.1) / 0.5))
    trojan_eq_float = 31.5 - (31.5 - 27.0) * np.linspace(0, 1, 20)
    trojan_eq_match = 27.0 - 0.3 * (1 - np.exp(-np.linspace(0, 4, 40)))  # Settling toward ~26.7
    trojan_eq_recon = np.array([26.7, 26.7])
    trojan_eq_norm = 26.7 * np.ones(10)

    lfp_start = np.array([28.4, 27.2])
    lfp_eq = 27.2 * np.ones(50)  # Orion holds steady
    lfp_eq_float = 27.2 * np.ones(20)  # Still on Orion, steady
    lfp_eq_match = 27.2 - 1.0 * (1 - np.exp(-np.linspace(0, 2.5, 40)))  # Orion off, settling
    lfp_eq_recon = np.array([26.2, 26.7])  # Jumps to bus on reconnect
    lfp_eq_norm = 26.7 * np.ones(10)

    for t, v in [(t_eq_start, trojan_start), (t_eq, trojan_eq), (t_eq_float, trojan_eq_float),
                 (t_eq_match, trojan_eq_match), (t_eq_recon, trojan_eq_recon),
                 (t_eq_norm, trojan_eq_norm)]:
        ax2.plot(t, v, color='#FF9800', linewidth=2)
    ax2.plot([], [], color='#FF9800', linewidth=2, label='Trojan FLA (bus voltage)')

    for t, v in [(t_eq_start, lfp_start), (t_eq, lfp_eq), (t_eq_float, lfp_eq_float),
                 (t_eq_match, lfp_eq_match), (t_eq_recon, lfp_eq_recon),
                 (t_eq_norm, lfp_eq_norm)]:
        ax2.plot(t, v, color='#2196F3', linewidth=2)
    ax2.plot([], [], color='#2196F3', linewidth=2, label='LFP (on Orion)')

    ax2.axvspan(0, 0.1, color='#d32f2f', alpha=0.08)
    ax2.axvspan(0.1, 2.5, color='#8B0000', alpha=0.08)
    ax2.axvspan(2.5, 3, color='#FFD700', alpha=0.08)
    ax2.axvspan(3, 6, color='#4a148c', alpha=0.05)
    ax2.axvspan(6, 7, color='#1a472a', alpha=0.08)

    ax2.text(1.3, 32.5, 'Equalisation\n31.5V (2.625V/cell)', ha='center', fontsize=9,
             color='#8B0000', fontweight='bold')
    ax2.text(2.75, 32.5, 'Float\n27V', ha='center', fontsize=9, color='#8B6914', fontweight='bold')
    ax2.text(4.5, 32.5, 'Voltage matching', ha='center', fontsize=9,
             color='#4a148c', fontweight='bold')
    ax2.text(6.5, 32.5, 'Normal', ha='center', fontsize=9, color='#1a472a', fontweight='bold')

    ax2.axvline(x=0, color='#d32f2f', linestyle='--', linewidth=1.5, alpha=0.7)
    ax2.text(0, 33.2, 'RELAY\nOPENS', ha='center', fontsize=8, color='#d32f2f', fontweight='bold')
    ax2.axvline(x=6, color='#2e7d32', linestyle='--', linewidth=1.5, alpha=0.7)
    ax2.text(6, 33.2, 'RELAY CLOSES\n(delta < 1V)', ha='center', fontsize=8,
             color='#2e7d32', fontweight='bold')

    # Show delta < 1V at reconnect
    ax2.annotate('', xy=(6, 26.7), xytext=(6, 26.2),
                 arrowprops=dict(arrowstyle='<->', color='#2e7d32', lw=1.5))
    ax2.text(6.3, 26.45, '<1V', fontsize=8, color='#2e7d32', fontweight='bold')

    ax2.axhline(y=28.4, color='#2196F3', linestyle=':', linewidth=0.8, alpha=0.5)
    ax2.annotate('LFP safe max 28.4V', xy=(0.2, 28.55), fontsize=7, color='#2196F3', alpha=0.7)

    ax2.set_xlabel('Time (hours)', fontsize=11)
    ax2.set_ylabel('Voltage (V)', fontsize=11)
    ax2.set_ylim(25, 33.5)
    ax2.set_title('FLA Equalisation Sequence (every 90 days)', fontsize=12, fontweight='bold')
    ax2.legend(loc='center right', fontsize=9)
    ax2.grid(True, alpha=0.2)

    plt.tight_layout()
    path = os.path.join(OUT_DIR, 'charge-sequences.png')
    fig.savefig(path, dpi=150, bbox_inches='tight')
    plt.close()
    print(f'Saved: {path}')


if __name__ == '__main__':
    voltage_profile()
    charge_sequence()
