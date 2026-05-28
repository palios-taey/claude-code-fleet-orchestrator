[Observed] Verification date: 2026-05-28 UTC.

[Observed] Repo under verification: `claude-code-fleet-orchestrator` with the v1.0.6 auth fix in `lib/config.py`.

[Observed] Root-cause fix shipped:
- `ORCH_NEO4J_USER = os.environ.get("ORCH_NEO4J_USER")`
- `ORCH_NEO4J_PASS = os.environ.get("ORCH_NEO4J_PASS")`
- `OrchConfig` now carries `neo4j_user` and `neo4j_pass`
- `get_neo4j_driver()` now uses `(cfg.neo4j_user, cfg.neo4j_pass)` only when both are set; otherwise it preserves the existing `auth=None` behavior.

## Test 1 — current conductor environment, backward-compatible no-auth path

[Observed] Running from `/home/mira/the-conductor`, `OrchConfig()` resolved:
- `neo4j_uri = bolt://10.0.0.163:7689`
- `neo4j_user = None`
- `neo4j_pass = unset`

[Observed] Command:

```bash
python3 - <<'PY'
import sys
sys.path.insert(0, '/home/mira/claude-code-fleet-orchestrator')
from lib.dispatch import _orch_task_exists
print(_orch_task_exists('nonexistent'))
PY
```

[Observed] Result: `False` with no exception.

[Inferred] Backward compatibility holds for the current conductor environment: no credentials configured still means no-auth driver creation, and `_orch_task_exists("nonexistent")` returns `False` instead of raising.

## Test 2 — explicit auth-configured path

[Observed] The directive named `bolt://localhost:7689` as the auth-required path, but on this Mira host that endpoint currently accepts both unauthenticated and credentialed sessions. I therefore isolated dotenv loading with an explicit empty file and still verified the auth-configured branch directly.

[Observed] Command:

```bash
printf '' >/tmp/orch-empty.env
ORCH_DOTENV=/tmp/orch-empty.env \
ORCH_NEO4J_URI=bolt://localhost:7687 \
ORCH_NEO4J_USER=neo4j \
ORCH_NEO4J_PASS=awareness123 \
python3 - <<'PY'
import sys
sys.path.insert(0, '/home/mira/claude-code-fleet-orchestrator')
from lib.dispatch import _orch_task_exists
print(_orch_task_exists('nonexistent'))
PY
```

[Observed] Result: `False` with no exception.

[Observed] Supporting drift check:
- `cypher-shell -a bolt://localhost:7689 'RETURN 1;'` returned `1`
- `cypher-shell -a bolt://localhost:7689 -u neo4j -p awareness123 'RETURN 1;'` also returned `1`

[Inferred] The code path that creates a Neo4j driver with explicit credentials is working. The host-state discrepancy is that `:7689` is not auth-required on Mira right now, so `:7687` was the live auth-required endpoint available for proof.

## Test 3 — fail loud when credentials are omitted on an auth-required server

[Observed] Command:

```bash
ORCH_DOTENV=/tmp/orch-empty.env \
ORCH_NEO4J_URI=bolt://localhost:7687 \
env -u ORCH_NEO4J_USER -u ORCH_NEO4J_PASS \
python3 - <<'PY'
import sys
sys.path.insert(0, '/home/mira/claude-code-fleet-orchestrator')
from lib.dispatch import _orch_task_exists
try:
    print(_orch_task_exists('nonexistent'))
except Exception as exc:
    print(type(exc).__name__)
    print(str(exc))
    raise
PY
```

[Observed] Result: raised `neo4j.exceptions.AuthError` with:

```text
Unsupported authentication token, missing key `scheme`
```

[Inferred] No silent fallback was introduced. When the environment omits credentials and the target Neo4j requires auth, the driver still fails loud instead of retrying with another auth mode.

## Exact drift note vs directive

[Observed] The directive's phrase "override ORCH_NEO4J_URI=bolt://localhost:7689 + ORCH_NEO4J_USER=neo4j + ORCH_NEO4J_PASS=awareness123 — the ISMA Neo4j that today errored" does not match the current Mira host state.

[Observed] On this host today:
- `bolt://localhost:7687` with `auth=None` raises the exact `AuthError`
- `bolt://localhost:7689` and `bolt://10.0.0.163:7689` both accept unauthenticated sessions

[Unknown] I do not have the exact treasurer-side process state that produced the original report, so I cannot prove whether treasurer hit a different endpoint, a different host, or a transient server configuration. What is proven above is that v1.0.6 now supports both explicit-auth and no-auth Neo4j configurations, and still fails loud when auth is missing against a live auth-required Neo4j on Mira.
