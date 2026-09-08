"""Crawl a catalog, profile every column, and persist the inventory to Delta.

Two design decisions worth reading before the code:

1. THE AUDITOR CORRUPTS ITS OWN EVIDENCE.
   Detecting orphan tables means asking "when was this table last read?" via
   `system.access.audit`. But profiling a table *is* a read, and it lands in that
   same audit log. Run this crawler twice and every orphan looks freshly accessed.
   Fix: each run records its own start/end window in `meta.scan_runs`, and the
   last-read query excludes any audit event that falls inside a recorded window.
   The agent is invisible to itself. Without this, finding #3 is worthless.

2. PII VALUES NEVER LEAVE THE COLUMN.
   Sample values are what make LLM-generated descriptions good, but a column that
   looks like PII should not have its values stored in an inventory table or later
   shipped to an LLM API. Pattern detection runs here, at collection time, and
   samples for flagged columns are redacted before they are ever written down.
   The LLM stage gets name, type and statistics for those columns -- never values.

Caveat discovered on Free Edition: `system.access.audit` lags by roughly 15 minutes,
so a scan run immediately after activity will not see it. Orphan findings are
"as of the audit log", not "as of now", and the report says so.

Run:
    python src/crawler.py [--catalog governance_lab]
"""

import argparse
import re
import uuid
from datetime import datetime, timezone

from dbio import batched_insert, connect

DEFAULT_CATALOG = "governance_lab"
META_SCHEMA = "meta"

# Backing tables the engine materializes for streaming tables / materialized views,
# plus DLT event logs. They mirror a real table one-for-one, so auditing them would
# double-count every finding. Not user-facing assets; excluded by design.
INTERNAL_TABLE_PATTERNS = (
    re.compile(r"^__materialization_mat_", re.IGNORECASE),
    re.compile(r"^event_log_[0-9a-f]{8}", re.IGNORECASE),
)

PII_NAME_PATTERNS = {
    "national_id": re.compile(r"\b(cpf|cnpj|ssn|nif|national_id)\b", re.IGNORECASE),
    "person_name": re.compile(r"(^|_)(nome|name|sobrenome|surname)($|_)", re.IGNORECASE),
    "email": re.compile(r"(email|e_mail|mail)", re.IGNORECASE),
    "phone": re.compile(r"(telefone|phone|celular|mobile|msisdn)", re.IGNORECASE),
    "birth_date": re.compile(r"(nascimento|birth|dob)", re.IGNORECASE),
    "address": re.compile(r"(endereco|address|logradouro|cep|zipcode|postal)", re.IGNORECASE),
    "network_id": re.compile(r"(ip_address|user_agent|device_id|mac_address)", re.IGNORECASE),
}

EMAIL_VALUE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.IGNORECASE)
CPF_VALUE = re.compile(r"^\d{3}\.?\d{3}\.?\d{3}-?\d{2}$")
PHONE_VALUE = re.compile(r"^\+?[\d\s().-]{10,20}$")

# Value sniffing only ever runs on text columns. A DATE column is not a phone
# number and a DOUBLE is not a national ID, no matter what the digits look like.
# The first version of this skipped the check and flagged every date and currency
# column in the catalog as PII -- 12 false positives out of 17 hits.
TEXTUAL_TYPES = ("string", "varchar", "char")

SAMPLE_ROWS = 5
SMALL_FILE_BYTES = 16 * 1024 * 1024


def cpf_has_valid_check_digits(value: str) -> bool:
    """A regex proves shape; the check digits prove it is a well-formed CPF.

    This is the pattern side of the pattern-vs-LLM comparison: it can *verify*,
    where a language model can only recognise the shape.
    """
    digits = [int(c) for c in re.sub(r"\D", "", value or "")]
    if len(digits) != 11 or len(set(digits)) == 1:
        return False
    for cut in (9, 10):
        total = sum(digits[i] * (cut + 1 - i) for i in range(cut))
        check = (total * 10) % 11
        if (0 if check == 10 else check) != digits[cut]:
            return False
    return True


