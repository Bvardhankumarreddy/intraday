"""Structured JSON logging with secret redaction.

Each line is one JSON object with ``ts`` (UTC), ``level``, ``logger``,
``msg``, plus context fields when set: ``component``, ``session_id``,
``correlation_id``, ``event_type`` (see ``log_context``), and any ``extra=``.

Redaction runs on the whole payload, including exception text:
- values under sensitive keys (secret, token, password, api key,
  authorization, cookie, totp, session);
- ``key=value`` / ``key: value`` pairs with a sensitive key, including
  ``Authorization: Bearer <token>``;
- bare bearer tokens and JWT-like strings;
- the exact values of secrets loaded at startup (``register_secret``), so a
  credential echoed inside an exception message is still hidden.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

_KEYS = r"secret|token|password|passwd|api[_-]?key|authorization|cookie|totp|session[_-]?key"
_SENSITIVE_KEY = re.compile(_KEYS, re.I)
# key [:=] [Bearer ]value  (quotes optional)
_SENSITIVE_VALUE = re.compile(
    rf"((?:{_KEYS})[\"']?\s*[:=]\s*[\"']?(?:bearer\s+)?)([^\s\"',}}]+)", re.I)
_BEARER = re.compile(r"(\bbearer\s+)([A-Za-z0-9._~+/=-]{8,})", re.I)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b")
_STANDARD_ATTRS = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message"}
_SECRETS: set[str] = set()
_CONTEXT: contextvars.ContextVar[dict] = contextvars.ContextVar("log_context", default={})


def register_secret(value: str | None) -> None:
    """Redact this exact value wherever it appears (min. 6 characters, so
    short common strings are not mangled)."""
    if value and len(value) >= 6:
        _SECRETS.add(value)


def register_settings_secrets(settings) -> None:
    for name in ("broker_api_key", "broker_api_secret", "broker_totp_secret", "llm_api_key"):
        v = getattr(settings, name, None)
        if v is not None:
            register_secret(v.get_secret_value())


def redact_text(text: str) -> str:
    for secret in _SECRETS:
        if secret in text:
            text = text.replace(secret, "***")
    text = _SENSITIVE_VALUE.sub(r"\1***", text)
    text = _BEARER.sub(r"\1***", text)
    return _JWT.sub("***", text)


def redact(value: object) -> object:
    if isinstance(value, dict):
        return {
            k: "***" if _SENSITIVE_KEY.search(str(k)) else redact(v) for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


@contextmanager
def log_context(**fields) -> Iterator[None]:
    """Attach fields (component, session_id, correlation_id, event_type, ...)
    to every log line written inside the block."""
    token = _CONTEXT.set({**_CONTEXT.get(), **{k: v for k, v in fields.items() if v is not None}})
    try:
        yield
    finally:
        _CONTEXT.reset(token)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            **_CONTEXT.get(),
        }
        extra = {k: v for k, v in vars(record).items() if k not in _STANDARD_ATTRS}
        payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(redact(payload), default=str)


def configure_logging(level: str = "INFO", settings=None) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    if settings is not None:
        register_settings_secrets(settings)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
