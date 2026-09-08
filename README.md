# Lakehouse Governance Agent

A governance audit agent for **Databricks Unity Catalog**. It crawls the catalog,
profiles metadata, and produces a single Lakehouse health report with actionable findings.

> **Status: work in progress.** Foundation being built. Numbers below are filled in
> as the agent runs against a real catalog — nothing here is estimated.

## The findings

| # | Finding | How it's detected |
|---|---------|-------------------|
| 1 | Undocumented tables and columns | `information_schema` + LLM description from name, type, value sample, and upstream lineage |
| 2 | Unclassified PII | Pattern matching + LLM, suggesting sensitivity tags and masking policies |
| 3 | Orphan tables | No reads in 90 days via `system.access.audit`, with estimated cost attached |
| 4 | Dead columns / quality | 100% null, cardinality of 1, suspicious types |
| 5 | Delta hygiene | Never `OPTIMIZE`d, no owner set |

Out of scope for v1: suggested expectations, visual lineage graph, scheduling via Workflows.

## Why build this when Databricks already generates comments with AI

It does — and that is the point. This is not "automatic documentation with an LLM",
which would be a worse version of a feature that already ships. This is a
**multi-finding governance audit** in which documentation is one item out of five,
and the native feature becomes a benchmark: where the agent wins, where it loses,
and when building your own is worth it.

## Results

_TBD — populated once the agent runs end to end._

- Columns documented: —
- Token cost: —
- Equivalent manual effort: —
- Findings by category: —
- Estimated monthly cost of orphan tables: —

## Architecture

Runs against a **SQL Warehouse** through `databricks-sql-connector`. No Databricks
Connect and no local Spark: the workload is almost entirely SQL over metadata, and
Databricks Connect requires dedicated compute that Free Edition does not provide.
Only comment writes and Delta persistence touch the workspace.

```
src/crawler.py    information_schema -> inventory
src/profiler.py   per-column profile (nulls, cardinality, sample)
src/agent.py      LLM: descriptions + PII classification
src/report.py     consolidated findings
app/              Streamlit report over the Delta table
```

Writes to the catalog run in **dry-run mode by default**, printing the diff instead
of applying it.

## Setup

```bash
python -m venv venv
venv\Scripts\activate          # Windows
pip install -r requirements.txt

copy .env.example .env         # then fill it in
python src/test_connection.py
```

`DATABRICKS_HOST` and `DATABRICKS_HTTP_PATH` come from
SQL Warehouses → your warehouse → Connection details.
`DATABRICKS_TOKEN` from Settings → Developer → Access tokens.

`test_connection.py` probes every system table the agent depends on and reports each
one independently, so a partial failure tells you exactly what is unavailable.

## What I would do differently

_TBD — written at the end, not the beginning._
