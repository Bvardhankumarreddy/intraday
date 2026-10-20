import json
import logging

from app.utils.logging import JsonFormatter, redact


def test_redact_keys_and_inline_values():
    data = {"api_key": "abc", "nested": {"access_token": "t"}, "note": "token=xyz123 ok"}
    out = redact(data)
    assert out["api_key"] == "***"
    assert out["nested"]["access_token"] == "***"
    assert "xyz123" not in out["note"]


def test_formatter_emits_json_without_secrets():
    record = logging.LogRecord("t", logging.INFO, "", 0, "auth password=hunter2", None, None)
    record.symbol = "SIMALPHA"
    record.api_secret = "s3cr3t"
    line = JsonFormatter().format(record)
    payload = json.loads(line)
    assert payload["symbol"] == "SIMALPHA"
    assert "hunter2" not in line and "s3cr3t" not in line
