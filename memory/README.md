# Memory Store

The wake-packet Memory tier reads operator-authored markdown from the memory
store and renders it as untrusted data in `## Memory`.

## Layout

- `global.md` is local operator config for every session.
- `supervisors/<session>.md` or `supervisor/<session>.md` is local operator config for one normalized supervisor session.
- `projects/<project>.md` or `project/<project>.md` is local operator config for one project.

The global, supervisor, and project memory files are intentionally gitignored.
Keep local operator memory out of the public repository.

## Root Selection

By default, the store root is this repository's `memory/` directory. Set
`ORCH_MEMORY_ROOT=/path/to/memory` to use another store.

Memory files can use simple frontmatter:

```markdown
---
name: handoff norms
type: reference
description: standing supervisor context
---
Keep this concise. Memory is injected into every matching wake packet.
```

Entries are ranked most-specific first (`project`, then `supervisor`, then
`global`) and capped before rendering. Oversized low-ranked entries are dropped
whole under the memory-tier budget; oversized item bodies are truncated with an
explicit marker.
