"""Seed `governance_lab` with a controlled, intentionally messy insurance dataset.

Everything here is self-authored synthetic data (Faker), not a real customer's
data -- full control over what's dirty and why, which is the point: every finding
the audit agent is supposed to catch has a deliberate, documented cause below.

Layout (medallion):
    raw.clientes            customers, with PII columns
    raw.apolices            policies
    raw.sinistros           claims (one suspicious-type column, on purpose)
    raw.legado_apolices_2019  never read again after creation -> genuine orphan
    silver.apolices_curated  cleaned view of policies, still undocumented
    gold.sinistralidade_mensal  monthly claim-rate rollup
    staging.import_backup_2026  a stray one-off backup nobody cleaned up

Deliberate dirtiness, one line each:
    - Every table is created with no COMMENT (documentation finding).
    - raw.clientes: nome/cpf/email/telefone/data_nascimento are PII (finding).
    - raw.clientes.apelido: 100% NULL column (dead-column finding).
    - raw.apolices.pais: constant 'Brasil' on every row, cardinality 1 (finding).
    - raw.sinistros.valor_sinistro: currency stored as STRING, not DECIMAL (finding).
    - raw.legado_apolices_2019 and staging.import_backup_2026: created once,
      never queried again -> real zero-reads in system.access.audit (finding).
    - No table here is ever OPTIMIZE'd or given an explicit owner (finding).

Run:
    python src/seed.py
"""

import random
from datetime import date, timedelta

from faker import Faker

from dbio import batched_insert, connect

CATALOG = "governance_lab"
SEED = 42

fake = Faker("pt_BR")
Faker.seed(SEED)
random.seed(SEED)

TIPOS_SEGURO = ["auto", "residencial", "vida", "viagem"]
TIPOS_SINISTRO = ["colisao", "roubo", "incendio", "furto", "danos_terceiros"]
STATUS_APOLICE = ["ativa", "cancelada", "vencida"]
STATUS_SINISTRO = ["aberto", "em_analise", "aprovado", "negado", "pago"]
ESTADOS = ["SP", "RJ", "MG", "RS", "BA", "PR", "SC", "PE", "CE", "GO"]


def cpf_digit(digits: list[int]) -> int:
    weight = len(digits) + 1
    total = sum(d * (weight - i) for i, d in enumerate(digits))
    remainder = (total * 10) % 11
    return 0 if remainder == 10 else remainder


def fake_cpf() -> str:
    """9 random digits + 2 real check digits -- passes CPF validation, no real person."""
    base = [random.randint(0, 9) for _ in range(9)]
    d1 = cpf_digit(base)
    d2 = cpf_digit(base + [d1])
    digits = base + [d1, d2]
    s = "".join(map(str, digits))
    return f"{s[0:3]}.{s[3:6]}.{s[6:9]}-{s[9:11]}"


def random_date(start: date, end: date) -> date:
    delta = (end - start).days
    return start + timedelta(days=random.randint(0, max(delta, 0)))


def gen_clientes(n: int) -> list[tuple]:
    rows = []
    for i in range(1, n + 1):
        rows.append(
            (
                i,
                fake.name(),
                fake_cpf(),
                fake.email(),
                fake.msisdn()[:11],
                random_date(date(1950, 1, 1), date(2005, 12, 31)),
                fake.city(),
                random.choice(ESTADOS),
                random_date(date(2020, 1, 1), date(2026, 8, 1)),
                None,  # apelido: always NULL by design
            )
        )
    return rows


def gen_apolices(n: int, n_clientes: int) -> list[tuple]:
    rows = []
    for i in range(1, n + 1):
        inicio = random_date(date(2022, 1, 1), date(2026, 6, 1))
        rows.append(
            (
                i,
                random.randint(1, n_clientes),
                random.choice(TIPOS_SEGURO),
                round(random.uniform(400, 6000), 2),
                round(random.uniform(10000, 500000), 2),
                inicio,
                inicio + timedelta(days=365),
                random.choice(STATUS_APOLICE),
                "Brasil",  # pais: constant on every row by design
            )
        )
    return rows


def gen_sinistros(n: int, n_apolices: int) -> list[tuple]:
    rows = []
    for i in range(1, n + 1):
        valor = round(random.uniform(300, 80000), 2)
        rows.append(
            (
                i,
                random.randint(1, n_apolices),
                random_date(date(2022, 1, 1), date(2026, 8, 1)),
                random.choice(TIPOS_SINISTRO),
                str(valor),  # stored as string by design -- should be DECIMAL
                random.choice(STATUS_SINISTRO),
                fake.sentence(nb_words=10),
            )
        )
    return rows


def gen_legado_apolices(n: int) -> list[tuple]:
    rows = []
    for i in range(1, n + 1):
        inicio = random_date(date(2017, 1, 1), date(2019, 12, 31))
        rows.append(
            (
                i,
                random.choice(TIPOS_SEGURO),
                round(random.uniform(300, 3000), 2),
                inicio,
                inicio + timedelta(days=365),
                "migrado",
            )
        )
    return rows


def gen_import_backup(n: int) -> list[tuple]:
    rows = []
    for i in range(1, n + 1):
        rows.append((i, fake.name(), fake.email(), "backup manual - nao usar"))
    return rows


