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

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_ROOT = REPO_ROOT / "docs"
DEFAULT_CATALOG = "governance_lab"
META = "meta"
ORPHAN_DAYS = 90


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

            run_rows = fetch(
                cursor,
                f"""SELECT run_id, model, tables_processed, columns_documented,
                           prompt_tokens, completion_tokens, est_cost_usd, applied,
                           unix_timestamp(finished_at) - unix_timestamp(started_at)
                    FROM {catalog}.{META}.llm_runs
                    WHERE applied = true ORDER BY started_at DESC LIMIT 1""",
            )
            run = run_rows[0] if run_rows else None

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
    }


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

    run = raw["run"]
    total_columns = sum(len(t["columns"]) for t in tables)
    documented = sum(1 for t in tables for c in t["columns"] if c["comment"])
    pii_columns = sum(t["piiCount"] for t in tables)
    return {
        "catalog": raw["catalog"],
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "scanId": raw["scan_id"][:8],
        "tables": tables,
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
  --bg: #16171b; --sidebar: #101114; --surface: #1d1f24; --raised: #24272e;
  --line: #2c2f37; --text: #dfe1e6; --muted: #8b909c; --faint: #636874;
  --accent: #62d5c4; --pii: #f0a3a3; --warn: #e3bd7a; --ok: #8fce9b;
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
.brand h1 { margin: 0; font-size: 14px; letter-spacing: .01em; }
.brand .sub { color: var(--faint); font-size: 11px; font-family: var(--mono); margin-top: 3px; }
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

@media (max-width: 780px) {
  body { grid-template-columns: 1fr; }
  .sidebar { position: fixed; z-index: 10; width: 264px; height: 100%; transform: translateX(-100%); transition: transform .2s; }
  .sidebar.open { transform: none; }
  .main { padding: 20px; }
}
</style>
</head>
<body>
<nav class="sidebar" id="sidebar">
  <div class="brand">
    <h1>__CATALOG__</h1>
    <div class="sub">scan __SCAN__ · __GENERATED__</div>
  </div>
  <div class="search"><input id="q" type="search" placeholder="Search tables and columns" autocomplete="off"></div>
  <div class="nav" id="nav"></div>
</nav>
<main class="main" id="main"><div class="wrap" id="content"></div></main>
<script id="payload" type="application/json">__DATA__</script>
<script id="docs" type="application/json">__DOCS__</script>
<script>
const DATA = JSON.parse(document.getElementById('payload').textContent);
const DOCS = JSON.parse(document.getElementById('docs').textContent);
const nav = document.getElementById('nav'), content = document.getElementById('content');
let current = {view: 'overview'}, filter = '';

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
  let html = `<div class="group"><button class="item ${current.view === 'overview' ? 'active' : ''}"
      data-view="overview"><span class="hash">≡</span> Overview</button></div>`;
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
    html += '<div class="group"><div class="group-label">Source documentation</div>';
    DOCS.forEach(d => {
      const active = current.view === 'doc' && current.key === d.id;
      html += `<button class="item ${active ? 'active' : ''}" data-view="doc" data-key="${esc(d.id)}">
        <span class="hash">§</span>${esc(d.id)}</button>`;
    });
    html += '</div>';
  }
  nav.innerHTML = html;
}

