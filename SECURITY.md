# Security

## Reporting a vulnerability

Email `security@palios-taey.dev` with:

- affected version
- impact summary
- reproduction steps
- any mitigation or patch guidance you already have
- a safe way to contact you

Do not file public GitHub issues for security disclosures.

## Disclosure process

1. Acknowledge the report.
2. Reproduce and scope the issue.
3. Build and verify a fix.
4. Coordinate disclosure timing with the reporter.
5. Publish the fix and any advisory material.

## Scope

This policy covers this repository only.

## GitHub outward broker

Worker processes must not hold a GitHub credential or an authenticated `gh`
binary. `scripts/github-brokerd` is the credential principal. It listens on two
Unix sockets:

- **exec** (`ORCH_GITHUB_BROKER_SOCKET`, mode `0660`): `op=exec` only. Mint
  and revoke are denied here. The broker maps `SO_PEERCRED` pid → TTY → tmux
  session and looks up the in-memory handle for that session. Workers never
  receive a bearer token in tmux or process env.
- **control** (`ORCH_GITHUB_BROKER_CONTROL_SOCKET`, mode `0660` in a
  broker-private dir): `op=mint` / `op=revoke` only after `SO_PEERCRED` uid
  mapping. Mode is `0660` so group `github-control` can mint; `0600` is
  owner-only and cannot be opened by uid-mira orch. `bind_current_task` mints
  into broker memory only. `session_unbind_current_task` revokes before Redis
  clear.

Worker `scripts/gh-outward` is an exec-socket client and never sends mint,
revoke, or a handle. Production deploy (CONTROL) runs the daemon as Unix user
`github-broker`; orch/taey-task join group `github-control`; workers join
`github-workers` only. See `deploy/systemd/github-broker.service`. This
repository does not enable that unit.

Observed live: seats share uid 1000. Control mint is the systemd API
daemon (`uvicorn` + `tasks_api`, no TTY) or a supervisor TTY. Only the
API unit joins `SupplementaryGroups=github-control`; mira is not added
to that group globally. Isolated tests prove a no-TTY API cmdline can
mint, a worker cmdline cannot, and a non-API caller cannot. Same-UID
ptrace remains a residual until distinct OS principals.

## Threat model

- Local single-user deployment on a trusted host.
- The localhost API is not an authenticated multi-tenant service.
- The mutable API fails closed on non-loopback binds without `ORCH_AUTH_TOKEN`
  unless `ORCH_ALLOW_UNAUTH_NON_LOOPBACK=1` is set. That override is an explicit
  trusted-LAN exposure acknowledgement, not authentication.
- Gate-runner command strings are trusted local input. `scripts/orch-gate-run`
  accepts `--clean`, `--boot`, and `--assert` command strings from the operator,
  and code callers pass the same values to `fleet_orchestrator.gate_runner.run_gate`
  as `clean`, `boot`, and `assert_cmd`. The runner executes those strings through
  the local shell by design so operator-authored gates can use normal shell
  pipelines and setup commands. The runner does not sandbox those gate commands
  and must not be treated as safe for untrusted gate definitions.
- `ORCH_SHIP_GATES` config selects which project-local task names must carry
  completion evidence before a project can receive a successful ship verdict; it
  does not supply shell command strings to the gate runner.
- If gate command definitions ever originate from untrusted input, another user,
  the network, or uploaded plans accepted from an untrusted source, the gate
  runner must move away from raw shell strings to structured argv commands, a
  sandbox, or both before executing them.
- Plan/source refs are enabled only when `ORCH_REF_ALLOWED_ROOT` is configured.
- Incoming `source_path` values are accepted for ref use only when they resolve inside `ORCH_REF_ALLOWED_ROOT`.
- Ref reads are sandboxed to both the persisted plan-source directory and `ORCH_REF_ALLOWED_ROOT`.
- Path sanitization rejects code points with `ord(ch) < 32`; `DEL` (`0x7f`), `NEL`, and `U+2028/U+2029` are not pre-filtered but still degrade gracefully during resolution/open.
- Non-regular files are refused, oversized files are refused, and unresolved refs fail loudly with warnings instead of silent fallback.
- The `META_RE` parser remains intrinsically quadratic in isolation; mitigation is the 4096-byte per-line cap plus the 512-byte meta-blob cap before regex meta scanning.
- There is no total-plan-size cap; operator self-DoS via very large overall plan uploads remains accepted in this local single-user model.
- `reset_project` intentionally does not clear session-global convergence keys; those keys are not project-qualified and are treated as broader session state.
- Residual `stat()/resolve()/open()` TOCTOU races on the local filesystem are accepted in this single-user model and are not treated as a supported remote attack surface.

## Supported versions

| Version | Supported |
|---|---|
| Latest release | Yes |
| Older releases | Best effort |
