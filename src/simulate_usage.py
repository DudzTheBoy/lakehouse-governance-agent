"""Query the catalog the way an analyst would, so orphan detection has a contrast.

Without this every table in `governance_lab` is unread, and "never read in 90 days"
flags all of them -- true, but useless as a demonstration. This runs plausible
analytical queries against the tables that are supposed to look alive, and leaves
the two intentional orphans strictly alone.

It is deliberately NOT part of the crawler: the crawler subtracts its own reads from
the audit log, so reads made here have to come from a separate run outside any
recorded scan window to count as real traffic.

Unity Catalog takes roughly 15 minutes to surface these events in
`system.access.audit`, so re-run the crawler a while after this, not immediately.

Run:
    python src/simulate_usage.py
"""

from dbio import connect

CATALOG = "governance_lab"

# Every table here should read as actively used. The two orphans -- raw.legado_apolices_2019
# and staging.import_backup_2026 -- are absent on purpose and must stay absent.
ANALYST_QUERIES = [
    (
        "monthly claim rate",
        f"""
        SELECT mes, total_sinistros, valor_total_sinistros
        FROM {CATALOG}.gold.sinistralidade_mensal
        ORDER BY mes DESC LIMIT 12
        """,
    ),
    (
        "active policies by insurance type",
        f"""
        SELECT tipo_seguro, count(*) AS policies, round(avg(valor_premio), 2) AS avg_premium
        FROM {CATALOG}.silver.apolices_curated
        GROUP BY tipo_seguro ORDER BY policies DESC
        """,
    ),
    (
        "claims joined to policies",
        f"""
        SELECT a.tipo_seguro, count(*) AS claims,
               round(sum(try_cast(s.valor_sinistro AS DOUBLE)), 2) AS claimed
        FROM {CATALOG}.raw.sinistros s
        JOIN {CATALOG}.raw.apolices a ON a.id_apolice = s.id_apolice
        GROUP BY a.tipo_seguro ORDER BY claimed DESC
        """,
    ),
    (
        "customers by state",
        f"""
        SELECT estado, count(*) AS customers
        FROM {CATALOG}.raw.clientes
        GROUP BY estado ORDER BY customers DESC LIMIT 10
        """,
    ),
    (
        "legacy registration volume",
        f"SELECT count(*) AS rows_loaded FROM {CATALOG}.staging.cad_gen_2021",
    ),
]


def main() -> None:
    with connect() as connection:
        with connection.cursor() as cursor:
            for label, query in ANALYST_QUERIES:
                cursor.execute(query)
                rows = cursor.fetchall()
                print(f"  {label}: {len(rows)} row(s)")
    print("\nDone. These reads are now in the audit pipeline.")
    print("Wait ~15 minutes, then run `python src/crawler.py` to see the contrast:")
    print(f"  {CATALOG}.raw.legado_apolices_2019 and {CATALOG}.staging.import_backup_2026")
    print("  should remain the only tables that were never read.")


if __name__ == "__main__":
    main()
