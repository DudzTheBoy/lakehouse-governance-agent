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
.wrap { max-width: 940px; }
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
   Three fixed layers behind everything: nebula wash, static starfield, and the
   moving pieces. All of it is pointer-events:none and sits below the content, and
   all motion stops under prefers-reduced-motion -- a page someone reads for the
   description of a column must not have anything crawling across it that they did
   not ask for. */
.sky { position: fixed; inset: 0; z-index: -1; pointer-events: none; overflow: hidden; }
.sky svg { position: absolute; }

.moon { top: 46px; right: 58px; width: 74px; height: 74px; opacity: .5; }
.planet-a { bottom: -96px; right: -72px; width: 340px; height: 340px; opacity: .26; }
.planet-b { top: 30%; left: 30%; width: 150px; height: 150px; opacity: .16; }

.shooting {
  position: absolute; width: 150px; height: 1.5px; top: 0; left: 0;
  background: linear-gradient(90deg, transparent, #ffffff 42%, transparent);
  filter: drop-shadow(0 0 5px rgba(255,255,255,.75));
  opacity: 0; transform: rotate(28deg);
  animation: shoot 15s linear infinite;
}
.shooting.b { animation-duration: 19s; animation-delay: 6.5s; }
.shooting.c { animation-duration: 24s; animation-delay: 12s; }
@keyframes shoot {
  0%      { opacity: 0; transform: translate(-14vw, 8vh) rotate(28deg); }
  1.6%    { opacity: .85; }
  9%      { opacity: 0; transform: translate(88vw, 56vh) rotate(28deg); }
  100%    { opacity: 0; transform: translate(88vw, 56vh) rotate(28deg); }
}
.shooting.b { top: 22%; }
.shooting.c { top: 44%; }

.drift { animation: drift 90s ease-in-out infinite alternate; }
@keyframes drift { from { transform: translateY(0); } to { transform: translateY(-16px); } }

@media (prefers-reduced-motion: reduce) {
  .shooting { display: none; }
  .drift { animation: none; }
}

/* the ask view -- the page that has to sell the project in ten seconds. */
.mission { padding: 8px 0 40px; }
.stage {
  position: relative; display: flex; justify-content: center; align-items: center;
  height: 260px; margin-bottom: 4px;
}
.stage .sun {
  position: absolute; width: 320px; height: 320px; border-radius: 50%;
  background: radial-gradient(circle, rgba(94,234,212,.20) 0%, rgba(94,234,212,.06) 42%, transparent 68%);
}
.stage .orbit {
  position: absolute; width: 300px; height: 300px; border-radius: 50%;
  border: 1px solid rgba(94,234,212,.18); border-top-color: rgba(94,234,212,.42);
  animation: spin 26s linear infinite;
}
.stage .orbit.two {
  width: 218px; height: 218px; border-color: rgba(255,255,255,.07);
  border-left-color: rgba(255,255,255,.20); animation-duration: 17s; animation-direction: reverse;
}
@keyframes spin { to { transform: rotate(360deg); } }
.stage .naut-big { position: relative; width: 132px; height: 132px; animation: float 6s ease-in-out infinite; }
.stage .rock {
  position: absolute; right: 16%; bottom: 8%; width: 62px; height: 62px; opacity: .8;
  animation: float 8s ease-in-out infinite reverse;
}
@media (prefers-reduced-motion: reduce) {
  .stage .orbit, .stage .naut-big, .stage .rock { animation: none; }
}

.mission h2 {
  text-align: center; font-size: 30px; letter-spacing: -.015em; margin: 0 0 10px;
}
.mission .lede {
  text-align: center; color: var(--muted); font-size: 15px; max-width: 560px;
  margin: 0 auto 8px;
}
.mission .counts {
  text-align: center; color: var(--faint); font-family: var(--mono); font-size: 11.5px;
  letter-spacing: .05em; margin-bottom: 26px;
}
.askbox { max-width: 660px; margin: 0 auto; }
.askbox form { display: flex; gap: 9px; }
.askbox input {
  flex: 1; padding: 13px 16px; font-size: 15px; border-radius: 10px;
  background: rgba(10,11,14,.72); border: 1px solid var(--line); color: var(--text);
  font-family: inherit;
}
.askbox input:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px rgba(94,234,212,.10); }
.askbox button {
  padding: 0 22px; border-radius: 10px; font-size: 14px; cursor: pointer; font-family: inherit;
  background: var(--accent); border: 1px solid var(--accent); color: #07211d; font-weight: 600;
}
.askbox button:hover { filter: brightness(1.08); }
.suggest { display: flex; gap: 7px; flex-wrap: wrap; justify-content: center; margin: 14px 0 6px; }
.suggest button {
  background: rgba(255,255,255,.03); border: 1px solid var(--line); color: var(--muted);
  border-radius: 20px; padding: 5px 13px; font-size: 12.5px; cursor: pointer; font-family: inherit;
}
.suggest button:hover { border-color: var(--accent); color: var(--accent); }
.suggest button.facet { border-style: dashed; }
.mission .answer { max-width: 660px; margin: 20px auto 0; }