def classify_by_name(column_name: str) -> str | None:
    for label, pattern in PII_NAME_PATTERNS.items():
        if pattern.search(column_name):
            return label
    return None


def classify_by_value(samples: list, data_type: str) -> str | None:
    """Sniff PII from sample values -- text columns only, most specific test first."""
    if not any(data_type.lower().startswith(t) for t in TEXTUAL_TYPES):
        return None

    values = [str(v) for v in samples if v is not None]
    if not values:
        return None

    if all(EMAIL_VALUE.match(v) for v in values):
        return "email"

    # An 11-digit CPF and an 11-digit mobile number are the same shape. Only the
    # check digits separate them, so a shape match alone never claims national_id.
    if all(CPF_VALUE.match(v) for v in values):
        if all(map(cpf_has_valid_check_digits, values)):
            return "national_id_verified"

    if all(PHONE_VALUE.match(v) and 10 <= len(re.sub(r"\D", "", v)) <= 15 for v in values):
        return "phone"

    if all(CPF_VALUE.match(v) for v in values):
        return "national_id_shape"

    return None


def redact(value) -> str | None:
    if value is None:
        return None
    text = str(value)
    return f"{text[:2]}***" if len(text) > 2 else "***"


def ensure_meta_tables(cursor, catalog: str) -> None:
    cursor.execute(f"CREATE SCHEMA IF NOT EXISTS {catalog}.{META_SCHEMA}")
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {catalog}.{META_SCHEMA}.scan_runs (
            scan_id STRING, started_at TIMESTAMP, finished_at TIMESTAMP, catalog STRING
        ) USING DELTA
        """
    )
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {catalog}.{META_SCHEMA}.table_inventory (
            scan_id STRING, scanned_at TIMESTAMP,
            table_catalog STRING, table_schema STRING, table_name STRING,
            table_type STRING, is_writable BOOLEAN,
            table_owner STRING, created_at TIMESTAMP, last_altered TIMESTAMP,
            table_comment STRING, has_comment BOOLEAN,
            row_count BIGINT, size_bytes BIGINT, num_files BIGINT, fragmented BOOLEAN,
            last_read_at TIMESTAMP, days_since_read INT, never_read BOOLEAN
        ) USING DELTA
        """
    )
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {catalog}.{META_SCHEMA}.column_profile (
            scan_id STRING, scanned_at TIMESTAMP,
            table_catalog STRING, table_schema STRING, table_name STRING,
            column_name STRING, ordinal_position INT, data_type STRING,
            column_comment STRING, has_comment BOOLEAN,
            row_count BIGINT, null_count BIGINT, null_ratio DOUBLE,
            distinct_count BIGINT, is_all_null BOOLEAN, is_constant BOOLEAN,
            pii_by_name STRING, pii_by_value STRING, is_pii_candidate BOOLEAN,
            sample_values STRING, sample_redacted BOOLEAN
        ) USING DELTA
        """
    )


def previous_scan_windows(cursor, catalog: str) -> list[tuple[datetime, datetime]]:
    cursor.execute(
        f"""
        SELECT started_at, finished_at
        FROM {catalog}.{META_SCHEMA}.scan_runs
        WHERE finished_at IS NOT NULL
        """
    )
    return [(row[0], row[1]) for row in cursor.fetchall()]


def last_read_per_table(cursor, catalog: str, windows: list[tuple[datetime, datetime]]) -> dict:
    """Last genuine read per table, with this tool's own scans subtracted."""
    exclusions = " ".join(
        f"AND NOT (event_time BETWEEN TIMESTAMP'{start:%Y-%m-%d %H:%M:%S}' "
        f"AND TIMESTAMP'{end:%Y-%m-%d %H:%M:%S}')"
        for start, end in windows
    )
    cursor.execute(
        f"""
        SELECT lower(request_params.full_name_arg) AS full_name, max(event_time) AS last_read
        FROM system.access.audit
        WHERE action_name IN ('getTable', 'generateTemporaryTableCredential')
          AND event_date >= current_date() - INTERVAL 365 DAYS
          AND request_params.full_name_arg IS NOT NULL
          AND lower(request_params.full_name_arg) LIKE '{catalog.lower()}.%'
          {exclusions}
        GROUP BY 1
        """
    )
    return {row[0]: row[1] for row in cursor.fetchall()}


