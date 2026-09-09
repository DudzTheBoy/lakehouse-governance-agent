"""Run the agent twice -- without and with source documentation -- and diff the output.

This is the experiment the project exists to run. Databricks generates table comments
natively from the data itself, and so does this agent when it has nothing else. The
question is what changes when the agent is handed the documentation of the system the
data came from.

Genesys Cloud is the honest test case: `tAcw`, `nOffered` and `oServiceLevel` are not
guessable from values. The columns hold bare integers and a ratio. A reader with no
documentation can describe the shape of the data and nothing about its meaning.

Writes a side-by-side comparison to out/grounding_comparison.md.

Run:
    python src/compare_grounding.py [--schema raw_genesys]
"""

import argparse
from pathlib import Path

from agent import run as run_agent
from dbio import connect

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = "governance_lab"
META = "meta"


def fetch_run(cursor, catalog: str, offset: int) -> str:
    cursor.execute(
        f"""
        SELECT run_id FROM {catalog}.{META}.llm_runs
        ORDER BY started_at DESC LIMIT 1 OFFSET {offset}
        """
    )
    return cursor.fetchone()[0]


def descriptions(cursor, catalog: str, run_id: str, schema: str | None) -> dict:
    schema_filter = f"AND table_schema = '{schema}'" if schema else ""
    cursor.execute(
        f"""
        SELECT table_schema, table_name, column_name, suggested_comment,
               doc_system, doc_citations, grounded_in_docs
        FROM {catalog}.{META}.llm_suggestions
        WHERE run_id = '{run_id}' {schema_filter}
        ORDER BY table_schema, table_name, column_name
        """
    )
    return {(r[0], r[1], r[2]): r for r in cursor.fetchall()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", default=DEFAULT_CATALOG)
    parser.add_argument("--schema", default="raw_genesys",
                        help="schema to compare; omit to compare the whole catalog")
    parser.add_argument("--out", default=str(REPO_ROOT / "out" / "grounding_comparison.md"))
    args = parser.parse_args()

    print("=== pass 1: no source documentation ===")
    run_agent(args.catalog, apply=False, use_docs=False)
    print("\n=== pass 2: with source documentation ===")
    run_agent(args.catalog, apply=False, use_docs=True)

    with connect() as connection:
        with connection.cursor() as cursor:
            grounded_run = fetch_run(cursor, args.catalog, 0)
            plain_run = fetch_run(cursor, args.catalog, 1)
            grounded = descriptions(cursor, args.catalog, grounded_run, args.schema)
            plain = descriptions(cursor, args.catalog, plain_run, args.schema)

    changed = [key for key in grounded if key in plain
               and (grounded[key][3] or "").strip() != (plain[key][3] or "").strip()]

    lines = [
        "# What source documentation is worth",
        "",
        "The same agent, the same model, the same catalog, run twice. The only "
        "difference is whether it was given the documentation of the system the data "
        "came from.",
        "",
        f"- Columns compared: {len(grounded)}",
        f"- Descriptions that changed: {len(changed)}",
        "",
    ]

    by_table: dict = {}
    for key in sorted(grounded):
        by_table.setdefault((key[0], key[1]), []).append(key)

    for (schema, table), keys in by_table.items():
        citations = next((grounded[k][5] for k in keys if grounded[k][5]), None)
        lines += [f"## `{schema}.{table}`", ""]
        if citations:
            lines += [f"Grounded in: {citations}", ""]
        lines += [
            "| Column | Without documentation | With documentation |",
            "|---|---|---|",
        ]
        for key in keys:
            without = (plain.get(key, (None,) * 4)[3] or "-").replace("|", "\\|")
            with_docs = (grounded[key][3] or "-").replace("|", "\\|")
            marker = " **←**" if key in changed else ""
            lines.append(f"| `{key[2]}` | {without} | {with_docs}{marker} |")
        lines.append("")

    destination = Path(args.out)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n{len(changed)} of {len(grounded)} descriptions changed")
    print(f"wrote {destination}")


if __name__ == "__main__":
    main()