function renderOverview() {
  const s = DATA.stats, r = DATA.run;
  const pct = s.columns ? Math.round(100 * s.documented / s.columns) : 0;
  content.innerHTML = `
    <div class="crumb">${esc(DATA.catalog)}</div>
    <h2>Catalog documentation</h2>
    <div class="meta"><span>${s.tables} tables</span><span>${s.columns} columns</span>
      <span>generated ${esc(DATA.generatedAt)}</span></div>
    <div class="cards">
      <div class="card good"><div class="n">${pct}%</div><div class="l">columns documented</div></div>
      <div class="card pii"><div class="n">${s.piiColumns}</div><div class="l">personal-data columns</div></div>
      <div class="card warn"><div class="n">${s.orphans}</div><div class="l">never read</div></div>
      <div class="card warn"><div class="n">${s.dead}</div><div class="l">dead columns</div></div>
      <div class="card warn"><div class="n">${s.fragmented}</div><div class="l">fragmented tables</div></div>
    </div>
    ${r ? `<div class="desc">Every description on this site was generated by
      <code>${esc(r.model)}</code> and written back to Unity Catalog:
      <strong>${r.comments} comments</strong> across ${r.tables} tables in ${r.seconds}s,
      using ${(r.promptTokens + r.completionTokens).toLocaleString()} tokens
      — <strong>$${r.cost.toFixed(4)}</strong> at list price.
      That figure is modelled, not billed: the run was made on a free tier that charges nothing.</div>` : ''}
    <p style="color:var(--muted)">Descriptions are grounded in the documentation of the system each table
    came from. Open any table to see which passages produced its descriptions, and follow them into the
    source documents in the sidebar.</p>
    <div class="note">A dot beside a table marks personal data
      (<span class="dot pii"></span>) or a table nothing has read (<span class="dot orphan"></span>).
      This is a snapshot of scan ${esc(DATA.scanId)}, not a live view.</div>`;
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
    const nulls = c.nullRatio !== null ? `${Math.round(c.nullRatio * 100)}% null` : '';
    return `<tr>
      <td class="cname">${esc(c.name)}</td>
      <td class="ctype">${esc(c.type)}</td>
      <td class="ccomment ${c.comment ? '' : 'missing'}">${esc(c.comment || 'not documented')}${flags ? '<div>' + flags + '</div>' : ''}</td>
      <td class="stat">${esc(nulls)}<br>${c.distinct !== null ? esc(c.distinct) + ' distinct' : ''}</td>
    </tr>`;
  }).join('');

  const p = t.provenance;
  content.innerHTML = `
    <div class="crumb">${esc(DATA.catalog)} / ${esc(t.schema)}</div>
    <h2>${esc(t.name)}</h2>
    <div class="meta">
      <span class="badge type">${esc(t.type)}</span>
      <span>${(t.rows ?? 0).toLocaleString()} rows</span>
      <span>${t.columns.length} columns</span>
      <span>${bytes(t.sizeBytes)}${t.numFiles ? ' in ' + t.numFiles + ' files' : ''}</span>
      ${t.orphan ? '<span style="color:var(--warn)">never read</span>'
                 : (t.lastRead ? '<span>last read ' + esc(t.lastRead.slice(0, 10)) + '</span>' : '')}
      ${t.fragmented ? '<span style="color:var(--warn)">fragmented</span>' : ''}
    </div>
    ${t.comment ? `<div class="desc">${esc(t.comment)}</div>` : ''}
    ${p ? `<div class="prov">Grounded in <strong>${esc(p.system)}</strong>:
        ${p.passages.map(x => `<span class="passage" data-doc="${esc(p.system)}">${esc(x.split(':').slice(1).join(':').trim() || x)}</span>`).join('')}
      </div>` : '<div class="prov" style="color:var(--faint)">No source documentation mapped to this table.</div>'}
    <table class="cols">
      <thead><tr><th>Column</th><th>Type</th><th>Description</th><th>Profile</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function renderDoc(id) {
  const d = DOCS.find(x => x.id === id);
  if (!d) return renderOverview();
  content.innerHTML = `<div class="crumb">source documentation</div><div class="doc">${d.html}</div>`;
}

function render() {
  renderNav();
  if (current.view === 'table') renderTable(current.key);
  else if (current.view === 'doc') renderDoc(current.key);
  else renderOverview();
  document.getElementById('main').scrollTop = 0;
}

nav.addEventListener('click', e => {
  const b = e.target.closest('.item');
  if (!b) return;
  current = {view: b.dataset.view, key: b.dataset.key};
  render();
});
content.addEventListener('click', e => {
  const p = e.target.closest('.passage');
  if (!p) return;
  current = {view: 'doc', key: p.dataset.doc};
  render();
});
document.getElementById('q').addEventListener('input', e => { filter = e.target.value; renderNav(); });
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
