"""Feature-flag helpers for promised capabilities."""

from __future__ import annotations

import logging
import os
from typing import Iterable


LOG = logging.getLogger(__name__)
TRUE_ENV_VALUES = {"1", "true", "yes", "on"}
FALSE_ENV_VALUES = {"0", "false", "no", "off"}


def default_on_feature_enabled(name: str, *, aliases: Iterable[str] = ()) -> bool:
    """Return true unless a canonical flag or alias explicitly requests off."""
    for key in (name, *tuple(aliases)):
        raw = os.environ.get(key)
        if raw is None:
            continue
        value = raw.strip().lower()
        if value in TRUE_ENV_VALUES:
            return True
        if value in FALSE_ENV_VALUES:
            return False
        if value:
            LOG.warning(
                "unrecognized value for default-on feature flag %s=%r; treating as enabled",
                key,
                raw,
            )
        return True
    return True


def chat_enabled() -> bool:
    return default_on_feature_enabled("ORCH_CHAT_ENABLED")


def decision_receipts_enabled() -> bool:
    return default_on_feature_enabled("ORCH_DECISION_RECEIPTS_ENABLED")


def loops_enabled() -> bool:
    return default_on_feature_enabled("ORCH_LOOPS_ENABLED")


def gate_template_enabled() -> bool:
    return default_on_feature_enabled("ORCH_GATE_TEMPLATE_ENABLED")


def wake_packet_endpoint_enabled() -> bool:
    return default_on_feature_enabled(
        "ORCH_WAKE_PACKET_ENDPOINT_ENABLED",
        aliases=("ORCH_WAKE_PACKET_ENABLED",),
    )
