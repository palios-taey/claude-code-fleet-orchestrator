"""Shared HTTP helper for operator CLIs."""
from __future__ import annotations

import errno
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Iterable, Optional


def _connection_refused(exc: BaseException) -> bool:
    if isinstance(exc, ConnectionRefusedError):
        return True
    if isinstance(exc, OSError) and exc.errno == errno.ECONNREFUSED:
        return True
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, ConnectionRefusedError):
            return True
        if isinstance(reason, OSError) and reason.errno == errno.ECONNREFUSED:
            return True
        return "Connection refused" in str(reason)
    return False


def _friendly_error(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.URLError):
        return str(exc.reason)
    return str(exc)


def api_json_or_exit(method: str,
                     base_url: str,
                     endpoint: str,
                     *,
                     data: Optional[Dict[str, Any]] = None,
                     timeout: float = 20,
                     retry_delays: Iterable[float] = (0.5, 1.5)) -> Dict[str, Any]:
    """Call the orchestrator API with deploy-flap retries for refused sockets."""
    url = f"{base_url.rstrip('/')}{endpoint}"
    delays = list(retry_delays)
    attempts = len(delays) + 1
    last_refused: Optional[BaseException] = None
    for attempt in range(attempts):
        if data is None:
            request = urllib.request.Request(url, method=method)
        else:
            request = urllib.request.Request(
                url,
                data=json.dumps(data).encode(),
                headers={"Content-Type": "application/json"},
                method=method,
            )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode()[:800]
            print(f"ERROR: HTTP {exc.code}: {body}", file=sys.stderr)
            sys.exit(1)
        except Exception as exc:
            if _connection_refused(exc):
                last_refused = exc
                if attempt < len(delays):
                    time.sleep(delays[attempt])
                    continue
                print(
                    "ERROR: orchestrator API at "
                    f"{base_url.rstrip('/')} unreachable after {attempts} attempts - "
                    "it may be mid-restart; retry in a moment. "
                    f"Last error: {_friendly_error(exc)}",
                    file=sys.stderr,
                )
                sys.exit(1)
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)
    print(
        "ERROR: orchestrator API at "
        f"{base_url.rstrip('/')} unreachable - retry in a moment. "
        f"Last error: {_friendly_error(last_refused) if last_refused else 'connection refused'}",
        file=sys.stderr,
    )
    sys.exit(1)