def list_tables(cursor, catalog: str) -> list[dict]:
    cursor.execute(
        f"""
        SELECT table_schema, table_name, table_type, table_owner,
               created, last_altered, comment
        FROM system.information_schema.tables
        WHERE table_catalog = '{catalog}'
          AND table_schema NOT IN ('information_schema', '{META_SCHEMA}')
        ORDER BY table_schema, table_name
        """
    )
    tables = []
    for schema, name, ttype, owner, created, altered, comment in cursor.fetchall():
        if any(p.search(name) for p in INTERNAL_TABLE_PATTERNS):
            continue
        tables.append(
            {
                "schema": schema,
                "name": name,
                "type": ttype,
                "owner": owner,
                "created": created,
                "last_altered": altered,
                "comment": comment,
                # Only plain managed/external Delta tables accept COMMENT ON writes;
                # streaming tables and materialized views are engine-managed.
                "writable": ttype in ("MANAGED", "EXTERNAL"),
            }
        )
    return tables


def list_columns(cursor, catalog: str) -> dict:
    cursor.execute(
        f"""
        SELECT table_schema, table_name, column_name, ordinal_position,
               full_data_type, comment
        FROM system.information_schema.columns
        WHERE table_catalog = '{catalog}'
          AND table_schema NOT IN ('information_schema', '{META_SCHEMA}')
        ORDER BY table_schema, table_name, ordinal_position
        """
    )
    columns: dict = {}
    for schema, table, column, position, dtype, comment in cursor.fetchall():
        columns.setdefault((schema, table), []).append(
            {
                "name": column,
                "position": position,
                "data_type": dtype,
                "comment": comment,
            }
        )
    return columns


def describe_detail(cursor, fqn: str) -> dict:
    """size/file counts for Delta hygiene. Non-Delta relations simply return nothing."""
    try:
        cursor.execute(f"DESCRIBE DETAIL {fqn}")
        row = cursor.fetchone().asDict()
        return {"size_bytes": row.get("sizeInBytes"), "num_files": row.get("numFiles")}
    except Exception:
        return {"size_bytes": None, "num_files": None}


def profile_table(cursor, fqn: str, columns: list[dict]) -> dict:
    """One aggregate query for every column, plus one sample query. Two reads per table."""
    aggregates = ["count(*)"]
    for column in columns:
        quoted = f"`{column['name']}`"
        aggregates.append(f"count({quoted})")
        aggregates.append(f"approx_count_distinct({quoted})")
    cursor.execute(f"SELECT {', '.join(aggregates)} FROM {fqn}")
    stats = list(cursor.fetchone())

    cursor.execute(f"SELECT * FROM {fqn} LIMIT {SAMPLE_ROWS}")
    sample_rows = [list(row) for row in cursor.fetchall()]

    row_count = stats[0]
    profile = {"row_count": row_count, "columns": {}}
    for index, column in enumerate(columns):
        non_null = stats[1 + index * 2]
        distinct = stats[2 + index * 2]
        samples = [row[index] for row in sample_rows] if sample_rows else []
        profile["columns"][column["name"]] = {
            "non_null": non_null,
            "null_count": row_count - non_null,
            "distinct": distinct,
            "samples": samples,
        }
    return profile


