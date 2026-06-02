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

## Threat model

- Local single-user deployment on a trusted host.
- The localhost API is not an authenticated multi-tenant service.
- Plan/source refs are enabled only when `ORCH_REF_ALLOWED_ROOT` is configured.
- Incoming `source_path` values are accepted for ref use only when they resolve inside `ORCH_REF_ALLOWED_ROOT`.
- Ref reads are sandboxed to both the persisted plan-source directory and `ORCH_REF_ALLOWED_ROOT`.
- Non-regular files are refused, oversized files are refused, and unresolved refs fail loudly with warnings instead of silent fallback.
- `reset_project` intentionally does not clear session-global convergence keys; those keys are not project-qualified and are treated as broader session state.
- Residual `stat()/resolve()/open()` TOCTOU races on the local filesystem are accepted in this single-user model and are not treated as a supported remote attack surface.

## Supported versions

| Version | Supported |
|---|---|
| Latest release | Yes |
| Older releases | Best effort |
