#!/usr/bin/env python3
"""
Merge scraper CSVs from each brand subfolder into root-level files.

For each of products.csv, variants.csv, technical_pdfs.csv, and product_pdfs.csv:
  - collect paths like <subdir>/products.csv (one level under ``--root``), or only under
    folder names you pass as positional arguments
  - take the header from the first file; skip headers on the rest
  - concatenate rows, optionally tag with parent folder name
  - sort by primary key (``id``)
  - optionally dedupe by composite key and drop orphan variants

Photo URLs live in ``variants.gallery_photos`` as JSON. Manufacturer PDFs use
``technical_pdfs.csv`` + ``product_pdfs.csv`` (FK ``product_id``, ``pdf_id``).

After dedupe, an optional global numeric-id pass (default on) remaps duplicate
integer ``id`` values to new ones and cascades ``variants.product_id`` so FK
relationships stay aligned (see ``--no-global-ids``).

Duplicate ``sku`` strings are then disambiguated (suffix ``-{variant id}``) so
databases with ``UNIQUE(sku)`` can load the merged variants CSV.

Outputs (default): merged_products.csv, merged_variants.csv, merged_technical_pdfs.csv,
merged_product_pdfs.csv

After the merge writes are complete, the Romanian translation step from
``translate_to_ro.py`` is invoked automatically. It snapshots the freshly
merged products + variants files to ``merged_<name>_original.csv`` and
overwrites ``merged_products.csv`` with the Romanian rewrite (variants are
left as-is per project policy: colors and variant names stay in their original
form). Pass ``--no-translate`` to skip this step. Translation is cached in
``translation_cache.sqlite`` so re-runs only translate cells whose source text
changed.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Any

from scraper_brand_utils import PRODUCT_CSV_COLUMNS

TARGETS = ("products.csv", "variants.csv", "technical_pdfs.csv", "product_pdfs.csv")

SORT_KEYS: dict[str, tuple[str, ...]] = {
    "products.csv": ("id",),
    "variants.csv": ("id",),
    "technical_pdfs.csv": ("id",),
    "product_pdfs.csv": ("id",),
}


def resolve_brand_directories(root: Path, folder_args: list[str]) -> list[Path] | None:
    """Explicit brand folders to merge, or ``None`` = every direct child of ``root`` that has CSVs."""
    if not folder_args:
        return None
    root_r = root.resolve()
    out: list[Path] = []
    seen: set[Path] = set()
    for raw in folder_args:
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = (root_r / p).resolve()
        else:
            p = p.resolve()
        if not p.is_dir():
            print(f"[error] not a directory: {p}", file=sys.stderr)
            raise SystemExit(2)
        if p not in seen:
            seen.add(p)
            out.append(p)
    return sorted(out, key=lambda x: str(x).casefold())


def collect_target_paths(root: Path, target: str, brand_dirs: list[Path] | None) -> list[Path]:
    """Sorted list of ``target`` CSV paths (e.g. ``products.csv``) to merge."""
    if brand_dirs is None:
        return sorted(p for p in root.glob(f"*/{target}") if p.is_file())
    return sorted((d / target) for d in brand_dirs if (d / target).is_file())


def read_csv_rows(path: Path) -> tuple[list[str], list[list[str]]]:
    """Return header row and data rows (no parsing beyond csv.reader)."""
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        rows = list(reader)
    if not rows:
        return [], []
    return rows[0], rows[1:]


def sort_key_func(header: list[str], sort_cols: tuple[str, ...]):
    idx_map = {name: i for i, name in enumerate(header)}

    def key(row: list[str]) -> tuple:
        out = []
        for col in sort_cols:
            if col not in idx_map:
                continue
            i = idx_map[col]
            val = row[i] if i < len(row) else ""
            try:
                out.append(int(val))
            except (ValueError, TypeError):
                out.append(val)
        return tuple(out)

    return key


def merge_one(
    name: str,
    paths: list[Path],
    add_source: bool,
    strict_header: bool,
) -> tuple[list[str], list[list[str]]]:
    if not paths:
        raise ValueError("no paths")

    if name == "products.csv":
        canonical = products_canonical_header(paths)
        header0, _ = read_csv_rows(paths[0])
        if not header0:
            raise ValueError(f"empty file: {paths[0]}")
    else:
        header, _ = read_csv_rows(paths[0])
        if not header:
            raise ValueError(f"empty file: {paths[0]}")
        canonical = header
    sort_candidates = SORT_KEYS.get(name, ("id",))
    sort_cols = tuple(c for c in sort_candidates if c in canonical)
    if not sort_cols:
        raise SystemExit(
            f"{name}: cannot sort — none of {sort_candidates} in header {canonical!r}"
        )

    merged: list[list[str]] = []
    out_header = [*canonical, "source"] if add_source else canonical

    for p in paths:
        h, data = read_csv_rows(p)
        if not h:
            continue
        if strict_header:
            if name == "products.csv":
                unknown = [c for c in h if c not in canonical]
                if unknown:
                    raise SystemExit(
                        f"{name}: unknown columns in {p}: {unknown!r}\n"
                        f"canonical is {canonical!r}"
                    )
            elif h != canonical:
                raise SystemExit(
                    f"{name}: header mismatch in {p}\nexpected: {canonical}\n  actual: {h}"
                )
        source_name = p.parent.name
        for row in data:
            if not row or all(cell.strip() == "" for cell in row):
                continue
            if name == "products.csv":
                row = project_row_to_header(h, row, canonical)
            row = align_row_to_canonical(row, canonical)
            if add_source:
                merged.append([*row, source_name])
            else:
                merged.append(row)

    sk = sort_key_func(out_header, sort_cols)
    merged.sort(key=sk)
    return out_header, merged


def idx(header: list[str], name: str) -> int:
    try:
        return header.index(name)
    except ValueError:
        return -1


def products_canonical_header(paths: list[Path]) -> list[str]:
    """Superset column order for ``products.csv``: shared schema + any extra columns from any brand."""
    out = list(PRODUCT_CSV_COLUMNS)
    seen = set(out)
    for p in paths:
        h, _ = read_csv_rows(p)
        for c in h:
            if c not in seen:
                seen.add(c)
                out.append(c)
    return out


def project_row_to_header(file_header: list[str], row: list[str], canonical: list[str]) -> list[str]:
    """Map a data row from ``file_header`` order into ``canonical`` column order (missing → empty)."""
    n = min(len(file_header), len(row))
    by_name = {file_header[i]: row[i] for i in range(n)}
    return [by_name.get(col, "") for col in canonical]


def align_row_to_canonical(row: list[str], canonical: list[str]) -> list[str]:
    """Pad short rows or join overflowing fields so row width matches the header (fixes CSVs where commas
    inside unquoted cells split into extra columns — e.g. fondovalle/products.csv sizes column)."""
    n = len(canonical)
    if len(row) < n:
        return [*row, *[""] * (n - len(row))]
    if len(row) > n:
        return [*row[: n - 1], ",".join(row[n - 1 :])]
    return list(row)


def source_cell(header: list[str], row: list[str]) -> str:
    """Value for the ``source`` column. Rows may be wider than ``header`` (ragged merges); the folder name
    is always appended last in merge_one, so the last cell is authoritative."""
    if not row or idx(header, "source") < 0:
        return ""
    if header and header[-1] == "source":
        return (row[-1] or "").strip()
    i = header.index("source")
    return (row[i] or "").strip() if i < len(row) else ""


def parse_int(cell: str) -> int | None:
    try:
        return int((cell or "").strip())
    except ValueError:
        return None


def fk_map_key(add_source: bool, source: str, num_id: int) -> Any:
    """Key for (source, id) lookups when updating FK columns."""
    return (source.strip(), num_id) if add_source else num_id


def global_unique_integer_column(
    rows: list[list[str]],
    i_id: int,
    add_source: bool,
    label: str,
) -> tuple[list[list[str]], dict[Any, int], int]:
    """
    First row in iteration order wins each integer id; rows that reuse an id already seen
    are appended at the end with fresh integer ids.

    Returns (updated rows, mapping old_key -> final int id, count moved).
    old_key is (source, old_id) when add_source else old_id only.
    Mapping includes identity entries for rows that kept their ids.
    """
    if i_id < 0:
        return rows, {}, 0

    used_global: set[int] = set()
    out: list[list[str]] = []
    deferred: list[tuple[list[str], int, str]] = []

    def src_of(row: list[str]) -> str:
        if not add_source:
            return ""
        return (row[-1] or "").strip() if row else ""

    full_map: dict[Any, int] = {}

    for row in rows:
        r = list(row)
        oid = parse_int(r[i_id] if i_id < len(r) else "")
        src = src_of(r)

        if oid is None:
            out.append(r)
            continue

        key_old = fk_map_key(add_source, src, oid)
        if oid not in used_global:
            used_global.add(oid)
            out.append(r)
            full_map[key_old] = oid
        else:
            deferred.append((r, oid, src))

    max_u = max(used_global) if used_global else 0
    nid = max_u + 1
    moved = 0
    for r, orig_oid, src in deferred:
        key_old = fk_map_key(add_source, src, orig_oid)
        if i_id < len(r):
            r[i_id] = str(nid)
        else:
            while len(r) <= i_id:
                r.append("")
            r[i_id] = str(nid)
        out.append(r)
        full_map[key_old] = nid
        used_global.add(nid)
        moved += 1
        nid += 1

    if moved:
        print(
            f"[remap] {label}: moved {moved} row(s) to end - duplicate global id, assigned new id(s)",
            file=sys.stderr,
        )
    return out, full_map, moved


def apply_product_id_map_to_variants(
    header_v: list[str],
    rows_v: list[list[str]],
    product_map: dict[Any, int],
    add_source: bool,
) -> None:
    """Rewrite product_id using map from (source, old pid) -> new pid."""
    if not product_map:
        return
    i_pid = idx(header_v, "product_id")
    if i_pid < 0:
        return
    for r in rows_v:
        p = parse_int(r[i_pid] if i_pid < len(r) else "")
        if p is None:
            continue
        src = source_cell(header_v, r) if add_source else ""
        k = fk_map_key(add_source, src, p)
        if k in product_map:
            nu = product_map[k]
            if i_pid < len(r):
                r[i_pid] = str(nu)
            else:
                while len(r) <= i_pid:
                    r.append("")
                r[i_pid] = str(nu)


def rewrite_fk_column(
    header: list[str],
    rows: list[list[str]],
    column: str,
    id_map: dict[Any, int],
    add_source: bool,
) -> None:
    """Rewrite numeric FK ``column`` using global_unique_integer_column id map."""
    if not id_map:
        return
    ic = idx(header, column)
    if ic < 0:
        return
    for r in rows:
        old = parse_int(r[ic] if ic < len(r) else "")
        if old is None:
            continue
        src = source_cell(header, r) if add_source else ""
        k = fk_map_key(add_source, src, old)
        if k in id_map:
            nu = id_map[k]
            if ic < len(r):
                r[ic] = str(nu)
            else:
                while len(r) <= ic:
                    r.append("")
                r[ic] = str(nu)


def ensure_globally_unique_skus(
    header: list[str],
    rows: list[list[str]],
) -> tuple[list[list[str]], int]:
    """
    SQLite UNIQUE(sku): keep the first occurrence of each SKU string; append ``-{variant id}``
    to later rows that repeat the same SKU (after stripping). Empty SKUs use the variant id alone.
    """
    i_sku = idx(header, "sku")
    i_id = idx(header, "id")
    if i_sku < 0:
        return rows, 0

    seen: set[str] = set()
    changed = 0
    out: list[list[str]] = []

    for row in rows:
        r = list(row)
        sku_raw = (r[i_sku] if i_sku < len(r) else "").strip()
        vid = (r[i_id] if i_id >= 0 and i_id < len(r) else "").strip() or "?"

        cand = sku_raw
        if cand in seen:
            cand = f"{sku_raw}-{vid}" if sku_raw else vid
            extra = 2
            while cand in seen:
                cand = (
                    f"{sku_raw}-{vid}-{extra}" if sku_raw else f"{vid}-{extra}"
                )
                extra += 1
            while len(r) <= i_sku:
                r.append("")
            r[i_sku] = cand
            changed += 1
        seen.add(cand)
        out.append(r)

    if changed:
        print(
            f"[remap] variants: adjusted {changed} SKU(s) for global uniqueness",
            file=sys.stderr,
        )
    return out, changed


def global_id_remap_pipeline(
    hp: list[str],
    rp: list[list[str]],
    hv: list[str],
    rv: list[list[str]],
    add_source: bool,
) -> tuple[list[list[str]], list[list[str]], dict[Any, int]]:
    """Products -> variants (product_id + variant id). Returns ``prod_map`` for other tables."""
    ip = idx(hp, "id")
    iv = idx(hv, "id")

    rp2, prod_map, _ = global_unique_integer_column(rp, ip, add_source, "products")

    rv_work = [list(x) for x in rv]
    apply_product_id_map_to_variants(hv, rv_work, prod_map, add_source)

    rv2, _var_map, _ = global_unique_integer_column(rv_work, iv, add_source, "variants")

    return rp2, rv2, prod_map


def dedupe_by_composite_key(
    header: list[str],
    rows: list[list[str]],
    key_names: tuple[str, ...],
    label: str,
) -> tuple[list[list[str]], int]:
    """Keep first row per composite key; later duplicates dropped."""
    indices = [idx(header, k) for k in key_names]
    if any(i < 0 for i in indices):
        return rows, 0

    seen: set[tuple[str, ...]] = set()
    out: list[list[str]] = []
    dropped = 0
    for row in rows:
        key = tuple((row[i] if i < len(row) else "").strip() for i in indices)
        if key in seen:
            dropped += 1
            continue
        seen.add(key)
        out.append(row)
    if dropped:
        print(
            f"[dedupe] {label}: dropped {dropped} duplicate row(s) "
            f"(same {', '.join(key_names)}, kept first after sort)",
            file=sys.stderr,
        )
    return out, dropped


def cross_table_cleanup(
    header_p: list[str],
    rows_p: list[list[str]],
    header_v: list[str],
    rows_v: list[list[str]],
    add_source: bool,
) -> tuple[list[list[str]], int]:
    """
    Drop variants whose product_id is not a product id (per source when present).
    Returns (filtered variants, n_variants_dropped).
    """
    if not add_source:
        ip = idx(header_p, "id")
        ivp = idx(header_v, "product_id")

        product_ids = {r[ip].strip() for r in rows_p if ip >= 0 and ip < len(r)}
        kept_v: list[list[str]] = []
        ov = 0
        for r in rows_v:
            pid = r[ivp].strip() if ivp >= 0 and ivp < len(r) else ""
            if pid in product_ids:
                kept_v.append(r)
            else:
                ov += 1

        if ov:
            print(
                f"[orphans] dropped {ov} variant row(s) "
                f"(product_id not found in products)",
                file=sys.stderr,
            )
        return kept_v, ov

    iid = idx(header_p, "id")
    product_keys: set[tuple[str, str]] = set()
    for r in rows_p:
        s = source_cell(header_p, r)
        pid = r[iid].strip() if iid >= 0 and iid < len(r) else ""
        product_keys.add((s, pid))

    v_pid = idx(header_v, "product_id")
    kept_v: list[list[str]] = []
    ov = 0
    for r in rows_v:
        s = source_cell(header_v, r)
        pid = r[v_pid].strip() if v_pid >= 0 and v_pid < len(r) else ""
        if (s, pid) in product_keys:
            kept_v.append(r)
        else:
            ov += 1

    if ov:
        print(
            f"[orphans] dropped {ov} variant row(s) "
            f"(no matching product for same source)",
            file=sys.stderr,
        )
    return kept_v, ov


def apply_duplicate_fixes(
    name: str,
    header: list[str],
    rows: list[list[str]],
    add_source: bool,
) -> tuple[list[str], list[list[str]]]:
    """Per-table duplicate-row removal by natural key."""
    if not rows:
        return header, rows

    if name == "products.csv":
        key: tuple[str, ...] = ("source", "id") if add_source and "source" in header else ("id",)
        rows, _ = dedupe_by_composite_key(header, rows, key, "products")
        return header, rows

    if name == "variants.csv":
        key = ("source", "id") if add_source and "source" in header else ("id",)
        rows, _ = dedupe_by_composite_key(header, rows, key, "variants")
        return header, rows

    if name == "technical_pdfs.csv":
        key = ("source", "id") if add_source and "source" in header else ("id",)
        rows, _ = dedupe_by_composite_key(header, rows, key, "technical_pdfs")
        return header, rows

    if name == "product_pdfs.csv":
        key = ("source", "id") if add_source and "source" in header else ("id",)
        rows, _ = dedupe_by_composite_key(header, rows, key, "product_pdfs")
        return header, rows

    return header, rows


def duplicate_keys(rows: list[list[str]], header: list[str], id_col: str) -> list[str]:
    """Same (id_col) twice in one table with the same composite key — true duplicate rows."""
    if id_col not in header:
        return []
    ix_id = header.index(id_col)

    seen: set[tuple[str, ...]] = set()
    dup_vals: set[str] = set()
    for row in rows:
        if ix_id >= len(row):
            continue
        vid = row[ix_id]
        src = source_cell(header, row) if "source" in header else ""
        key = (vid, src) if "source" in header else (vid,)
        if key in seen:
            dup_vals.add(vid)
        seen.add(key)
    return sorted(dup_vals, key=lambda x: int(x) if x.isdigit() else x)


def ids_shared_across_sources(
    rows: list[list[str]], header: list[str], id_col: str
) -> list[str]:
    """Numeric id values that appear under more than one source (expected with per-brand scrapers)."""
    if id_col not in header or "source" not in header:
        return []
    ix_id = header.index(id_col)
    by_id: dict[str, set[str]] = {}
    for row in rows:
        if ix_id >= len(row):
            continue
        vid = (row[ix_id] or "").strip()
        src = source_cell(header, row)
        if not vid:
            continue
        by_id.setdefault(vid, set()).add(src)
    shared = [k for k, sources in by_id.items() if len(sources) > 1]
    return sorted(shared, key=lambda x: int(x) if x.isdigit() else x)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "folders",
        nargs="*",
        metavar="FOLDER",
        help=(
            "Brand folder names or paths to merge only those sources "
            "(relative to --root unless absolute). Default: all immediate subfolders of --root."
        ),
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="Project root containing brand subfolders (default: script directory)",
    )
    parser.add_argument(
        "--prefix",
        default="merged_",
        help='Output filename prefix (default: "merged_")',
    )
    parser.add_argument(
        "--no-source",
        action="store_true",
        help="Do not add a trailing 'source' column (folder name). "
        "Not recommended when numeric ids overlap across brands.",
    )
    parser.add_argument(
        "--no-strict-header",
        action="store_true",
        help="Allow differing headers between files (still uses first file as column order).",
    )
    parser.add_argument(
        "--no-dedupe",
        action="store_true",
        help="Do not remove duplicate keys or orphan variant rows.",
    )
    parser.add_argument(
        "--no-global-ids",
        action="store_true",
        help="Do not enforce globally unique numeric ids or cascade ids to variants.",
    )
    parser.add_argument(
        "--no-translate",
        action="store_true",
        help="Skip the Romanian translation step that normally runs after merge "
        "(see translate_to_ro.py).",
    )
    parser.add_argument(
        "--translate-workers",
        type=int,
        default=8,
        help="Concurrency for the translation step (default: 8).",
    )
    parser.add_argument(
        "--translate-model",
        default=None,
        help="Cloudflare Workers AI model id for translation "
        "(default uses translate_to_ro.DEFAULT_MODEL).",
    )
    args = parser.parse_args()
    root: Path = args.root.resolve()
    brand_dirs = resolve_brand_directories(root, args.folders)
    add_source = not args.no_source
    strict_header = not args.no_strict_header

    exit_status = 0
    merged: dict[str, tuple[list[str], list[list[str]]]] = {}

    for target in TARGETS:
        paths = collect_target_paths(root, target, brand_dirs)
        if not paths:
            print(f"[skip] no files matching */{target}", file=sys.stderr)
            continue

        header, rows = merge_one(target, paths, add_source=add_source, strict_header=strict_header)
        merged[target] = (header, rows)

    if not args.no_dedupe and merged:
        for target in TARGETS:
            if target not in merged:
                continue
            h, r = merged[target]
            h, r = apply_duplicate_fixes(target, h, r, add_source)
            merged[target] = (h, r)

        if (
            not args.no_global_ids
            and "products.csv" in merged
            and "variants.csv" in merged
        ):
            hp, rp = merged["products.csv"]
            hv, rv = merged["variants.csv"]
            rp2, rv2, prod_map = global_id_remap_pipeline(hp, rp, hv, rv, add_source)
            merged["products.csv"] = (hp, rp2)
            merged["variants.csv"] = (hv, rv2)

            pdf_map: dict[Any, int] = {}
            if "technical_pdfs.csv" in merged:
                ht, rt = merged["technical_pdfs.csv"]
                itid = idx(ht, "id")
                rt2, pdf_map, _ = global_unique_integer_column(
                    rt, itid, add_source, "technical_pdfs"
                )
                merged["technical_pdfs.csv"] = (ht, rt2)

            if "product_pdfs.csv" in merged:
                hpp, rpp = merged["product_pdfs.csv"]
                rpp_work = [list(x) for x in rpp]
                rewrite_fk_column(hpp, rpp_work, "product_id", prod_map, add_source)
                if pdf_map:
                    rewrite_fk_column(hpp, rpp_work, "pdf_id", pdf_map, add_source)
                ipp = idx(hpp, "id")
                rpp2, _, _ = global_unique_integer_column(
                    rpp_work, ipp, add_source, "product_pdfs"
                )
                merged["product_pdfs.csv"] = (hpp, rpp2)

        if "variants.csv" in merged:
            hv, rv = merged["variants.csv"]
            rv_sku, _ = ensure_globally_unique_skus(hv, rv)
            merged["variants.csv"] = (hv, rv_sku)

        if "products.csv" in merged and "variants.csv" in merged:
            hp, rp = merged["products.csv"]
            hv, rv = merged["variants.csv"]
            rv2, _ov = cross_table_cleanup(hp, rp, hv, rv, add_source)
            merged["variants.csv"] = (hv, rv2)

    for target in TARGETS:
        if target not in merged:
            continue
        header, rows = merged[target]

        out_path = root / f"{args.prefix}{target}"
        with out_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f, lineterminator="\n")
            writer.writerow(header)
            writer.writerows(rows)

        paths = collect_target_paths(root, target, brand_dirs)

        sort_col = SORT_KEYS[target][0]
        dups = duplicate_keys(rows, header, sort_col)
        if dups:
            preview = ", ".join(dups[:15])
            more = f" (+{len(dups) - 15} more)" if len(dups) > 15 else ""
            print(
                f"[warn] {target}: duplicate rows same ({sort_col}, source): {preview}{more}",
                file=sys.stderr,
            )
            exit_status = 1

        if add_source:
            overlap = ids_shared_across_sources(rows, header, sort_col)
            if overlap:
                preview = ", ".join(overlap[:25])
                more = f" (+{len(overlap) - 25} more)" if len(overlap) > 25 else ""
                print(
                    f"[info] {target}: same numeric {sort_col} used by multiple sources "
                    f"(distinct rows; DB key is source+{sort_col}): {preview}{more}",
                    file=sys.stderr,
                )

        print(f"[ok] {target}: {len(rows)} rows -> {out_path} ({len(paths)} sources)", flush=True)

    if not args.no_translate and "products.csv" in merged:
        rc = _run_translation_step(
            root=root,
            prefix=args.prefix,
            workers=args.translate_workers,
            model_override=args.translate_model,
        )
        if rc != 0:
            exit_status = exit_status or rc

    return exit_status


def _run_translation_step(
    *,
    root: Path,
    prefix: str,
    workers: int,
    model_override: str | None,
) -> int:
    """Invoke translate_to_ro after the merge step.

    The translator only knows the default merged paths; we honour ``--prefix``
    by refusing to translate when the user has chosen a non-default prefix --
    they probably want a side-by-side comparison run that is not yet the
    canonical Romanian copy.
    """
    if prefix != "merged_":
        print(
            f"[translate] skipped (non-default --prefix={prefix!r}; "
            "run translate_to_ro.py manually if needed)",
            file=sys.stderr,
        )
        return 0

    try:
        import translate_to_ro
    except ImportError as e:
        print(f"[translate] skipped: cannot import translate_to_ro ({e})", file=sys.stderr)
        return 0

    products_csv = root / f"{prefix}products.csv"
    if not products_csv.is_file():
        print(f"[translate] skipped: {products_csv} not found", file=sys.stderr)
        return 0

    print("\n[translate] running Romanian translation step "
          "(disable with --no-translate)", flush=True)
    model = model_override or translate_to_ro.DEFAULT_MODEL
    try:
        return translate_to_ro.run(
            limit=None,
            model=model,
            workers=workers,
            force=False,
            dry_run=False,
            no_snapshot=False,
            force_snapshot=True,
        )
    except translate_to_ro.CloudflareAuthError as e:
        print(f"\n[translate] aborted: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
