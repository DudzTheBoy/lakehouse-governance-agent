"""Consolidate the crawl and the LLM run into one prioritised governance report.

Writes Markdown to out/report.md and prints a summary. Reads only the `meta` tables,
so it never touches the audited data and cannot disturb orphan detection.

On the health score: it is a weighted mean of five pass rates, each of which is a
plain fraction you can recompute by hand from the tables below. There is no tuned
constant and no magic. A score is only useful if the reader can argue with it, so
the weights are printed in the report itself.

On the PII comparison: a column the pattern layer flagged has its values withheld
from the model by design. When such a column comes back as "not PII" from the model,
that is the withholding working, not the model failing, and the report separates the
two rather than scoring the model on questions it was never shown.

Run:
    python src/report.py
"""

import argparse
import os
from datetime import datetime, timezone
from pathlib import Path

from dbio import connect, load_env

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = "governance_lab"
META = "meta"
ORPHAN_DAYS = 90

# Weights sum to 1.0. Documentation carries the most because it is the finding the
# other four are usually discovered through.
WEIGHTS = {
    "documentation": 0.30,
    "pii_governance": 0.25,
    "freshness": 0.20,
    "column_quality": 0.15,
    "delta_hygiene": 0.10,
}

# Modelled, not measured: Free Edition reports no meaningful billing for tables this
# small. Override in .env to match your storage tier.
STORAGE_PRICE_PER_GB_MONTH = float(os.getenv("STORAGE_PRICE_PER_GB_MONTH", "0.023"))


def fetch_all(cursor, query: str) -> list[tuple]:
    cursor.execute(query)
    return cursor.fetchall()


def latest_ids(cursor, catalog: str) -> tuple[str, str | None]:
    scan = fetch_all(
        cursor, f"SELECT scan_id FROM {catalog}.{META}.scan_runs ORDER BY started_at DESC LIMIT 1"
    )
    if not scan:
        raise SystemExit("No scan found. Run `python src/crawler.py` first.")
    run = fetch_all(
        cursor, f"SELECT run_id FROM {catalog}.{META}.llm_runs ORDER BY started_at DESC LIMIT 1"
    )
    return scan[0][0], (run[0][0] if run else None)


def gather(cursor, catalog: str, scan_id: str, run_id: str | None) -> dict:
    tables = fetch_all(
        cursor,
        f"""
        SELECT table_schema, table_name, table_type, is_writable, table_owner,
               has_comment, row_count, size_bytes, num_files, fragmented,
               last_read_at, days_since_read, never_read
        FROM {catalog}.{META}.table_inventory WHERE scan_id = '{scan_id}'
        ORDER BY table_schema, table_name
        """,
    )
    columns = fetch_all(
        cursor,
        f"""
        SELECT table_schema, table_name, column_name, data_type, has_comment,
               null_ratio, distinct_count, is_all_null, is_constant,
               is_pii_candidate, pii_by_name, pii_by_value
        FROM {catalog}.{META}.column_profile WHERE scan_id = '{scan_id}'
        ORDER BY table_schema, table_name, ordinal_position
        """,
    )
    suggestions = []
    llm_run = None
    if run_id:
        suggestions = fetch_all(
            cursor,
            f"""
            SELECT table_schema, table_name, column_name, pii_by_regex, pii_by_llm,
                   pii_agreement, type_looks_wrong, values_withheld, suggested_comment
            FROM {catalog}.{META}.llm_suggestions WHERE run_id = '{run_id}'
            """,
        )
        llm_run = fetch_all(
            cursor,
            f"""
            SELECT model, tables_processed, columns_documented, prompt_tokens,
                   completion_tokens, est_cost_usd,
                   unix_timestamp(finished_at) - unix_timestamp(started_at) AS seconds
            FROM {catalog}.{META}.llm_runs WHERE run_id = '{run_id}'
            """,
        )[0]
    return {"tables": tables, "columns": columns, "suggestions": suggestions, "llm_run": llm_run}