/* the ask panel */
.ask {
  display: flex; gap: 16px; align-items: flex-start;
  background: linear-gradient(180deg, rgba(36,39,46,.75), rgba(26,28,33,.75));
  border: 1px solid var(--line); border-radius: 12px; padding: 16px 18px; margin: 0 0 26px;
}
.ask .naut { flex: none; width: 62px; height: 62px; animation: float 5.5s ease-in-out infinite; }
@keyframes float { 0%,100% { transform: translateY(0) rotate(-3deg); } 50% { transform: translateY(-7px) rotate(3deg); } }
@media (prefers-reduced-motion: reduce) { .ask .naut { animation: none; } }
.ask .body { flex: 1; min-width: 0; }
.ask h4 { margin: 0 0 3px; font-size: 14px; }
.ask .sub { color: var(--faint); font-size: 12px; margin-bottom: 11px; }
.ask form { display: flex; gap: 8px; }
.ask input {
  flex: 1; padding: 9px 12px; background: var(--bg); border: 1px solid var(--line);
  border-radius: 7px; color: var(--text); font-size: 13.5px; font-family: inherit;
}
.ask input:focus { outline: none; border-color: var(--accent); }
.ask button {
  background: var(--accent-dim); border: 1px solid var(--accent); color: var(--accent);
  border-radius: 7px; padding: 0 15px; font-size: 13px; cursor: pointer; font-family: inherit;
}
.ask button:hover { background: var(--accent); color: #08211e; }
.answer { margin-top: 13px; border-top: 1px solid var(--line); padding-top: 12px; }
.answer .hit { padding: 6px 0; border-bottom: 1px solid var(--line-soft); font-size: 13px; }
.answer .hit:last-child { border-bottom: none; }
.answer .where { font-family: var(--mono); font-size: 11.5px; color: var(--accent); cursor: pointer; }
.answer .where:hover { text-decoration: underline; }
.answer .what { color: var(--muted); }
.answer .none { color: var(--faint); font-size: 13px; }
.answer .note { color: var(--faint); font-size: 11.5px; margin-top: 9px; }
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
  <svg class="moon drift" viewBox="0 0 64 64">
    <defs>
      <radialGradient id="mg" cx="38%" cy="34%">
        <stop offset="0" stop-color="#f2f5ff"/><stop offset="1" stop-color="#b9c2dc"/>
      </radialGradient>
    </defs>
    <circle cx="32" cy="32" r="26" fill="url(#mg)"/>
    <circle cx="24" cy="25" r="4.5" fill="#9aa5c4" opacity=".55"/>
    <circle cx="39" cy="38" r="6.5" fill="#9aa5c4" opacity=".42"/>
    <circle cx="42" cy="21" r="3" fill="#9aa5c4" opacity=".5"/>
    <circle cx="27" cy="43" r="2.6" fill="#9aa5c4" opacity=".38"/>
  </svg>

  <svg class="planet-a drift" viewBox="0 0 200 200">
    <defs>
      <radialGradient id="pa" cx="34%" cy="30%">
        <stop offset="0" stop-color="#3f7f96"/><stop offset="1" stop-color="#123243"/>
      </radialGradient>
    </defs>
    <circle cx="100" cy="100" r="66" fill="url(#pa)"/>
    <ellipse cx="100" cy="100" rx="96" ry="26" fill="none" stroke="#7fb6c9"
      stroke-width="5" opacity=".5" transform="rotate(-19 100 100)"/>
    <ellipse cx="100" cy="100" rx="86" ry="21" fill="none" stroke="#a9d4e2"
      stroke-width="2" opacity=".35" transform="rotate(-19 100 100)"/>
    <path d="M52 84a66 66 0 0 1 40-24" stroke="#8fc4d6" stroke-width="3" fill="none" opacity=".28"/>
  </svg>

  <svg class="planet-b" viewBox="0 0 120 120">
    <defs>
      <radialGradient id="pb" cx="36%" cy="30%">
        <stop offset="0" stop-color="#8f7ad6"/><stop offset="1" stop-color="#2b2350"/>
      </radialGradient>
    </defs>
    <circle cx="60" cy="60" r="46" fill="url(#pb)"/>
    <ellipse cx="52" cy="46" rx="15" ry="9" fill="#b8a6f0" opacity=".22"/>
    <ellipse cx="72" cy="76" rx="19" ry="10" fill="#1e1838" opacity=".3"/>
  </svg>

  <i class="shooting"></i><i class="shooting b"></i><i class="shooting c"></i>
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
const ASTRONAUT = `<svg class="naut" viewBox="0 0 64 64" aria-hidden="true">
  <defs>
    <linearGradient id="suit" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#f4f6fa"/><stop offset="1" stop-color="#c8cede"/>
    </linearGradient>
    <linearGradient id="visor" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#1b3a44"/><stop offset="1" stop-color="#0d1c22"/>
    </linearGradient>
  </defs>
  <rect x="20" y="39" width="10" height="14" rx="5" fill="url(#suit)" transform="rotate(12 25 46)"/>
  <rect x="34" y="39" width="10" height="14" rx="5" fill="url(#suit)" transform="rotate(-12 39 46)"/>
  <rect x="8" y="24" width="12" height="9" rx="4.5" fill="url(#suit)" transform="rotate(-22 14 28)"/>
  <rect x="44" y="24" width="12" height="9" rx="4.5" fill="url(#suit)" transform="rotate(22 50 28)"/>
  <rect x="17" y="22" width="30" height="24" rx="11" fill="url(#suit)"/>
  <rect x="26" y="30" width="12" height="8" rx="3" fill="#aeb6c8" opacity=".75"/>
  <circle cx="30" cy="34" r="1.5" fill="#5eead4"/><circle cx="35" cy="34" r="1.5" fill="#fab219"/>
  <circle cx="32" cy="19" r="15" fill="url(#suit)"/>
  <path d="M22 19a10 10 0 0 1 20 0 10 10 0 0 1-20 0z" fill="url(#visor)"/>
  <path d="M25 15c2-3 6-4.5 9-4" stroke="#5eead4" stroke-width="2" stroke-linecap="round"
    fill="none" opacity=".8"/>
  <circle cx="38" cy="22" r="2" fill="#fff" opacity=".22"/>
</svg>`;

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
  const rows = DATA.tables.flatMap(FACETS[name]);
  box.innerHTML = `<div class="answer">${rows.map(h => `<div class="hit">
      <span class="where" data-go="${esc(h.table)}">${esc(h.table)}${h.column ? '.' + esc(h.column) : ''}</span>
      <div class="what">${esc(h.text)}</div></div>`).join('')}
    <div class="note">${T().askFacetNote}</div></div>`;
}

