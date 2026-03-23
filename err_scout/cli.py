"""CLI for err-scout: serve, dashboard, report, flush."""

import sys

import click
from rich.console import Console

console = Console()


@click.group()
@click.version_option(package_name="err-scout")
def cli():
    """Lightweight error tracking without Sentry."""
    pass


@cli.command()
@click.option("-h", "--host", default="0.0.0.0", help="Bind host")
@click.option("-p", "--port", default=8000, help="Bind port")
@click.option("--db", default="err_scout.db", help="SQLite database path")
def serve(host, port, db):
    """Start the error ingest API server.

    Example: err-scout serve --port 8000
    """
    import uvicorn
    from .server import create_app

    app = create_app(db_path=db)

    console.print(f"\n[bold red]err-scout[/bold red] ingest server")
    console.print(f"  API:      http://{host}:{port}")
    console.print(f"  Database: {db}")
    console.print(f"  Endpoint: POST http://{host}:{port}/api/events")
    console.print()

    uvicorn.run(app, host=host, port=port, log_level="info")


@cli.command()
@click.option("-h", "--host", default="0.0.0.0", help="Bind host")
@click.option("-p", "--port", default=8001, help="Dashboard port")
@click.option("--db", default="err_scout.db", help="SQLite database path")
def dashboard(host, port, db):
    """Start the web dashboard.

    Example: err-scout dashboard --port 8001
    """
    from .dashboard import create_dashboard_app

    app = create_dashboard_app(db_path=db)

    console.print(f"\n[bold red]err-scout[/bold red] dashboard")
    console.print(f"  URL:      http://{host}:{port}")
    console.print(f"  Database: {db}")
    console.print()

    app.run(host=host, port=port, debug=False)


@cli.command()
@click.option("--db", default="err_scout.db", help="SQLite database path")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def report(db, as_json):
    """Show error statistics report.

    Example: err-scout report
    """
    import json
    import sqlite3
    from .server import init_db, DB_PATH
    from pathlib import Path

    db_path = Path(db)
    if not db_path.exists():
        console.print("[yellow]No database found. Run 'err-scout serve' first.[/yellow]")
        return

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    total_groups = conn.execute("SELECT COUNT(*) as c FROM error_groups").fetchone()["c"]
    unresolved = conn.execute(
        "SELECT COUNT(*) as c FROM error_groups WHERE status = 'unresolved'"
    ).fetchone()["c"]
    total_events = conn.execute("SELECT COUNT(*) as c FROM error_events").fetchone()["c"]

    top_errors = conn.execute("""
        SELECT exception, message_template, count, last_seen, status
        FROM error_groups
        ORDER BY count DESC
        LIMIT 15
    """).fetchall()

    conn.close()

    if as_json:
        data = {
            "total_groups": total_groups,
            "unresolved": unresolved,
            "total_events": total_events,
            "top_errors": [dict(r) for r in top_errors],
        }
        click.echo(json.dumps(data, indent=2))
        return

    console.print(f"\n[bold red]err-scout[/bold red] Report")
    console.print(f"  Error groups: {total_groups}")
    console.print(f"  Unresolved:   {unresolved}")
    console.print(f"  Total events: {total_events}")
    console.print()

    if top_errors:
        from rich.table import Table
        table = Table(title="Top Errors")
        table.add_column("Exception")
        table.add_column("Message")
        table.add_column("Count", justify="right")
        table.add_column("Last Seen")
        table.add_column("Status")

        for row in top_errors:
            status_style = {
                "unresolved": "red",
                "resolved": "green",
                "regression": "yellow",
                "ignored": "dim",
            }.get(row["status"], "white")

            table.add_row(
                row["exception"],
                row["message_template"][:50],
                str(row["count"]),
                row["last_seen"][:19],
                f"[{status_style}]{row['status']}[/{status_style}]",
            )

        console.print(table)


@cli.command()
@click.option("--db", default="err_scout.db", help="SQLite database path")
@click.option("--days", default=30, help="Delete events older than N days")
@click.option("--confirm", is_flag=True, help="Skip confirmation")
def flush(db, days, confirm):
    """Delete old error events.

    Example: err-scout flush --days 30
    """
    import sqlite3
    from pathlib import Path

    db_path = Path(db)
    if not db_path.exists():
        console.print("[yellow]No database found.[/yellow]")
        return

    conn = sqlite3.connect(str(db_path))
    count = conn.execute(
        "SELECT COUNT(*) as c FROM error_events WHERE timestamp < datetime('now', ?)",
        (f"-{days} days",),
    ).fetchone()[0]

    if count == 0:
        console.print("[green]No events to flush.[/green]")
        conn.close()
        return

    if not confirm:
        click.confirm(f"Delete {count} events older than {days} days?", abort=True)

    conn.execute(
        "DELETE FROM error_events WHERE timestamp < datetime('now', ?)",
        (f"-{days} days",),
    )
    conn.commit()
    conn.close()

    console.print(f"[green]Deleted {count} events.[/green]")


if __name__ == "__main__":
    cli()
