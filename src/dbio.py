"""Shared SQL Warehouse plumbing: connection, literal rendering, batched inserts."""

import os
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_VARS = ("DATABRICKS_HOST", "DATABRICKS_HTTP_PATH", "DATABRICKS_TOKEN")


def load_env() -> None:
    load_dotenv(REPO_ROOT / ".env")
    missing = [name for name in REQUIRED_VARS if not os.getenv(name)]
    if missing:
        raise SystemExit(
            f"Missing env vars: {', '.join(missing)}.\n"
            "Copy .env.example to .env and fill it in."
        )


def connect():
    # Imported here rather than at module scope so that importing anything from this
    # package does not drag in the SQL driver. The classifiers in crawler.py are pure
    # functions, and their tests should not need a database client installed to run.
    from databricks import sql

    load_env()
    return sql.connect(
        server_hostname=os.environ["DATABRICKS_HOST"].removeprefix("https://").rstrip("/"),
        http_path=os.environ["DATABRICKS_HTTP_PATH"],
        access_token=os.environ["DATABRICKS_TOKEN"],
    )


def sql_literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, datetime):
        return f"TIMESTAMP'{value.strftime('%Y-%m-%d %H:%M:%S')}'"
    if isinstance(value, date):
        return f"DATE'{value.isoformat()}'"
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


def batched_insert(cursor, table: str, columns: list[str], rows: list[tuple], batch_size: int = 200) -> None:
    if not rows:
        return
    col_list = ", ".join(f"`{c}`" for c in columns)
    for start in range(0, len(rows), batch_size):
        chunk = rows[start : start + batch_size]
        values_sql = ", ".join(
            "(" + ", ".join(sql_literal(v) for v in row) + ")" for row in chunk
        )
        cursor.execute(f"INSERT INTO {table} ({col_list}) VALUES {values_sql}")
