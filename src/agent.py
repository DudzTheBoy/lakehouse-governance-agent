"""Generate table and column documentation with an LLM, and classify PII.

Reads the newest crawl from `meta.column_profile`, asks the model to describe every
undocumented column, and records what it would write. Nothing touches the catalog
unless you pass --apply: the default is a dry run that prints the exact SQL.

Three decisions worth knowing about:

1. PII VALUES ARE NEVER SENT TO THE MODEL.
   The crawler already flags PII candidates by name and by value pattern. For those
   columns the prompt carries name, type and statistics only -- no sample values.
   The model therefore has *less* evidence on exactly the columns the pattern layer
   already caught, which is the trade we want: the pattern layer is cheap and local,
   the model is remote. Anything already known to be sensitive stays home.

2. THE MODEL CLASSIFIES PII TOO, SO THE TWO LAYERS CAN BE COMPARED.
   Every suggestion records both the regex label and the model label. Patterns can
   *verify* (a CPF check digit either passes or it does not) but cannot recognise a
   person's name; the model is the reverse. The comparison is the point.

3. ONE CALL PER TABLE, NOT PER COLUMN.
   The model sees a whole table at once, so it can use sibling columns as context --
   and 7 calls cost a fraction of 47.

Run:
    python src/agent.py               # dry run, prints the SQL it would execute
    python src/agent.py --apply       # actually writes COMMENT ON / ALTER COLUMN
"""

import argparse
import json
import os
import time
import uuid
from datetime import datetime, timezone

from groq import Groq

from dbio import batched_insert, connect, load_env, sql_literal
from docs_index import DocsIndex

DEFAULT_CATALOG = "governance_lab"
META_SCHEMA = "meta"
MAX_SAMPLE_CHARS = 200

SYSTEM_PROMPT = """You document data catalogs. You will be given one table and its
columns, with profiling statistics and, for non-sensitive columns, a few sample values.

For each column write a one-sentence description in English of what the column holds.
Describe the data, not the statistics. Never invent business meaning you cannot support
from the name, type and samples; if a column is genuinely ambiguous, say what it appears
to hold and keep it short.

Also classify each column for personal data. Use one of:
  none, person_name, national_id, email, phone, birth_date, address, network_id, other_pii

Also flag columns whose declared type looks wrong for the data (for example a monetary
amount stored as a string).

Some columns are marked SENSITIVE: their values were withheld on purpose. Classify them
from the name and type alone.

You may be given SOURCE DOCUMENTATION for the system the table came from. When it
covers a column, follow it over your own reading of the name and values -- it is the
system of record for what the column means. Say so plainly rather than hedging. When
it does not cover a column, fall back to the name, type and samples as usual, and do
not stretch the documentation to fit.

Return JSON only:
{"table_description": "...",
 "columns": [{"name": "...", "description": "...", "pii": "...", "type_looks_wrong": true|false,
              "grounded_in_docs": true|false}]}"""


def price_per_mtok(name: str) -> float | None:
    raw = os.getenv(name)
    try:
        return float(raw) if raw not in (None, "") else None
    except ValueError:
        return None


def ensure_meta_tables(cursor, catalog: str) -> None:
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {catalog}.{META_SCHEMA}.llm_suggestions (
            run_id STRING, scan_id STRING, generated_at TIMESTAMP, model STRING,
            table_schema STRING, table_name STRING, column_name STRING,
            suggested_comment STRING,
            pii_by_regex STRING, pii_by_llm STRING, pii_agreement STRING,
            type_looks_wrong BOOLEAN, values_withheld BOOLEAN,
            applied BOOLEAN,
            doc_system STRING, doc_citations STRING, doc_matched_terms STRING,
            grounded_in_docs BOOLEAN
        ) USING DELTA
        """
    )
    # Tables created before grounding existed need the new columns added in place.
    for column, dtype in (
        ("doc_system", "STRING"), ("doc_citations", "STRING"),
        ("doc_matched_terms", "STRING"), ("grounded_in_docs", "BOOLEAN"),
    ):
        try:
            cursor.execute(
                f"ALTER TABLE {catalog}.{META_SCHEMA}.llm_suggestions ADD COLUMN {column} {dtype}"
            )
        except Exception:
            pass  # already present
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {catalog}.{META_SCHEMA}.llm_runs (
            run_id STRING, scan_id STRING, started_at TIMESTAMP, finished_at TIMESTAMP,
            model STRING, tables_processed INT, columns_documented INT,
            prompt_tokens BIGINT, completion_tokens BIGINT,
            est_cost_usd DOUBLE, applied BOOLEAN
        ) USING DELTA
        """
    )


