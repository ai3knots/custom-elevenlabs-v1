"""Unit tests for structured logging and sensitive data redaction."""

import json
import logging

from src.logging import (
    StructuredJsonFormatter,
    get_call_id,
    redact_sensitive_data,
    set_call_id,
)


def test_redact_sensitive_keys():
    data = {
        "api_key": "secret_123",
        "nested": {
            "password": "my_password",
            "safe_field": "hello world",
            "bearer_token": "token_xyz",
        },
        "clean_list": ["ok", {"user_secret": "hidden"}],
    }
    redacted = redact_sensitive_data(data)
    assert redacted["api_key"] == "[REDACTED]"
    assert redacted["nested"]["password"] == "[REDACTED]"
    assert redacted["nested"]["bearer_token"] == "[REDACTED]"
    assert redacted["nested"]["safe_field"] == "hello world"
    assert redacted["clean_list"][1]["user_secret"] == "[REDACTED]"


def test_redact_credit_cards():
    text = "User provided card 4532-1234-5678-9012 for billing."
    redacted = redact_sensitive_data(text)
    assert "[REDACTED]" in redacted
    assert "4532" not in redacted


def test_structured_json_formatter():
    formatter = StructuredJsonFormatter()
    set_call_id("call_test_999")

    record = logging.LogRecord(
        name="test_logger",
        level=logging.INFO,
        pathname="test.py",
        lineno=10,
        msg="Test message with param: %s",
        args=("sample",),
        exc_info=None,
    )
    record.custom_secret = "sensitive_value"  # type: ignore

    output = formatter.format(record)
    parsed = json.loads(output)

    assert parsed["level"] == "INFO"
    assert parsed["logger"] == "test_logger"
    assert parsed["message"] == "Test message with param: sample"
    assert parsed["call_id"] == "call_test_999"
    assert parsed["extra"]["custom_secret"] == "[REDACTED]"

    set_call_id(None)
    assert get_call_id() is None