def scores(data: dict) -> dict:
    tables, columns = data["tables"], data["columns"]
    n_tables, n_columns = len(tables), len(columns)

    documented = sum(1 for c in columns if c[4]) + sum(1 for t in tables if t[5])
    documentation = documented / (n_columns + n_tables) if (n_columns + n_tables) else 1.0

    pii_columns = [c for c in columns if c[9]]
    pii_documented = sum(1 for c in pii_columns if c[4])
    pii_governance = (pii_documented / len(pii_columns)) if pii_columns else 1.0

    orphans = [t for t in tables if t[12] or (t[11] is not None and t[11] > ORPHAN_DAYS)]
    freshness = 1 - (len(orphans) / n_tables) if n_tables else 1.0

    dead = [c for c in columns if c[7] or c[8]]
    column_quality = 1 - (len(dead) / n_columns) if n_columns else 1.0

    fragmented = [t for t in tables if t[9]]
    delta_hygiene = 1 - (len(fragmented) / n_tables) if n_tables else 1.0

    parts = {
        "documentation": documentation,
        "pii_governance": pii_governance,
        "freshness": freshness,
        "column_quality": column_quality,
        "delta_hygiene": delta_hygiene,
    }
    overall = sum(parts[k] * WEIGHTS[k] for k in WEIGHTS)
    return {"parts": parts, "overall": overall, "orphans": orphans, "dead": dead,
            "fragmented": fragmented, "pii_columns": pii_columns}


def human_bytes(value) -> str:
    if not value:
        return "-"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024


