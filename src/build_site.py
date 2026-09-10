"""Generate a browsable documentation portal from the catalog inventory.

A documented catalog nobody can browse does not look like a documented catalog.
This renders the agent's output as a static site: every table, every column, every
generated description, and -- the part that matters -- the passage of source
documentation each description came from, one click away.

It emits a single self-contained HTML file with the data embedded, so it can be
opened from disk, served by GitHub Pages, or mailed to someone. No build step, no
runtime dependency, no server. The catalog it describes is a snapshot: regenerate
after a crawl.

Deliberately not a Databricks App. An App lives behind a workspace login, which is
correct for an internal tool and useless for anything a stranger should be able to
open.

Run:
    python src/build_site.py            # -> docs/index.html
"""

import argparse
import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from dbio import connect, load_env
# The health score is computed by report.py. Reimplementing the formula here would
# let the site and the report drift apart while both claim to be authoritative.
from report import gather as gather_for_score
from report import scores as compute_scores

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_ROOT = REPO_ROOT / "docs"
DEFAULT_CATALOG = "governance_lab"
META = "meta"
ORPHAN_DAYS = 90
REPO_URL = "https://github.com/DudzTheBoy/lakehouse-governance-agent"


# --------------------------------------------------------------------------- data


def fetch(cursor, query: str) -> list[tuple]:
    cursor.execute(query)
    return cursor.fetchall()


def collect(catalog: str) -> dict:
    with connect() as connection:
        with connection.cursor() as cursor:
            scan_id = fetch(
                cursor,
                f"""SELECT scan_id FROM {catalog}.{META}.scan_runs
                    WHERE catalog = '{catalog}' ORDER BY started_at DESC LIMIT 1""",
            )[0][0]

            # Two different runs matter here and conflating them produces nonsense.
            # `write_run` is the one that actually wrote comments to the catalog, and
            # carries the before/after story. Re-running the agent afterwards writes
            # nothing, because the comments already exist -- taking the latest applied
            # run instead put "0 comments written" at the top of the page.
            run_rows = fetch(
                cursor,
                f"""SELECT run_id, model, tables_processed, columns_documented,
                           prompt_tokens, completion_tokens, est_cost_usd, applied,
                           unix_timestamp(finished_at) - unix_timestamp(started_at),
                           scan_id
                    FROM {catalog}.{META}.llm_runs
                    WHERE applied = true AND columns_documented > 0
                    ORDER BY started_at DESC LIMIT 1""",
            )
            run = run_rows[0] if run_rows else None

            # `text_run` is the newest run of any kind: it holds the descriptions
            # currently on display, including the Portuguese ones.
            text_rows = fetch(
                cursor,
                f"""SELECT run_id, est_cost_usd, prompt_tokens, completion_tokens
                    FROM {catalog}.{META}.llm_runs ORDER BY started_at DESC LIMIT 1""",
            )
            text_run = text_rows[0] if text_rows else None

            # The applied run was computed from the scan taken before it wrote
            # anything, so that scan is the "before" state, for free.
            before_score = after_score = None
            after_score = compute_scores(gather_for_score(cursor, catalog, scan_id, None))["overall"]
            if run and run[9] and run[9] != scan_id:
                try:
                    before_score = compute_scores(
                        gather_for_score(cursor, catalog, run[9], None)
                    )["overall"]
                except Exception:
                    before_score = None

            tables = fetch(
                cursor,
                f"""SELECT table_schema, table_name, table_type, table_owner,
                           table_comment, row_count, size_bytes, num_files, fragmented,
                           last_read_at, days_since_read, never_read, is_writable
                    FROM {catalog}.{META}.table_inventory
                    WHERE scan_id = '{scan_id}' ORDER BY table_schema, table_name""",
            )

            columns = fetch(
                cursor,
                f"""SELECT table_schema, table_name, column_name, ordinal_position,
                           data_type, column_comment, null_ratio, distinct_count,
                           is_all_null, is_constant, is_pii_candidate,
                           pii_by_name, pii_by_value
                    FROM {catalog}.{META}.column_profile
                    WHERE scan_id = '{scan_id}'
                    ORDER BY table_schema, table_name, ordinal_position""",
            )

            # Portuguese lives only here: Unity Catalog holds one COMMENT per object,
            # and English is what was written to it.
            comments_pt, table_pt = {}, {}
            if text_run:
                for schema, table, column, pt in fetch(
                    cursor,
                    f"""SELECT table_schema, table_name, column_name, suggested_comment_pt
                        FROM {catalog}.{META}.llm_suggestions
                        WHERE run_id = '{text_run[0]}' AND suggested_comment_pt IS NOT NULL""",
                ):
                    comments_pt[f"{schema}.{table}.{column}"] = pt
                for schema, table, pt in fetch(
                    cursor,
                    f"""SELECT table_schema, table_name, description_pt
                        FROM {catalog}.{META}.llm_table_descriptions
                        WHERE run_id = '{text_run[0]}' AND description_pt IS NOT NULL""",
                ):
                    table_pt[f"{schema}.{table}"] = pt

            provenance = {}
            if run:
                for schema, table, system, citations in fetch(
                    cursor,
                    f"""SELECT DISTINCT table_schema, table_name, doc_system, doc_citations
                        FROM {catalog}.{META}.llm_suggestions
                        WHERE run_id = '{run[0]}' AND doc_system IS NOT NULL""",
                ):
                    provenance[f"{schema}.{table}"] = {
                        "system": system,
                        "passages": [c.strip() for c in (citations or "").split(";") if c.strip()],
                    }

            llm_pii = {}
            if run:
                for schema, table, column, label in fetch(
                    cursor,
                    f"""SELECT table_schema, table_name, column_name, pii_by_llm
                        FROM {catalog}.{META}.llm_suggestions
                        WHERE run_id = '{run[0]}' AND pii_by_llm IS NOT NULL
                          AND pii_by_llm <> 'none'""",
                ):
                    llm_pii[f"{schema}.{table}.{column}"] = label

    return {
        "catalog": catalog,
        "scan_id": scan_id,
        "run": run,
        "tables": tables,
        "columns": columns,
        "provenance": provenance,
        "llm_pii": llm_pii,
        "text_run": text_run,
        "comments_pt": comments_pt,
        "table_pt": table_pt,
        "before_score": before_score,
        "after_score": after_score,
    }


def human_bytes(value) -> str:
    if not value:
        return "-"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


def shape(raw: dict) -> dict:
    columns_by_table: dict = {}
    for row in raw["columns"]:
        key = f"{row[0]}.{row[1]}"
        pii_labels = [label for label in (row[11], row[12]) if label]
        llm_label = raw["llm_pii"].get(f"{key}.{row[2]}")
        if llm_label and llm_label not in pii_labels:
            pii_labels.append(f"{llm_label} (model)")
        columns_by_table.setdefault(key, []).append(
            {
                "name": row[2],
                "type": row[4],
                "comment": row[5],
                "commentPt": raw["comments_pt"].get(f"{key}.{row[2]}"),
                "nullRatio": float(row[6]) if row[6] is not None else None,
                "distinct": row[7],
                "allNull": bool(row[8]),
                "constant": bool(row[9]),
                "pii": bool(row[10]) or bool(llm_label),
                "piiLabels": pii_labels,
            }
        )

    tables = []
    for row in raw["tables"]:
        key = f"{row[0]}.{row[1]}"
        cols = columns_by_table.get(key, [])
        orphan = bool(row[11]) or (row[10] is not None and row[10] > ORPHAN_DAYS)
        tables.append(
            {
                "schema": row[0],
                "name": row[1],
                "key": key,
                "type": row[2],
                "owner": row[3],
                "comment": row[4],
                "commentPt": raw["table_pt"].get(key),
                "rows": row[5],
                "sizeBytes": row[6],
                "numFiles": row[7],
                "fragmented": bool(row[8]),
                "lastRead": row[9].isoformat() if row[9] else None,
                "orphan": orphan,
                "writable": bool(row[12]),
                "columns": cols,
                "piiCount": sum(1 for c in cols if c["pii"]),
                "deadCount": sum(1 for c in cols if c["allNull"] or c["constant"]),
                "provenance": raw["provenance"].get(key),
            }
        )

    # Coverage per schema, so a gap is visible as a short bar rather than as a
    # number the reader has to compare against another number.
    schemas: dict = {}
    for table in tables:
        entry = schemas.setdefault(table["schema"], {"schema": table["schema"], "columns": 0,
                                                     "documented": 0, "tables": 0})
        entry["tables"] += 1
        entry["columns"] += len(table["columns"])
        entry["documented"] += sum(1 for c in table["columns"] if c["comment"])
    schema_coverage = sorted(schemas.values(), key=lambda s: -s["columns"])

    # Findings ranked by what they oblige someone to do, not by how many there are.
    findings = []
    for table in tables:
        n = table["piiCount"]
        if n:
            findings.append({
                "severity": "high", "table": table["key"],
                "what": f"{n} personal-data column{'s' if n > 1 else ''}",
                "whatPt": f"{n} coluna{'s' if n > 1 else ''} com dado pessoal",
                "why": "needs a masking policy and a retention decision",
                "whyPt": "exige política de mascaramento e decisão de retenção",
            })
        if table["orphan"]:
            findings.append({
                "severity": "medium", "table": table["key"],
                "what": "never read", "whatPt": "nunca lida",
                "why": f"{human_bytes(table['sizeBytes'])} nobody has queried",
                "whyPt": f"{human_bytes(table['sizeBytes'])} que ninguém consultou",
            })
        d = table["deadCount"]
        if d:
            findings.append({
                "severity": "low", "table": table["key"],
                "what": f"{d} dead column{'s' if d > 1 else ''}",
                "whatPt": f"{d} coluna{'s' if d > 1 else ''} morta{'s' if d > 1 else ''}",
                "why": "always null or a single repeated value",
                "whyPt": "sempre nula ou com um único valor repetido",
            })
        if table["fragmented"]:
            findings.append({
                "severity": "low", "table": table["key"],
                "what": "fragmented", "whatPt": "fragmentada",
                "why": f"{table['numFiles']} files for {human_bytes(table['sizeBytes'])}",
                "whyPt": f"{table['numFiles']} arquivos para {human_bytes(table['sizeBytes'])}",
            })
    order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: (order[f["severity"]], f["table"]))

    run = raw["run"]
    total_columns = sum(len(t["columns"]) for t in tables)
    documented = sum(1 for t in tables for c in t["columns"] if c["comment"])
    pii_columns = sum(t["piiCount"] for t in tables)
    return {
        "catalog": raw["catalog"],
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "scanId": raw["scan_id"][:8],
        "repoUrl": REPO_URL,
        "tables": tables,
        "schemaCoverage": schema_coverage,
        "findings": findings,
        "beforeScore": round(raw["before_score"] * 100) if raw["before_score"] else None,
        "afterScore": round(raw["after_score"] * 100) if raw["after_score"] else None,
        "textRun": None if not raw["text_run"] else {
            "cost": float(raw["text_run"][1]) if raw["text_run"][1] is not None else None,
            "tokens": (raw["text_run"][2] or 0) + (raw["text_run"][3] or 0),
        },
        "stats": {
            "tables": len(tables),
            "columns": total_columns,
            "documented": documented,
            "piiColumns": pii_columns,
            "orphans": sum(1 for t in tables if t["orphan"]),
            "dead": sum(t["deadCount"] for t in tables),
            "fragmented": sum(1 for t in tables if t["fragmented"]),
        },
        "run": None
        if not run
        else {
            "model": run[1],
            "tables": run[2],
            "comments": run[3],
            "promptTokens": run[4],
            "completionTokens": run[5],
            "cost": float(run[6]) if run[6] is not None else None,
            "seconds": int(run[8]) if run[8] is not None else None,
        },
    }


# ------------------------------------------------------------------- markdown


def markdown_to_html(text: str) -> str:
    """Enough Markdown for the documentation files in this repo, and no more.

    A dependency-free renderer beats pulling a library in for five constructs, and
    the input is not arbitrary: these are files this project ships.
    """
    def inline(s: str) -> str:
        s = html.escape(s)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", s)
        s = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)",
                   r'<a href="\2" target="_blank" rel="noopener">\1</a>', s)
        s = re.sub(r"(?<!\")(https?://[^\s<]+)",
                   r'<a href="\1" target="_blank" rel="noopener">\1</a>', s)
        return s

    out, buffer, in_list, in_table = [], [], False, False

    def flush_paragraph():
        nonlocal buffer
        if buffer:
            out.append(f"<p>{inline(' '.join(buffer))}</p>")
            buffer = []

    def close_blocks():
        nonlocal in_list, in_table
        if in_list:
            out.append("</ul>")
            in_list = False
        if in_table:
            out.append("</tbody></table>")
            in_table = False

    for line in text.splitlines():
        stripped = line.strip()

        if not stripped:
            flush_paragraph()
            close_blocks()
            continue

        if stripped.startswith("#"):
            flush_paragraph()
            close_blocks()
            level = min(len(stripped) - len(stripped.lstrip("#")), 4)
            out.append(f"<h{level}>{inline(stripped.lstrip('#').strip())}</h{level}>")
            continue

        if stripped.startswith(">"):
            flush_paragraph()
            close_blocks()
            out.append(f"<blockquote>{inline(stripped.lstrip('>').strip())}</blockquote>")
            continue

        if stripped.startswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if set("".join(cells)) <= set("-: "):
                continue  # separator row
            flush_paragraph()
            if not in_table:
                out.append("<table><tbody>")
                in_table = True
            tag = "th" if len(out) and out[-1] == "<table><tbody>" else "td"
            row = "".join(f"<{tag}>{inline(c)}</{tag}>" for c in cells)
            out.append(f"<tr>{row}</tr>")
            continue

        if stripped.startswith(("- ", "* ")):
            flush_paragraph()
            if in_table:
                out.append("</tbody></table>")
                in_table = False
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{inline(stripped[2:])}</li>")
            continue

        close_blocks()
        buffer.append(stripped)

    flush_paragraph()
    close_blocks()
    return "\n".join(out)