function renderAnswer(query) {
  const box = document.getElementById('answer');
  if (!box) return;
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

function renderAsk() {
  const s = DATA.stats;
  const examples = ['tAcw', 'milliseconds', 'wrapUpCode', 'cpf', 'service level'];
  return `<div class="mission">
    <div class="stage">
      <div class="sun"></div>
      <div class="orbit"></div><div class="orbit two"></div>
      ${ASTRONAUT.replace('class="naut"', 'class="naut-big"')}
      <svg class="rock" viewBox="0 0 64 64" aria-hidden="true">
        <defs><radialGradient id="rk" cx="34%" cy="30%">
          <stop offset="0" stop-color="#7f8aa6"/><stop offset="1" stop-color="#2a3040"/>
        </radialGradient></defs>
        <circle cx="32" cy="32" r="22" fill="url(#rk)"/>
        <circle cx="25" cy="26" r="4" fill="#1f2431" opacity=".55"/>
        <circle cx="38" cy="37" r="5.5" fill="#1f2431" opacity=".45"/>
      </svg>
    </div>

    <h2>${T().askTitle}</h2>
    <p class="lede">${T().missionLede}</p>
    <div class="counts">${s.tables} ${T().mTables} · ${s.columns} ${T().mColumns}
      · ${DOCS.length} ${T().mDocs} · ${s.piiColumns} ${T().mPii}</div>

    <div class="askbox">
      <form id="askform" autocomplete="off">
        <input id="askq" type="search" placeholder="${T().askPlaceholder}">
        <button type="submit">${T().askGo}</button>
      </form>
      <div class="suggest">
        ${examples.map(q => `<button data-q="${esc(q)}">${esc(q)}</button>`).join('')}
        <button class="facet" data-facet="pii">${T().chipPii}</button>
        <button class="facet" data-facet="orphan">${T().chipOrphan}</button>
        <button class="facet" data-facet="dead">${T().chipDead}</button>
      </div>
      <div id="answer"></div>
    </div>
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
  applyLanguage();
  renderNav();
  if (current.view === 'table') renderTable(current.key);
  else if (current.view === 'doc') renderDoc(current.key);
  else if (current.view === 'ask') content.innerHTML = renderAsk();
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
