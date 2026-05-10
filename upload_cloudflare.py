#!/usr/bin/env python3
"""
Upload merged CSV data to Cloudflare D1.

Prerequisites
-------------
1. Apply schema to D1 (see d1_schema_merged.sql) or ensure your tables expose the
   same columns as merged CSVs. Photos in ``variants.gallery_photos`` (JSON).
   PDF metadata and product links use ``merged_technical_pdfs.csv`` and
   ``merged_product_pdfs.csv``.

2. Install deps: pip install -r requirements-upload.txt

3. Environment: copy `.env.example` to `.env`, fill values (see “Where to get” in `.env.example`).
   This script loads `.env` via python-dotenv.

Usage (after `.env` is filled)
-----
  python upload_cloudflare.py --dry-run
  python upload_cloudflare.py --truncate-d1
  python upload_cloudflare.py --id-offset 1000   # default; avoids id clashes with existing DB rows

Defaults read merged_*.csv from this directory.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_ROOT_DIR = Path(__file__).resolve().parent


def _load_env_stdlib(path: Path) -> None:
    """Parse KEY=VALUE lines if python-dotenv is not installed."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if not key:
            continue
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        os.environ[key] = val


def _load_project_env() -> None:
    env_path = _ROOT_DIR / ".env"
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path, override=True)
    except ImportError:
        _load_env_stdlib(env_path)


_load_project_env()

ROOT = _ROOT_DIR

# Upload order respects FKs: technical_pdfs → products → variants → product_pdfs.
TABLE_UPLOAD_ORDER: list[tuple[str, Path]] = [
    ("technical_pdfs", ROOT / "merged_technical_pdfs.csv"),
    ("products", ROOT / "merged_products.csv"),
    ("variants", ROOT / "merged_variants.csv"),
    ("product_pdfs", ROOT / "merged_product_pdfs.csv"),
]

REQUIRED_TABLES = frozenset({"products", "variants"})

TABLE_NAMES = {
    "technical_pdfs": "technical_pdfs",
    "products": "products",
    "variants": "variants",
    "product_pdfs": "product_pdfs",
}

TRUNCATE_TABLE_ORDER = ["product_pdfs", "variants", "products", "technical_pdfs"]

# Merged CSV column names → your D1 column names when they differ (see align output / PRAGMA).
CSV_HEADER_TO_DB: dict[str, dict[str, str]] = {
    "variants": {"url": "url_on_manufacturer_website"},
}


def _bump_id(row: dict[str, Any], key: str, offset: int) -> None:
    if offset == 0 or key not in row or row[key] is None:
        return
    row[key] = int(row[key]) + offset


def apply_numeric_id_offset(logical_table: str, rows: list[dict[str, Any]], offset: int) -> None:
    """Shift scraped ids so merged data does not collide with existing rows (FKs stay aligned)."""
    if offset == 0 or not rows:
        return
    if logical_table == "products":
        for r in rows:
            _bump_id(r, "id", offset)
    elif logical_table == "variants":
        for r in rows:
            _bump_id(r, "id", offset)
            _bump_id(r, "product_id", offset)
    elif logical_table == "technical_pdfs":
        for r in rows:
            _bump_id(r, "id", offset)
    elif logical_table == "product_pdfs":
        for r in rows:
            _bump_id(r, "id", offset)
            _bump_id(r, "product_id", offset)
            _bump_id(r, "pdf_id", offset)


def apply_csv_header_aliases(logical_table: str, headers: list[str]) -> tuple[list[str], list[str]]:
    """Return (new_headers, applied_notes). Renames CSV headers so they match the database."""
    aliases = CSV_HEADER_TO_DB.get(logical_table, {})
    if not aliases:
        return headers, []
    out: list[str] = []
    notes: list[str] = []
    for h in headers:
        dbn = aliases.get(h, h)
        if dbn != h:
            notes.append(f"{h!r}→{dbn!r}")
        out.append(dbn)
    return out, notes


def env(name: str, required: bool = True) -> str | None:
    v = os.environ.get(name, "").strip()
    if not v:
        if required:
            raise SystemExit(f"Missing environment variable: {name}")
        return None
    return v


