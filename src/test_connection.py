"""Validate the SQL Warehouse connection and the system tables the agent depends on.

Run this before writing the crawler. Every probe below maps to something the
agent will actually query, so a green run means the foundation is real.

    python src/test_connection.py
"""

import os
import sys
from pathlib import Path

from databricks import sql
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_VARS = ("DATABRICKS_HOST", "DATABRICKS_HTTP_PATH", "DATABRICKS_TOKEN")


def load_config() -> dict:
    load_dotenv(REPO_ROOT / ".env")

    missing = [name for name in REQUIRED_VARS if not os.getenv(name)]
    if missing:
        sys.exit(
            f"Missing env vars: {', '.join(missing)}.\n"
            f"Copy .env.example to .env and fill it in."
        )

    host = os.environ["DATABRICKS_HOST"].removeprefix("https://").rstrip("/")
    return {
        "host": host,
        "http_path": os.environ["DATABRICKS_HTTP_PATH"],
        "token": os.environ["DATABRICKS_TOKEN"],
        "catalog": os.getenv("TARGET_CATALOG", "emissions"),
        "schema": os.getenv("TARGET_SCHEMA", "default"),
    }


def probes(catalog: str, schema: str) -> list[tuple[str, str]]:
    """(label, query) pairs. Each one backs a specific finding in the audit."""
    return [
        (
            "warehouse reachable",
            "SELECT current_user() AS user, current_version().dbsql_version AS version",
        ),
        (
            "information_schema.tables (inventory)",
            f"""
            SELECT count(*) AS n
            FROM system.information_schema.tables
            WHERE table_catalog = '{catalog}' AND table_schema = '{schema}'
            """,
        ),
        (
            "information_schema.columns (missing comments)",
            f"""
            SELECT count(*) AS n
            FROM system.information_schema.columns
            WHERE table_catalog = '{catalog}'
              AND table_schema = '{schema}'
              AND (comment IS NULL OR comment = '')
            """,
        ),
        (
            "system.access.audit (orphan tables)",
            """
            SELECT count(*) AS n
            FROM system.access.audit
            WHERE event_date >= current_date() - INTERVAL 7 DAYS
            """,
        ),
        (
            "system.billing.usage (cost of orphans)",
            """
            SELECT count(*) AS n
            FROM system.billing.usage
            WHERE usage_date >= current_date() - INTERVAL 7 DAYS
            """,
        ),
    ]


def main() -> int:
    config = load_config()
    print(f"host    : {config['host']}")
    print(f"path    : {config['http_path']}")
    print(f"target  : {config['catalog']}.{config['schema']}\n")

    failures = 0
    with sql.connect(
        server_hostname=config["host"],
        http_path=config["http_path"],
        access_token=config["token"],
    ) as connection:
        for label, query in probes(config["catalog"], config["schema"]):
            try:
                with connection.cursor() as cursor:
                    cursor.execute(query)
                    row = cursor.fetchone()
                print(f"  OK   {label}: {tuple(row) if row else '(no rows)'}")
            except Exception as exc:  # noqa: BLE001 - report every probe, don't stop
                failures += 1
                print(f"  FAIL {label}: {type(exc).__name__}: {exc}")

    print()
    if failures:
        print(f"{failures} probe(s) failed. Fix these before writing the crawler.")
        return 1

    print("All probes passed. Foundation is ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
