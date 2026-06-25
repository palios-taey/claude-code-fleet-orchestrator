# Rules Store

The wake-packet Rules tier reads operator-authored markdown from the rules store.

## Layout

- `global.md` is committed and applies to every session. It is loaded first.
- `supervisors/<session>.md` or `supervisor/<session>.md` is local operator config for one normalized supervisor session.
- `projects/<project>.md` or `project/<project>.md` is local operator config for one project.

The supervisor and project directories are intentionally gitignored. Keep local operator overlays out of the public repository.

## Root Selection

By default, the store root is this repository's `rules/` directory. Set `ORCH_RULES_ROOT=/path/to/rules` to use another store.

An override store must include `global.md`. If the effective store is missing or lacks `global.md`, the wake packet renders a distinct teaching line instead of silently saying `none selected`.

An empty supervisor or project scope is valid when the store is bootstrapped with `global.md`; the global baseline still renders for every session.
