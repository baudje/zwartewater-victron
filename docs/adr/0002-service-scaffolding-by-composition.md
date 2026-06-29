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
