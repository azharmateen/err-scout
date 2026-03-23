# err-scout

**Lightweight error tracking: collect, group, and triage exceptions without Sentry.**

> You need error tracking. You do not need a $29/month SaaS with 200 features you will never use. **err-scout** is a single-binary error tracker with a FastAPI ingest server, SQLite storage, smart grouping, a dashboard, and alerting -- all in one `pip install`.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)

## The Problem

Sentry is overkill for most projects. You want to know when exceptions happen, group similar ones, see a timeline, and get alerts for spikes. **err-scout** does exactly that with zero infrastructure cost.

## Features

- **Ingest Server** -- FastAPI endpoint accepts error events, stores in SQLite
- **Smart Grouping** -- Fingerprints errors by exception type + culprit frame + message template. Same bug = same group, even with different variable values.
- **Dashboard** -- Dark-mode web UI with Chart.js: error timeline, top errors, per-release breakdown, search/filter
- **Alerting** -- New error groups, spike detection (10x normal rate), regressions (resolved errors that return). Sends to webhooks, Slack, or email.
- **Client SDK** -- `import err_scout; err_scout.init(dsn="http://...")` -- auto-captures unhandled exceptions

## Install

```bash
pip install err-scout
```

## Quick Start

```bash
# 1. Start the ingest server
err-scout serve --port 8000

# 2. Start the dashboard
err-scout dashboard --port 8001

# 3. Open http://localhost:8001
```

### Client Integration

```python
import err_scout

# Initialize (auto-captures unhandled exceptions)
err_scout.init(
    dsn="http://localhost:8000",
    release="1.2.0",
    environment="production",
)

# Manual capture
try:
    risky_operation()
except Exception:
    err_scout.capture_exception(
        tags={"component": "payments"},
        user_id="user_123",
    )

# Capture messages
err_scout.capture_message("Deployment complete", level="info")
```

### Send Events Directly

```bash
curl -X POST http://localhost:8000/api/events \
  -H "Content-Type: application/json" \
  -d '{
    "exception": "ValueError",
    "message": "Invalid input: expected int, got str",
    "stack_trace": "Traceback...",
    "release": "1.2.0",
    "environment": "production",
    "tags": {"component": "api"}
  }'
```

## Error Grouping

err-scout groups errors by fingerprinting:

```
fingerprint = hash(exception_type + culprit_frame + message_template)
```

- `ValueError: invalid literal for int() with base 10: '42'`
- `ValueError: invalid literal for int() with base 10: 'abc'`

Both group together because the message template is the same: `ValueError: invalid literal for int() with base {N}: '{S}'`

## CLI Reference

| Command | Description |
|---------|-------------|
| `err-scout serve` | Start ingest API server (default: port 8000) |
| `err-scout dashboard` | Start web dashboard (default: port 8001) |
| `err-scout report` | Show error statistics |
| `err-scout flush` | Delete old events |

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/events` | Ingest error event |
| GET | `/api/groups` | List error groups |
| GET | `/api/groups/{id}` | Get group with events |
| PATCH | `/api/groups/{id}` | Update group status |
| GET | `/api/stats` | Get statistics |
| DELETE | `/api/events` | Flush old events |

## Alerting

```python
from err_scout.alerter import AlertEngine, AlertConfig

engine = AlertEngine(config=AlertConfig(
    webhook_url="https://hooks.example.com/alerts",
    slack_webhook_url="https://hooks.slack.com/...",
    spike_multiplier=10.0,  # Alert when 10x normal rate
))

# Check for spikes
alerts = engine.check_spike()
for alert in alerts:
    engine.send_alert(alert)
```

## License

MIT
