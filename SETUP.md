# Setup

This document is the operator setup guide for `claude-code-fleet-orchestrator`.

Observed:
- the installer and lifecycle commands live in `scripts/install` and `scripts/orch`
- the setup transaction logic lives in `fleet_orchestrator/easy_setup.py`
- the product is designed for a local, single-user workstation with loopback-only trust boundaries and no built-in auth layer
- this guide is for the release line that includes the easy-setup entrypoints `scripts/install` and `orch`; `SETUP.md` and those entrypoints ship together

## Threat Model

This project intentionally trusts the local machine.

Observed:
- Redis, Neo4j, and the local API are treated as trusted local infrastructure
- stop-hook fail-open behavior is intentional
- settings and hook ownership are tracked so uninstall can remove only managed changes by default

Inferred:
- this setup is correct for a single-user workstation and incorrect for multi-user or internet-exposed deployment without an external security layer

If you need network auth, shared-user isolation, or remote secret management, this setup is the wrong default.

## Prerequisites

Required:
- Python 3.10+
- a `python3` install with the stdlib `venv` module available
- a local checkout of `claude-code-fleet-orchestrator`
- a sibling checkout of `claude-code-fleet-notify`
- Claude Code installed and using `~/.claude/settings.json`

Optional:
- Docker, if you want the bundled Redis + Neo4j path

Observed:
- `README.md` sets the project Python floor at 3.10+
- `scripts/install` runs `python3 -m venv`, so the host Python must include `venv`
- `scripts/install` can skip Docker entirely when `--skip-compose` is passed or when local infra is already reachable on the expected ports
- notify hook installation is delegated to the sibling notify repo's `claude-code-fleet-notify/scripts/install-hooks.sh`

Operator note:
- on Debian or Ubuntu, a missing `venv` module usually means you need the `python3-venv` system package
- no exact Claude Code minimum version is encoded in this repo; you need a Claude Code build that supports the managed hooks and `permissions.deny` settings shape used here

## Install

From the orchestrator repo, start with a no-write preview:

```bash
scripts/install --dry-run
```

Observed:
- `--dry-run` previews the install flow and Claude settings diff
- `--dry-run` writes nothing: no state file, no pristine backup, no settings mutation

Then run the real install:

```bash
scripts/install
```

Observed install flow:
1. preflight local infra
2. start bundled Redis + Neo4j only if compose is being managed for this install
3. create `.venv`
4. install the package into that virtualenv
5. wire Claude settings and notify hooks
6. start notify daemons
7. start local orchestrator services
8. run `orch doctor`

Observed settings behavior:
- the installer takes a pristine backup of the original Claude settings on first install
- writes are atomic: tempfile, fsync, `os.replace`, directory fsync, and re-read verification
- managed deny entries are ownership-tracked and limited to:
  - `AskUserQuestion`
  - `AskUserQuestion(*)`
- hook ownership is tracked by normalized full path, not basename
- a pending hook transaction journal is written before the external hook installer runs and reconciled later if a crash interrupts that window

Layout note:
- the default auto-discovery path expects `claude-code-fleet-orchestrator` and `claude-code-fleet-notify` as sibling checkouts
- if notify is not in that sibling layout, set `ORCH_NOTIFY_LIB_ROOT=/absolute/path/to/claude-code-fleet-notify`
- runtime config, dispatch, and doctor use the same notify-root resolver; an invalid explicit `ORCH_NOTIFY_LIB_ROOT` fails loudly instead of falling back to another checkout

## Bring Your Own Infra

If local Redis and Neo4j are already running on the expected ports, or if you do not want Docker-managed services:

```bash
scripts/install --skip-compose
```

Observed:
- the default local ports are Redis `127.0.0.1:6379` and Neo4j `127.0.0.1:7687`
- BYO mode does not require Docker
- setup state records whether compose is managed
- later doctor runs skip Docker checks when the install was done in BYO mode
- doctor probes the configured Redis and Neo4j endpoints with real connectivity checks

BYO configuration:
- `ORCH_REDIS_HOST`
- `ORCH_REDIS_PORT`
- `ORCH_NEO4J_URI`
- `ORCH_NEO4J_DB`
- `ORCH_NOTIFY_LIB_ROOT`

## Doctor

Run:

```bash
scripts/orch doctor --explain-scope
```

Observed doctor coverage:
- Docker readiness when compose is managed
- Redis reachability with a real `PING`
- Neo4j reachability with a real query
- env validation
- notify-root resolution, including sibling checkout auto-discovery or the exact `ORCH_NOTIFY_LIB_ROOT` remediation
- `/health` identity and version
- Claude deny entries present exactly once
- expected hook paths installed exactly once
- stop-decision round trip
- notify daemon running
- `orch-watch` running with the managed pidfile / process identity
- notify hook fail-open behavior
- orchestrator stop-hook fail-open behavior

The hook fail-open checks are labeled:
- `notify-hook-fail-open`
- `orch-hook-fail-open`

## Lifecycle

Enable services and re-apply managed integration idempotently:

```bash
scripts/orch enable
```

Observed:
- re-running `scripts/install` or `scripts/orch enable` is intended to reconcile the managed delta rather than duplicate it
- `orch doctor --explain-scope` is the documented post-install verification path

Stop managed services without removing settings ownership:

```bash
scripts/orch disable
```

Remove only orchestrator-managed Claude changes and stop managed services:

```bash
scripts/orch uninstall
```

Restore the original pristine Claude settings backup instead of the managed delta:

```bash
scripts/orch uninstall --restore-original-settings
```

Observed uninstall behavior:
- default uninstall is surgical
- unmanaged state is a no-op
- the pristine pre-install backup is preserved
- `--restore-original-settings` is destructive with respect to post-install edits in the Claude settings file
- `scripts/orch uninstall --restore-original-settings` prints a preflight diff before restoring
- full restore refuses path drift or fingerprint mismatch unless explicitly overridden

Override only if you intentionally want to restore despite path drift:

```bash
scripts/orch uninstall --restore-original-settings --allow-path-drift-restore
```

## Stop-Discipline Escape Hatches

Observed:
- stop-hook paths are intentionally fail-open
- if the local API is unavailable, stop hooks return success with neutral output instead of trapping the operator in a stuck stop path

Inferred:
- this is the right tradeoff for a local dev workstation where availability is favored over enforcement during degraded states

## Companion Products

Required:
- [`claude-code-fleet-notify`](https://github.com/palios-taey/claude-code-fleet-notify)

Optional:
- [`claude-code-api-watchdog`](https://github.com/palios-taey/claude-code-api-watchdog)
- [`mcp-reconnect`](https://github.com/palios-taey/mcp-reconnect)
- [`restart-safe-agents`](https://github.com/palios-taey/restart-safe-agents)
- [`claude-code-fleet-cockpit-template`](https://github.com/palios-taey/claude-code-fleet-cockpit-template)

Observed:
- this repo's installer expects the notify companion repo
- the other companion products are not required by `scripts/install`

## OS Notes

Observed:
- Claude settings are resolved through the current user's `.claude` directory under the current user's home directory
- the managed local environment expects a Unix-like shell and process model

Unknown:
- a fully supported Windows-native path and workflow have not been encoded or tested in this repo
