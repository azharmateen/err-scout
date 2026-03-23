"""Error grouping and fingerprinting logic."""

import hashlib
import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ErrorFingerprint:
    """Fingerprint components for an error."""
    exception_type: str
    culprit_frame: str
    message_template: str
    fingerprint: str

    @classmethod
    def from_error(
        cls,
        exception: str,
        message: str = "",
        stack_trace: str = "",
    ) -> "ErrorFingerprint":
        """Create fingerprint from error details."""
        culprit = extract_culprit_frame(stack_trace)
        template = templatize_message(message)
        raw = f"{exception}|{culprit}|{template}"
        fp = hashlib.sha256(raw.encode()).hexdigest()[:16]

        return cls(
            exception_type=exception,
            culprit_frame=culprit,
            message_template=template,
            fingerprint=fp,
        )


def extract_culprit_frame(stack_trace: str) -> str:
    """
    Extract the most relevant frame from a Python stack trace.

    Strategy:
    1. Find the last "File" line that's NOT in site-packages/stdlib
    2. Fall back to the last "File" line
    3. Fall back to last non-empty line
    """
    if not stack_trace:
        return ""

    lines = stack_trace.strip().split("\n")

    # Collect all File lines
    file_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("File "):
            file_lines.append(stripped)

    if not file_lines:
        # No file lines, return last non-empty
        for line in reversed(lines):
            if line.strip():
                return line.strip()[:200]
        return ""

    # Prefer user code (not site-packages, not stdlib)
    stdlib_patterns = [
        "site-packages/",
        "/lib/python",
        "/Lib/python",
        "<frozen ",
        "<string>",
    ]

    for fline in reversed(file_lines):
        if not any(pat in fline for pat in stdlib_patterns):
            return _normalize_frame(fline)

    # Fall back to last file line
    return _normalize_frame(file_lines[-1])


def _normalize_frame(frame_line: str) -> str:
    """Normalize a frame line for consistent fingerprinting."""
    # Remove absolute path prefix, keep relative
    # File "/home/user/app/module.py", line 42, in handler
    # -> File "module.py", line {N}, in handler
    result = frame_line

    # Remove line numbers (they change often)
    result = re.sub(r'line \d+', 'line {N}', result)

    # Shorten paths
    result = re.sub(r'File ".*?([^/\\]+\.py)"', r'File "\1"', result)

    return result[:200]


def templatize_message(message: str) -> str:
    """
    Convert error message to a template for grouping.

    Replaces variable parts (numbers, strings, UUIDs, etc.) with placeholders
    so that similar errors group together.
    """
    if not message:
        return ""

    template = message

    # Replace UUIDs
    template = re.sub(
        r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
        '{UUID}', template, flags=re.IGNORECASE,
    )

    # Replace hex strings (0x...)
    template = re.sub(r'0x[0-9a-fA-F]+', '{HEX}', template)

    # Replace IP addresses
    template = re.sub(r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b', '{IP}', template)

    # Replace URLs
    template = re.sub(r'https?://\S+', '{URL}', template)

    # Replace file paths
    template = re.sub(r'[/\\][\w/\\.-]+\.\w+', '{PATH}', template)

    # Replace quoted strings
    template = re.sub(r"'[^']{2,}'", "'{S}'", template)
    template = re.sub(r'"[^"]{2,}"', '"{S}"', template)

    # Replace numbers (but not in placeholders)
    template = re.sub(r'(?<!\{)\b\d+\b(?!\})', '{N}', template)

    return template[:500]


def group_errors(errors: list[dict]) -> dict[str, list[dict]]:
    """
    Group a list of error dicts by fingerprint.

    Args:
        errors: List of dicts with at least 'exception', 'message', 'stack_trace'

    Returns:
        Dict mapping fingerprint -> list of matching errors
    """
    groups: dict[str, list[dict]] = {}

    for error in errors:
        fp = ErrorFingerprint.from_error(
            exception=error.get("exception", "Unknown"),
            message=error.get("message", ""),
            stack_trace=error.get("stack_trace", ""),
        )

        if fp.fingerprint not in groups:
            groups[fp.fingerprint] = []
        groups[fp.fingerprint].append(error)

    return groups
