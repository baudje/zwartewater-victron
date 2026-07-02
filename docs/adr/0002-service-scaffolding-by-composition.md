# Share service scaffolding by composition, not inheritance

The equalisation and charge services duplicate near-identical web/settings/status
plumbing (HTTP handler, settings-cache, D-Bus settings read/write, status
registration). We will de-duplicate it as **closed shared engines configured by a
per-service Operation profile** (a plain data card) — NOT as base classes the
services subclass and override.

The goal is to make drift between the two services *impossible*, not merely
discouraged. A closed engine has nothing to override, so the plumbing cannot
diverge again; a base class re-introduces override hooks, which is exactly where
the two copies quietly diverged in the first place. Inheritance is the obvious
Python path here, so this is a deliberate deviation worth recording: do not
"simplify" the engines into a shared base class — that reopens the drift surface
this decision closed.

The genuinely-varying parts (HTML, the settings/state *lists*) stay per-service
inside the Operation profile, where the variation is visible and intended.

## Amendment (unified dashboard, issue #24)

"The genuinely-varying parts (HTML, ...) stay per-service" is superseded for
the HTML: the dashboard page no longer varies per service at all. ONE shared,
fully data-driven page (`fla-shared/unified_page.py`) is served identically at
both ports; it renders each operation's panel at runtime from that service's
`GET /api/config`. The per-service variation moved out of markup into
declarative Operation-profile fields (`settings_rows`, `panel_fields`,
`run_now_confirm`), which is a *stronger* form of this ADR's rule: the closed
engine now owns even the page, and the only per-service surface left is data.
The settings/state *lists* still live per-service in the profile, as before.
