# Logging Guide

## Goals
- Keep production logs high-signal for incident diagnosis.
- Ensure order lifecycle, reconciliation, and degraded-mode transitions are traceable.
- Make logs reproducible for coding agents by adding stable context fields and controllable verbosity.

## Standard Log Context
All logs include:
- `run_id`: unique per process start (or `LIGHTER_RUN_ID` override)
- `mode`: `live` or `simulation`
- `pid`

Configured format:
`timestamp level logger [run_id=... mode=... pid=...] message`

## Production Defaults
Default behavior favors signal over volume:
- File logs capture debug detail by default for post-incident forensics.
- Console logs remain concise (info-level by default).
- Detailed per-order stream chatter is disabled.
- Healthy reconciliation heartbeat is debug-level by default.
- Zone-order fan-out logs are debug-level; summary activation logs remain info-level.
- Simulation per-order queue/fill logs are debug-level unless explicitly enabled.

## Runtime Controls
- `LIGHTER_LOG_LEVEL`: base level override (console default uses this; file defaults to debug unless explicitly set).
- `LIGHTER_CONSOLE_LOG_LEVEL`: console handler level.
- `LIGHTER_FILE_LOG_LEVEL`: file handler level.
- `LIGHTER_LOG_BACKUP_COUNT`: timed-rotation retention count (default `30`).
- `LIGHTER_LOG_DIR`: log directory (default `logs`).
- `LIGHTER_RUN_ID`: optional run identifier override.
- `LIGHTER_SDK_LOG_LEVEL`: `lighter` SDK logger level override.
- `LIGHTER_WEBSOCKET_LOG_LEVEL`: `websockets` logger level override.
- `LIGHTER_THIRD_PARTY_LOG_LEVEL`: fallback level for third-party loggers.

High-volume toggles:
- `LIGHTER_LOG_VERBOSE_ORDER_STREAM=true`
- `LIGHTER_LOG_RECONCILIATION_HEALTHY=true`
- `LIGHTER_LOG_SIM_ORDER_DETAILS=true`

## Incident Reproduction Workflow
1. Capture `run_id` from startup log.
2. Filter logs by `run_id` and timeframe around incident.
3. Correlate order lifecycle using `cloid`, `oid`, strategy events, and reconciliation messages.
4. Default production logs should contain sufficient error context without requiring a rerun.
5. Optional: enable high-volume toggles only when deeper stream-level traces are needed.
