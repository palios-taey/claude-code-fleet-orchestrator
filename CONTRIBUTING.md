# Contributing

This repository is developed as a local-runtime tool, not a hosted SaaS. Keep contributions tied to the code that actually ships on the machine.

## Local Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

If you touch the install surface, also verify the clean-install path from a fresh venv:

```bash
python3 -m venv /tmp/orch-verify
source /tmp/orch-verify/bin/activate
pip install .
python -c "import fleet_orchestrator; print(fleet_orchestrator.__version__)"
orch-cron --help
orch-watch --help
taey-plan --help
taey-task --help
```

## Required Checks

Before you commit:

```bash
python3 tools/lint_no_silent_fallbacks.py --all
python3 -m py_compile src/fleet_orchestrator/*.py src/fleet_orchestrator/scripts/*.py
```

On GitHub, the branch protection contract expects two green checks on `main`:

- `no-silent-fallbacks`
- `installs-clean`

## Documentation Standard

Documentation in this repo is treated as a claim about the running product.

- Bind every statement to the actual code or a command you just ran.
- Do not document multi-tenant or auth-required behavior that the current code does not implement.
- If you show a command, run it first.
- If you quote a version, cross-check `pyproject.toml`, `src/fleet_orchestrator/__init__.py`, and the current branch state.

## Local Trust Model

The intended deployment is one operator on one machine. Default assumptions:

- API binds to loopback
- Redis and Neo4j are local trusted services
- there is no built-in auth layer

If your change assumes a network-exposed or untrusted-caller environment, say so explicitly and justify it.

## Removing Fields or Paths

Do not remove a graph field or runtime path just because it looks stale.

Example: `forced_continuation_count` is still live on this branch. It is:

- initialized in task creation
- updated in `update_task_status()`
- read back in task fetch paths
- exercised by the stage-a migration acceptance harness

Run code-intel first, then remove only when the field is actually dead everywhere that matters.
