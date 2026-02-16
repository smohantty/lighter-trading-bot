import logging

from src.logging_utils import BenignWebSocketHandshakeFilter


def _record(
    *,
    name: str,
    level: int,
    message: str,
    exc: BaseException | None = None,
) -> logging.LogRecord:
    exc_info = (type(exc), exc, None) if exc else None
    return logging.LogRecord(
        name=name,
        level=level,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=exc_info,
    )


def test_suppresses_benign_websocket_probe_handshake_error():
    filt = BenignWebSocketHandshakeFilter()
    exc = RuntimeError("did not receive a valid HTTP request")
    record = _record(
        name="websockets.server",
        level=logging.ERROR,
        message="opening handshake failed",
        exc=exc,
    )

    assert filt.filter(record) is False


def test_keeps_non_benign_handshake_errors_visible():
    filt = BenignWebSocketHandshakeFilter()
    exc = RuntimeError("TLS handshake failed")
    record = _record(
        name="websockets.server",
        level=logging.ERROR,
        message="opening handshake failed",
        exc=exc,
    )

    assert filt.filter(record) is True


def test_ignores_other_loggers():
    filt = BenignWebSocketHandshakeFilter()
    exc = RuntimeError("did not receive a valid HTTP request")
    record = _record(
        name="websockets.client",
        level=logging.ERROR,
        message="opening handshake failed",
        exc=exc,
    )

    assert filt.filter(record) is True
