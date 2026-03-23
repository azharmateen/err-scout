"""err-scout: Lightweight error tracking without Sentry."""

__version__ = "0.1.0"

from .client import init, capture_exception, capture_message

__all__ = ["init", "capture_exception", "capture_message"]