TABLES = {
    f"{CATALOG}.raw.clientes": dict(
        ddl="""
            id_cliente BIGINT,
            nome STRING,
            cpf STRING,
            email STRING,
            telefone STRING,
            data_nascimento DATE,
            cidade STRING,
            estado STRING,
            data_cadastro DATE,
            apelido STRING
        """,
        columns=[
            "id_cliente", "nome", "cpf", "email", "telefone",
            "data_nascimento", "cidade", "estado", "data_cadastro", "apelido",
        ],
        generator=lambda: gen_clientes(600),
    ),
    f"{CATALOG}.raw.apolices": dict(
        ddl="""
            id_apolice BIGINT,
            id_cliente BIGINT,
            tipo_seguro STRING,
            valor_premio DOUBLE,
            valor_cobertura DOUBLE,
            data_inicio DATE,
            data_fim DATE,
            status STRING,
            pais STRING
        """,
        columns=[
            "id_apolice", "id_cliente", "tipo_seguro", "valor_premio",
            "valor_cobertura", "data_inicio", "data_fim", "status", "pais",
        ],
        generator=lambda: gen_apolices(1200, 600),
    ),
    f"{CATALOG}.raw.sinistros": dict(
        ddl="""
            id_sinistro BIGINT,
            id_apolice BIGINT,
            data_ocorrencia DATE,
            tipo_sinistro STRING,
            valor_sinistro STRING,
            status_sinistro STRING,
            descricao STRING
        """,
        columns=[
            "id_sinistro", "id_apolice", "data_ocorrencia", "tipo_sinistro",
            "valor_sinistro", "status_sinistro", "descricao",
        ],
        generator=lambda: gen_sinistros(400, 1200),
    ),
    f"{CATALOG}.raw.legado_apolices_2019": dict(
        ddl="""
            id_apolice BIGINT,
            tipo_seguro STRING,
            valor_premio DOUBLE,
            data_inicio DATE,
            data_fim DATE,
            status STRING
        """,
        columns=["id_apolice", "tipo_seguro", "valor_premio", "data_inicio", "data_fim", "status"],
        generator=lambda: gen_legado_apolices(50),
        orphan=True,
    ),
    f"{CATALOG}.staging.import_backup_2026": dict(
        ddl="""
            id BIGINT,
            nome STRING,
            email STRING,
            nota STRING
        """,
        columns=["id", "nome", "email", "nota"],
        generator=lambda: gen_import_backup(20),
        orphan=True,
    ),
}


def create_schemas(cursor) -> None:
    cursor.execute(f"CREATE CATALOG IF NOT EXISTS {CATALOG}")
    for schema in ("raw", "silver", "gold", "staging"):
        cursor.execute(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{schema}")


def seed_raw_tables(cursor) -> None:
    for table, spec in TABLES.items():
        cursor.execute(f"DROP TABLE IF EXISTS {table}")
        cursor.execute(f"CREATE TABLE {table} ({spec['ddl']}) USING DELTA")
        rows = spec["generator"]()
        batched_insert(cursor, table, spec["columns"], rows)
        marker = " (orphan by design, not verified below)" if spec.get("orphan") else ""
        print(f"  seeded {table}: {len(rows)} rows{marker}")


def build_silver_and_gold(cursor) -> None:
    silver = f"{CATALOG}.silver.apolices_curated"
    cursor.execute(f"DROP TABLE IF EXISTS {silver}")
    cursor.execute(
        f"""
        CREATE TABLE {silver} USING DELTA AS
        SELECT
            a.id_apolice,
            a.id_cliente,
            a.tipo_seguro,
            a.valor_premio,
            a.valor_cobertura,
            a.data_inicio,
            a.data_fim,
            a.status
        FROM {CATALOG}.raw.apolices a
        WHERE a.status = 'ativa'
        """
    )
    print(f"  built {silver}")

    gold = f"{CATALOG}.gold.sinistralidade_mensal"
    cursor.execute(f"DROP TABLE IF EXISTS {gold}")
    cursor.execute(
        f"""
        CREATE TABLE {gold} USING DELTA AS
        SELECT
            date_trunc('MONTH', s.data_ocorrencia) AS mes,
            count(*) AS total_sinistros,
            round(sum(try_cast(s.valor_sinistro AS DOUBLE)), 2) AS valor_total_sinistros
        FROM {CATALOG}.raw.sinistros s
        GROUP BY 1
        ORDER BY 1
        """
    )
    print(f"  built {gold}")


def verify(cursor) -> None:
    print("\nverification (row counts; orphan tables skipped on purpose):")
    for table, spec in TABLES.items():
        if spec.get("orphan"):
            continue
        cursor.execute(f"SELECT count(*) FROM {table}")
        print(f"  {table}: {cursor.fetchone()[0]} rows")
    for table in (f"{CATALOG}.silver.apolices_curated", f"{CATALOG}.gold.sinistralidade_mensal"):
        cursor.execute(f"SELECT count(*) FROM {table}")
        print(f"  {table}: {cursor.fetchone()[0]} rows")


def main() -> None:
    with connect() as connection:
        with connection.cursor() as cursor:
            print("creating catalog/schemas...")
            create_schemas(cursor)
            print("seeding raw tables...")
            seed_raw_tables(cursor)
            print("building silver/gold...")
            build_silver_and_gold(cursor)
            verify(cursor)
    print("\nDone. governance_lab is ready -- no comments, no owners, no OPTIMIZE.")
    print("Do not SELECT from the orphan tables again, or you'll un-orphan them:")
    for table, spec in TABLES.items():
        if spec.get("orphan"):
            print(f"  - {table}")


if __name__ == "__main__":
    main()
