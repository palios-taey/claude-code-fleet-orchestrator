# Contributing

This repo is a local-first orchestration tool for AI agent fleets. Human contributors are welcome, but design decisions should treat the primary operator as an AI session using the system at speed.

## AI-Native / AI-First / AI-Speed

The core product principle is: every surface should teach its own use just in time, in band.

For contributors, that means:

- Error messages, wake packets, CLI output, and API responses should say what the agent has, what state it is in, and what to do next.
- "See the docs" and "ask the operator" are bugs when the product can provide the next action directly.
- The standing test is: if an AI hits this with no surrounding chat context, does the output itself say what it has and what to do next?
- A legitimate participant must never be rejected from its own work. Supervisor-only capabilities can be gated, but self-service status surfaces such as current work and next-ready work must remain available to the participant.
- The felt litmus is: designed for me, by me; fixes what frustrates my user about me; trivial to set up.

Concrete patterns already in the code include error-as-teacher responses, wake-packet Operating affordances, evidence-gated completion messages, and participant-safe current/next-ready endpoints.

[PR #163](https://github.com/palios-taey/claude-code-fleet-orchestrator/pull/163) is the first named example: it unified supervisor plan/root access so unregistered peers are not promised capabilities they cannot use, then narrowed the fix so those same peers can still read their own current and next-ready work.