def collect_source_docs() -> list[dict]:
    sources = DOCS_ROOT / "sources"
    docs = []
    for path in sorted(sources.glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        title = next((l.lstrip("#").strip() for l in raw.splitlines() if l.startswith("#")), path.stem)
        docs.append({"id": path.stem, "title": title, "html": markdown_to_html(raw)})
    return docs


# ----------------------------------------------------------------------- render

PAGE = """<!doctype html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__CATALOG__ — catalog documentation</title>
<style>
:root {
  --bg: #131418; --sidebar: #0d0e11; --surface: #1a1c21; --raised: #22252b;
  --line: #292c33; --line-soft: #1f2228;
  --text: #e6e8ec; --muted: #9aa0ac; --faint: #6b7280;
  --accent: #5eead4; --accent-dim: #1c3c39;
  --pii: #f2a5a5; --pii-bg: #2a1c1e; --pii-line: #4d3336;
  --warn: #e8c37f; --warn-bg: #262016; --warn-line: #4a3f28;
  --ok: #86d99b;
  --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--text); font: 14px/1.6 system-ui, -apple-system, "Segoe UI", sans-serif;
  display: grid; grid-template-columns: 264px 1fr; height: 100vh; overflow: hidden;
}
a { color: var(--accent); }

/* sidebar */
.sidebar { background: var(--sidebar); border-right: 1px solid var(--line); display: flex; flex-direction: column; overflow: hidden; }
.brand { padding: 16px 16px 12px; border-bottom: 1px solid var(--line); }
.brand { display: flex; align-items: flex-start; gap: 8px; }
.brand .who { flex: 1; min-width: 0; }
.brand h1 { margin: 0; font-size: 14px; letter-spacing: .01em; }
.brand .sub { color: var(--faint); font-size: 11px; font-family: var(--mono); margin-top: 3px; }
.lang { display: flex; border: 1px solid var(--line); border-radius: 5px; overflow: hidden; flex: none; }
.lang button {
  background: none; border: none; color: var(--faint); font-family: var(--mono);
  font-size: 10.5px; padding: 3px 7px; cursor: pointer; letter-spacing: .04em;
}
.lang button.on { background: var(--accent-dim); color: var(--accent); }
.lang button:hover:not(.on) { color: var(--text); }
.search { padding: 10px 12px; }
.search input {
  width: 100%; padding: 7px 10px; background: var(--surface); border: 1px solid var(--line);
  border-radius: 6px; color: var(--text); font-size: 13px;
}
.search input:focus { outline: none; border-color: var(--accent); }
.nav { overflow-y: auto; padding: 0 8px 24px; flex: 1; }
.group { margin-top: 14px; }
.group-label {
  color: var(--faint); font-size: 10.5px; text-transform: uppercase; letter-spacing: .09em;
  padding: 0 8px 5px; font-weight: 600;
}
.item {
  display: flex; align-items: center; gap: 7px; padding: 5px 8px; border-radius: 5px;
  color: var(--muted); cursor: pointer; font-size: 13.5px; border: none; background: none;
  width: 100%; text-align: left; font-family: inherit;
}
.item:hover { background: var(--surface); color: var(--text); }
.item.active { background: var(--raised); color: var(--text); }
.item .hash { color: var(--faint); font-family: var(--mono); }
.item .flags { margin-left: auto; display: flex; gap: 3px; }
.dot { width: 6px; height: 6px; border-radius: 50%; display: inline-block; }
.dot.pii { background: var(--pii); } .dot.orphan { background: var(--warn); }

/* main */
.main { overflow-y: auto; padding: 28px 36px 80px; }
.wrap { max-width: 940px; margin-inline: auto; }
.crumb { color: var(--faint); font-size: 12px; font-family: var(--mono); margin-bottom: 8px; }
h2 { margin: 0 0 6px; font-size: 22px; }
.meta { color: var(--muted); font-size: 12.5px; margin-bottom: 18px; display: flex; gap: 14px; flex-wrap: wrap; }
.desc {
  background: var(--surface); border: 1px solid var(--line); border-left: 2px solid var(--accent);
  padding: 12px 14px; border-radius: 0 6px 6px 0; margin-bottom: 16px;
}
.prov { font-size: 12.5px; color: var(--muted); margin-bottom: 22px; }
.prov .passage {
  display: inline-block; background: var(--raised); border: 1px solid var(--line);
  border-radius: 4px; padding: 1px 7px; margin: 2px 4px 2px 0; font-size: 11.5px; cursor: pointer;
}
.prov .passage:hover { border-color: var(--accent); color: var(--text); }

table.cols { width: 100%; border-collapse: collapse; font-size: 13px; }
table.cols th {
  text-align: left; color: var(--faint); font-size: 10.5px; text-transform: uppercase;
  letter-spacing: .07em; padding: 0 10px 7px; border-bottom: 1px solid var(--line); font-weight: 600;
}
table.cols td { padding: 9px 10px; border-bottom: 1px solid var(--line); vertical-align: top; }
table.cols tr:hover td { background: var(--surface); }
.cname { font-family: var(--mono); font-size: 12.5px; white-space: nowrap; }
.ctype { font-family: var(--mono); font-size: 11.5px; color: var(--faint); white-space: nowrap; }
.ccomment { color: var(--text); }
.ccomment.missing { color: var(--faint); font-style: italic; }
.badge {
  display: inline-block; font-size: 10.5px; padding: 1px 6px; border-radius: 3px;
  border: 1px solid; margin: 1px 3px 1px 0; white-space: nowrap; font-family: var(--mono);
}
.badge.pii { color: var(--pii); border-color: #58393c; background: #2a1e20; }
.badge.dead { color: var(--warn); border-color: #52452e; background: #262117; }
.badge.type { color: var(--muted); border-color: var(--line); }
.stat { color: var(--faint); font-family: var(--mono); font-size: 11.5px; white-space: nowrap; }

.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin: 18px 0 26px; }
.card { background: var(--surface); border: 1px solid var(--line); border-radius: 7px; padding: 13px 15px; }
.card .n { font-size: 24px; font-weight: 600; font-variant-numeric: tabular-nums; }
.card .l { color: var(--faint); font-size: 11.5px; margin-top: 2px; }
.card.good .n { color: var(--ok); } .card.warn .n { color: var(--warn); } .card.pii .n { color: var(--pii); }

/* space theme.
   Styling only -- a starfield behind the page, a faint nebula wash, and a mascot.
   It is skin, and it stays out of the way of the parts that carry meaning: the
   descriptions, the badges and the provenance keep the contrast they had. */
body::before {
  content: ''; position: fixed; inset: 0; z-index: -2; pointer-events: none;
  background:
    radial-gradient(900px 600px at 78% -8%, rgba(94, 234, 212, .07), transparent 62%),
    radial-gradient(760px 520px at 8% 108%, rgba(129, 140, 248, .07), transparent 60%),
    var(--bg);
}
body::after {
  content: ''; position: fixed; inset: 0; z-index: -1; pointer-events: none; opacity: .5;
  background-image:
    radial-gradient(1.1px 1.1px at 12% 18%, #fff, transparent),
    radial-gradient(1px 1px at 47% 7%, #cfe9ff, transparent),
    radial-gradient(1.3px 1.3px at 73% 29%, #fff, transparent),
    radial-gradient(1px 1px at 24% 61%, #dfe8ff, transparent),
    radial-gradient(1.1px 1.1px at 88% 71%, #fff, transparent),
    radial-gradient(1px 1px at 61% 88%, #cfe9ff, transparent),
    radial-gradient(1.2px 1.2px at 34% 41%, #fff, transparent),
    radial-gradient(1px 1px at 92% 14%, #fff, transparent);
  background-size: 620px 620px;
}
.sidebar, .main { background: transparent; }
.sidebar { background: rgba(13, 14, 17, .82); backdrop-filter: blur(7px); }

/* the sky.

   A fixed backdrop under everything, on a strict contrast budget: orbit rings sit at
   4-6% white, planets are a few pixels across, and nothing on this layer is allowed
   to compete with a column description. The old version failed on exactly that count
   in the other direction -- three flat discs and a straight streak read as clip art,
   not as depth.

   Depth comes from parallax: three star layers drift at different rates, so the field
   has front and back instead of being one flat plane. Motion is slow enough to be
   noticed only when the eye rests -- the fastest orbit takes 74 seconds.

   Everything here stops under prefers-reduced-motion. */
.sky { position: fixed; inset: 0; z-index: -1; pointer-events: none; overflow: hidden; }
.sky svg { width: 100%; height: 100%; display: block; }

.path { fill: none; stroke: #cfe3ff; stroke-opacity: .10; stroke-width: 1.1; }
.path.dim { stroke-opacity: .06; }

@keyframes revolve { to { transform: rotate(360deg); } }
.rev { animation: revolve var(--dur, 120s) linear infinite; transform-origin: 0 0; }

/* Parallax. The near layer moves most, the far layer barely at all. */
@keyframes driftFar  { to { transform: translate3d(-14px, 7px, 0); } }
@keyframes driftMid  { to { transform: translate3d(-30px, 15px, 0); } }
@keyframes driftNear { to { transform: translate3d(-58px, 26px, 0); } }
.layer.far  { animation: driftFar 190s ease-in-out infinite alternate; }
.layer.mid  { animation: driftMid 150s ease-in-out infinite alternate; }
.layer.near { animation: driftNear 110s ease-in-out infinite alternate; }

@keyframes twinkle { 0%, 100% { opacity: inherit; } 50% { opacity: .18; } }
.tw { animation: twinkle var(--d, 4s) ease-in-out var(--t, 0s) infinite; }

/* A comet is a head with a tail behind it, so the streak is a gradient that fades
   backwards and the head is a point. The previous one was a bar with soft ends,
   which is why it read as a scratch on the screen. */
@keyframes comet {
  0%      { opacity: 0; transform: translate(-16vw, -8vh) scale(.8); }
  2%      { opacity: 1; }
  11%     { opacity: 0; transform: translate(86vw, 44vh) scale(1.15); }
  100%    { opacity: 0; transform: translate(86vw, 44vh) scale(1.15); }
}
.comet { opacity: 0; animation: comet 26s cubic-bezier(.25,.5,.4,1) infinite; }
.comet.b { animation-duration: 41s; animation-delay: 15s; }
.comet.b g { transform-origin: 0 0; }

@media (prefers-reduced-motion: reduce) {
  .rev, .layer, .tw, .comet { animation: none; }
  .comet { display: none; }
}

/* the ask view/* the ask view -- the page that has to sell the project in ten seconds.

   The astronaut is rigged: shoulders and hips are their own pivots, so limbs swing
   rather than the whole sprite tilting. He stays at the top of the moon and the moon
   turns underneath him, which is both easier to keep smooth than moving him along an
   arc and the reason the walk reads as walking rather than sliding.

   A small state machine picks what he does next. Everything stops flat under
   prefers-reduced-motion -- a character idling in the corner of the eye is exactly
   the kind of motion that makes a page unusable for some readers. */
.scene { position: relative; height: 262px; margin: 0 0 4px; }
.scene svg { width: 100%; height: 100%; display: block; overflow: hidden; }
.scene .corona {
  position: absolute; left: 50%; top: 46%; width: 380px; height: 380px; margin: -190px 0 0 -190px;
  border-radius: 50%; pointer-events: none;
  background: radial-gradient(circle, rgba(94,234,212,.10) 0%, rgba(94,234,212,.03) 45%, transparent 68%);
}

.rig { transform-box: fill-box; transform-origin: 50% 100%; }
.limb { transform-box: fill-box; }
.arm-l, .arm-r { transform-origin: 50% 10%; }
.leg-l, .leg-r { transform-origin: 50% 7%; }
.head { transform-origin: 50% 96%; }

@keyframes stepA { 0%,100% { transform: translateY(0) rotate(3deg); } 50% { transform: translateY(-3.4px) rotate(-3deg); } }
@keyframes stepB { 0%,100% { transform: translateY(-3.4px) rotate(-3deg); } 50% { transform: translateY(0) rotate(3deg); } }
@keyframes armA   { 0%,100% { transform: rotate(-9deg); } 50% { transform: rotate(9deg); } }
@keyframes armB   { 0%,100% { transform: rotate(9deg); } 50% { transform: rotate(-9deg); } }
@keyframes bob    { 0%,100% { transform: translateY(0); } 50% { transform: translateY(-1.6px); } }

.walk .leg-l { animation: stepA .68s ease-in-out infinite; }
.walk .leg-r { animation: stepB .68s ease-in-out infinite; }
.walk .arm-l { animation: armB .68s ease-in-out infinite; }
.walk .arm-r { animation: armA .68s ease-in-out infinite; }
.walk .rig   { animation: bob .34s ease-in-out infinite; }

@keyframes hop {
  0% { transform: translateY(0); } 30% { transform: translateY(-42px); }
  52% { transform: translateY(-46px); } 100% { transform: translateY(0); }
}
@keyframes tuck { 0%,100% { transform: rotate(0); } 40% { transform: rotate(34deg); } }
@keyframes reach { 0%,100% { transform: rotate(0); } 40% { transform: rotate(-118deg); } }
.hop .rig { animation: hop 1.5s cubic-bezier(.34,0,.28,1); }
.hop .leg-l, .hop .leg-r { animation: tuck 1.5s cubic-bezier(.34,0,.28,1); }
.hop .arm-l, .hop .arm-r { animation: reach 1.5s cubic-bezier(.34,0,.28,1); }

@keyframes waving { 0%,100% { transform: rotate(-128deg); } 50% { transform: rotate(-92deg); } }
.wave .arm-r { animation: waving .46s ease-in-out 5; }

@keyframes peek { 0%,100% { transform: rotate(0); } 25% { transform: rotate(-6deg); } 75% { transform: rotate(6deg); } }
.look .head { animation: peek 2.6s ease-in-out; }

@keyframes breathe { 0%,100% { transform: translateY(0); } 50% { transform: translateY(-1.1px); } }
.idle .rig { animation: breathe 3.4s ease-in-out infinite; }

@keyframes puff { 0% { opacity: .38; transform: translate(0,0) scale(.5); } 100% { opacity: 0; transform: translate(var(--dx),-9px) scale(1.5); } }
.dust { opacity: 0; }
.walk .dust { animation: puff .68s linear infinite; }
.dust.b { animation-delay: .34s; }

@keyframes bubblein {
  0% { opacity: 0; transform: translateY(5px) scale(.6); }
  16% { opacity: 1; transform: translateY(0) scale(1); }
  80% { opacity: 1; transform: translateY(0) scale(1); }
  100% { opacity: 0; transform: translateY(-7px) scale(.9); }
}
.bubble { transform-box: fill-box; transform-origin: 50% 100%; opacity: 0; }
.bubble.show { animation: bubblein 2.1s ease-out; }

@media (prefers-reduced-motion: reduce) {
  .rig, .limb, .head, .dust, .bubble, .moon-spin { animation: none !important; }
}

/* Two columns while there is room. Centred, the page was a narrow ribbon of
   content down the middle with the scene floating over a lot of nothing; side by
   side, the character has somewhere to be and the question sits where the eye
   already is. */
.mission { padding: 4px 0 40px; }
.split {
  display: grid; grid-template-columns: minmax(300px, 420px) 1fr; gap: 30px;
  align-items: center; margin-bottom: 30px;
}
.split .scene { margin: 0; height: 300px; }
.mission h2 { font-size: 32px; letter-spacing: -.02em; margin: 0 0 10px; line-height: 1.15; }
.mission .lede { color: var(--muted); font-size: 15px; margin: 0 0 14px; max-width: 46ch; }
.mission .counts {
  color: var(--faint); font-family: var(--mono); font-size: 11.5px;
  letter-spacing: .04em; margin-bottom: 18px;
}
.askbox form { display: flex; gap: 9px; }
.askbox input {
  flex: 1; min-width: 0; padding: 13px 16px; font-size: 15px; border-radius: 10px;
  background: rgba(10,11,14,.72); border: 1px solid var(--line); color: var(--text);
  font-family: inherit;
}
.askbox input:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px rgba(94,234,212,.10); }
.askbox button {
  padding: 0 22px; border-radius: 10px; font-size: 14px; cursor: pointer; font-family: inherit;
  background: var(--accent); border: 1px solid var(--accent); color: #07211d; font-weight: 600;
}
.askbox button:hover { filter: brightness(1.08); }
.suggest { display: flex; gap: 7px; flex-wrap: wrap; margin: 12px 0 0; }
.suggest button {
  background: rgba(255,255,255,.03); border: 1px solid var(--line); color: var(--muted);
  border-radius: 20px; padding: 5px 13px; font-size: 12.5px; cursor: pointer; font-family: inherit;
}
.suggest button:hover { border-color: var(--accent); color: var(--accent); }

/* Landing on an empty results pane is what made the page feel unfinished. Until a
   question is asked, the space carries what there is to ask about. */
.tiles { display: grid; grid-template-columns: repeat(auto-fill, minmax(215px, 1fr)); gap: 11px; }
.tile {
  text-align: left; background: rgba(255,255,255,.022); border: 1px solid var(--line);
  border-radius: 10px; padding: 13px 15px; cursor: pointer; font-family: inherit;
  color: var(--text); transition: border-color .15s, background .15s;
}
.tile:hover { border-color: var(--accent); background: rgba(94,234,212,.05); }
.tile .t { font-family: var(--mono); font-size: 13px; margin-bottom: 3px; }
.tile .d { color: var(--faint); font-size: 12px; line-height: 1.45; }
.tile .n { color: var(--accent); font-variant-numeric: tabular-nums; }
.tile.warn .n { color: var(--warn); }
.tile.pii .n { color: var(--pii); }

.mission .answer { margin-top: 4px; }

@media (max-width: 940px) {
  .split { grid-template-columns: 1fr; gap: 8px; }
  /* The label and its hint sat side by side and wrapped into each other. */
  .mission .sec { flex-direction: column; gap: 2px; }
  .split .scene { height: 240px; }
  .mission h2, .mission .lede, .mission .counts { text-align: center; }
  .mission .lede { margin-left: auto; margin-right: auto; }
  .suggest { justify-content: center; }
}


.chips { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 9px; }
.chips button {
  background: none; border: 1px solid var(--line); color: var(--muted); border-radius: 20px;
  padding: 3px 11px; font-size: 11.5px; cursor: pointer; font-family: inherit;
}
.chips button:hover { border-color: var(--accent); color: var(--accent); }

/* hero */
.hero { padding: 8px 0 30px; border-bottom: 1px solid var(--line); margin-bottom: 30px; }
.hero .eyebrow {
  font-family: var(--mono); font-size: 11px; letter-spacing: .1em; text-transform: uppercase;
  color: var(--accent); margin-bottom: 14px;
}
.hero h2 { font-size: 27px; letter-spacing: -.01em; margin-bottom: 10px; }
.hero .lede { color: var(--muted); font-size: 15px; max-width: 620px; margin-bottom: 26px; }
.score { display: flex; align-items: center; gap: 18px; margin-bottom: 8px; flex-wrap: wrap; }
.score .v { font-size: 52px; font-weight: 650; line-height: 1; font-variant-numeric: tabular-nums; }
.score .v.was { color: var(--faint); font-size: 34px; font-weight: 500; }
.score .v.now { color: var(--ok); }
.score .arrow { color: var(--line); font-size: 22px; }
.score .cap { color: var(--faint); font-size: 12px; font-family: var(--mono); }
.runline { color: var(--muted); font-size: 13px; margin-top: 16px; }
.runline strong { color: var(--text); font-variant-numeric: tabular-nums; }

/* section headings */
.sec { margin: 34px 0 14px; display: flex; align-items: baseline; gap: 10px; }
.sec h3 {
  margin: 0; font-size: 11px; letter-spacing: .1em; text-transform: uppercase;
  color: var(--faint); font-weight: 600;
}
.sec .hint { color: var(--faint); font-size: 12px; }

/* coverage bars */
.cov { display: grid; grid-template-columns: 130px 1fr auto; gap: 10px 14px; align-items: center; }
.cov .name { font-family: var(--mono); font-size: 12.5px; color: var(--muted); }
.cov .track { background: var(--raised); height: 7px; border-radius: 4px; overflow: hidden; }
.cov .fill { background: var(--accent); height: 100%; border-radius: 4px; }
.cov .fill.partial { background: var(--warn); }
.cov .val { font-family: var(--mono); font-size: 12px; color: var(--faint); white-space: nowrap; }

/* findings */
.find { width: 100%; border-collapse: collapse; font-size: 13px; }
.find td { padding: 8px 10px; border-bottom: 1px solid var(--line-soft); }
.find tr:hover td { background: var(--surface); }
.find .sev {
  font-family: var(--mono); font-size: 10.5px; text-transform: uppercase;
  letter-spacing: .06em; width: 62px;
}
.find .sev.high { color: var(--pii); } .find .sev.medium { color: var(--warn); }
.find .sev.low { color: var(--faint); }
.find .tbl { font-family: var(--mono); font-size: 12.5px; white-space: nowrap; }
.find .tbl a { color: var(--muted); text-decoration: none; }
.find .tbl a:hover { color: var(--accent); }
.find .why { color: var(--faint); }

/* sidebar footer + mobile */
.sidefoot {
  border-top: 1px solid var(--line); padding: 11px 16px; font-size: 11.5px; color: var(--faint);
}
.sidefoot a { color: var(--muted); text-decoration: none; }
.sidefoot a:hover { color: var(--accent); }
.burger {
  display: none; position: fixed; top: 12px; left: 12px; z-index: 20;
  background: var(--raised); border: 1px solid var(--line); color: var(--text);
  border-radius: 6px; width: 34px; height: 34px; font-size: 15px; cursor: pointer;
}

.doc h1, .doc h2 { border-bottom: 1px solid var(--line); padding-bottom: 6px; }
.doc h1 { font-size: 20px; margin-top: 0; } .doc h2 { font-size: 16px; margin-top: 28px; }
.doc h3 { font-size: 14px; margin-top: 22px; color: var(--muted); }
.doc blockquote {
  margin: 14px 0; padding: 10px 14px; border-left: 2px solid var(--warn);
  background: var(--surface); color: var(--muted); font-size: 13px;
}
.doc code { background: var(--raised); padding: 1px 5px; border-radius: 3px; font-family: var(--mono); font-size: 12.5px; }
.doc table { border-collapse: collapse; margin: 12px 0; font-size: 13px; width: 100%; }
.doc th, .doc td { border: 1px solid var(--line); padding: 6px 10px; text-align: left; }
.doc th { background: var(--surface); color: var(--muted); }
.doc ul { padding-left: 20px; } .doc li { margin: 3px 0; }
.note { color: var(--faint); font-size: 12px; margin-top: 34px; padding-top: 14px; border-top: 1px solid var(--line); }

@media (max-width: 820px) {
  body { grid-template-columns: 1fr; }
  .sidebar {
    position: fixed; z-index: 15; width: 264px; height: 100%;
    transform: translateX(-100%); transition: transform .2s;
  }
  .sidebar.open { transform: none; box-shadow: 0 0 40px rgba(0,0,0,.6); }
  .burger { display: block; }
  .main { padding: 58px 20px 60px; }
  .cov { grid-template-columns: 100px 1fr auto; }
  .score .v { font-size: 42px; }
  .find .why { display: none; }
}
</style>
</head>
<body>
<div class="sky" aria-hidden="true">
  <svg viewBox="0 0 1600 900" preserveAspectRatio="xMidYMid slice">
    <defs>
      <radialGradient id="sunglow" cx="50%" cy="50%">
        <stop offset="0%" stop-color="#bff6ec" stop-opacity=".20"/>
        <stop offset="34%" stop-color="#5eead4" stop-opacity=".07"/>
        <stop offset="100%" stop-color="#5eead4" stop-opacity="0"/>
      </radialGradient>
      <radialGradient id="neb1" cx="50%" cy="50%">
        <stop offset="0%" stop-color="#3f5f8f" stop-opacity=".16"/>
        <stop offset="100%" stop-color="#3f5f8f" stop-opacity="0"/>
      </radialGradient>
      <radialGradient id="neb2" cx="50%" cy="50%">
        <stop offset="0%" stop-color="#5c4a86" stop-opacity=".14"/>
        <stop offset="100%" stop-color="#5c4a86" stop-opacity="0"/>
      </radialGradient>
      <radialGradient id="pl1" cx="34%" cy="30%">
        <stop offset="0%" stop-color="#9fc4d8"/><stop offset="100%" stop-color="#24384a"/>
      </radialGradient>
      <radialGradient id="pl2" cx="34%" cy="30%">
        <stop offset="0%" stop-color="#c8b39a"/><stop offset="100%" stop-color="#4a3a2c"/>
      </radialGradient>
      <radialGradient id="pl3" cx="34%" cy="30%">
        <stop offset="0%" stop-color="#8fa8d6"/><stop offset="100%" stop-color="#2a3350"/>
      </radialGradient>
      <radialGradient id="pl4" cx="34%" cy="30%">
        <stop offset="0%" stop-color="#a8d8d0"/><stop offset="100%" stop-color="#22463f"/>
      </radialGradient>
      <radialGradient id="scrim" cx="44%" cy="48%">
        <stop offset="0%" stop-color="#131418" stop-opacity=".46"/>
        <stop offset="42%" stop-color="#131418" stop-opacity=".28"/>
        <stop offset="78%" stop-color="#131418" stop-opacity=".08"/>
        <stop offset="100%" stop-color="#131418" stop-opacity="0"/>
      </radialGradient>
      <linearGradient id="tail" x1="0" y1="0" x2="1" y2="0">
        <stop offset="0%" stop-color="#ffffff" stop-opacity="0"/>
        <stop offset="72%" stop-color="#cfe8ff" stop-opacity=".42"/>
        <stop offset="100%" stop-color="#ffffff" stop-opacity=".95"/>
      </linearGradient>
    </defs>

    <ellipse cx="300" cy="210" rx="520" ry="320" fill="url(#neb1)"/>
    <ellipse cx="1330" cy="760" rx="470" ry="330" fill="url(#neb2)"/>

    <g class="layer far"><circle cx="1411" cy="228" r="0.42" fill="#dce9ff" opacity="0.26"/><circle cx="1146" cy="510" r="0.68" fill="#dce9ff" opacity="0.12"/><circle cx="300" cy="116" r="0.63" fill="#dce9ff" opacity="0.29"/><circle cx="583" cy="83" r="0.65" fill="#dce9ff" opacity="0.16"/><circle cx="226" cy="248" r="0.44" fill="#dce9ff" opacity="0.20"/><circle cx="1263" cy="606" r="0.72" fill="#dce9ff" opacity="0.25"/><circle cx="907" cy="153" r="0.60" fill="#dce9ff" opacity="0.24"/><circle cx="1564" cy="7" r="0.50" fill="#dce9ff" opacity="0.26"/><circle cx="414" cy="890" r="0.44" fill="#dce9ff" opacity="0.21"/><circle cx="804" cy="301" r="0.73" fill="#dce9ff" opacity="0.21"/><circle cx="1083" cy="546" r="0.70" fill="#dce9ff" opacity="0.31"/><circle cx="1295" cy="602" r="0.36" fill="#dce9ff" opacity="0.30"/><circle cx="1244" cy="685" r="0.56" fill="#dce9ff" opacity="0.24"/><circle cx="370" cy="742" r="0.45" fill="#dce9ff" opacity="0.31"/><circle cx="1100" cy="403" r="0.69" fill="#dce9ff" opacity="0.33"/><circle cx="523" cy="203" r="0.40" fill="#dce9ff" opacity="0.28"/><circle cx="1114" cy="254" r="0.37" fill="#dce9ff" opacity="0.16"/><circle cx="534" cy="569" r="0.57" fill="#dce9ff" opacity="0.30"/><circle cx="740" cy="797" r="0.73" fill="#dce9ff" opacity="0.14"/><circle cx="94" cy="462" r="0.56" fill="#dce9ff" opacity="0.33"/><circle cx="846" cy="798" r="0.36" fill="#dce9ff" opacity="0.19"/><circle cx="1158" cy="332" r="0.44" fill="#dce9ff" opacity="0.32"/><circle cx="773" cy="98" r="0.45" fill="#dce9ff" opacity="0.24"/><circle cx="564" cy="766" r="0.40" fill="#dce9ff" opacity="0.25"/><circle cx="1060" cy="750" r="0.52" fill="#dce9ff" opacity="0.28"/><circle cx="611" cy="166" r="0.37" fill="#dce9ff" opacity="0.10"/><circle cx="679" cy="79" r="0.47" fill="#dce9ff" opacity="0.29"/><circle cx="261" cy="306" r="0.70" fill="#dce9ff" opacity="0.14"/><circle cx="171" cy="251" r="0.50" fill="#dce9ff" opacity="0.30"/><circle cx="1330" cy="869" r="0.38" fill="#dce9ff" opacity="0.31"/><circle cx="1322" cy="269" r="0.74" fill="#dce9ff" opacity="0.13"/><circle cx="1413" cy="2" r="0.44" fill="#dce9ff" opacity="0.30"/><circle cx="1243" cy="381" r="0.68" fill="#dce9ff" opacity="0.23"/><circle cx="15" cy="643" r="0.51" fill="#dce9ff" opacity="0.20"/><circle cx="1057" cy="814" r="0.61" fill="#dce9ff" opacity="0.16"/><circle cx="220" cy="862" r="0.74" fill="#dce9ff" opacity="0.18"/><circle cx="1450" cy="733" r="0.38" fill="#dce9ff" opacity="0.29"/><circle cx="903" cy="671" r="0.65" fill="#dce9ff" opacity="0.34"/><circle cx="237" cy="132" r="0.53" fill="#dce9ff" opacity="0.28"/><circle cx="654" cy="487" r="0.62" fill="#dce9ff" opacity="0.11"/><circle cx="424" cy="296" r="0.61" fill="#dce9ff" opacity="0.24"/><circle cx="507" cy="503" r="0.64" fill="#dce9ff" opacity="0.26"/><circle cx="1454" cy="531" r="0.50" fill="#dce9ff" opacity="0.32"/><circle cx="92" cy="833" r="0.60" fill="#dce9ff" opacity="0.20"/><circle cx="1265" cy="148" r="0.71" fill="#dce9ff" opacity="0.16"/><circle cx="367" cy="804" r="0.52" fill="#dce9ff" opacity="0.19"/><circle cx="1273" cy="594" r="0.50" fill="#dce9ff" opacity="0.33"/><circle cx="1076" cy="526" r="0.53" fill="#dce9ff" opacity="0.17"/><circle cx="1457" cy="747" r="0.48" fill="#dce9ff" opacity="0.17"/><circle cx="462" cy="669" r="0.56" fill="#dce9ff" opacity="0.27"/><circle cx="810" cy="103" r="0.63" fill="#dce9ff" opacity="0.16"/><circle cx="1541" cy="118" r="0.70" fill="#dce9ff" opacity="0.18"/><circle cx="12" cy="34" r="0.42" fill="#dce9ff" opacity="0.22"/><circle cx="1418" cy="622" r="0.70" fill="#dce9ff" opacity="0.15"/><circle cx="1523" cy="79" r="0.48" fill="#dce9ff" opacity="0.34"/><circle cx="564" cy="579" r="0.52" fill="#dce9ff" opacity="0.12"/><circle cx="411" cy="730" r="0.45" fill="#dce9ff" opacity="0.20"/><circle cx="604" cy="612" r="0.66" fill="#dce9ff" opacity="0.11"/><circle cx="638" cy="250" r="0.74" fill="#dce9ff" opacity="0.26"/><circle cx="1560" cy="392" r="0.41" fill="#dce9ff" opacity="0.22"/><circle cx="1391" cy="746" r="0.65" fill="#dce9ff" opacity="0.15"/><circle cx="39" cy="863" r="0.39" fill="#dce9ff" opacity="0.22"/><circle cx="465" cy="474" r="0.60" fill="#dce9ff" opacity="0.23"/><circle cx="1130" cy="338" r="0.67" fill="#dce9ff" opacity="0.15"/><circle cx="1167" cy="231" r="0.39" fill="#dce9ff" opacity="0.13"/><circle cx="624" cy="455" r="0.40" fill="#dce9ff" opacity="0.23"/><circle cx="730" cy="888" r="0.38" fill="#dce9ff" opacity="0.30"/><circle cx="765" cy="686" r="0.75" fill="#dce9ff" opacity="0.16"/><circle cx="961" cy="698" r="0.60" fill="#dce9ff" opacity="0.14"/><circle cx="1421" cy="238" r="0.49" fill="#dce9ff" opacity="0.19"/><circle cx="258" cy="846" r="0.55" fill="#dce9ff" opacity="0.11"/><circle cx="1254" cy="643" r="0.42" fill="#dce9ff" opacity="0.25"/><circle cx="10" cy="21" r="0.58" fill="#dce9ff" opacity="0.31"/><circle cx="1554" cy="97" r="0.39" fill="#dce9ff" opacity="0.13"/><circle cx="1007" cy="131" r="0.49" fill="#dce9ff" opacity="0.27"/><circle cx="236" cy="135" r="0.45" fill="#dce9ff" opacity="0.34"/><circle cx="167" cy="742" r="0.52" fill="#dce9ff" opacity="0.34"/><circle cx="1365" cy="898" r="0.48" fill="#dce9ff" opacity="0.31"/><circle cx="775" cy="226" r="0.70" fill="#dce9ff" opacity="0.23"/><circle cx="1065" cy="182" r="0.60" fill="#dce9ff" opacity="0.11"/><circle cx="995" cy="673" r="0.42" fill="#dce9ff" opacity="0.25"/><circle cx="1584" cy="4" r="0.56" fill="#dce9ff" opacity="0.16"/><circle cx="940" cy="523" r="0.40" fill="#dce9ff" opacity="0.26"/><circle cx="93" cy="872" r="0.48" fill="#dce9ff" opacity="0.24"/><circle cx="1139" cy="721" r="0.72" fill="#dce9ff" opacity="0.11"/><circle cx="687" cy="588" r="0.50" fill="#dce9ff" opacity="0.21"/><circle cx="459" cy="478" r="0.41" fill="#dce9ff" opacity="0.11"/><circle cx="283" cy="29" r="0.38" fill="#dce9ff" opacity="0.23"/><circle cx="1583" cy="443" r="0.60" fill="#dce9ff" opacity="0.12"/><circle cx="786" cy="262" r="0.60" fill="#dce9ff" opacity="0.21"/><circle cx="1442" cy="155" r="0.67" fill="#dce9ff" opacity="0.24"/><circle cx="1350" cy="808" r="0.59" fill="#dce9ff" opacity="0.18"/><circle cx="900" cy="477" r="0.53" fill="#dce9ff" opacity="0.11"/><circle cx="393" cy="235" r="0.54" fill="#dce9ff" opacity="0.21"/><circle cx="1363" cy="555" r="0.44" fill="#dce9ff" opacity="0.31"/><circle cx="881" cy="598" r="0.47" fill="#dce9ff" opacity="0.32"/><circle cx="703" cy="529" r="0.50" fill="#dce9ff" opacity="0.23"/><circle cx="406" cy="540" r="0.64" fill="#dce9ff" opacity="0.13"/><circle cx="1438" cy="100" r="0.73" fill="#dce9ff" opacity="0.15"/><circle cx="975" cy="487" r="0.40" fill="#dce9ff" opacity="0.12"/><circle cx="1586" cy="492" r="0.38" fill="#dce9ff" opacity="0.23"/><circle cx="991" cy="353" r="0.46" fill="#dce9ff" opacity="0.28"/><circle cx="1236" cy="246" r="0.66" fill="#dce9ff" opacity="0.25"/><circle cx="1143" cy="833" r="0.64" fill="#dce9ff" opacity="0.18"/><circle cx="985" cy="271" r="0.52" fill="#dce9ff" opacity="0.10"/><circle cx="752" cy="812" r="0.66" fill="#dce9ff" opacity="0.18"/><circle cx="1344" cy="822" r="0.56" fill="#dce9ff" opacity="0.26"/><circle cx="611" cy="839" r="0.49" fill="#dce9ff" opacity="0.25"/><circle cx="969" cy="608" r="0.62" fill="#dce9ff" opacity="0.19"/><circle cx="995" cy="836" r="0.74" fill="#dce9ff" opacity="0.22"/><circle cx="211" cy="104" r="0.54" fill="#dce9ff" opacity="0.23"/><circle cx="1571" cy="495" r="0.71" fill="#dce9ff" opacity="0.30"/><circle cx="182" cy="362" r="0.42" fill="#dce9ff" opacity="0.21"/><circle cx="1094" cy="150" r="0.70" fill="#dce9ff" opacity="0.24"/><circle cx="972" cy="405" r="0.39" fill="#dce9ff" opacity="0.21"/><circle cx="1322" cy="213" r="0.58" fill="#dce9ff" opacity="0.13"/><circle cx="1333" cy="473" r="0.41" fill="#dce9ff" opacity="0.23"/><circle cx="992" cy="745" r="0.49" fill="#dce9ff" opacity="0.22"/><circle cx="219" cy="609" r="0.41" fill="#dce9ff" opacity="0.10"/><circle cx="679" cy="634" r="0.69" fill="#dce9ff" opacity="0.28"/><circle cx="459" cy="240" r="0.54" fill="#dce9ff" opacity="0.26"/><circle cx="254" cy="707" r="0.49" fill="#dce9ff" opacity="0.18"/><circle cx="343" cy="136" r="0.37" fill="#dce9ff" opacity="0.29"/><circle cx="1401" cy="288" r="0.62" fill="#dce9ff" opacity="0.14"/><circle cx="1578" cy="830" r="0.75" fill="#dce9ff" opacity="0.15"/><circle cx="894" cy="663" r="0.55" fill="#dce9ff" opacity="0.30"/><circle cx="947" cy="369" r="0.55" fill="#dce9ff" opacity="0.28"/><circle cx="845" cy="194" r="0.50" fill="#dce9ff" opacity="0.31"/><circle cx="577" cy="119" r="0.63" fill="#dce9ff" opacity="0.28"/><circle cx="478" cy="60" r="0.70" fill="#dce9ff" opacity="0.30"/><circle cx="1444" cy="127" r="0.72" fill="#dce9ff" opacity="0.18"/><circle cx="169" cy="634" r="0.57" fill="#dce9ff" opacity="0.12"/><circle cx="788" cy="317" r="0.56" fill="#dce9ff" opacity="0.14"/><circle cx="225" cy="389" r="0.38" fill="#dce9ff" opacity="0.12"/><circle cx="693" cy="579" r="0.55" fill="#dce9ff" opacity="0.27"/><circle cx="454" cy="594" r="0.53" fill="#dce9ff" opacity="0.22"/><circle cx="918" cy="548" r="0.61" fill="#dce9ff" opacity="0.20"/><circle cx="322" cy="281" r="0.59" fill="#dce9ff" opacity="0.24"/><circle cx="1040" cy="838" r="0.75" fill="#dce9ff" opacity="0.22"/><circle cx="904" cy="692" r="0.63" fill="#dce9ff" opacity="0.11"/><circle cx="217" cy="351" r="0.49" fill="#dce9ff" opacity="0.21"/><circle cx="1219" cy="32" r="0.47" fill="#dce9ff" opacity="0.33"/><circle cx="1231" cy="615" r="0.63" fill="#dce9ff" opacity="0.16"/><circle cx="773" cy="186" r="0.51" fill="#dce9ff" opacity="0.25"/><circle cx="1337" cy="70" r="0.53" fill="#dce9ff" opacity="0.25"/><circle cx="582" cy="609" r="0.74" fill="#dce9ff" opacity="0.21"/><circle cx="740" cy="75" r="0.41" fill="#dce9ff" opacity="0.31"/><circle cx="1486" cy="554" r="0.42" fill="#dce9ff" opacity="0.15"/><circle cx="149" cy="337" r="0.68" fill="#dce9ff" opacity="0.21"/><circle cx="327" cy="28" r="0.36" fill="#dce9ff" opacity="0.27"/><circle cx="1316" cy="383" r="0.38" fill="#dce9ff" opacity="0.26"/><circle cx="87" cy="229" r="0.37" fill="#dce9ff" opacity="0.22"/><circle cx="638" cy="601" r="0.55" fill="#dce9ff" opacity="0.33"/><circle cx="600" cy="663" r="0.52" fill="#dce9ff" opacity="0.16"/><circle cx="595" cy="66" r="0.74" fill="#dce9ff" opacity="0.28"/><circle cx="1490" cy="380" r="0.59" fill="#dce9ff" opacity="0.12"/><circle cx="1065" cy="447" r="0.40" fill="#dce9ff" opacity="0.23"/><circle cx="990" cy="347" r="0.37" fill="#dce9ff" opacity="0.24"/><circle cx="154" cy="654" r="0.40" fill="#dce9ff" opacity="0.29"/><circle cx="1241" cy="149" r="0.58" fill="#dce9ff" opacity="0.24"/><circle cx="338" cy="399" r="0.54" fill="#dce9ff" opacity="0.18"/><circle cx="32" cy="462" r="0.61" fill="#dce9ff" opacity="0.24"/><circle cx="234" cy="659" r="0.61" fill="#dce9ff" opacity="0.20"/><circle cx="314" cy="41" r="0.62" fill="#dce9ff" opacity="0.32"/><circle cx="641" cy="359" r="0.58" fill="#dce9ff" opacity="0.28"/><circle cx="844" cy="536" r="0.61" fill="#dce9ff" opacity="0.27"/><circle cx="944" cy="672" r="0.41" fill="#dce9ff" opacity="0.16"/><circle cx="1582" cy="254" r="0.64" fill="#dce9ff" opacity="0.14"/><circle cx="86" cy="760" r="0.74" fill="#dce9ff" opacity="0.16"/><circle cx="534" cy="396" r="0.70" fill="#dce9ff" opacity="0.13"/><circle cx="131" cy="439" r="0.64" fill="#dce9ff" opacity="0.17"/><circle cx="1555" cy="789" r="0.68" fill="#dce9ff" opacity="0.26"/><circle cx="739" cy="261" r="0.60" fill="#dce9ff" opacity="0.21"/><circle cx="10" cy="505" r="0.55" fill="#dce9ff" opacity="0.18"/><circle cx="208" cy="670" r="0.61" fill="#dce9ff" opacity="0.11"/><circle cx="1161" cy="323" r="0.38" fill="#dce9ff" opacity="0.29"/><circle cx="829" cy="40" r="0.60" fill="#dce9ff" opacity="0.17"/><circle cx="1552" cy="130" r="0.71" fill="#dce9ff" opacity="0.24"/><circle cx="77" cy="623" r="0.47" fill="#dce9ff" opacity="0.11"/><circle cx="307" cy="834" r="0.73" fill="#dce9ff" opacity="0.15"/><circle cx="1351" cy="2" r="0.67" fill="#dce9ff" opacity="0.29"/><circle cx="314" cy="126" r="0.57" fill="#dce9ff" opacity="0.31"/><circle cx="1168" cy="692" r="0.73" fill="#dce9ff" opacity="0.13"/><circle cx="1405" cy="309" r="0.49" fill="#dce9ff" opacity="0.17"/><circle cx="1219" cy="60" r="0.45" fill="#dce9ff" opacity="0.14"/><circle cx="531" cy="395" r="0.72" fill="#dce9ff" opacity="0.11"/><circle cx="1474" cy="249" r="0.44" fill="#dce9ff" opacity="0.32"/><circle cx="1483" cy="241" r="0.53" fill="#dce9ff" opacity="0.24"/><circle cx="814" cy="105" r="0.42" fill="#dce9ff" opacity="0.30"/><circle cx="627" cy="318" r="0.73" fill="#dce9ff" opacity="0.11"/></g>
    <g class="layer mid"><circle class="tw" style="--d:5.6s;--t:1.6s" cx="78" cy="789" r="1.10" fill="#dce9ff" opacity="0.26"/><circle cx="188" cy="279" r="0.96" fill="#dce9ff" opacity="0.27"/><circle cx="1274" cy="733" r="1.13" fill="#dce9ff" opacity="0.51"/><circle cx="891" cy="879" r="1.03" fill="#dce9ff" opacity="0.46"/><circle cx="1476" cy="863" r="0.99" fill="#dce9ff" opacity="0.36"/><circle cx="1313" cy="43" r="0.98" fill="#dce9ff" opacity="0.33"/><circle class="tw" style="--d:3.3s;--t:5.5s" cx="1376" cy="77" r="1.09" fill="#dce9ff" opacity="0.39"/><circle cx="202" cy="741" r="0.82" fill="#dce9ff" opacity="0.24"/><circle cx="79" cy="407" r="0.93" fill="#dce9ff" opacity="0.32"/><circle cx="1563" cy="287" r="0.97" fill="#dce9ff" opacity="0.31"/><circle cx="1431" cy="430" r="0.71" fill="#dce9ff" opacity="0.23"/><circle cx="624" cy="378" r="0.61" fill="#dce9ff" opacity="0.32"/><circle class="tw" style="--d:3.6s;--t:0.5s" cx="395" cy="738" r="0.83" fill="#dce9ff" opacity="0.24"/><circle cx="247" cy="640" r="0.67" fill="#dce9ff" opacity="0.28"/><circle cx="1521" cy="178" r="1.03" fill="#dce9ff" opacity="0.28"/><circle cx="720" cy="400" r="0.96" fill="#dce9ff" opacity="0.50"/><circle cx="1432" cy="474" r="1.03" fill="#dce9ff" opacity="0.23"/><circle cx="415" cy="392" r="1.02" fill="#dce9ff" opacity="0.35"/><circle class="tw" style="--d:2.8s;--t:5.4s" cx="151" cy="113" r="0.82" fill="#dce9ff" opacity="0.50"/><circle cx="1349" cy="78" r="1.10" fill="#dce9ff" opacity="0.30"/><circle cx="260" cy="148" r="1.12" fill="#dce9ff" opacity="0.42"/><circle cx="845" cy="143" r="0.60" fill="#dce9ff" opacity="0.31"/><circle cx="777" cy="471" r="1.03" fill="#dce9ff" opacity="0.32"/><circle cx="1465" cy="385" r="0.86" fill="#dce9ff" opacity="0.41"/><circle class="tw" style="--d:3.3s;--t:0.6s" cx="1496" cy="401" r="0.97" fill="#dce9ff" opacity="0.37"/><circle cx="991" cy="529" r="0.77" fill="#dce9ff" opacity="0.28"/><circle cx="961" cy="285" r="0.97" fill="#dce9ff" opacity="0.30"/><circle cx="1274" cy="60" r="0.84" fill="#dce9ff" opacity="0.50"/><circle cx="75" cy="739" r="0.94" fill="#dce9ff" opacity="0.32"/><circle cx="581" cy="201" r="0.95" fill="#dce9ff" opacity="0.28"/><circle class="tw" style="--d:4.4s;--t:0.9s" cx="1546" cy="672" r="0.80" fill="#dce9ff" opacity="0.41"/><circle cx="797" cy="42" r="0.80" fill="#dce9ff" opacity="0.44"/><circle cx="1159" cy="522" r="0.95" fill="#dce9ff" opacity="0.23"/><circle cx="886" cy="150" r="1.11" fill="#dce9ff" opacity="0.41"/><circle cx="574" cy="242" r="0.75" fill="#dce9ff" opacity="0.47"/><circle cx="1446" cy="598" r="1.04" fill="#dce9ff" opacity="0.46"/><circle class="tw" style="--d:3.5s;--t:3.9s" cx="1540" cy="402" r="0.75" fill="#dce9ff" opacity="0.24"/><circle cx="131" cy="253" r="0.71" fill="#dce9ff" opacity="0.36"/><circle cx="375" cy="817" r="1.04" fill="#dce9ff" opacity="0.22"/><circle cx="138" cy="785" r="0.96" fill="#dce9ff" opacity="0.43"/><circle cx="1221" cy="341" r="0.87" fill="#dce9ff" opacity="0.41"/><circle cx="12" cy="352" r="0.87" fill="#dce9ff" opacity="0.47"/><circle class="tw" style="--d:4.1s;--t:2.3s" cx="1302" cy="58" r="1.04" fill="#dce9ff" opacity="0.43"/><circle cx="649" cy="798" r="0.90" fill="#dce9ff" opacity="0.43"/><circle cx="1144" cy="861" r="0.61" fill="#dce9ff" opacity="0.35"/><circle cx="333" cy="413" r="0.79" fill="#dce9ff" opacity="0.42"/><circle cx="1426" cy="422" r="0.76" fill="#dce9ff" opacity="0.28"/><circle cx="1126" cy="684" r="0.73" fill="#dce9ff" opacity="0.33"/><circle class="tw" style="--d:4.1s;--t:3.2s" cx="649" cy="215" r="0.87" fill="#dce9ff" opacity="0.24"/><circle cx="63" cy="725" r="1.14" fill="#dce9ff" opacity="0.26"/><circle cx="233" cy="138" r="0.95" fill="#dce9ff" opacity="0.39"/><circle cx="1070" cy="549" r="0.82" fill="#dce9ff" opacity="0.43"/><circle cx="334" cy="587" r="1.01" fill="#dce9ff" opacity="0.24"/><circle cx="564" cy="281" r="1.03" fill="#dce9ff" opacity="0.46"/><circle class="tw" style="--d:3.4s;--t:4.8s" cx="445" cy="172" r="0.62" fill="#dce9ff" opacity="0.24"/><circle cx="28" cy="544" r="0.71" fill="#dce9ff" opacity="0.39"/><circle cx="856" cy="63" r="0.79" fill="#dce9ff" opacity="0.36"/><circle cx="629" cy="426" r="1.06" fill="#dce9ff" opacity="0.37"/><circle cx="1371" cy="451" r="0.98" fill="#dce9ff" opacity="0.33"/><circle cx="351" cy="187" r="1.14" fill="#dce9ff" opacity="0.29"/><circle class="tw" style="--d:4.1s;--t:5.9s" cx="145" cy="338" r="1.04" fill="#dce9ff" opacity="0.39"/><circle cx="838" cy="787" r="0.81" fill="#dce9ff" opacity="0.22"/><circle cx="1043" cy="174" r="0.75" fill="#dce9ff" opacity="0.50"/><circle cx="316" cy="814" r="0.75" fill="#dce9ff" opacity="0.32"/><circle cx="200" cy="229" r="0.92" fill="#dce9ff" opacity="0.49"/><circle cx="717" cy="760" r="0.74" fill="#dce9ff" opacity="0.46"/><circle class="tw" style="--d:6.5s;--t:3.6s" cx="486" cy="522" r="0.84" fill="#dce9ff" opacity="0.44"/><circle cx="468" cy="456" r="0.91" fill="#dce9ff" opacity="0.41"/><circle cx="806" cy="425" r="0.65" fill="#dce9ff" opacity="0.51"/><circle cx="1126" cy="43" r="0.73" fill="#dce9ff" opacity="0.39"/><circle cx="1025" cy="94" r="0.60" fill="#dce9ff" opacity="0.24"/><circle cx="563" cy="59" r="1.09" fill="#dce9ff" opacity="0.48"/><circle class="tw" style="--d:5.3s;--t:3.6s" cx="295" cy="738" r="1.12" fill="#dce9ff" opacity="0.27"/><circle cx="1012" cy="820" r="1.11" fill="#dce9ff" opacity="0.30"/><circle cx="1366" cy="418" r="0.86" fill="#dce9ff" opacity="0.36"/><circle cx="604" cy="203" r="0.81" fill="#dce9ff" opacity="0.32"/><circle cx="1346" cy="386" r="1.04" fill="#dce9ff" opacity="0.41"/><circle cx="132" cy="240" r="1.02" fill="#dce9ff" opacity="0.49"/><circle class="tw" style="--d:6.2s;--t:0.6s" cx="442" cy="565" r="0.64" fill="#dce9ff" opacity="0.39"/><circle cx="1144" cy="630" r="0.75" fill="#dce9ff" opacity="0.40"/><circle cx="782" cy="627" r="0.75" fill="#dce9ff" opacity="0.50"/><circle cx="1025" cy="507" r="0.93" fill="#dce9ff" opacity="0.46"/><circle cx="481" cy="604" r="0.65" fill="#dce9ff" opacity="0.50"/><circle cx="1251" cy="235" r="1.10" fill="#dce9ff" opacity="0.44"/><circle class="tw" style="--d:5.8s;--t:5.5s" cx="378" cy="592" r="1.01" fill="#dce9ff" opacity="0.25"/><circle cx="1115" cy="855" r="1.08" fill="#dce9ff" opacity="0.26"/><circle cx="1045" cy="179" r="0.66" fill="#dce9ff" opacity="0.35"/><circle cx="1048" cy="97" r="0.77" fill="#dce9ff" opacity="0.46"/><circle cx="350" cy="313" r="0.88" fill="#dce9ff" opacity="0.25"/><circle cx="1054" cy="659" r="0.91" fill="#dce9ff" opacity="0.43"/></g>
    <g class="layer near"><circle class="tw" style="--d:3.4s;--t:4.7s" cx="614" cy="829" r="1.07" fill="#dce9ff" opacity="0.37"/><circle cx="974" cy="179" r="1.54" fill="#dce9ff" opacity="0.73"/><circle cx="718" cy="19" r="1.52" fill="#dce9ff" opacity="0.35"/><circle class="tw" style="--d:3.3s;--t:0.0s" cx="907" cy="434" r="1.09" fill="#dce9ff" opacity="0.34"/><circle cx="435" cy="235" r="1.60" fill="#dce9ff" opacity="0.69"/><circle cx="618" cy="710" r="1.09" fill="#dce9ff" opacity="0.47"/><circle class="tw" style="--d:3.1s;--t:2.6s" cx="423" cy="77" r="1.21" fill="#dce9ff" opacity="0.46"/><circle cx="718" cy="695" r="1.38" fill="#dce9ff" opacity="0.54"/><circle cx="1586" cy="102" r="1.20" fill="#dce9ff" opacity="0.40"/><circle class="tw" style="--d:4.6s;--t:3.1s" cx="761" cy="726" r="1.67" fill="#dce9ff" opacity="0.60"/><circle cx="244" cy="548" r="1.64" fill="#dce9ff" opacity="0.65"/><circle cx="1001" cy="537" r="1.04" fill="#dce9ff" opacity="0.49"/><circle class="tw" style="--d:4.5s;--t:1.1s" cx="1579" cy="245" r="1.68" fill="#dce9ff" opacity="0.43"/><circle cx="331" cy="306" r="1.44" fill="#dce9ff" opacity="0.62"/><circle cx="1188" cy="357" r="1.37" fill="#dce9ff" opacity="0.45"/><circle class="tw" style="--d:5.1s;--t:1.2s" cx="1399" cy="574" r="1.65" fill="#dce9ff" opacity="0.57"/><circle cx="404" cy="325" r="1.50" fill="#dce9ff" opacity="0.67"/><circle cx="1139" cy="808" r="1.26" fill="#dce9ff" opacity="0.66"/><circle class="tw" style="--d:4.3s;--t:0.9s" cx="641" cy="304" r="1.14" fill="#dce9ff" opacity="0.44"/><circle cx="8" cy="627" r="1.22" fill="#dce9ff" opacity="0.62"/><circle cx="614" cy="424" r="1.52" fill="#dce9ff" opacity="0.52"/><circle class="tw" style="--d:5.5s;--t:3.9s" cx="1373" cy="396" r="1.16" fill="#dce9ff" opacity="0.45"/><circle cx="876" cy="478" r="1.41" fill="#dce9ff" opacity="0.61"/><circle cx="774" cy="141" r="1.04" fill="#dce9ff" opacity="0.56"/><circle class="tw" style="--d:6.8s;--t:4.7s" cx="1348" cy="422" r="1.31" fill="#dce9ff" opacity="0.59"/><circle cx="398" cy="637" r="1.32" fill="#dce9ff" opacity="0.44"/><circle cx="150" cy="864" r="1.52" fill="#dce9ff" opacity="0.63"/><circle class="tw" style="--d:5.0s;--t:0.3s" cx="1240" cy="501" r="1.31" fill="#dce9ff" opacity="0.61"/><circle cx="1216" cy="520" r="1.33" fill="#dce9ff" opacity="0.48"/><circle cx="1048" cy="673" r="1.56" fill="#dce9ff" opacity="0.76"/><circle class="tw" style="--d:2.7s;--t:2.6s" cx="1540" cy="122" r="1.41" fill="#dce9ff" opacity="0.51"/><circle cx="88" cy="57" r="1.05" fill="#dce9ff" opacity="0.53"/><circle cx="908" cy="3" r="1.15" fill="#dce9ff" opacity="0.39"/><circle class="tw" style="--d:7.2s;--t:2.8s" cx="667" cy="696" r="1.43" fill="#dce9ff" opacity="0.73"/></g>

    <!-- The system. Orbits share one centre and one tilt, which is what makes it
         read as a system rather than as scattered circles. -->
    <g transform="translate(1372 838) rotate(-15)">
      <circle cx="0" cy="0" r="230" fill="url(#sunglow)"/>
      <circle cx="0" cy="0" r="13" fill="#d9fbf3" opacity=".42"/>

      <g transform="scale(1 0.29)">
        <circle class="path" r="290"/>
        <g class="rev" style="--dur:74s"><g transform="translate(290 0)">
          <g transform="scale(1 3.448)"><circle r="6" fill="url(#pl1)" opacity=".8"/></g></g></g>
      </g>
      <g transform="scale(1 0.29)">
        <circle class="path" r="455"/>
        <g class="rev" style="--dur:118s"><g transform="translate(455 0)">
          <g transform="scale(1 3.448)"><circle r="8.5" fill="url(#pl2)" opacity=".76"/></g></g></g>
      </g>
      <g transform="scale(1 0.29)">
        <circle class="path" r="680"/>
        <g class="rev" style="--dur:186s"><g transform="translate(680 0)">
          <g transform="scale(1 3.448)">
            <circle r="13" fill="url(#pl4)" opacity=".72"/>
            <ellipse rx="23" ry="6.2" fill="none" stroke="#9fd8cf" stroke-width="1.6"
              opacity=".48" transform="rotate(-14)"/>
          </g></g></g>
      </g>
      <g transform="scale(1 0.29)">
        <circle class="path dim" r="940"/>
        <g class="rev" style="--dur:268s"><g transform="translate(940 0)">
          <g transform="scale(1 3.448)"><circle r="7" fill="url(#pl3)" opacity=".6"/></g></g></g>
      </g>
    </g>

    <rect width="1600" height="900" fill="url(#scrim)"/>

    <g class="comet a"><g transform="rotate(24)">
      <path d="M0 0 L170 0" stroke="url(#tail)" stroke-width="1.6" stroke-linecap="round"/>
      <circle cx="170" cy="0" r="1.7" fill="#ffffff"/>
    </g></g>
    <g class="comet b"><g transform="rotate(31)">
      <path d="M0 0 L120 0" stroke="url(#tail)" stroke-width="1.2" stroke-linecap="round"/>
      <circle cx="120" cy="0" r="1.3" fill="#ffffff"/>
    </g></g>
  </svg>
</div>
<button class="burger" id="burger" aria-label="Toggle navigation">☰</button>
<nav class="sidebar" id="sidebar">
  <div class="brand">
    <div class="who">
      <h1>__CATALOG__</h1>
      <div class="sub">scan __SCAN__ · __GENERATED__</div>
    </div>
    <div class="lang" id="lang">
      <button data-lang="en" class="on">EN</button><button data-lang="pt">PT</button>
    </div>
  </div>
  <div class="search"><input id="q" type="search" placeholder="Search tables and columns" autocomplete="off"></div>
  <div class="nav" id="nav"></div>
  <div class="sidefoot"><span id="genby">Generated by</span>
    <a href="__REPO__" target="_blank" rel="noopener">lakehouse-governance-agent</a></div>
</nav>
<main class="main" id="main"><div class="wrap" id="content"></div></main>
<script id="payload" type="application/json">__DATA__</script>
<script id="docs" type="application/json">__DOCS__</script>
<script>
const DATA = JSON.parse(document.getElementById('payload').textContent);
const DOCS = JSON.parse(document.getElementById('docs').textContent);
const nav = document.getElementById('nav'), content = document.getElementById('content');
let current = {view: 'overview'}, filter = '';

// Both languages come out of the same model call. Only English reaches Unity
// Catalog, which holds one COMMENT per object; Portuguese exists here.
let lang = (() => { try { return localStorage.getItem('lang') || 'en'; } catch { return 'en'; } })();
const STR = {
  en: {
    overview: 'Overview', sources: 'Source documentation', search: 'Search tables and columns',
    title: 'Catalog documentation',
    lede: 'Every description here was generated from the data <em>and</em> from the documentation of the system each table came from. Open a table to see which passages produced its descriptions.',
    score: 'catalog health score', scoreSub: ',<br>before and after one run',
    coverage: 'Coverage by schema', findings: 'Findings',
    findingsHint: n => `${n} open, ranked by what they oblige someone to do`,
    documented: 'columns documented', piiCols: 'personal-data columns',
    neverRead: 'never read', deadCols: 'dead columns', fragmented: 'fragmented tables',
    fragmentedOne: 'fragmented', neverReadOne: 'never read',
    colCol: 'Column', colType: 'Type', colDesc: 'Description', colProfile: 'Profile',
    notDocumented: 'not documented', rows: 'rows', columns: 'columns', files: 'files',
    lastRead: 'last read', groundedIn: 'Grounded in', noDocs: 'No source documentation mapped to this table.',
    sourceDoc: 'source documentation', distinct: 'distinct', nullPct: 'null',
    note: 'A dot beside a table marks personal data (§PII§) or a table nothing has read (§ORPHAN§). The score stops short of 100 on purpose: what remains are not documentation problems but decisions a person has to make. Snapshot of scan §SCAN§, not a live view.',
    generatedBy: 'Generated by',
    askTitle: 'Ask the catalog',
    askSub: 'Search every generated description and every source document on this page.',
    askPlaceholder: 'What is tAcw? Which columns hold personal data?',
    askGo: 'Ask', askNone: 'Nothing matched. Try a column name or a word from a description.',
    askNav: 'Ask',
    missionLede: 'Every table and column in this catalog was described by a model, grounded in the documentation of the system it came from. Ask about any of it.',
    mTables: 'tables',
    mColumns: 'columns',
    mDocs: 'source documents',
    mPii: 'carrying personal data',
    sceneAlt: 'A small astronaut walking on a moon.',
    tileGrounds: 'grounds',
    tileUnused: 'no table mapped to it',
    mTable1: 'table',
    tileHint: 'the documentation each description was grounded in',
    tilePii: 'columns a masking policy has to cover',
    tileOrphan: 'tables nothing has queried',
    tileDead: 'always null, or one value on every row',
    askDocs: 'Also in:',
    chipPii: 'personal data', chipOrphan: 'never read', chipDead: 'dead columns',
    askFacetNote: 'A filter over what the crawler recorded, not a search.',
    askNote: 'Lexical search over this page, running in your browser. Connecting a Databricks Genie Agent here would answer in natural language over the same descriptions -- that is the next step, and this is not pretending to be it.',
    runline: r => `<strong>${r.comments} comments</strong> written to Unity Catalog across ${r.tables} tables in <strong>${r.seconds}s</strong>, using <strong>${(r.promptTokens + r.completionTokens).toLocaleString()}</strong> tokens — <strong>$${r.cost.toFixed(4)}</strong> at list price. Modelled, not billed: the run was made on a free tier that charges nothing.`,
    bilingual: t => `Descriptions are generated in English and Portuguese in the same model call: <strong>$${t.cost.toFixed(4)}</strong> for both, against $0.0048 for English alone.`,
    sev: {high: 'high', medium: 'medium', low: 'low'},
  },
  pt: {
    overview: 'Visão geral', sources: 'Documentação de origem', search: 'Buscar tabelas e colunas',
    title: 'Documentação do catálogo',
    lede: 'Cada descrição aqui foi gerada a partir do dado <em>e</em> da documentação do sistema de origem de cada tabela. Abra uma tabela para ver quais trechos produziram suas descrições.',
    score: 'índice de saúde do catálogo', scoreSub: ',<br>antes e depois de uma execução',
    coverage: 'Cobertura por schema', findings: 'Achados',
    findingsHint: n => `${n} em aberto, ordenados pelo que exigem de alguém`,
    documented: 'colunas documentadas', piiCols: 'colunas com dado pessoal',
    neverRead: 'nunca lidas', deadCols: 'colunas mortas', fragmented: 'tabelas fragmentadas',
    fragmentedOne: 'fragmentada', neverReadOne: 'nunca lida',
    colCol: 'Coluna', colType: 'Tipo', colDesc: 'Descrição', colProfile: 'Perfil',
    notDocumented: 'sem documentação', rows: 'linhas', columns: 'colunas', files: 'arquivos',
    lastRead: 'última leitura', groundedIn: 'Ancorada em', noDocs: 'Nenhuma documentação de origem mapeada para esta tabela.',
    sourceDoc: 'documentação de origem', distinct: 'distintos', nullPct: 'nulos',
    note: 'Um ponto ao lado da tabela indica dado pessoal (§PII§) ou tabela que ninguém leu (§ORPHAN§). O índice para antes de 100 de propósito: o que resta não são problemas de documentação, e sim decisões que cabem a uma pessoa. Retrato do scan §SCAN§, não uma visão ao vivo.',
    generatedBy: 'Gerado por',
    askTitle: 'Pergunte ao catálogo',
    askSub: 'Busca em todas as descrições geradas e em toda a documentação desta página.',
    askPlaceholder: 'O que é tAcw? Quais colunas têm dado pessoal?',
    askGo: 'Perguntar', askNone: 'Nada encontrado. Tente o nome de uma coluna ou uma palavra de alguma descrição.',
    askNav: 'Perguntar',
    missionLede: 'Cada tabela e coluna deste catálogo foi descrita por um modelo, ancorada na documentação do sistema de origem. Pergunte sobre qualquer uma.',
    mTables: 'tabelas',
    mColumns: 'colunas',
    mDocs: 'documentos de origem',
    mPii: 'com dado pessoal',
    askDocs: 'Também em:',
    chipPii: 'dado pessoal', chipOrphan: 'nunca lidas', chipDead: 'colunas mortas',
    askFacetNote: 'Um filtro sobre o que o crawler registrou, não uma busca.',
    askNote: 'Busca léxica sobre esta página, rodando no seu navegador. Conectar um Genie Agent do Databricks aqui responderia em linguagem natural sobre as mesmas descrições -- esse é o próximo passo, e isto não está fingindo ser ele.',
    runline: r => `<strong>${r.comments} comentários</strong> escritos no Unity Catalog em ${r.tables} tabelas em <strong>${r.seconds}s</strong>, usando <strong>${(r.promptTokens + r.completionTokens).toLocaleString()}</strong> tokens — <strong>$${r.cost.toFixed(4)}</strong> a preço de tabela. Modelado, não faturado: a execução foi feita em plano gratuito, que não cobra.`,
    bilingual: t => `As descrições são geradas em inglês e português na mesma chamada ao modelo: <strong>$${t.cost.toFixed(4)}</strong> pelas duas, contra $0,0048 só em inglês.`,
    sev: {high: 'alta', medium: 'média', low: 'baixa'},
  },
};
const T = () => STR[lang];
const desc = o => (lang === 'pt' && o.commentPt) ? o.commentPt : o.comment;

const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const bytes = n => {
  if (!n) return '-';
  const u = ['B','KB','MB','GB','TB']; let i = 0, v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return v.toFixed(1) + ' ' + u[i];
};

function matches(t) {
  if (!filter) return true;
  const f = filter.toLowerCase();
  return t.key.toLowerCase().includes(f) || t.columns.some(c => c.name.toLowerCase().includes(f));
}

function renderNav() {
  const schemas = {};
  DATA.tables.filter(matches).forEach(t => (schemas[t.schema] ??= []).push(t));
  let html = `<div class="group">
    <button class="item ${current.view === 'ask' ? 'active' : ''}" data-view="ask">
      <span class="hash">✦</span> ${T().askNav}</button>
    <button class="item ${current.view === 'overview' ? 'active' : ''}" data-view="overview">
      <span class="hash">≡</span> ${T().overview}</button></div>`;
  for (const [schema, tables] of Object.entries(schemas)) {
    html += `<div class="group"><div class="group-label">${esc(schema)}</div>`;
    tables.forEach(t => {
      const active = current.view === 'table' && current.key === t.key;
      html += `<button class="item ${active ? 'active' : ''}" data-view="table" data-key="${esc(t.key)}">
        <span class="hash">#</span>${esc(t.name)}<span class="flags">
        ${t.piiCount ? '<span class="dot pii" title="personal data"></span>' : ''}
        ${t.orphan ? '<span class="dot orphan" title="never read"></span>' : ''}</span></button>`;
    });
    html += '</div>';
  }
  if (DOCS.length) {
    html += `<div class="group"><div class="group-label">${T().sources}</div>`;
    DOCS.forEach(d => {
      const active = current.view === 'doc' && current.key === d.id;
      html += `<button class="item ${active ? 'active' : ''}" data-view="doc" data-key="${esc(d.id)}">
        <span class="hash">§</span>${esc(d.id)}</button>`;
    });
    html += '</div>';
  }
  nav.innerHTML = html;
}

// --- the mascot and the ask panel ------------------------------------------
// Drawn inline rather than loaded: one file with no external requests is the whole
// point of how this page is built.
// Local lexical search over what the page already carries. This is the seam: swap
// `askLocally` for a call to a Databricks Genie Agent and the surface around it is
// unchanged. It is labelled as search, not as an assistant, because that is what it
// currently is.
function askLocally(query) {
  // Term frequency alone is useless here. Asking "which columns hold personal data"
  // returned every Portuguese date column, because `data` is a date in one language
  // and the query word in the other, and a substring match cannot tell them apart.
  // Weighting by how rare a term is across the catalog demotes `data` on its own and
  // lets `personal` carry the query.
  const STOP = new Set(['the','and','for','что','which','what','does','hold','holds',
    'have','has','are','is','of','in','on','a','an','to','with','que','qual','quais',
    'como','onde','tem','tem','sao','são','é','e','de','da','do','das','dos','um','uma']);
  const terms = query.toLowerCase().split(/[^a-z0-9_À-ſ]+/i)
    .filter(w => w.length > 2 && !STOP.has(w));
  if (!terms.length) return {columns: [], docs: []};

  const fields = [];
  DATA.tables.forEach(t => t.columns.forEach(c => fields.push({
    table: t.key, column: c.name, text: desc(c) || '',
    hay: `${t.key} ${c.name} ${(desc(c) || '')} ${(c.piiLabels || []).join(' ')}`.toLowerCase(),
    // Kept separate and scored lower: a term that only appears in the table's own
    // description is true of every column in it, so it must never outrank a column
    // that matches on its own text. Folding it into `hay` made a search for
    // "milliseconds" return three identifier columns.
    tableHay: `${(desc(t) || '')}`.toLowerCase(),
    name: c.name.toLowerCase(),
    pii: c.pii,
  })));

  const idf = {};
  terms.forEach(w => {
    const seen = fields.filter(f => f.hay.includes(w)).length;
    idf[w] = Math.log((fields.length + 1) / (seen + 1)) + 1;
  });

  const columns = [];
  fields.forEach(f => {
    let score = 0, matched = 0;
    terms.forEach(w => {
      if (f.name.includes(w)) { score += idf[w] * 2; matched++; }
      else if (f.hay.includes(w)) { score += idf[w]; matched++; }
      else if (f.tableHay.includes(w)) { score += idf[w] * 0.25; matched++; }
    });
    // Coverage matters more than any single strong hit: a row that answers half the
    // question is not half as good as one that answers all of it. Without this, one
    // common term in a column name outranks a row that matches every term.
    score *= matched / terms.length;
    if (score > 0.9) columns.push({score, table: f.table, column: f.column, text: f.text});
  });
  columns.sort((a, b) => b.score - a.score);

  const docs = [];
  DOCS.forEach(d => {
    const plain = d.html.replace(/<[^>]+>/g, ' ').toLowerCase();
    const score = terms.reduce((n, w) => n + (plain.includes(w) ? (idf[w] || 1) : 0), 0);
    if (score) docs.push({score, id: d.id, title: d.title});
  });
  docs.sort((a, b) => b.score - a.score);

  return {columns: columns.slice(0, 6), docs: docs.slice(0, 2)};
}

// Facets, not search. "Which columns hold personal data" cannot be answered by
// matching words -- no description contains the phrase; they say CPF, or full name.
// A filter over a flag the crawler already set is honest; a synonym table pretending
// to understand the question is not. This is the gap a Genie Agent would close.
const FACETS = {
  pii: t => t.columns.filter(c => c.pii).map(c => ({table: t.key, column: c.name, text: desc(c) || ''})),
  dead: t => t.columns.filter(c => c.allNull || c.constant)
    .map(c => ({table: t.key, column: c.name, text: c.allNull ? '100% null' : 'single repeated value'})),
  orphan: t => t.orphan ? [{table: t.key, column: '', text: desc(t) || ''}] : [],
};

function renderFacet(name) {
  const box = document.getElementById('answer');
  showingAnswer(true);
  const rows = DATA.tables.flatMap(FACETS[name]);
  box.innerHTML = `<div class="answer">${rows.map(h => `<div class="hit">
      <span class="where" data-go="${esc(h.table)}">${esc(h.table)}${h.column ? '.' + esc(h.column) : ''}</span>
      <div class="what">${esc(h.text)}</div></div>`).join('')}
    <div class="note">${T().askFacetNote}</div></div>`;
}

function showingAnswer(on) {
  const browse = document.getElementById('browse');
  if (browse) browse.hidden = on;
}

function renderAnswer(query) {
  const box = document.getElementById('answer');
  if (!box) return;
  showingAnswer(true);
  const {columns, docs} = askLocally(query);
  if (!columns.length && !docs.length) {
    box.innerHTML = `<div class="answer"><div class="none">${T().askNone}</div></div>`;
    return;
  }
  const hits = columns.map(h => `<div class="hit">
      <span class="where" data-go="${esc(h.table)}">${esc(h.table)}.${esc(h.column)}</span>
      <div class="what">${esc(h.text)}</div></div>`).join('');
  const sources = docs.map(d =>
    `<span class="where" data-doc="${esc(d.id)}">§ ${esc(d.id)}</span>`).join(' · ');
  box.innerHTML = `<div class="answer">${hits}
    ${sources ? `<div class="note">${T().askDocs} ${sources}</div>` : ''}
    <div class="note">${T().askNote}</div></div>`;
}

// The moon scene. Every part that has to move is its own group with its own pivot:
// a sprite that only translates reads as sliding, and the difference between that
// and a walk lives entirely in the hips and shoulders.
function moonScene() {
  const MX = 240, MY = 190, MR = 64;   // moon centre and radius, in view units
  const craters = [
    [-29, -39, 6.5], [21, -46, 4.5], [44, -17, 8], [-48, -9, 5],
    [4, -58, 3.5], [-13, -52, 2.6], [33, 11, 6], [-36, 17, 4],
  ].map(([dx, dy, r]) =>
    `<circle cx="${MX + dx}" cy="${MY + dy}" r="${r}" fill="#0f1319" opacity=".55"/>
     <circle cx="${MX + dx - r * .18}" cy="${MY + dy - r * .18}" r="${r * .82}" fill="#20262f" opacity=".7"/>`
  ).join('');

  return `<div class="scene" id="scene">
    <div class="corona"></div>
    <svg viewBox="90 0 300 270" role="img" aria-label="${T().sceneAlt}">
      <defs>
        <linearGradient id="suit" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stop-color="#ffffff"/><stop offset="1" stop-color="#ccd3e0"/>
        </linearGradient>
        <linearGradient id="glass" x1="0" y1="0" x2="0.7" y2="1">
          <stop offset="0" stop-color="#cfeef0" stop-opacity=".34"/>
          <stop offset="45%" stop-color="#8fd8d6" stop-opacity=".13"/>
          <stop offset="100%" stop-color="#5eead4" stop-opacity=".2"/>
        </linearGradient>
        <radialGradient id="moonface" cx="36%" cy="24%">
          <stop offset="0" stop-color="#3a4250"/><stop offset="1" stop-color="#171b22"/>
        </radialGradient>
      </defs>

      <g id="moonspin">
        <circle cx="${MX}" cy="${MY}" r="${MR}" fill="url(#moonface)"/>
        ${craters}
      </g>
      <ellipse cx="${MX}" cy="${MY - MR + 3}" rx="26" ry="4" fill="#000" opacity=".3"/>

      <g transform="translate(${MX} ${MY - MR + 1})">
        <circle class="dust" cx="-12" cy="-1.5" r="1.7" fill="#8b929e" style="--dx:-8px"/>
        <circle class="dust b" cx="11" cy="-1.5" r="1.4" fill="#8b929e" style="--dx:8px"/>

        <g class="rig" id="rig">
          <g transform="translate(-7.5 -13)"><g class="limb leg-l">
            <rect x="-5" y="0" width="10" height="14" rx="3.6" fill="url(#suit)"/>
            <rect x="-2.6" y="1.5" width="1.6" height="7" rx=".8" fill="#5eead4" opacity=".5"/>
            <rect x="-5.6" y="9.5" width="11.2" height="5" rx="2.2" fill="#e8ecf4"/>
          </g></g>
          <g transform="translate(7.5 -13)"><g class="limb leg-r">
            <rect x="-5" y="0" width="10" height="14" rx="3.6" fill="url(#suit)"/>
            <rect x="1" y="1.5" width="1.6" height="7" rx=".8" fill="#5eead4" opacity=".5"/>
            <rect x="-5.6" y="9.5" width="11.2" height="5" rx="2.2" fill="#e8ecf4"/>
          </g></g>

          <g transform="translate(-14 -33)"><g class="limb arm-l">
            <rect x="-4.3" y="0" width="8.6" height="16" rx="3.4" fill="url(#suit)"/>
            <rect x="-4.3" y="3.4" width="8.6" height="1.6" fill="#5eead4" opacity=".55"/>
            <rect x="-4" y="14" width="8" height="6" rx="3" fill="#dfe4ee"/>
          </g></g>
          <g transform="translate(14 -33)"><g class="limb arm-r">
            <rect x="-4.3" y="0" width="8.6" height="16" rx="3.4" fill="url(#suit)"/>
            <rect x="-4.3" y="3.4" width="8.6" height="1.6" fill="#5eead4" opacity=".55"/>
            <rect x="-4" y="14" width="8" height="6" rx="3" fill="#dfe4ee"/>
          </g></g>

          <rect x="-13.5" y="-35" width="27" height="24" rx="6" fill="url(#suit)"/>
          <rect x="-13.5" y="-33.5" width="27" height="2.2" fill="#5eead4" opacity=".55"/>
          <circle cx="0" cy="-26" r="4" fill="#dfe4ee"/>
          <circle cx="0" cy="-26" r="2.1" fill="#5eead4" opacity=".8"/>
          <rect x="-10.5" y="-21.5" width="7" height="4.6" rx="1.5" fill="#5eead4" opacity=".45"/>

          <g class="head" id="head">
            <rect x="-9" y="-39" width="18" height="8" rx="3" fill="#e8ecf4"/>
            <rect x="-25" y="-86" width="50" height="49" rx="15" fill="#f0c8a4"/>
            <ellipse cx="-25" cy="-62" rx="2.4" ry="3.6" fill="#e6b993"/>
            <ellipse cx="25" cy="-62" rx="2.4" ry="3.6" fill="#e6b993"/>
            <path d="M-25 -68c0-14 4-24 25-24 20 0 25 9 25 22 0-4-2-8-6-10
                     -5-3-9 1-15 1-7 0-11-4-18-2-7 2-11 7-11 13z" fill="#463b2f"/>
            <path d="M-24 -70c2-8 8-14 17-15 4 0 7 1 9 3-9 1-19 5-26 12z"
                  fill="#584a3b" opacity=".85"/>
            <ellipse cx="-9.5" cy="-61" rx="5.8" ry="7.2" fill="#0c0c0c"/>
            <ellipse cx="9.5" cy="-61" rx="5.8" ry="7.2" fill="#0c0c0c"/>
            <path d="M0 -56l2.6 6h-5.2z" fill="#e0b78e"/>
          </g>

          <circle cx="0" cy="-60" r="35" fill="url(#glass)"/>
          <circle cx="0" cy="-60" r="35" fill="none" stroke="#e4ebf6" stroke-width="2.6" opacity=".62"/>
          <circle cx="0" cy="-60" r="31.5" fill="none" stroke="#ffffff" stroke-width="1" opacity=".16"/>
          <path d="M-25 -74a31 31 0 0 1 20-14" stroke="#ffffff" stroke-width="4.5"
            stroke-linecap="round" fill="none" opacity=".38"/>
          <path d="M20 -44a31 31 0 0 0 9-13" stroke="#ffffff" stroke-width="2.6"
            stroke-linecap="round" fill="none" opacity=".16"/>
          <rect x="-17" y="-40" width="34" height="10" rx="5" fill="#e8ecf4"/>
          <rect x="-17" y="-36.6" width="34" height="2" fill="#5eead4" opacity=".5"/>

          <g transform="translate(38 -84)"><g class="bubble" id="bubble">
            <rect x="-15" y="-15" width="30" height="26" rx="9" fill="#1c2027" stroke="#2f3540"/>
            <path d="M-6 11l6 8 6-8z" fill="#1c2027"/>
            <text id="bubbletext" x="0" y="3" text-anchor="middle" font-size="15" fill="#e6e8ec">*</text>
          </g></g>
        </g>
      </g>
    </svg>
  </div>`;
}

// An idle character looping one animation reads as a screensaver. Picking the next
// action at random, with pauses of uneven length, is what makes it read as someone
// who is actually there.
const EMOTES = ['\u2726', '\u2605', '?', '!', '\u266a', '\u263a', '\u263e', '\u2301'];
let sceneTimer = null, moonAngle = 0, moonSpeed = 0, rafId = null;

function stopScene() {
  clearTimeout(sceneTimer);
  cancelAnimationFrame(rafId);
  sceneTimer = null; rafId = null; moonSpeed = 0;
}

function startScene() {
  const scene = document.getElementById('scene');
  const moon = document.getElementById('moonspin');
  const bubble = document.getElementById('bubble');
  const bubbleText = document.getElementById('bubbletext');
  if (!scene || !moon) return;
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

  const spin = () => {
    if (moonSpeed) {
      moonAngle += moonSpeed;
      moon.setAttribute('transform', `rotate(${moonAngle.toFixed(2)} ${240} ${190})`);
    }
    rafId = requestAnimationFrame(spin);
  };
  spin();

  const actions = [
    ['walk', 4, () => 1800 + Math.random() * 2600,
      () => { moonSpeed = (Math.random() < 0.5 ? -1 : 1) * (0.13 + Math.random() * 0.1); }],
    ['idle', 3, () => 1400 + Math.random() * 2000, () => { moonSpeed = 0; }],
    ['wave', 2, () => 2400, () => { moonSpeed = 0; }],
    ['hop', 2, () => 1700, () => { moonSpeed = 0; }],
    ['look', 2, () => 2700, () => { moonSpeed = 0; }],
  ];
  const bag = actions.flatMap(a => Array(a[1]).fill(a));

  let last = '';
  const next = () => {
    let pick;
    do { pick = bag[Math.floor(Math.random() * bag.length)]; }
    while (pick[0] === last && Math.random() < 0.75);   // rarely repeat straight away
    last = pick[0];
    pick[3]();
    scene.className = 'scene ' + pick[0];

    // An emote now and then, more often when standing still.
    if (bubble && Math.random() < (pick[0] === 'walk' ? 0.12 : 0.45)) {
      bubbleText.textContent = EMOTES[Math.floor(Math.random() * EMOTES.length)];
      bubble.classList.remove('show');
      void bubble.getBoundingClientRect();
      bubble.classList.add('show');
    }
    sceneTimer = setTimeout(next, pick[2]());
  };
  next();
}

function renderBrowse() {
  const bySystem = {};
  DATA.tables.forEach(t => {
    const sys = t.provenance && t.provenance.system;
    if (sys) (bySystem[sys] ??= []).push(t);
  });

  const docTiles = DOCS.map(d => {
    const n = (bySystem[d.id] || []).length;
    return `<button class="tile" data-doc="${esc(d.id)}">
      <div class="t">§ ${esc(d.id)}</div>
      <div class="d">${n
        ? `${T().tileGrounds} <span class="n">${n}</span> ${n === 1 ? T().mTable1 : T().mTables}`
        : T().tileUnused}</div></button>`;
  }).join('');

  const s = DATA.stats;
  const facetTiles = [
    ['pii', 'pii', s.piiColumns, T().chipPii, T().tilePii],
    ['orphan', 'warn', s.orphans, T().chipOrphan, T().tileOrphan],
    ['dead', 'warn', s.dead, T().chipDead, T().tileDead],
  ].map(([facet, tone, n, label, why]) => `<button class="tile ${tone}" data-facet="${facet}">
      <div class="t"><span class="n">${n}</span> ${esc(label)}</div>
      <div class="d">${why}</div></button>`).join('');

  return `<div id="browse">
    <div class="sec"><h3>${T().sources}</h3>
      <span class="hint">${T().tileHint}</span></div>
    <div class="tiles">${docTiles}</div>
    <div class="sec"><h3>${T().findings}</h3></div>
    <div class="tiles">${facetTiles}</div>
  </div>`;
}

function renderAsk() {
  const s = DATA.stats;
  const examples = ['tAcw', 'milliseconds', 'wrapUpCode', 'cpf', 'service level'];
  return `<div class="mission">
    <div class="split">
      ${moonScene()}
      <div class="askbox">
        <h2>${T().askTitle}</h2>
        <p class="lede">${T().missionLede}</p>
        <div class="counts">${s.tables} ${T().mTables} \u00b7 ${s.columns} ${T().mColumns}
          \u00b7 ${DOCS.length} ${T().mDocs} \u00b7 ${s.piiColumns} ${T().mPii}</div>
        <form id="askform" autocomplete="off">
          <input id="askq" type="search" placeholder="${T().askPlaceholder}">
          <button type="submit">${T().askGo}</button>
        </form>
        <div class="suggest">
          ${examples.map(q => `<button data-q="${esc(q)}">${esc(q)}</button>`).join('')}
        </div>
      </div>
    </div>
    <div id="answer"></div>
    ${renderBrowse()}
  </div>`;
}

function renderOverview() {
  const s = DATA.stats, r = DATA.run;
  const pct = s.columns ? Math.round(100 * s.documented / s.columns) : 0;

  const scoreBlock = DATA.afterScore === null ? '' : `
    <div class="score">
      ${DATA.beforeScore !== null ? `<div class="v was">${DATA.beforeScore}</div>
        <div class="arrow">&rarr;</div>` : ''}
      <div class="v now">${DATA.afterScore}</div>
      <div class="cap">${T().score}${DATA.beforeScore !== null ? T().scoreSub : ''}</div>
    </div>`;

  const coverage = DATA.schemaCoverage.map(c => {
    const p = c.columns ? Math.round(100 * c.documented / c.columns) : 0;
    return `<div class="name">${esc(c.schema)}</div>
      <div class="track"><div class="fill ${p < 100 ? 'partial' : ''}" style="width:${p}%"></div></div>
      <div class="val">${p}% · ${c.columns} cols</div>`;
  }).join('');

  const findings = DATA.findings.map(f => `<tr>
      <td class="sev ${f.severity}">${T().sev[f.severity]}</td>
      <td class="tbl"><a href="#table/${encodeURIComponent(f.table)}">${esc(f.table)}</a></td>
      <td>${esc(lang === 'pt' && f.whatPt ? f.whatPt : f.what)}</td>
      <td class="why">${esc(lang === 'pt' && f.whyPt ? f.whyPt : f.why)}</td>
    </tr>`).join('');

  const note = T().note
    .replace('§PII§', '<span class="dot pii"></span>')
    .replace('§ORPHAN§', '<span class="dot orphan"></span>')
    .replace('§SCAN§', esc(DATA.scanId));

  content.innerHTML = `
    <div class="hero">
      <div class="eyebrow">${esc(DATA.catalog)}</div>
      <h2>${T().title}</h2>
      <p class="lede">${T().lede}</p>
      ${scoreBlock}
      ${r ? `<div class="runline">${T().runline(r)}</div>` : ''}
      ${DATA.textRun && DATA.textRun.cost ? `<div class="runline">${T().bilingual(DATA.textRun)}</div>` : ''}
    </div>

    <div class="sec"><h3>${T().coverage}</h3></div>
    <div class="cov">${coverage}</div>

    <div class="sec"><h3>${T().findings}</h3>
      <span class="hint">${T().findingsHint(DATA.findings.length)}</span></div>
    <table class="find"><tbody>${findings}</tbody></table>

    <div class="cards" style="margin-top:26px">
      <div class="card good"><div class="n">${pct}%</div><div class="l">${T().documented}</div></div>
      <div class="card pii"><div class="n">${s.piiColumns}</div><div class="l">${T().piiCols}</div></div>
      <div class="card warn"><div class="n">${s.orphans}</div><div class="l">${T().neverRead}</div></div>
      <div class="card warn"><div class="n">${s.dead}</div><div class="l">${T().deadCols}</div></div>
      <div class="card warn"><div class="n">${s.fragmented}</div><div class="l">${T().fragmented}</div></div>
    </div>

    <div class="note">${note}</div>`;
}

function renderTable(key) {
  const t = DATA.tables.find(x => x.key === key);
  if (!t) return renderOverview();
  const rows = t.columns.map(c => {
    const flags = [
      ...(c.piiLabels || []).map(l => `<span class="badge pii">${esc(l)}</span>`),
      c.allNull ? '<span class="badge dead">100% null</span>' : '',
      c.constant ? '<span class="badge dead">single value</span>' : '',
    ].join('');
    const nulls = c.nullRatio !== null ? `${Math.round(c.nullRatio * 100)}% ${T().nullPct}` : '';
    const text = desc(c);
    return `<tr>
      <td class="cname">${esc(c.name)}</td>
      <td class="ctype">${esc(c.type)}</td>
      <td class="ccomment ${text ? '' : 'missing'}">${esc(text || T().notDocumented)}${flags ? '<div>' + flags + '</div>' : ''}</td>
      <td class="stat">${esc(nulls)}<br>${c.distinct !== null ? esc(c.distinct) + ' ' + T().distinct : ''}</td>
    </tr>`;
  }).join('');

  const p = t.provenance;
  content.innerHTML = `
    <div class="crumb">${esc(DATA.catalog)} / ${esc(t.schema)}</div>
    <h2>${esc(t.name)}</h2>
    <div class="meta">
      <span class="badge type">${esc(t.type)}</span>
      <span>${(t.rows ?? 0).toLocaleString()} ${T().rows}</span>
      <span>${t.columns.length} ${T().columns}</span>
      <span>${bytes(t.sizeBytes)}${t.numFiles ? ' / ' + t.numFiles + ' ' + T().files : ''}</span>
      ${t.orphan ? `<span style="color:var(--warn)">${T().neverReadOne}</span>`
                 : (t.lastRead ? `<span>${T().lastRead} ${esc(t.lastRead.slice(0, 10))}</span>` : '')}
      ${t.fragmented ? `<span style="color:var(--warn)">${T().fragmentedOne}</span>` : ''}
    </div>
    ${desc(t) ? `<div class="desc">${esc(desc(t))}</div>` : ''}
    ${p ? `<div class="prov">${T().groundedIn} <strong>${esc(p.system)}</strong>:
        ${p.passages.map(x => `<span class="passage" data-doc="${esc(p.system)}">${esc(x.split(':').slice(1).join(':').trim() || x)}</span>`).join('')}
      </div>` : `<div class="prov" style="color:var(--faint)">${T().noDocs}</div>`}
    <table class="cols">
      <thead><tr><th>${T().colCol}</th><th>${T().colType}</th><th>${T().colDesc}</th><th>${T().colProfile}</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function renderDoc(id) {
  const d = DOCS.find(x => x.id === id);
  if (!d) return renderOverview();
  content.innerHTML = `<div class="crumb">${T().sourceDoc}</div><div class="doc">${d.html}</div>`;
}

// Source documents are the vendor's words. Translating them would defeat the point
// of grounding, so they stay as written and only the chrome around them switches.
function applyLanguage() {
  document.getElementById('q').placeholder = T().search;
  document.documentElement.lang = lang === 'pt' ? 'pt-BR' : 'en';
  document.getElementById('genby').textContent = T().generatedBy;
  document.querySelectorAll('#lang button').forEach(b =>
    b.classList.toggle('on', b.dataset.lang === lang));
}

function render() {
  stopScene();
  applyLanguage();
  renderNav();
  if (current.view === 'table') renderTable(current.key);
  else if (current.view === 'doc') renderDoc(current.key);
  else if (current.view === 'ask') { content.innerHTML = renderAsk(); startScene(); }
  else renderOverview();
  document.getElementById('main').scrollTop = 0;
  document.title = (current.key ? current.key + ' — ' : '') + DATA.catalog + ' catalog documentation';
}

// The address bar is the state. A documentation portal whose pages cannot be linked
// to is a slideshow: anyone who finds something useful has no way to point at it.
function fromHash() {
  const raw = decodeURIComponent(location.hash.replace(/^#/, ''));
  const [view, ...rest] = raw.split('/');
  const key = rest.join('/');
  if (view === 'ask') return {view: 'ask'};
  return (view === 'table' || view === 'doc') && key ? {view, key} : {view: 'overview'};
}
function go(next, push = true) {
  current = next;
  const hash = next.view === 'overview' ? ''
    : next.view === 'ask' ? '#ask'
    : `#${next.view}/${encodeURIComponent(next.key)}`;
  if (push && location.hash !== hash) history.pushState(null, '', hash || location.pathname);
  render();
  sidebar.classList.remove('open');
}

const sidebar = document.getElementById('sidebar');
nav.addEventListener('click', e => {
  const b = e.target.closest('.item');
  if (b) go({view: b.dataset.view, key: b.dataset.key});
});
content.addEventListener('click', e => {
  const passage = e.target.closest('.passage');
  if (passage) return go({view: 'doc', key: passage.dataset.doc});
  const chip = e.target.closest('[data-facet]');
  if (chip) return renderFacet(chip.dataset.facet);
  const pill = e.target.closest('[data-q]');
  if (pill) {
    const box = document.getElementById('askq');
    if (box) box.value = pill.dataset.q;
    return renderAnswer(pill.dataset.q);
  }
  const where = e.target.closest('.where');
  if (where) {
    return go(where.dataset.doc ? {view: 'doc', key: where.dataset.doc}
                                : {view: 'table', key: where.dataset.go});
  }
  const star = e.target.closest('.star');
  if (star) return go({view: 'table', key: star.dataset.key});
  const link = e.target.closest('a[href^="#table/"]');
  if (link) { e.preventDefault(); go({view: 'table', key: decodeURIComponent(link.hash.slice(7))}); }
});
content.addEventListener('submit', e => {
  if (e.target.id !== 'askform') return;
  e.preventDefault();
  renderAnswer(document.getElementById('askq').value);
});
document.getElementById('q').addEventListener('input', e => { filter = e.target.value; renderNav(); });
document.getElementById('burger').addEventListener('click', () => sidebar.classList.toggle('open'));
document.getElementById('lang').addEventListener('click', e => {
  const b = e.target.closest('button');
  if (!b || b.dataset.lang === lang) return;
  lang = b.dataset.lang;
  try { localStorage.setItem('lang', lang); } catch {}
  render();
});
window.addEventListener('popstate', () => { current = fromHash(); render(); });

current = fromHash();
render();
</script>
</body>
</html>"""


def build(catalog: str, destination: Path) -> None:
    load_env()
    data = shape(collect(catalog))
    docs = collect_source_docs()

    page = (
        PAGE.replace("__CATALOG__", html.escape(catalog))
        .replace("__SCAN__", html.escape(data["scanId"]))
        .replace("__GENERATED__", html.escape(data["generatedAt"]))
        .replace("__REPO__", html.escape(REPO_URL))
        .replace("__DATA__", json.dumps(data).replace("</", "<\\/"))
        .replace("__DOCS__", json.dumps(docs).replace("</", "<\\/"))
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(page, encoding="utf-8")

    s = data["stats"]
    print(f"wrote {destination}  ({destination.stat().st_size / 1024:.0f} KB)")
    print(f"  {s['tables']} tables, {s['columns']} columns, "
          f"{s['documented']} documented, {len(docs)} source documents")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", default=DEFAULT_CATALOG)
    parser.add_argument("--out", default=str(DOCS_ROOT / "index.html"))
    args = parser.parse_args()
    build(args.catalog, Path(args.out))


if __name__ == "__main__":
    main()
