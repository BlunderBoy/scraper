#!/usr/bin/env python3
"""
One-shot migration: fold every ``variant_photos.csv`` into its sibling
``variants.csv`` as two new JSON-array columns and delete the obsolete file.

Affected inputs:
  - <brand>/variants.csv  +  <brand>/variant_photos.csv        (per-brand)
  - merged_variants.csv   +  merged_variant_photos.csv         (root merge)

For each variant row we append:
  - gallery_photos    JSON array of photo URLs where kind == 'gallery' (or empty)
  - technical_photos  JSON array of photo URLs where kind indicates technical

Rows in ``variant_photos.csv`` that don't match any variant_id are ignored.
Photos preserve their original order (stable-sorted by fid when present).

Existing brand scraper output had every kind='gallery', so in practice all URLs
end up under gallery_photos and technical_photos is an empty list.

Run once:  python collapse_variant_photos.py

The script is idempotent: if ``variants.csv`` already has the two columns it is
left unchanged (so running after a fresh scrape is safe).
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent

PHOTO_ID_ALIASES = ("fid", "id")  # bathco uses 'id' for the photo row id
GALLERY_KINDS = {"gallery", "", None, "photo", "image"}


def _find_pairs() -> list[tuple[str, Path, Path]]:
    """Return (label, variants_path, photos_path) pairs that exist on disk."""
    pairs: list[tuple[str, Path, Path]] = []
    merged_v = ROOT / "merged_variants.csv"
    merged_p = ROOT / "merged_variant_photos.csv"
    if merged_v.exists():
        pairs.append(("merged", merged_v, merged_p))
    for variants in sorted(ROOT.glob("*/variants.csv")):
        if variants.parent == ROOT:
            continue
        photos = variants.with_name("variant_photos.csv")
        pairs.append((variants.parent.name, variants, photos))
    return pairs


def _read_csv(path: Path) -> tuple[list[str], list[list[str]]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        return [], []
    return rows[0], rows[1:]


def _photo_row_key(header: list[str], row: list[str]) -> int:
    for alias in PHOTO_ID_ALIASES:
        if alias in header:
            i = header.index(alias)
            if i < len(row):
                try:
                    return int(row[i])
                except ValueError:
                    return 0
    return 0


def _classify_kind(kind: str) -> str:
    """Return 'gallery' or 'technical'."""
    k = (kind or "").strip().lower()
    if k in GALLERY_KINDS:
        return "gallery"
    if k in {"technical", "tech", "drawing", "technical_drawing", "spec", "pdf"}:
        return "technical"
    # Anything else -> treat as gallery (safe default).
    return "gallery"


def _photos_grouped_by_variant(
    photos_header: list[str],
    photos_rows: list[list[str]],
) -> dict[str, tuple[list[str], list[str]]]:
    """Return {variant_id_str: ([gallery_urls...], [technical_urls...])}.

    Keyed as a string because the per-brand CSVs leave numeric ids un-parsed
    and we want exact textual match with `variants.id`.
    """
    i_vid = photos_header.index("variant_id") if "variant_id" in photos_header else -1
    i_url = photos_header.index("url") if "url" in photos_header else -1
    i_kind = photos_header.index("kind") if "kind" in photos_header else -1
    if i_vid < 0 or i_url < 0:
        return {}

    # Sort by photo id so output order matches the source file's id ordering.
    sorted_rows = sorted(photos_rows, key=lambda r: _photo_row_key(photos_header, r))

    grouped: dict[str, tuple[list[str], list[str]]] = {}
    for row in sorted_rows:
        vid = (row[i_vid] or "").strip() if i_vid < len(row) else ""
        url = (row[i_url] or "").strip() if i_url < len(row) else ""
        if not vid or not url:
            continue
        kind = (row[i_kind] or "").strip() if i_kind >= 0 and i_kind < len(row) else ""
        bucket = _classify_kind(kind)
        g, t = grouped.setdefault(vid, ([], []))
        (g if bucket == "gallery" else t).append(url)
    return grouped


def _augment_variants(
    variants_header: list[str],
    variants_rows: list[list[str]],
    grouped: dict[str, tuple[list[str], list[str]]],
) -> tuple[list[str], list[list[str]]]:
    already = "gallery_photos" in variants_header and "technical_photos" in variants_header
    if already:
        return variants_header, variants_rows

    new_header = [*variants_header, "gallery_photos", "technical_photos"]
    i_id = variants_header.index("id") if "id" in variants_header else -1
    out: list[list[str]] = []
    for row in variants_rows:
        vid = (row[i_id] or "").strip() if i_id >= 0 and i_id < len(row) else ""
        g, t = grouped.get(vid, ([], []))
        # Preserve original trailing source col (if present) by appending at
        # the right position: the merged file uses 'source' as the trailing
        # column, so we need to insert before it.
        if variants_header and variants_header[-1] == "source":
            new_header = [*variants_header[:-1], "gallery_photos", "technical_photos", "source"]
            src_cell = row[-1] if row else ""
            body = row[:-1] if row else row
            out.append(
                [
                    *body,
                    json.dumps(g, ensure_ascii=False),
                    json.dumps(t, ensure_ascii=False),
                    src_cell,
                ]
            )
        else:
            new_header = [*variants_header, "gallery_photos", "technical_photos"]
            out.append(
                [
                    *row,
                    json.dumps(g, ensure_ascii=False),
                    json.dumps(t, ensure_ascii=False),
                ]
            )
    return new_header, out


def _write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, quoting=csv.QUOTE_NONNUMERIC, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)


def collapse(label: str, variants_path: Path, photos_path: Path) -> None:
    print(f"[{label}] variants={variants_path}  photos={photos_path}", flush=True)

    v_header, v_rows = _read_csv(variants_path)
    if not v_header:
        print(f"  [skip] {variants_path}: empty", flush=True)
        return

    if "gallery_photos" in v_header and "technical_photos" in v_header:
        print("  already migrated (columns present) — nothing to do", flush=True)
        # Still delete any leftover variant_photos.csv
        if photos_path.exists():
            photos_path.unlink()
            print(f"  removed {photos_path}", flush=True)
        return

    if photos_path.exists():
        p_header, p_rows = _read_csv(photos_path)
        # Normalise 'id' alias in the photo id column (bathco uses 'id').
        if p_header and p_header[0] == "id" and "fid" not in p_header:
            p_header = ["fid", *p_header[1:]]
        grouped = _photos_grouped_by_variant(p_header, p_rows)
    else:
        grouped = {}

    new_header, new_rows = _augment_variants(v_header, v_rows, grouped)
    _write_csv(variants_path, new_header, new_rows)

    gallery_total = sum(len(g) for g, _ in grouped.values())
    tech_total = sum(len(t) for _, t in grouped.values())
    matched = sum(
        1
        for r in new_rows
        if (
            "gallery_photos" in new_header
            and r[new_header.index("gallery_photos")] not in ("[]", '"[]"')
        )
    )
    print(
        f"  variants rows={len(new_rows)}  "
        f"matched with photos={matched}  "
        f"gallery_urls={gallery_total}  technical_urls={tech_total}",
        flush=True,
    )

    if photos_path.exists():
        photos_path.unlink()
        print(f"  removed {photos_path}", flush=True)


def main() -> int:
    pairs = _find_pairs()
    if not pairs:
        print("No variants.csv found.", file=sys.stderr)
        return 1
    for label, vp, pp in pairs:
        collapse(label, vp, pp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