def cf_api_request(
    method: str,
    url: str,
    token: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8")
    except HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Cloudflare API HTTP {e.code}: {err_body}") from e
    except URLError as e:
        raise SystemExit(f"Cloudflare API network error: {e}") from e

    out = json.loads(raw)
    if not out.get("success"):
        errs = out.get("errors") or []
        raise SystemExit(f"Cloudflare API error: {errs or raw[:2000]}")
    return out


def d1_query(account_id: str, database_id: str, token: str, sql: str, params: list[Any] | None = None) -> dict[str, Any]:
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/d1/database/{database_id}/query"
    body: dict[str, Any] = {"sql": sql}
    if params is not None:
        body["params"] = params
    return cf_api_request("POST", url, token, body)


def d1_table_columns(
    account_id: str,
    database_id: str,
    token: str,
    table: str,
) -> list[tuple[str, str, int]]:
    """Return list of (name, type, pk) from PRAGMA table_info."""
    if not table.replace("_", "").isalnum():
        raise SystemExit(f"Unsafe table name: {table}")
    res = d1_query(account_id, database_id, token, f'PRAGMA table_info("{table}")')
    rows = _extract_result_rows(res)
    out = []
    for row in rows:
        name = row.get("name") or row.get("NAME")
        typ = row.get("type") or row.get("TYPE") or ""
        pk = row.get("pk") or row.get("PK") or 0
        if name is None and isinstance(row, dict):
            vals = list(row.values())
            if len(vals) >= 6:
                name, typ, pk = str(vals[1]), str(vals[2]), int(vals[5] or 0)
        if name:
            out.append((str(name), str(typ), int(pk)))
    return out


def _extract_result_rows(res: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize D1 HTTP API query response rows."""
    chunks = res.get("result") or []
    rows: list[dict[str, Any]] = []
    for chunk in chunks:
        if isinstance(chunk, dict):
            inner = chunk.get("results")
            if isinstance(inner, list):
                for r in inner:
                    if isinstance(r, dict):
                        rows.append(r)
            elif isinstance(inner, dict):
                rows.append(inner)
        elif isinstance(chunk, list):
            for r in chunk:
                if isinstance(r, dict):
                    rows.append(r)
    return rows


def read_csv_simple(path: Path) -> tuple[list[str], list[list[str]]]:
    import csv

    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        rows = list(reader)
    if not rows:
        return [], []
    return rows[0], rows[1:]


def normalize_cell(column: str, raw: str, sqlite_type: str) -> Any:
    s = raw.strip()
    if s == "":
        return None
    lt = sqlite_type.upper()
    if lt == "INTEGER":
        sl = s.lower()
        if sl in ("false", "true", "0", "1"):
            return 1 if sl in ("true", "1") else 0
        try:
            return int(float(s)) if "." in s else int(s)
        except ValueError:
            return None
    if lt == "REAL":
        return float(s)
    return s


def coerce_row(
    headers: list[str],
    db_cols: dict[str, str],
    cells: list[str],
) -> dict[str, Any]:
    row = dict(zip(headers, cells))
    out: dict[str, Any] = {}
    for name, typ in db_cols.items():
        if name not in row:
            continue
        val = row[name]
        out[name] = normalize_cell(name, val, typ)
    return out


def insert_batches(
    account_id: str,
    database_id: str,
    token: str,
    table: str,
    columns: list[str],
    rows_data: list[dict[str, Any]],
) -> None:
    """One INSERT per row — avoids D1 multi-row bound-parameter limits."""
    if len(columns) != len(set(columns)):
        raise SystemExit(f'Duplicate column names in INSERT list: {columns}')
    col_sql = ",".join(f'"{c}"' for c in columns)
    placeholders = ",".join(["?"] * len(columns))
    sql = f'INSERT INTO "{table}" ({col_sql}) VALUES ({placeholders})'
    n = len(rows_data)
    print(
        f"  (D1: one row per request, {len(columns)} bound params each; {n} requests total)",
        flush=True,
    )
    total = 0
    for r in rows_data:
        params = [r[c] for c in columns]
        d1_query(account_id, database_id, token, sql, params)
        total += 1
        if total % 100 == 0 or total == n:
            print(f"  inserted {total}/{n} into {table}", flush=True)


def align_columns(csv_headers: list[str], db_col_info: list[tuple[str, str, int]]) -> tuple[list[str], dict[str, str]]:
    db_map = {name: typ for name, typ, _pk in db_col_info}
    missing_in_csv = [c for c in db_map if c not in csv_headers]
    missing_in_db = [c for c in csv_headers if c not in db_map]
    use = [c for c in csv_headers if c in db_map]
    if missing_in_csv:
        print(f"    (DB columns not in CSV, skipped: {missing_in_csv[:8]}{'...' if len(missing_in_csv) > 8 else ''})")
    if missing_in_db:
        print(f"    (CSV columns not in DB, skipped: {missing_in_db[:8]}{'...' if len(missing_in_db) > 8 else ''})")
    if not use:
        raise SystemExit("No overlapping columns between CSV and database.")
    return use, {c: db_map[c] for c in use}


def truncate_tables(account_id: str, database_id: str, token: str, tables: list[str]) -> None:
    for t in tables:
        d1_query(account_id, database_id, token, f'DELETE FROM "{t}"')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not call Cloudflare APIs; print CSV row counts only",
    )
    parser.add_argument("--skip-d1", action="store_true")
    parser.add_argument("--truncate-d1", action="store_true", help="DELETE FROM variants, products first")
    parser.add_argument(
        "--id-offset",
        type=int,
        default=1000,
        help="Added to numeric ids before insert (products, variants, technical_pdfs, "
        "product_pdfs including FK columns) so merged ids do not clash with existing rows "
        "(default: 1000; use 0 to disable)",
    )
    parser.add_argument("--root", type=Path, default=ROOT, help="Project root with merged CSVs")
    args = parser.parse_args()

    root = args.root.resolve()

    need_d1 = not args.skip_d1 and not args.dry_run

    acct = env("CLOUDFLARE_ACCOUNT_ID", required=need_d1)
    db_id = env("CLOUDFLARE_D1_DATABASE_ID", required=need_d1)
    token = env("CLOUDFLARE_API_TOKEN", required=need_d1)

    if not args.skip_d1:
        print("=== D1 ===")
        for logical, csv_path in TABLE_UPLOAD_ORDER:
            if logical in REQUIRED_TABLES and not csv_path.is_file():
                raise SystemExit(f"Missing CSV: {csv_path} (run merge_csvs.py first)")
            if not csv_path.is_file():
                print(f"  skip {logical}: no {csv_path.name}")
                continue
            csv_h, csv_rows = read_csv_simple(csv_path)
            csv_h, alias_notes = apply_csv_header_aliases(logical, csv_h)
            tbl = TABLE_NAMES[logical]
            if args.dry_run:
                print(f"  {tbl}: would upload {len(csv_rows)} rows from {csv_path.name} ({len(csv_h)} CSV columns)")
                continue

            cols = d1_table_columns(acct, db_id, token, tbl)
            print(f"  {tbl}: DB has {len(cols)} columns; CSV has {len(csv_h)} columns")
            if alias_notes:
                print(f"    CSV→DB column names: {', '.join(alias_notes)}")

        if args.dry_run:
            pass
        elif args.truncate_d1:
            print("Truncating D1 tables (FK-safe order)...")
            truncate_tables(acct, db_id, token, TRUNCATE_TABLE_ORDER)

        if not args.dry_run:
            if args.id_offset != 0:
                print(f"  Shifting numeric ids by +{args.id_offset} (--id-offset) for uploaded tables.")
            for logical, csv_path in TABLE_UPLOAD_ORDER:
                if not csv_path.is_file():
                    continue
                tbl = TABLE_NAMES[logical]
                csv_h, csv_rows = read_csv_simple(csv_path)
                csv_h, alias_notes = apply_csv_header_aliases(logical, csv_h)
                if alias_notes:
                    print(f"  {tbl}: CSV→DB column names: {', '.join(alias_notes)}")
                db_cols_info = d1_table_columns(acct, db_id, token, tbl)
                use_cols, typ_map = align_columns(csv_h, db_cols_info)
                rows_out: list[dict[str, Any]] = []
                for cells in csv_rows:
                    if len(cells) < len(csv_h):
                        cells = [*cells, *([""] * (len(csv_h) - len(cells)))]
                    row = coerce_row(csv_h, typ_map, cells[: len(csv_h)])
                    rows_out.append(row)
                apply_numeric_id_offset(logical, rows_out, args.id_offset)
                print(f"Uploading {len(rows_out)} rows -> {tbl}...")
                insert_batches(acct, db_id, token, tbl, use_cols, rows_out)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
