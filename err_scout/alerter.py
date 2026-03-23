"""Alert rules: new groups, spike detection, regressions."""

import json
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Callable

import requests


@dataclass
class Alert:
    """An alert that should be sent."""
    alert_type: str  # "new_error", "spike", "regression"
    title: str
    message: str
    severity: str  # "info", "warning", "critical"
    group_id: Optional[str] = None
    data: dict = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class AlertConfig:
    """Alert configuration."""
    # Webhook URL for alerts
    webhook_url: Optional[str] = None
    # Slack webhook
    slack_webhook_url: Optional[str] = None
    # Email settings (stub)
    email_to: Optional[str] = None
    email_from: str = "err-scout@localhost"
    # Spike detection: alert if error rate exceeds N times the baseline
    spike_multiplier: float = 10.0
    # Baseline window in hours
    baseline_hours: int = 24
    # Minimum events before spike detection kicks in
    spike_min_events: int = 5
    # Alert on new error groups
    alert_on_new: bool = True
    # Alert on regressions
    alert_on_regression: bool = True
    # Cooldown between alerts for same group (seconds)
    cooldown_seconds: int = 300


class AlertEngine:
    """Engine for detecting and sending alerts."""

    def __init__(self, config: Optional[AlertConfig] = None, db_path: str = "err_scout.db"):
        self.config = config or AlertConfig()
        self.db_path = db_path
        self._last_alert_time: dict[str, float] = {}

    def check_new_group(self, group_id: str, exception: str, message: str) -> Optional[Alert]:
        """Check if this is a new error group that should trigger an alert."""
        if not self.config.alert_on_new:
            return None

        return Alert(
            alert_type="new_error",
            title=f"New error: {exception}",
            message=f"A new error group was created:\n{exception}: {message}",
            severity="warning",
            group_id=group_id,
            data={"exception": exception, "message": message},
        )

    def check_regression(self, group_id: str, exception: str) -> Optional[Alert]:
        """Check if a resolved error has recurred."""
        if not self.config.alert_on_regression:
            return None

        return Alert(
            alert_type="regression",
            title=f"Regression: {exception}",
            message=f"A previously resolved error has recurred: {exception}",
            severity="critical",
            group_id=group_id,
            data={"exception": exception},
        )

    def check_spike(self) -> list[Alert]:
        """Check for error rate spikes by comparing recent rate to baseline."""
        alerts = []

        try:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row

            # Get recent event count (last hour)
            recent = conn.execute("""
                SELECT group_id, COUNT(*) as count
                FROM error_events
                WHERE timestamp > datetime('now', '-1 hour')
                GROUP BY group_id
            """).fetchall()

            for row in recent:
                group_id = row["group_id"]
                recent_count = row["count"]

                # Get baseline (average per hour over baseline window)
                baseline = conn.execute("""
                    SELECT COUNT(*) * 1.0 / ? as avg_per_hour
                    FROM error_events
                    WHERE group_id = ?
                      AND timestamp > datetime('now', ?)
                      AND timestamp <= datetime('now', '-1 hour')
                """, (self.config.baseline_hours, group_id,
                      f"-{self.config.baseline_hours} hours")).fetchone()

                avg_per_hour = baseline["avg_per_hour"] if baseline else 0

                if (avg_per_hour > 0
                        and recent_count >= self.config.spike_min_events
                        and recent_count > avg_per_hour * self.config.spike_multiplier):

                    # Check cooldown
                    if self._is_in_cooldown(f"spike:{group_id}"):
                        continue

                    # Get group info
                    group = conn.execute(
                        "SELECT exception, message_template FROM error_groups WHERE group_id = ?",
                        (group_id,),
                    ).fetchone()

                    exception = group["exception"] if group else "Unknown"

                    alerts.append(Alert(
                        alert_type="spike",
                        title=f"Error spike: {exception}",
                        message=(
                            f"Error rate spike detected for {exception}.\n"
                            f"Last hour: {recent_count} events "
                            f"(baseline: {avg_per_hour:.1f}/hour, "
                            f"{recent_count / max(avg_per_hour, 0.1):.1f}x normal)"
                        ),
                        severity="critical",
                        group_id=group_id,
                        data={
                            "recent_count": recent_count,
                            "baseline_per_hour": round(avg_per_hour, 2),
                            "multiplier": round(recent_count / max(avg_per_hour, 0.1), 1),
                        },
                    ))
                    self._record_alert(f"spike:{group_id}")

            conn.close()

        except Exception:
            pass  # Don't crash on alert check failures

        return alerts

    def send_alert(self, alert: Alert) -> bool:
        """Send an alert through configured channels."""
        sent = False

        # Webhook
        if self.config.webhook_url:
            try:
                resp = requests.post(
                    self.config.webhook_url,
                    json={
                        "type": alert.alert_type,
                        "title": alert.title,
                        "message": alert.message,
                        "severity": alert.severity,
                        "group_id": alert.group_id,
                        "data": alert.data,
                        "timestamp": alert.timestamp,
                    },
                    timeout=10,
                )
                sent = resp.status_code < 400
            except Exception:
                pass

        # Slack
        if self.config.slack_webhook_url:
            try:
                color = {"info": "#36a64f", "warning": "#ffa500", "critical": "#ff0000"}.get(
                    alert.severity, "#808080"
                )
                resp = requests.post(
                    self.config.slack_webhook_url,
                    json={
                        "attachments": [{
                            "color": color,
                            "title": alert.title,
                            "text": alert.message,
                            "footer": "err-scout",
                            "ts": int(time.time()),
                        }],
                    },
                    timeout=10,
                )
                sent = resp.status_code < 400
            except Exception:
                pass

        # Email (stub - logs the alert)
        if self.config.email_to:
            # In a real implementation, use smtplib
            print(f"[EMAIL STUB] To: {self.config.email_to}")
            print(f"  Subject: [{alert.severity.upper()}] {alert.title}")
            print(f"  Body: {alert.message}")
            sent = True

        return sent

    def _is_in_cooldown(self, key: str) -> bool:
        """Check if we're in cooldown for this alert key."""
        last = self._last_alert_time.get(key, 0)
        return (time.time() - last) < self.config.cooldown_seconds

    def _record_alert(self, key: str):
        """Record that we sent an alert."""
        self._last_alert_time[key] = time.time()
