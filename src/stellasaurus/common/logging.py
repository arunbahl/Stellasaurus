"""Structured logging + an audit-event helper.

The audit helper writes a structured log line AND (when a repo is provided)
appends to the durable ``audit_log`` table — the append-only decision record
required by DESIGN §6.11.
"""

from __future__ import annotations

import logging
from typing import Any

import structlog


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(format="%(message)s", level=getattr(logging, level.upper(), logging.INFO))
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level.upper())),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger


# Lightweight protocol to avoid importing storage into common.
class _AuditSink:
    def append(
        self, *, actor: str, event_type: str, pair_id: str | None, detail: dict[str, Any]
    ) -> None: ...


def audit(
    sink: _AuditSink | None,
    *,
    actor: str,
    event_type: str,
    pair_id: str | None = None,
    **detail: Any,
) -> None:
    """Emit an audit event to the log and (if present) the durable sink."""
    get_logger("audit").info(event_type, actor=actor, pair_id=pair_id, **detail)
    if sink is not None:
        sink.append(actor=actor, event_type=event_type, pair_id=pair_id, detail=detail)