def latest_scan_id(cursor, catalog: str) -> str:
    cursor.execute(
        f"SELECT scan_id FROM {catalog}.{META_SCHEMA}.scan_runs ORDER BY started_at DESC LIMIT 1"
    )
    row = cursor.fetchone()
    if not row:
        raise SystemExit("No scan found. Run `python src/crawler.py` first.")
    return row[0]


def load_scan(cursor, catalog: str, scan_id: str) -> dict:
    cursor.execute(
        f"""
        SELECT c.table_schema, c.table_name, c.column_name, c.data_type,
               c.row_count, c.null_count, c.distinct_count,
               c.is_pii_candidate, c.pii_by_name, c.pii_by_value,
               c.sample_values, c.has_comment,
               t.table_type, t.is_writable, t.has_comment AS table_has_comment
        FROM {catalog}.{META_SCHEMA}.column_profile c
        JOIN {catalog}.{META_SCHEMA}.table_inventory t
          ON t.scan_id = c.scan_id
         AND t.table_schema = c.table_schema
         AND t.table_name = c.table_name
        WHERE c.scan_id = '{scan_id}'
        ORDER BY c.table_schema, c.table_name, c.ordinal_position
        """
    )
    tables: dict = {}
    for row in cursor.fetchall():
        key = (row[0], row[1])
        entry = tables.setdefault(
            key,
            {"schema": row[0], "name": row[1], "table_type": row[12],
             "writable": row[13], "table_has_comment": row[14], "columns": []},
        )
        entry["columns"].append(
            {
                "name": row[2], "data_type": row[3], "row_count": row[4],
                "null_count": row[5], "distinct_count": row[6],
                "is_pii_candidate": row[7],
                "regex_label": row[8] or row[9],
                "samples": row[10], "has_comment": row[11],
            }
        )
    return tables


def build_prompt(table: dict, grounding: dict | None) -> str:
    lines = []
    if grounding:
        lines += [
            f"SOURCE DOCUMENTATION ({grounding['system']}):",
            grounding["context"],
            "",
            "END OF DOCUMENTATION.",
            "",
        ]
    lines += [f"Table: {table['schema']}.{table['name']} ({table['table_type']})", "Columns:"]
    for column in table["columns"]:
        stats = (
            f"{column['row_count']} rows, {column['null_count']} nulls, "
            f"{column['distinct_count']} distinct"
        )
        if column["is_pii_candidate"]:
            # Deliberate: sample values for flagged columns never leave the warehouse.
            lines.append(
                f"- {column['name']} ({column['data_type']}), {stats} [SENSITIVE: values withheld]"
            )
        else:
            samples = (column["samples"] or "")[:MAX_SAMPLE_CHARS]
            lines.append(f"- {column['name']} ({column['data_type']}), {stats}, samples: {samples}")
    return "\n".join(lines)


def describe_table(client: Groq, model: str, table: dict, grounding: dict | None) -> tuple[dict, int, int]:
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_prompt(table, grounding)},
        ],
        response_format={"type": "json_object"},
        max_completion_tokens=8192,
        reasoning_effort="low",
        temperature=0.2,
    )
    usage = response.usage
    parsed = json.loads(response.choices[0].message.content)
    return parsed, usage.prompt_tokens, usage.completion_tokens


def agreement(regex_label: str | None, llm_label: str | None) -> str:
    regex_hit = bool(regex_label)
    llm_hit = bool(llm_label and llm_label != "none")
    if regex_hit and llm_hit:
        return "both"
    if regex_hit:
        return "regex_only"
    if llm_hit:
        return "llm_only"
    return "neither"


def comment_statements(catalog: str, table: dict, described: dict) -> list[str]:
    fqn = f"`{catalog}`.`{table['schema']}`.`{table['name']}`"
    statements = []
    table_description = (described.get("table_description") or "").strip()
    if table_description and not table["table_has_comment"]:
        statements.append(f"COMMENT ON TABLE {fqn} IS {sql_literal(table_description)}")
    existing = {c["name"]: c for c in table["columns"]}
    for suggestion in described.get("columns", []):
        name = suggestion.get("name")
        text = (suggestion.get("description") or "").strip()
        column = existing.get(name)
        if not column or not text or column["has_comment"]:
            continue
        statements.append(
            f"ALTER TABLE {fqn} ALTER COLUMN `{name}` COMMENT {sql_literal(text)}"
        )
    return statements


