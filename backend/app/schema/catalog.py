"""Loads real schema + enum metadata from the database at startup, so the agent's system
prompt is never hand-typed against assumptions about the data. See db/README.md and the
project plan for why this matters: the original spec's hardcoded enum hints (Title-Case
divisions incl. "Mymensingh", payment_type "mobile_banking") didn't match the real data at
all (UPPERCASE, 7 divisions, "mobile"). Hand-typed hints silently drift from reality;
DB-sourced ones can't.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import asyncpg

# Columns worth surfacing as closed-ish enums in the prompt. Kept short deliberately -
# high-cardinality columns (item_name, store_district) are left as free text; the LLM
# should filter on them with pattern/equality against values seen in a prior turn or
# ask a clarifying question, not guess from a giant hint list.
ENUM_HINT_COLUMNS: dict[str, str] = {
    "store_division": "public.mv_sales_analysis",
    "payment_type": "public.mv_sales_analysis",
    "sale_quarter": "public.mv_sales_analysis",
    "item_manufacturer_country": "public.mv_sales_analysis",
}

CATALOG_TABLES = ("mv_sales_analysis", "mv_sales_daily_rollup")

_CATALOG_TTL_SECONDS = 3600


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    data_type: str


@dataclass(frozen=True)
class SchemaCatalog:
    tables: dict[str, list[ColumnInfo]]
    enum_hints: dict[str, list[str]]
    # Real MIN/MAX(sale_year) from the data - backs "which years do you have data for?"
    # style meta-questions (see Orchestrator's Intent.DATA_COVERAGE branch) with an
    # actual answer instead of routing them through the analytical query pipeline.
    year_min: int | None = None
    year_max: int | None = None
    loaded_at: float = field(default_factory=time.monotonic)

    def is_stale(self) -> bool:
        return (time.monotonic() - self.loaded_at) > _CATALOG_TTL_SECONDS

    def as_prompt_context(self) -> str:
        lines: list[str] = []
        for table, columns in self.tables.items():
            col_desc = ", ".join(f"{c.name} ({c.data_type})" for c in columns)
            lines.append(f"public.{table}: {col_desc}")
        lines.append("")
        lines.append("Known column values (use these exact values when filtering):")
        for column, values in self.enum_hints.items():
            lines.append(f"  {column}: {values}")
        if self.year_min is not None:
            lines.append(f"Data available from year {self.year_min} to {self.year_max}.")
        return "\n".join(lines)


async def load_catalog(pool: asyncpg.Pool) -> SchemaCatalog:
    tables: dict[str, list[ColumnInfo]] = {}
    async with pool.acquire() as conn:
        for table in CATALOG_TABLES:
            rows = await conn.fetch(
                """
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = $1
                ORDER BY ordinal_position
                """,
                table,
            )
            tables[table] = [ColumnInfo(r["column_name"], r["data_type"]) for r in rows]

        enum_hints: dict[str, list[str]] = {}
        for column, table in ENUM_HINT_COLUMNS.items():
            rows = await conn.fetch(
                f"SELECT DISTINCT {column} FROM {table} WHERE {column} IS NOT NULL ORDER BY 1"
            )
            enum_hints[column] = [r[column] for r in rows]

        year_row = await conn.fetchrow(
            "SELECT MIN(sale_year) AS year_min, MAX(sale_year) AS year_max "
            "FROM public.mv_sales_daily_rollup"
        )

    return SchemaCatalog(
        tables=tables,
        enum_hints=enum_hints,
        year_min=year_row["year_min"] if year_row else None,
        year_max=year_row["year_max"] if year_row else None,
    )
