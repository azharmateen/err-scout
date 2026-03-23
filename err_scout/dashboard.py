"""Web dashboard using Flask + Jinja2 with Chart.js visualizations."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, render_template, request, jsonify, redirect, url_for


def create_dashboard_app(db_path: str = "err_scout.db") -> Flask:
    """Create the Flask dashboard app."""
    template_dir = Path(__file__).parent / "templates"
    app = Flask(__name__, template_folder=str(template_dir))
    app.config["DB_PATH"] = db_path

    def get_db():
        conn = sqlite3.connect(app.config["DB_PATH"])
        conn.row_factory = sqlite3.Row
        return conn

    @app.route("/")
    def index():
        """Main dashboard page."""
        conn = get_db()

        # Stats
        total_groups = conn.execute("SELECT COUNT(*) as c FROM error_groups").fetchone()["c"]
        unresolved = conn.execute(
            "SELECT COUNT(*) as c FROM error_groups WHERE status = 'unresolved'"
        ).fetchone()["c"]
        total_events = conn.execute("SELECT COUNT(*) as c FROM error_events").fetchone()["c"]

        # Top error groups
        groups = conn.execute("""
            SELECT * FROM error_groups
            WHERE status != 'ignored'
            ORDER BY last_seen DESC
            LIMIT 25
        """).fetchall()

        # Timeline data (last 24h, hourly)
        timeline = conn.execute("""
            SELECT strftime('%H:00', timestamp) as hour, COUNT(*) as count
            FROM error_events
            WHERE timestamp > datetime('now', '-24 hours')
            GROUP BY hour
            ORDER BY hour
        """).fetchall()

        timeline_labels = json.dumps([r["hour"] for r in timeline])
        timeline_data = json.dumps([r["count"] for r in timeline])

        # Per-release breakdown
        releases = conn.execute("""
            SELECT release, COUNT(*) as count
            FROM error_events
            WHERE release IS NOT NULL
            GROUP BY release
            ORDER BY count DESC
            LIMIT 10
        """).fetchall()

        release_labels = json.dumps([r["release"] for r in releases])
        release_data = json.dumps([r["count"] for r in releases])

        conn.close()

        parsed_groups = []
        for g in groups:
            parsed_groups.append({
                "group_id": g["group_id"],
                "exception": g["exception"],
                "message_template": g["message_template"],
                "culprit_frame": g["culprit_frame"],
                "first_seen": g["first_seen"],
                "last_seen": g["last_seen"],
                "count": g["count"],
                "status": g["status"],
                "releases": json.loads(g["releases"]),
                "environments": json.loads(g["environments"]),
            })

        return render_template(
            "dashboard.html",
            total_groups=total_groups,
            unresolved=unresolved,
            total_events=total_events,
            groups=parsed_groups,
            timeline_labels=timeline_labels,
            timeline_data=timeline_data,
            release_labels=release_labels,
            release_data=release_data,
        )

    @app.route("/group/<group_id>")
    def group_detail(group_id):
        """Group detail page."""
        conn = get_db()

        group = conn.execute(
            "SELECT * FROM error_groups WHERE group_id = ?", (group_id,)
        ).fetchone()

        if not group:
            return "Group not found", 404

        events = conn.execute(
            "SELECT * FROM error_events WHERE group_id = ? ORDER BY timestamp DESC LIMIT 50",
            (group_id,),
        ).fetchall()

        conn.close()

        return render_template(
            "dashboard.html",
            detail_mode=True,
            group=dict(group),
            events=[dict(e) for e in events],
            total_groups=0,
            unresolved=0,
            total_events=0,
            groups=[],
            timeline_labels="[]",
            timeline_data="[]",
            release_labels="[]",
            release_data="[]",
        )

    @app.route("/resolve/<group_id>", methods=["POST"])
    def resolve_group(group_id):
        """Resolve an error group."""
        conn = get_db()
        conn.execute(
            "UPDATE error_groups SET status = 'resolved' WHERE group_id = ?",
            (group_id,),
        )
        conn.commit()
        conn.close()
        return redirect(url_for("index"))

    @app.route("/ignore/<group_id>", methods=["POST"])
    def ignore_group(group_id):
        """Ignore an error group."""
        conn = get_db()
        conn.execute(
            "UPDATE error_groups SET status = 'ignored' WHERE group_id = ?",
            (group_id,),
        )
        conn.commit()
        conn.close()
        return redirect(url_for("index"))

    @app.route("/api/search")
    def search():
        """Search error groups."""
        q = request.args.get("q", "")
        conn = get_db()
        groups = conn.execute(
            """SELECT * FROM error_groups
               WHERE exception LIKE ? OR message_template LIKE ?
               ORDER BY last_seen DESC LIMIT 25""",
            (f"%{q}%", f"%{q}%"),
        ).fetchall()
        conn.close()
        return jsonify({"groups": [dict(g) for g in groups]})

    return app