def crawl(catalog: str) -> None:
    scan_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc).replace(tzinfo=None)
    print(f"scan {scan_id} on `{catalog}`")

    with connect() as connection:
        with connection.cursor() as cursor:
            ensure_meta_tables(cursor, catalog)

            # Step 1, before touching any data: read the audit log with this tool's
            # own past scan windows subtracted, so we measure other people's reads.
            windows = previous_scan_windows(cursor, catalog)
            reads = last_read_per_table(cursor, catalog, windows)
            print(f"  audit snapshot taken ({len(windows)} prior scan windows excluded)")

            tables = list_tables(cursor, catalog)
            columns_by_table = list_columns(cursor, catalog)
            print(f"  {len(tables)} tables in scope (internal backing tables filtered out)")

            table_rows, column_rows = [], []
            scanned_at = datetime.now(timezone.utc).replace(tzinfo=None)

            for table in tables:
                fqn = f"`{catalog}`.`{table['schema']}`.`{table['name']}`"
                full_name = f"{catalog}.{table['schema']}.{table['name']}".lower()
                columns = columns_by_table.get((table["schema"], table["name"]), [])

                detail = describe_detail(cursor, fqn)
                profile = profile_table(cursor, fqn, columns)

                last_read = reads.get(full_name)
                days_since = (scanned_at - last_read.replace(tzinfo=None)).days if last_read else None
                num_files = detail["num_files"] or 0
                size_bytes = detail["size_bytes"] or 0
                fragmented = num_files > 1 and (size_bytes / num_files) < SMALL_FILE_BYTES

                table_rows.append(
                    (
                        scan_id, scanned_at, catalog, table["schema"], table["name"],
                        table["type"], table["writable"], table["owner"],
                        table["created"], table["last_altered"],
                        table["comment"], bool(table["comment"]),
                        profile["row_count"], detail["size_bytes"], detail["num_files"],
                        fragmented,
                        last_read.replace(tzinfo=None) if last_read else None,
                        days_since, last_read is None,
                    )
                )

                for column in columns:
                    stats = profile["columns"][column["name"]]
                    row_count = profile["row_count"]
                    samples = stats["samples"]

                    by_name = classify_by_name(column["name"])
                    by_value = classify_by_value(samples, column["data_type"])
                    is_pii = bool(by_name or by_value)

                    shown = [redact(v) for v in samples] if is_pii else samples
                    sample_text = ", ".join("NULL" if v is None else str(v) for v in shown[:SAMPLE_ROWS])

                    column_rows.append(
                        (
                            scan_id, scanned_at, catalog, table["schema"], table["name"],
                            column["name"], column["position"], column["data_type"],
                            column["comment"], bool(column["comment"]),
                            row_count, stats["null_count"],
                            (stats["null_count"] / row_count) if row_count else None,
                            stats["distinct"],
                            row_count > 0 and stats["non_null"] == 0,
                            row_count > 0 and stats["distinct"] == 1,
                            by_name, by_value, is_pii,
                            sample_text[:500], is_pii,
                        )
                    )

                print(
                    f"    {table['schema']}.{table['name']}: "
                    f"{profile['row_count']} rows, {len(columns)} cols, "
                    f"last_read={last_read or 'never'}"
                )

            batched_insert(
                cursor, f"{catalog}.{META_SCHEMA}.table_inventory",
                [
                    "scan_id", "scanned_at", "table_catalog", "table_schema", "table_name",
                    "table_type", "is_writable", "table_owner", "created_at", "last_altered",
                    "table_comment", "has_comment", "row_count", "size_bytes", "num_files",
                    "fragmented", "last_read_at", "days_since_read", "never_read",
                ],
                table_rows,
            )
            batched_insert(
                cursor, f"{catalog}.{META_SCHEMA}.column_profile",
                [
                    "scan_id", "scanned_at", "table_catalog", "table_schema", "table_name",
                    "column_name", "ordinal_position", "data_type", "column_comment",
                    "has_comment", "row_count", "null_count", "null_ratio", "distinct_count",
                    "is_all_null", "is_constant", "pii_by_name", "pii_by_value",
                    "is_pii_candidate", "sample_values", "sample_redacted",
                ],
                column_rows,
            )

            finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
            batched_insert(
                cursor, f"{catalog}.{META_SCHEMA}.scan_runs",
                ["scan_id", "started_at", "finished_at", "catalog"],
                [(scan_id, started_at, finished_at, catalog)],
            )

    print(f"\n  wrote {len(table_rows)} tables and {len(column_rows)} columns")
    print(f"  scan window {started_at:%H:%M:%S}-{finished_at:%H:%M:%S} recorded; "
          "future scans will ignore these reads")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", default=DEFAULT_CATALOG)
    crawl(parser.parse_args().catalog)


if __name__ == "__main__":
    main()
