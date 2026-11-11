"""Structured JSON logging with call_id context and sensitive data redaction."""

from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
from typing import Any

# Context variable tracking call_id across async tasks
call_id_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar("call_id", default=None)

# Patterns for redacting sensitive values
REDACTED_TEXT = "[REDACTED]"
SENSITIVE_KEY_PATTERNS = [
    re.compile(r"api[_-]?key", re.IGNORECASE),
    re.compile(r"secret", re.IGNORECASE),
    re.compile(r"password", re.IGNORECASE),
    re.compile(r"token", re.IGNORECASE),
    re.compile(r"authorization", re.IGNORECASE),
    re.compile(r"bearer", re.IGNORECASE),
    re.compile(r"ssn", re.IGNORECASE),
    re.compile(r"routing", re.IGNORECASE),
]

# Credit card number pattern (basic 13-19 digit card numbers)
CC_PATTERN = re.compile(r"\b(?:\d[ -]*?){13,19}\b")

# Phone numbers in E.164 form. Logs keep the last four digits, enough to tell calls apart.
PHONE_PATTERN = re.compile(r"\+\d{7,14}(\d{4})\b")


def mask_phone(phone: str | None) -> str:
    """`+14045551234` as `***1234`; an empty or short value as `***`."""
    digits = re.sub(r"\D", "", phone or "")
    return f"***{digits[-4:]}" if len(digits) >= 8 else "***"


def set_call_id(call_id: str | None) -> None:
    """Set the current call ID for context propagation."""
    call_id_ctx.set(call_id)


def get_call_id() -> str | None:
    """Get the active call ID from context."""
    return call_id_ctx.get()


def redact_sensitive_data(obj: Any) -> Any:
    """Recursively scrub sensitive keys and patterns from log records."""
    if isinstance(obj, dict):
        clean: dict[str, Any] = {}
        for k, v in obj.items():
            key_str = str(k)
            if any(p.search(key_str) for p in SENSITIVE_KEY_PATTERNS):
                clean[k] = REDACTED_TEXT
            else:
                clean[k] = redact_sensitive_data(v)
        return clean
    elif isinstance(obj, list):
        return [redact_sensitive_data(item) for item in obj]
    elif isinstance(obj, str):
        obj = PHONE_PATTERN.sub(r"***\1", obj)
        # Scrub credit card numbers
        if len(obj) >= 13 and re.search(r"\d", obj):
            return CC_PATTERN.sub(REDACTED_TEXT, obj)
        return obj
    return obj


class StructuredJsonFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        current_call_id = getattr(record, "call_id", None) or get_call_id()

        log_data: dict[str, Any] = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        if current_call_id:
            log_data["call_id"] = current_call_id

        # Attach custom attributes passed via extra={}
        standard_attrs = {
            "name",
            "msg",
            "args",
            "levelname",
            "levelno",
            "pathname",
            "filename",
            "module",
            "exc_info",
            "exc_text",
            "stack_info",
            "lineno",
            "funcName",
            "created",
            "msecs",
            "relativeCreated",
            "thread",
            "threadName",
            "processName",
            "process",
            "message",
            "call_id",
        }
        extras = {k: v for k, v in record.__dict__.items() if k not in standard_attrs}
        if extras:
            log_data["extra"] = redact_sensitive_data(extras)

        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        return json.dumps(redact_sensitive_data(log_data), default=str)


def configure_logging(level: int = logging.INFO) -> None:
    """Configure the root logger with the structured JSON formatter."""
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Clear existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(StructuredJsonFormatter())
    root_logger.addHandler(console_handler)
