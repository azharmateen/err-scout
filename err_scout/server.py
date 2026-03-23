"""FastAPI ingest server for error events with SQLite storage."""

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# Database path
DB_PATH = Path("err_scout.db")

app = FastAPI(title="err-scout", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Models ---

class ErrorEvent(BaseModel):
    """An error event submitted by a client."""
    exception: str = Field(..., description="Exception class name")
    message: str = Field(default="", description="Error message")
    stack_trace: str = Field(default="", description="Full stack trace")
    tags: dict = Field(default_factory=dict, description="Arbitrary tags")
    release: Optional[str] = Field(default=None, description="Release/version")
    environment: str = Field(default="production", description="Environment name")
    user_id: Optional[str] = Field(default=None, description="Affected user")
    timestamp: Optional[str] = Field(default=None, description="ISO timestamp")
    extra: dict = Field(default_factory=dict, description="Extra context data")


class ErrorEventResponse(BaseModel):
    event_id: str
    group_id: str
    is_new_group: bool


class ErrorGroup(BaseModel):
    group_id: str
    fingerprint: str
    exception: str
    message_template: str
    culprit_frame: str
    first_seen: str
    last_seen: str
    count: int
    status: str  # "unresolved", "resolved", "ignored"
    releases: list[str]
    environments: list[str]


# --- Database ---

def init_db(db_path: Optional[Path] = None):
    """Initialize the SQLite database."""
    path = db_path or DB_PATH
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS error_groups (
            group_id TEXT PRIMARY KEY,
            fingerprint TEXT UNIQUE NOT NULL,
            exception TEXT NOT NULL,
            message_template TEXT NOT NULL,
            culprit_frame TEXT NOT NULL DEFAULT '',
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'unresolved',
            releases TEXT NOT NULL DEFAULT '[]',
            environments TEXT NOT NULL DEFAULT '[]'
        );

        CREATE TABLE IF NOT EXISTS error_events (
            event_id TEXT PRIMARY KEY,
            group_id TEXT NOT NULL,
            exception TEXT NOT NULL,
            message TEXT NOT NULL DEFAULT '',
            stack_trace TEXT NOT NULL DEFAULT '',
            tags TEXT NOT NULL DEFAULT '{}',
            release TEXT,
            environment TEXT NOT NULL DEFAULT 'production',
            user_id TEXT,
            timestamp TEXT NOT NULL,
            extra TEXT NOT NULL DEFAULT '{}',
            FOREIGN KEY (group_id) REFERENCES error_groups(group_id)
        );

        CREATE INDEX IF NOT EXISTS idx_events_group ON error_events(group_id);
        CREATE INDEX IF NOT EXISTS idx_events_timestamp ON error_events(timestamp);
        CREATE INDEX IF NOT EXISTS idx_groups_status ON error_groups(status);
        CREATE INDEX IF NOT EXISTS idx_groups_last_seen ON error_groups(last_seen);
    """)

    conn.close()


@contextmanager
def get_db(db_path: Optional[Path] = None):
    """Get a database connection."""
    path = db_path or DB_PATH
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --- Fingerprinting ---

def _extract_culprit_frame(stack_trace: str) -> str:
    """Extract the most relevant frame from a stack trace."""
    if not stack_trace:
        return ""

    lines = stack_trace.strip().split("\n")
    # Look for the last "File" line (typically the user's code)
    for line in reversed(lines):
        line = line.strip()
        if line.startswith("File "):
            # Extract file and line: File "path.py", line 42, in func
            return line
    # Fallback: last non-empty line
    for line in reversed(lines):
        if line.strip():
            return line.strip()
    return ""


def _templatize_message(message: str) -> str:
    """Convert error message to a template by replacing variable parts."""
    import re
    # Replace numbers
    template = re.sub(r'\b\d+\b', '{N}', message)
    # Replace quoted strings
    template = re.sub(r"'[^']*'", "'{S}'", template)
    template = re.sub(r'"[^"]*"', '"{S}"', template)
    # Replace UUIDs
    template = re.sub(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', '{UUID}', template)
    # Replace hex strings
    template = re.sub(r'0x[0-9a-f]+', '{HEX}', template, flags=re.IGNORECASE)
    return template


def compute_fingerprint(event: ErrorEvent) -> str:
    """Compute a fingerprint for grouping similar errors."""
    culprit = _extract_culprit_frame(event.stack_trace)
    template = _templatize_message(event.message)

    raw = f"{event.exception}|{culprit}|{template}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# --- API Endpoints ---

@app.on_event("startup")
async def startup():
    init_db()


@app.post("/api/events", response_model=ErrorEventResponse)
async def ingest_event(event: ErrorEvent):
    """Ingest an error event."""
    fingerprint = compute_fingerprint(event)
    now = event.timestamp or datetime.now(timezone.utc).isoformat()
    event_id = hashlib.sha256(f"{fingerprint}{now}{time.time_ns()}".encode()).hexdigest()[:16]

    is_new_group = False

    with get_db() as conn:
        # Check if group exists
        row = conn.execute(
            "SELECT group_id, releases, environments, count FROM error_groups WHERE fingerprint = ?",
            (fingerprint,),
        ).fetchone()

        if row:
            group_id = row["group_id"]
            # Update group
            releases = json.loads(row["releases"])
            environments = json.loads(row["environments"])
            if event.release and event.release not in releases:
                releases.append(event.release)
            if event.environment not in environments:
                environments.append(event.environment)

            conn.execute(
                """UPDATE error_groups
                   SET last_seen = ?, count = count + 1,
                       releases = ?, environments = ?,
                       status = CASE WHEN status = 'resolved' THEN 'regression' ELSE status END
                   WHERE group_id = ?""",
                (now, json.dumps(releases), json.dumps(environments), group_id),
            )
        else:
            # Create new group
            group_id = hashlib.sha256(f"group:{fingerprint}".encode()).hexdigest()[:16]
            is_new_group = True
            releases = [event.release] if event.release else []
            environments = [event.environment]
            culprit = _extract_culprit_frame(event.stack_trace)
            template = _templatize_message(event.message)

            conn.execute(
                """INSERT INTO error_groups
                   (group_id, fingerprint, exception, message_template, culprit_frame,
                    first_seen, last_seen, count, status, releases, environments)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'unresolved', ?, ?)""",
                (group_id, fingerprint, event.exception, template, culprit,
                 now, now, json.dumps(releases), json.dumps(environments)),
            )

        # Store the event
        conn.execute(
            """INSERT INTO error_events
               (event_id, group_id, exception, message, stack_trace, tags,
                release, environment, user_id, timestamp, extra)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event_id, group_id, event.exception, event.message,
             event.stack_trace, json.dumps(event.tags), event.release,
             event.environment, event.user_id, now, json.dumps(event.extra)),
        )

    return ErrorEventResponse(
        event_id=event_id,
        group_id=group_id,
        is_new_group=is_new_group,
    )


