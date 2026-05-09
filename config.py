from __future__ import annotations
import threading

_lock = threading.RLock()

_check_interval_seconds: int = 60
_request_timeout_ms: int = 5000


def get_config() -> dict:
    with _lock:
        return {
            "check_interval_seconds": _check_interval_seconds,
            "request_timeout_ms": _request_timeout_ms,
        }


def set_config(check_interval_seconds: int, request_timeout_ms: int) -> None:
    global _check_interval_seconds, _request_timeout_ms
    with _lock:
        _check_interval_seconds = check_interval_seconds
        _request_timeout_ms = request_timeout_ms