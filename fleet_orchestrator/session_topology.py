"""Session-family and configured control-principal topology."""
from __future__ import annotations

from collections.abc import Iterable
from typing import Optional


PEER_SUFFIXES = ("-codex", "-gemini", "-grok", "-claude")


def seat_family(session_id: str) -> str:
    session = str(session_id or "").strip()
    lowered = session.lower()
    for suffix in PEER_SUFFIXES:
        if lowered.endswith(suffix):
            return session[: -len(suffix)]
    return session


def _registered_lookup(registered_sessions: Iterable[str]) -> dict[str, str]:
    return {
        session.lower(): session
        for raw in registered_sessions or ()
        if (session := str(raw or "").strip())
    }


def _configured_control_family(control: str) -> str:
    if control.lower().endswith("-codex"):
        return control[: -len("-codex")]
    return control


def configured_supervisor_for_session(
    session_id: str,
    registered_sessions: Iterable[str],
) -> Optional[str]:
    """Return the configured control principal for a session, if one exists."""
    session = str(session_id or "").strip()
    if not session:
        return None
    registered = _registered_lookup(registered_sessions)
    exact = registered.get(session.lower())
    if exact:
        return exact
    lowered = session.lower()
    for control in registered.values():
        family = _configured_control_family(control)
        if control.lower().endswith("-codex"):
            workers = (
                family,
                f"{family}-gemini",
                f"{family}-grok",
                f"{family}-claude",
            )
        else:
            workers = tuple(f"{family}{suffix}" for suffix in PEER_SUFFIXES)
        if lowered in {worker.lower() for worker in workers}:
            return control
    return None


def control_principal_for_session(
    session_id: str,
    registered_sessions: Iterable[str],
    *,
    explicit_parent: Optional[str] = None,
) -> str:
    """Resolve control authority, preferring configured topology over stale state."""
    session = str(session_id or "").strip()
    if not session:
        return ""
    configured = configured_supervisor_for_session(session, registered_sessions)
    if configured:
        return configured
    parent = str(explicit_parent or "").strip()
    if parent and parent != session:
        return parent
    return seat_family(session)


def session_family(session_id: str, registered_sessions: Iterable[str]) -> str:
    session = str(session_id or "").strip()
    configured = configured_supervisor_for_session(session, registered_sessions)
    return _configured_control_family(configured) if configured else seat_family(session)


def supervised_worker_sessions(
    supervisor: str,
    registered_sessions: Iterable[str],
) -> tuple[str, ...]:
    """Return workers controlled by a configured supervisor in either topology."""
    control = str(supervisor or "").strip()
    if not control:
        return ()
    registered = _registered_lookup(registered_sessions)
    configured = registered.get(control.lower(), control)
    family = _configured_control_family(configured)
    if configured.lower().endswith("-codex"):
        candidates = (
            family,
            f"{family}-gemini",
            f"{family}-grok",
            f"{family}-claude",
        )
    else:
        candidates = tuple(f"{family}{suffix}" for suffix in PEER_SUFFIXES)
    return tuple(
        candidate
        for candidate in candidates
        if candidate != configured
        and control_principal_for_session(candidate, registered.values()) == configured
    )


def session_aliases(session_id: str, registered_sessions: Iterable[str]) -> tuple[str, ...]:
    """Return one seat's control and worker spellings without nested suffixes."""
    session = str(session_id or "").strip()
    control = control_principal_for_session(session, registered_sessions)
    family = _configured_control_family(control or session)
    values = (
        session,
        control,
        family,
        f"{family}-codex",
        f"{family}-gemini",
        f"{family}-grok",
        f"{family}-claude",
    )
    return tuple(dict.fromkeys(value for value in values if value))
