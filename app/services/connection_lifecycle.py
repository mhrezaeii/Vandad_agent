"""Connection lifecycle state machine.

Allowed transitions are explicit; anything else is rejected. Every transition
must go through ``apply_transition`` so the event trail stays complete.
"""

from __future__ import annotations

from app.models import ConnectionStatus

#: from_status -> set of allowed to_status values
ALLOWED_TRANSITIONS: dict[ConnectionStatus, frozenset[ConnectionStatus]] = {
    ConnectionStatus.DRAFT: frozenset(
        {ConnectionStatus.CONFIGURED, ConnectionStatus.VALIDATING, ConnectionStatus.DISCONNECTED}
    ),
    ConnectionStatus.CONFIGURED: frozenset(
        {ConnectionStatus.VALIDATING, ConnectionStatus.CONNECTED, ConnectionStatus.DISCONNECTED, ConnectionStatus.ERROR}
    ),
    ConnectionStatus.VALIDATING: frozenset(
        {ConnectionStatus.CONNECTED, ConnectionStatus.DEGRADED, ConnectionStatus.ERROR, ConnectionStatus.CONFIGURED}
    ),
    ConnectionStatus.CONNECTED: frozenset(
        {ConnectionStatus.VALIDATING, ConnectionStatus.DEGRADED, ConnectionStatus.DISCONNECTED, ConnectionStatus.ERROR}
    ),
    ConnectionStatus.DEGRADED: frozenset(
        {ConnectionStatus.VALIDATING, ConnectionStatus.CONNECTED, ConnectionStatus.DISCONNECTED, ConnectionStatus.ERROR}
    ),
    ConnectionStatus.ERROR: frozenset(
        {ConnectionStatus.VALIDATING, ConnectionStatus.CONFIGURED, ConnectionStatus.DISCONNECTED}
    ),
    ConnectionStatus.DISCONNECTED: frozenset(
        {ConnectionStatus.VALIDATING, ConnectionStatus.CONFIGURED}
    ),
}


def can_transition(current: str, target: str) -> bool:
    try:
        allowed = ALLOWED_TRANSITIONS[ConnectionStatus(current)]
    except ValueError:
        return False
    return target in {status.value for status in allowed}