@app.get("/api/groups")
async def list_groups(
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    sort: str = "last_seen",
):
    """List error groups."""
    with get_db() as conn:
        query = "SELECT * FROM error_groups"
        params = []

        if status:
            query += " WHERE status = ?"
            params.append(status)

        if sort == "count":
            query += " ORDER BY count DESC"
        else:
            query += " ORDER BY last_seen DESC"

        query += " LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = conn.execute(query, params).fetchall()

        groups = []
        for row in rows:
            groups.append({
                "group_id": row["group_id"],
                "fingerprint": row["fingerprint"],
                "exception": row["exception"],
                "message_template": row["message_template"],
                "culprit_frame": row["culprit_frame"],
                "first_seen": row["first_seen"],
                "last_seen": row["last_seen"],
                "count": row["count"],
                "status": row["status"],
                "releases": json.loads(row["releases"]),
                "environments": json.loads(row["environments"]),
            })

        # Get total count
        count_query = "SELECT COUNT(*) as total FROM error_groups"
        if status:
            count_query += " WHERE status = ?"
            total = conn.execute(count_query, [status]).fetchone()["total"]
        else:
            total = conn.execute(count_query).fetchone()["total"]

    return {"groups": groups, "total": total}


@app.get("/api/groups/{group_id}")
async def get_group(group_id: str):
    """Get a specific error group with recent events."""
    with get_db() as conn:
        group = conn.execute(
            "SELECT * FROM error_groups WHERE group_id = ?", (group_id,)
        ).fetchone()

        if not group:
            raise HTTPException(status_code=404, detail="Group not found")

        events = conn.execute(
            "SELECT * FROM error_events WHERE group_id = ? ORDER BY timestamp DESC LIMIT 20",
            (group_id,),
        ).fetchall()

    return {
        "group": dict(group),
        "events": [dict(e) for e in events],
    }


@app.patch("/api/groups/{group_id}")
async def update_group(group_id: str, status: str):
    """Update group status (resolve, ignore, unresolve)."""
    if status not in ("unresolved", "resolved", "ignored"):
        raise HTTPException(400, "Invalid status")

    with get_db() as conn:
        conn.execute(
            "UPDATE error_groups SET status = ? WHERE group_id = ?",
            (status, group_id),
        )

    return {"ok": True, "group_id": group_id, "status": status}


@app.get("/api/stats")
async def get_stats():
    """Get error statistics."""
    with get_db() as conn:
        total_groups = conn.execute("SELECT COUNT(*) as c FROM error_groups").fetchone()["c"]
        unresolved = conn.execute(
            "SELECT COUNT(*) as c FROM error_groups WHERE status = 'unresolved'"
        ).fetchone()["c"]
        total_events = conn.execute("SELECT COUNT(*) as c FROM error_events").fetchone()["c"]

        # Events per hour (last 24h)
        timeline = conn.execute("""
            SELECT strftime('%Y-%m-%dT%H:00:00', timestamp) as hour, COUNT(*) as count
            FROM error_events
            WHERE timestamp > datetime('now', '-24 hours')
            GROUP BY hour
            ORDER BY hour
        """).fetchall()

        # Top errors
        top_errors = conn.execute("""
            SELECT exception, message_template, count, last_seen
            FROM error_groups
            WHERE status != 'ignored'
            ORDER BY count DESC
            LIMIT 10
        """).fetchall()

    return {
        "total_groups": total_groups,
        "unresolved_groups": unresolved,
        "total_events": total_events,
        "timeline": [{"hour": r["hour"], "count": r["count"]} for r in timeline],
        "top_errors": [dict(r) for r in top_errors],
    }


@app.delete("/api/events")
async def flush_events(older_than_days: int = 30):
    """Flush old events."""
    with get_db() as conn:
        result = conn.execute(
            "DELETE FROM error_events WHERE timestamp < datetime('now', ?)",
            (f"-{older_than_days} days",),
        )
        deleted = result.rowcount

    return {"deleted_events": deleted}


def create_app(db_path: Optional[str] = None) -> FastAPI:
    """Create the FastAPI app with custom DB path."""
    global DB_PATH
    if db_path:
        DB_PATH = Path(db_path)
    init_db()
    return app