def build_markdown(catalog: str, data: dict, computed: dict, scan_id: str) -> str:
    tables, columns = data["tables"], data["columns"]
    parts, overall = computed["parts"], computed["overall"]
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    out = [
        f"# Governance report: `{catalog}`",
        "",
        f"Generated {generated} from scan `{scan_id[:8]}`.",
        "",
        f"## Health score: {overall * 100:.0f}/100",
        "",
        "| Dimension | Weight | Pass rate |",
        "|---|---|---|",
    ]
    for name, weight in WEIGHTS.items():
        out.append(f"| {name.replace('_', ' ')} | {weight:.2f} | {parts[name] * 100:.0f}% |")

    undocumented_cols = [c for c in columns if not c[4]]
    undocumented_tables = [t for t in tables if not t[5]]

    out += [
        "",
        "## Findings",
        "",
        "| Severity | Finding | Count |",
        "|---|---|---|",
        f"| High | Personal data columns with no documentation or tag | {sum(1 for c in computed['pii_columns'] if not c[4])} |",
        f"| Medium | Tables never read in {ORPHAN_DAYS} days | {len(computed['orphans'])} |",
        f"| Medium | Undocumented tables | {len(undocumented_tables)} |",
        f"| Low | Undocumented columns | {len(undocumented_cols)} |",
        f"| Low | Dead columns (all null or single-valued) | {len(computed['dead'])} |",
        f"| Low | Fragmented tables (never optimised) | {len(computed['fragmented'])} |",
        "",
        "### Personal data",
        "",
        "Both detection layers are listed. A column found by only one of them is still "
        "a finding: the report exists to surface personal data, not to defend a method.",
        "",
        "| Table | Column | By name | By value | By model |",
        "|---|---|---|---|---|",
    ]
    llm_pii = {
        (s[0], s[1], s[2]): s[4]
        for s in data["suggestions"]
        if s[4] and s[4] != "none"
    }
    pattern_keys = {(c[0], c[1], c[2]) for c in computed["pii_columns"]}
    for c in computed["pii_columns"]:
        key = (c[0], c[1], c[2])
        out.append(
            f"| `{c[0]}.{c[1]}` | `{c[2]}` | {c[10] or '-'} | {c[11] or '-'} "
            f"| {llm_pii.get(key, '-')} |"
        )
    for key, label in sorted(llm_pii.items()):
        if key not in pattern_keys:
            out.append(f"| `{key[0]}.{key[1]}` | `{key[2]}` | - | - | {label} |")

    if computed["orphans"]:
        total_bytes = sum(t[7] or 0 for t in computed["orphans"])
        monthly = (total_bytes / 1024**3) * STORAGE_PRICE_PER_GB_MONTH
        out += [
            "",
            "### Orphan tables",
            "",
            "| Table | Rows | Size | Last read |",
            "|---|---|---|---|",
        ]
        for t in computed["orphans"]:
            last = "never" if t[12] else f"{t[11]} days ago"
            out.append(f"| `{t[0]}.{t[1]}` | {t[6]:,} | {human_bytes(t[7])} | {last} |")
        # At demonstration scale the monthly figure rounds to zero, and printing
        # "$0.0000" says nothing. The honest presentation is the rate, which holds at
        # any size, plus what it becomes on a catalog that actually matters.
        per_tb = 1024 * STORAGE_PRICE_PER_GB_MONTH
        out += [
            "",
            f"Combined storage: {human_bytes(total_bytes)} across {len(computed['orphans'])} "
            f"tables -- ${monthly:.6f} a month at ${STORAGE_PRICE_PER_GB_MONTH:.3f}/GB-month. "
            "Negligible here, which is the point of saying it precisely: the cost of an "
            f"orphan table is its size, so the same finding over 1 TB of abandoned data is "
            f"${per_tb:.2f} a month, every month, for data nobody reads. "
            "Modelled at list price, not a billed amount.",
        ]

    if computed["dead"]:
        out += ["", "### Dead columns", "", "| Table | Column | Problem |", "|---|---|---|"]
        for c in computed["dead"]:
            problem = "100% null" if c[7] else f"single value across all rows"
            out.append(f"| `{c[0]}.{c[1]}` | `{c[2]}` | {problem} |")

    if data["suggestions"]:
        agree = {}
        for s in data["suggestions"]:
            agree[s[5]] = agree.get(s[5], 0) + 1
        withheld_only = sum(1 for s in data["suggestions"] if s[5] == "regex_only" and s[7])
        model, n_tables, n_cols, prompt_tokens, completion_tokens, cost, seconds = data["llm_run"]

        out += [
            "",
            "## Pattern matching versus the model",
            "",
            "| Outcome | Columns |",
            "|---|---|",
            f"| Both agreed it is personal data | {agree.get('both', 0)} |",
            f"| Both agreed it is not | {agree.get('neither', 0)} |",
            f"| Model found it, patterns did not | {agree.get('llm_only', 0)} |",
            f"| Patterns found it, model did not | {agree.get('regex_only', 0)} |",
            "",
            f"Of those {agree.get('regex_only', 0)} the model 'missed', {withheld_only} had their "
            "values withheld from it on purpose, because the pattern layer had already flagged "
            "them as sensitive. The model was not shown the evidence, so it does not get "
            "scored on them.",
            "",
            "### Cost of this run",
            "",
            f"- Model: `{model}`",
            f"- {n_tables} tables, {n_cols} comments generated",
            f"- {prompt_tokens:,} input tokens, {completion_tokens:,} output tokens, {seconds:.0f}s",
        ]
        if cost is not None:
            out.append(f"- Modelled cost at list price: **${cost:.4f}**")
            if n_cols:
                out.append(f"- Per generated comment: ${cost / n_cols:.6f}")

        wrong_type = [s for s in data["suggestions"] if s[6]]
        if wrong_type:
            out += ["", "### Columns whose declared type looks wrong", "",
                    "| Table | Column |", "|---|---|"]
            for s in wrong_type:
                out.append(f"| `{s[0]}.{s[1]}` | `{s[2]}` |")

    out += [
        "",
        "## Method notes",
        "",
        f"- Orphan threshold: {ORPHAN_DAYS} days without a read in `system.access.audit`.",
        "- The crawler subtracts its own scan windows from that log, padded for the "
        "asynchronous delay in audit ingestion. Without this the tool marks every table "
        "it profiles as recently used.",
        "- Backing tables for streaming tables and materialized views "
        "(`__materialization_*`, `event_log_*`) are excluded: they mirror a real table "
        "one-for-one and would double-count every finding.",
        "- Sample values for columns flagged as personal data are redacted before storage "
        "and withheld from the model. That guarantee is only as strong as the detector: a "
        "column the patterns miss will have its values sent.",
        "",
    ]
    return "\n".join(out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", default=DEFAULT_CATALOG)
    parser.add_argument("--out", default=str(REPO_ROOT / "out" / "report.md"))
    args = parser.parse_args()

    load_env()
    with connect() as connection:
        with connection.cursor() as cursor:
            scan_id, run_id = latest_ids(cursor, args.catalog)
            data = gather(cursor, args.catalog, scan_id, run_id)

    computed = scores(data)
    markdown = build_markdown(args.catalog, data, computed, scan_id)

    destination = Path(args.out)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(markdown, encoding="utf-8")

    print(f"health score: {computed['overall'] * 100:.0f}/100")
    for name, weight in WEIGHTS.items():
        print(f"  {name:<16} {computed['parts'][name] * 100:5.0f}%  (weight {weight:.2f})")
    print(f"\n  {len(computed['pii_columns'])} personal-data columns, "
          f"{len(computed['orphans'])} orphan tables, "
          f"{len(computed['dead'])} dead columns, "
          f"{len(computed['fragmented'])} fragmented tables")
    print(f"\nwrote {destination}")


if __name__ == "__main__":
    main()