def run(catalog: str, apply: bool, use_docs: bool) -> None:
    load_env()
    model = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
    client = Groq(api_key=os.environ["GROQ_API_KEY"])
    docs = DocsIndex() if use_docs else None

    run_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc).replace(tzinfo=None)
    prompt_tokens = completion_tokens = 0
    suggestion_rows: list[tuple] = []
    documented = grounded_tables = 0

    print(f"run {run_id}  model={model}  mode={'APPLY' if apply else 'DRY RUN'}")
    if docs:
        print(f"  grounding: {docs.total_chunks} doc chunks across {len(docs.chunks)} system(s)")
    else:
        print("  grounding: off")

    with connect() as connection:
        with connection.cursor() as cursor:
            ensure_meta_tables(cursor, catalog)
            scan_id = latest_scan_id(cursor, catalog)
            tables = load_scan(cursor, catalog, scan_id)
            print(f"  scan {scan_id}: {len(tables)} tables\n")

            for table in tables.values():
                grounding = None
                if docs:
                    grounding = docs.retrieve(
                        table["schema"], table["name"],
                        [c["name"] for c in table["columns"]],
                    )
                    if grounding:
                        grounded_tables += 1

                described, used_prompt, used_completion = describe_table(
                    client, model, table, grounding
                )
                prompt_tokens += used_prompt
                completion_tokens += used_completion

                by_name = {c.get("name"): c for c in described.get("columns", [])}
                statements = comment_statements(catalog, table, described)

                writable = table["writable"]
                will_apply = apply and writable
                if statements:
                    label = "would write" if not apply else ("writing" if writable else "SKIPPED (not writable)")
                    print(f"  {table['schema']}.{table['name']} -- {label} {len(statements)} comment(s)")
                    for statement in statements:
                        print(f"      {statement[:150]}{'...' if len(statement) > 150 else ''}")
                        if will_apply:
                            cursor.execute(statement)
                    documented += len(statements)

                for column in table["columns"]:
                    suggestion = by_name.get(column["name"], {})
                    llm_label = suggestion.get("pii")
                    suggestion_rows.append(
                        (
                            run_id, scan_id, started_at, model,
                            table["schema"], table["name"], column["name"],
                            (suggestion.get("description") or "")[:1000],
                            column["regex_label"], llm_label,
                            agreement(column["regex_label"], llm_label),
                            bool(suggestion.get("type_looks_wrong")),
                            bool(column["is_pii_candidate"]),
                            bool(will_apply and not column["has_comment"]),
                            grounding["system"] if grounding else None,
                            "; ".join(grounding["citations"]) if grounding else None,
                            ", ".join(grounding["matched_terms"]) if grounding else None,
                            bool(suggestion.get("grounded_in_docs")) if grounding else False,
                        )
                    )

            batched_insert(
                cursor, f"{catalog}.{META_SCHEMA}.llm_suggestions",
                [
                    "run_id", "scan_id", "generated_at", "model",
                    "table_schema", "table_name", "column_name", "suggested_comment",
                    "pii_by_regex", "pii_by_llm", "pii_agreement",
                    "type_looks_wrong", "values_withheld", "applied",
                    "doc_system", "doc_citations", "doc_matched_terms", "grounded_in_docs",
                ],
                suggestion_rows,
            )

            input_price = price_per_mtok("GROQ_PRICE_INPUT_PER_MTOK")
            output_price = price_per_mtok("GROQ_PRICE_OUTPUT_PER_MTOK")
            cost = None
            if input_price is not None and output_price is not None:
                cost = (prompt_tokens / 1e6) * input_price + (completion_tokens / 1e6) * output_price

            finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
            batched_insert(
                cursor, f"{catalog}.{META_SCHEMA}.llm_runs",
                [
                    "run_id", "scan_id", "started_at", "finished_at", "model",
                    "tables_processed", "columns_documented",
                    "prompt_tokens", "completion_tokens", "est_cost_usd", "applied",
                ],
                [(run_id, scan_id, started_at, finished_at, model, len(tables),
                  documented, prompt_tokens, completion_tokens, cost, apply)],
            )

    elapsed = (finished_at - started_at).total_seconds()
    print(f"\n  {len(tables)} tables, {documented} comments {'written' if apply else 'staged'}")
    if docs:
        print(f"  {grounded_tables} of {len(tables)} tables had source documentation")
    print(f"  tokens: {prompt_tokens} in / {completion_tokens} out  in {elapsed:.1f}s")
    if cost is not None:
        print(f"  estimated cost: ${cost:.4f}")
    else:
        print("  cost: set GROQ_PRICE_INPUT_PER_MTOK and GROQ_PRICE_OUTPUT_PER_MTOK in .env")
    if not apply:
        print("\n  Nothing was written. Re-run with --apply to execute the statements above.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", default=DEFAULT_CATALOG)
    parser.add_argument("--apply", action="store_true", help="execute the statements instead of printing them")
    parser.add_argument("--no-docs", action="store_true",
                        help="skip source documentation, to measure what grounding is worth")
    args = parser.parse_args()
    run(args.catalog, args.apply, use_docs=not args.no_docs)


if __name__ == "__main__":
    main()
