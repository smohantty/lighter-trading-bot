import logging
import os
import sys
import uuid
from logging.handlers import TimedRotatingFileHandler


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default

    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _parse_log_level(raw_level: str | None, default: int) -> int:
    if not raw_level:
        return default

    normalized = raw_level.strip().upper()
    if normalized.isdigit():
        return int(normalized)

    parsed = logging.getLevelName(normalized)
    if isinstance(parsed, int):
        return parsed
    return default


class RuntimeLogContextFilter(logging.Filter):
    def __init__(self, run_id: str, mode: str):
        super().__init__()
        self.run_id = run_id
        self.mode = mode

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = self.run_id
        record.mode = self.mode
        return True


class BenignWebSocketHandshakeFilter(logging.Filter):
    """Suppress noisy websocket handshake errors from non-HTTP probe traffic."""

    _HANDSHAKE_ERROR_MESSAGE = "opening handshake failed"
    _BENIGN_EXCEPTION_TEXT = "did not receive a valid HTTP request"

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != "websockets.server":
            return True
        if record.levelno < logging.ERROR:
            return True
        if record.getMessage() != self._HANDSHAKE_ERROR_MESSAGE:
            return True

        exc = record.exc_info[1] if record.exc_info else None
        return not self._is_benign_exception(exc)

    def _is_benign_exception(self, exc: BaseException | None) -> bool:
        current = exc
        while current is not None:
            if self._BENIGN_EXCEPTION_TEXT in str(current):
                return True
            current = current.__cause__
        return False


def configure_logging(is_simulation: bool = False) -> str:
    mode = "simulation" if is_simulation else "live"
    run_id = os.getenv("LIGHTER_RUN_ID", uuid.uuid4().hex[:12])

    log_dir = os.getenv("LIGHTER_LOG_DIR", "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = (
        os.path.join(log_dir, "simulation.log")
        if is_simulation
        else os.path.join(log_dir, "lighter-trading-bot.log")
    )

    base_level = _parse_log_level(os.getenv("LIGHTER_LOG_LEVEL"), logging.INFO)
    console_level = _parse_log_level(
        os.getenv("LIGHTER_CONSOLE_LOG_LEVEL"),
        base_level,
    )
    file_level = _parse_log_level(
        os.getenv("LIGHTER_FILE_LOG_LEVEL"),
        _parse_log_level(os.getenv("LIGHTER_LOG_LEVEL"), logging.DEBUG),
    )
    backup_count = int(os.getenv("LIGHTER_LOG_BACKUP_COUNT", "30"))

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        fmt=(
            "%(asctime)s %(levelname)s %(name)s "
            "[run_id=%(run_id)s mode=%(mode)s pid=%(process)d] %(message)s"
        )
    )
    context_filter = RuntimeLogContextFilter(run_id=run_id, mode=mode)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(console_level)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(context_filter)

    file_handler = TimedRotatingFileHandler(
        log_file,
        when="midnight",
        interval=1,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(file_level)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(context_filter)

    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    third_party_default_level = _parse_log_level(
        os.getenv("LIGHTER_THIRD_PARTY_LOG_LEVEL"),
        logging.INFO,
    )
    sdk_level = _parse_log_level(
        os.getenv("LIGHTER_SDK_LOG_LEVEL"),
        third_party_default_level,
    )
    websocket_level = _parse_log_level(
        os.getenv("LIGHTER_WEBSOCKET_LOG_LEVEL"),
        third_party_default_level,
    )

    logging.getLogger("lighter").setLevel(sdk_level)
    logging.getLogger("websockets").setLevel(websocket_level)
    websocket_server_logger = logging.getLogger("websockets.server")
    if not any(
        isinstance(existing_filter, BenignWebSocketHandshakeFilter)
        for existing_filter in websocket_server_logger.filters
    ):
        websocket_server_logger.addFilter(BenignWebSocketHandshakeFilter())
    logging.getLogger("urllib3").setLevel(third_party_default_level)

    return run_id
