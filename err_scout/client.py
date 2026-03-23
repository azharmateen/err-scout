"""Client SDK: automatic exception capturing and reporting."""

import sys
import threading
import traceback
from typing import Optional
from queue import Queue, Empty

import requests


class _ErrScoutClient:
    """Internal client singleton."""

    def __init__(self):
        self.dsn: Optional[str] = None
        self.release: Optional[str] = None
        self.environment: str = "production"
        self.tags: dict = {}
        self.enabled: bool = False
        self._queue: Queue = Queue(maxsize=1000)
        self._worker: Optional[threading.Thread] = None
        self._original_excepthook = None
        self._batch_size: int = 10
        self._flush_interval: float = 5.0

    def configure(
        self,
        dsn: str,
        release: Optional[str] = None,
        environment: str = "production",
        tags: Optional[dict] = None,
        auto_capture: bool = True,
    ):
        """Configure the err-scout client."""
        self.dsn = dsn.rstrip("/")
        self.release = release
        self.environment = environment
        self.tags = tags or {}
        self.enabled = True

        # Install global exception handler
        if auto_capture:
            self._original_excepthook = sys.excepthook
            sys.excepthook = self._excepthook

        # Start background worker
        self._worker = threading.Thread(target=self._send_worker, daemon=True)
        self._worker.start()

    def capture_exception(
        self,
        exc_info=None,
        tags: Optional[dict] = None,
        extra: Optional[dict] = None,
        user_id: Optional[str] = None,
    ) -> Optional[str]:
        """
        Capture an exception and send to err-scout.

        Args:
            exc_info: Exception info tuple (type, value, tb). If None, uses sys.exc_info()
            tags: Additional tags for this event
            extra: Extra context data
            user_id: Affected user ID
        """
        if not self.enabled:
            return None

        if exc_info is None:
            exc_info = sys.exc_info()

        exc_type, exc_value, exc_tb = exc_info

        if exc_type is None:
            return None

        stack_trace = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        exception_name = exc_type.__name__
        message = str(exc_value)

        event = {
            "exception": exception_name,
            "message": message,
            "stack_trace": stack_trace,
            "tags": {**self.tags, **(tags or {})},
            "release": self.release,
            "environment": self.environment,
            "user_id": user_id,
            "extra": extra or {},
        }

        try:
            self._queue.put_nowait(event)
        except Exception:
            pass  # Queue full, drop event

        return f"{exception_name}: {message}"

    def capture_message(
        self,
        message: str,
        level: str = "info",
        tags: Optional[dict] = None,
        extra: Optional[dict] = None,
    ) -> Optional[str]:
        """Capture a message (not an exception)."""
        if not self.enabled:
            return None

        event = {
            "exception": f"Message.{level}",
            "message": message,
            "stack_trace": "",
            "tags": {**self.tags, "level": level, **(tags or {})},
            "release": self.release,
            "environment": self.environment,
            "extra": extra or {},
        }

        try:
            self._queue.put_nowait(event)
        except Exception:
            pass

        return message

    def flush(self, timeout: float = 5.0):
        """Flush pending events."""
        events = []
        while True:
            try:
                events.append(self._queue.get_nowait())
            except Empty:
                break

        for event in events:
            self._send_event(event)

    def _excepthook(self, exc_type, exc_value, exc_tb):
        """Global exception handler."""
        self.capture_exception(exc_info=(exc_type, exc_value, exc_tb))
        self.flush()

        # Call original handler
        if self._original_excepthook:
            self._original_excepthook(exc_type, exc_value, exc_tb)

    def _send_event(self, event: dict) -> bool:
        """Send a single event to the server."""
        if not self.dsn:
            return False

        try:
            resp = requests.post(
                f"{self.dsn}/api/events",
                json=event,
                timeout=5.0,
            )
            return resp.status_code < 400
        except Exception:
            return False

    def _send_worker(self):
        """Background worker that sends events."""
        import time

        while True:
            events = []
            # Collect events
            try:
                event = self._queue.get(timeout=self._flush_interval)
                events.append(event)

                # Grab more if available
                while len(events) < self._batch_size:
                    try:
                        events.append(self._queue.get_nowait())
                    except Empty:
                        break

            except Empty:
                continue

            # Send events
            for event in events:
                self._send_event(event)


# Global client instance
_client = _ErrScoutClient()


def init(
    dsn: str,
    release: Optional[str] = None,
    environment: str = "production",
    tags: Optional[dict] = None,
    auto_capture: bool = True,
):
    """
    Initialize err-scout client SDK.

    Args:
        dsn: Server URL (e.g., "http://localhost:8000")
        release: Application release/version
        environment: Environment name (production, staging, etc.)
        tags: Default tags for all events
        auto_capture: Auto-capture unhandled exceptions

    Usage:
        import err_scout
        err_scout.init(dsn="http://localhost:8000", release="1.0.0")
    """
    _client.configure(
        dsn=dsn,
        release=release,
        environment=environment,
        tags=tags,
        auto_capture=auto_capture,
    )


def capture_exception(
    exc_info=None,
    tags: Optional[dict] = None,
    extra: Optional[dict] = None,
    user_id: Optional[str] = None,
) -> Optional[str]:
    """
    Capture an exception.

    Usage:
        try:
            risky_operation()
        except Exception:
            err_scout.capture_exception()
    """
    return _client.capture_exception(exc_info=exc_info, tags=tags, extra=extra, user_id=user_id)


def capture_message(
    message: str,
    level: str = "info",
    tags: Optional[dict] = None,
    extra: Optional[dict] = None,
) -> Optional[str]:
    """
    Capture a message.

    Usage:
        err_scout.capture_message("User signup completed", level="info")
    """
    return _client.capture_message(message=message, level=level, tags=tags, extra=extra)


def flush(timeout: float = 5.0):
    """Flush pending events to the server."""
    _client.flush(timeout=timeout)
