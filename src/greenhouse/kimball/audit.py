"""Audit script verifying complete absence of physical core dependency (T24).

Uses real pg_catalog columns (relnamespace, nspname, relkind, confrelid, contype)
to prove that the Kimball database has zero core tables, zero foreign keys pointing
to core, zero foreign tables (FDW), and zero dblink extensions.
"""
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CatalogAuditResult:
    is_pure_kimball: bool
    core_table_count: int
    core_fk_count: int
    foreign_table_count: int
    fdw_extension_count: int
    kimball_table_count: int
    details: dict[str, Any]


def verify_kimball_isolation(connection) -> CatalogAuditResult:
    """Audit the database catalog for physical core independence."""
    with connection.cursor() as cur:
        # 1. Count tables/views/materialized views in schema 'core'
        cur.execute("""
            SELECT count(*)
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'core' AND c.relkind IN ('r', 'v', 'm')
        """)
        core_tables = cur.fetchone()[0]

        # 2. Count foreign keys pointing to schema 'core' using real pg_catalog columns
        cur.execute("""
            SELECT count(*)
            FROM pg_constraint con
            JOIN pg_class conf ON conf.oid = con.confrelid
            JOIN pg_namespace confn ON confn.oid = conf.relnamespace
            WHERE con.contype = 'f' AND confn.nspname = 'core'
        """)
        core_fks = cur.fetchone()[0]

        # 3. Check for Foreign Data Wrappers / foreign tables
        cur.execute("SELECT count(*) FROM pg_foreign_table")
        foreign_tables = cur.fetchone()[0]

        # 4. Check for remote query extensions (dblink / postgres_fdw)
        cur.execute("""
            SELECT count(*)
            FROM pg_extension
            WHERE extname IN ('dblink', 'postgres_fdw')
        """)
        fdw_extensions = cur.fetchone()[0]

        # 5. Count tables in 'kimball' schema
        cur.execute("""
            SELECT count(*)
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'kimball' AND c.relkind IN ('r', 'v', 'm')
        """)
        kimball_tables = cur.fetchone()[0]

        # 6. List all tables in kimball schema for audit details
        cur.execute("""
            SELECT c.relname
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'kimball' AND c.relkind = 'r'
            ORDER BY c.relname
        """)
        table_names = [r[0] for r in cur.fetchall()]

    is_pure = (
        core_tables == 0
        and core_fks == 0
        and foreign_tables == 0
        and fdw_extensions == 0
        and kimball_tables > 0
    )

    return CatalogAuditResult(
        is_pure_kimball=is_pure,
        core_table_count=core_tables,
        core_fk_count=core_fks,
        foreign_table_count=foreign_tables,
        fdw_extension_count=fdw_extensions,
        kimball_table_count=kimball_tables,
        details={
            "kimball_tables": table_names,
            "core_tables_found": core_tables,
            "core_fks_found": core_fks,
            "foreign_tables_found": foreign_tables,
            "fdw_extensions_found": fdw_extensions,
        },
    )
