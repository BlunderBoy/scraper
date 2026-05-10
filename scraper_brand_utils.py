"""Shared helpers for brand folder scrapers (products/variants/PDF CSVs)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

PRODUCT_CSV_COLUMNS = [
    "id",
    "title",
    "description",
    "category",
    "type",
    "collection",
    "is_new",
    "subtype",
    "manufacturer",
    "catalog_id",
    "finishes",
    "position",
    "sizes",
    "thickness",
    "material",
    "shape",
    "cut",
    "diameter",
    "length",
    "width",
    "height",
]

VARIANT_CSV_COLUMNS = [
    "id",
    "product_id",
    "sku",
    "color",
    "url",
    "gallery_photos",
    "technical_photos",
]


def normalize_space(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def normalize_category(raw: str) -> str:
    """Spreadsheet ``Categorie`` -> output ``category`` (lower-cased; ``placi ceramice`` -> ``ceramice``)."""
    s = (raw or "").strip().lower()
    if not s:
        return ""
    if "placi" in s and ("ceramice" in s or "ceramic" in s.replace(" ", "")):
        return "ceramice"
    return s


def default_color(raw: str) -> str:
    """Variant ``color``: empty / nan-ish -> ``Standard`` (the only fallback the brief allows)."""
    s = (raw or "").strip()
    if not s or s.lower() == "nan":
        return "Standard"
    return s


_RE_NUM = r"(?:\d+(?:[.,]\d+)?)"
_RE_UNIT = r"(?:mm|cm|m)"

# A run of dimensions like ``120x120 cm``, ``50 mm``, ``Length: 1540 mm``, ``Diameter: 50mm``.
_LABEL_PATTERNS = {
    "diameter": re.compile(r"\b(?:diameter|diametro|diam\.?|diametru|⌀|Ø)\s*[:\-]?\s*(" + _RE_NUM + r")\s*(" + _RE_UNIT + r")?", re.I),
    "length":   re.compile(r"\b(?:length|lunghezza|longitud|lungime|lungimea|long\.|long)\s*[:\-]?\s*(" + _RE_NUM + r")\s*(" + _RE_UNIT + r")?", re.I),
    "width":    re.compile(r"\b(?:width|larghezza|ancho|latime|lățime|lăţime|wide|w\.)\s*[:\-]?\s*(" + _RE_NUM + r")\s*(" + _RE_UNIT + r")?", re.I),
    "height":   re.compile(r"\b(?:height|altezza|alto|altura|inaltime|înălțime|înălţime|high|h\.)\s*[:\-]?\s*(" + _RE_NUM + r")\s*(" + _RE_UNIT + r")?", re.I),
}

# Trailing-label patterns: ``189 mm high``, ``145 mm long``, ``50 cm wide``.
_TRAILING_LABEL_PATTERNS = {
    "height": re.compile(r"(" + _RE_NUM + r")\s*(" + _RE_UNIT + r")\s+(?:high|tall|altezza)\b", re.I),
    "length": re.compile(r"(" + _RE_NUM + r")\s*(" + _RE_UNIT + r")\s+(?:long|lungime|lunghezza)\b", re.I),
    "width":  re.compile(r"(" + _RE_NUM + r")\s*(" + _RE_UNIT + r")\s+(?:wide|larghezza)\b", re.I),
}


def _format_mm(v_mm: float) -> str:
    if abs(v_mm - round(v_mm)) < 1e-9:
        return f"{int(round(v_mm))} mm"
    return f"{v_mm:g} mm"


def _to_mm(num: str, unit: str | None) -> float:
    n = float(num.replace(",", "."))
    u = (unit or "mm").strip().lower()
    if u == "mm":
        return n
    if u == "cm":
        return n * 10.0
    if u == "m":
        return n * 1000.0
    return n


def parse_dimensions_from_text(text: str) -> dict[str, str]:
    """Pick out labelled diameter/length/width/height values from free product text.

    Returns a dict with strings like ``"50 mm"`` for whichever keys could be confidently
    parsed from labels (``Diameter:``, ``Width:``, ``W.``, ``H.``, ``Lungime``, ``⌀``)
    or compact ``Ø43x13h cm`` / ``43x13h cm`` patterns.
    Bare unlabelled ``WxHxD`` patterns are ignored (they belong in ``sizes``).
    """
    out: dict[str, str] = {}
    if not text:
        return out
    blob = " ".join((text or "").split())

    # Compact Ø43x13h cm / Ø50 cm patterns commonly used on sanitary-ware sites.
    diam_h = re.compile(
        r"(?:[Ø⌀])\s*(" + _RE_NUM + r")\s*[x×]\s*(" + _RE_NUM + r")\s*h?\s*(" + _RE_UNIT + r")?",
        re.I,
    )
    diam_only = re.compile(r"(?:[Ø⌀])\s*(" + _RE_NUM + r")\s*(" + _RE_UNIT + r")?", re.I)
    m = diam_h.search(blob)
    if m:
        try:
            out.setdefault("diameter", _format_mm(_to_mm(m.group(1), m.group(3))))
            out.setdefault("height", _format_mm(_to_mm(m.group(2), m.group(3))))
        except ValueError:
            pass
    else:
        m = diam_only.search(blob)
        if m:
            try:
                out.setdefault("diameter", _format_mm(_to_mm(m.group(1), m.group(2))))
            except ValueError:
                pass

    for key, pat in _LABEL_PATTERNS.items():
        if key in out:
            continue
        m = pat.search(blob)
        if not m:
            continue
        try:
            mm = _to_mm(m.group(1), m.group(2))
        except (ValueError, AttributeError):
            continue
        out[key] = _format_mm(mm)

    for key, pat in _TRAILING_LABEL_PATTERNS.items():
        if key in out:
            continue
        m = pat.search(blob)
        if not m:
            continue
        try:
            mm = _to_mm(m.group(1), m.group(2))
        except (ValueError, AttributeError):
            continue
        out[key] = _format_mm(mm)
    return out


def split_dimension_token(text: str) -> dict[str, str]:
    """Parse a single bare ``WxH``, ``WxHxT``, or ``LxWxH`` token into width/height/length.

    Used when the page exposes a clean 2- or 3-axis dimension string for one product
    (e.g. ``238 x 92 x 71 cm`` for a sofa). Returns empty dict if the token does not look
    like a single-product spec.
    """
    if not text:
        return {}
    m = re.fullmatch(
        r"\s*(" + _RE_NUM + r")\s*[x×*]\s*(" + _RE_NUM + r")\s*(?:[x×*]\s*(" + _RE_NUM + r"))?\s*("
        + _RE_UNIT + r")?\s*", text, re.I,
    )
    if not m:
        return {}
    a, b, c, u = m.group(1), m.group(2), m.group(3), m.group(4)
    if c is None:
        return {
            "width": _format_mm(_to_mm(a, u)),
            "height": _format_mm(_to_mm(b, u)),
        }
    return {
        "length": _format_mm(_to_mm(a, u)),
        "width": _format_mm(_to_mm(b, u)),
        "height": _format_mm(_to_mm(c, u)),
    }


def join_unique_csv(values, sep: str = ", ") -> str:
    """Lossless de-dupe + ``", "`` join used by the brief's "lists separated by ', '" rule."""
    out: list[str] = []
    seen: set[str] = set()
    for v in values:
        s = normalize_space(str(v or ""))
        if not s:
            continue
        k = s.casefold()
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
    return sep.join(out)


def clean_cell(val: object) -> str:
    try:
        if val is None or pd.isna(val):
            return ""
    except (TypeError, ValueError):
        pass
    s = normalize_space(str(val))
    if s.lower() == "nan":
        return ""
    return s


def norm_key(s: str) -> str:
    return normalize_space(s).casefold()


def enrich_technical_pdf_title(
    raw_title: str,
    *,
    product_title: str = "",
    collection: str = "",
) -> str:
    """
    Make ``technical_pdfs.csv`` titles identifiable: append product and/or collection
    when the anchor/filename text does not already contain them (avoids generic
    duplicates like ``Technical specifications`` across products).
    """
    label = normalize_space(raw_title)
    pt = clean_cell(product_title)
    col = clean_cell(collection)
    extra: list[str] = []
    if pt and pt.casefold() not in label.casefold():
        extra.append(pt)
    if col and col.casefold() not in label.casefold():
        extra.append(col)
    if not label:
        return " — ".join(extra) if extra else "Technical PDF"
    if extra:
        return f"{label} — {' — '.join(extra)}"
    return label


def format_csv_title(nume_produs: str, colectie: str) -> str:
    """Prefer spreadsheet naming: ``Nume produs`` plus ``Colectie`` when they differ."""
    np = clean_cell(nume_produs)
    col = clean_cell(colectie)
    if not np:
        return col
    if col and norm_key(col) not in norm_key(np):
        return f"{np} — {col}"
    return np


def aggregate_unique_column(
    rows: Sequence[Any],
    column: str,
    *,
    sep: str = " | ",
) -> str:
    """Join distinct non-empty values for ``column`` across variant rows (sorted, stable)."""
    vals = sorted(
        {clean_cell(r.get(column)) for r in rows if clean_cell(r.get(column))},
        key=lambda s: s.casefold(),
    )
    return sep.join(vals)


def sanitize_filename(text: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", text).strip()


def variant_sku(prefix: str, variant_id: int, cod_from_csv: str) -> str:
    """Use CSV code when present; else ``PREFIX_variant_id`` (e.g. ``GN_1205``)."""
    cod = clean_cell(cod_from_csv)
    if cod:
        return sanitize_filename(cod.replace("  ", " "))
    return f"{prefix}_{variant_id}"


def dedupe_urls(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def write_brand_outputs(
    script_dir: Path,
    *,
    products: list[dict[str, Any]],
    variants: list[dict[str, Any]],
    technical_pdfs: list[dict[str, Any]],
    product_pdfs: list[dict[str, Any]],
) -> None:
    """Write the four CSV files next to ``scrape.py``."""
    df_p = pd.DataFrame(products)
    if not df_p.empty:
        ordered = [c for c in PRODUCT_CSV_COLUMNS if c in df_p.columns]
        extra = [c for c in df_p.columns if c not in ordered]
        df_p = df_p[ordered + extra]
    df_p.to_csv(script_dir / "products.csv", index=False, encoding="utf-8-sig")

    pd.DataFrame(variants, columns=VARIANT_CSV_COLUMNS).to_csv(
        script_dir / "variants.csv", index=False, encoding="utf-8-sig"
    )

    tp_cols = ["id", "title", "r2_key", "url", "created_at"]
    pd.DataFrame(technical_pdfs, columns=tp_cols).to_csv(
        script_dir / "technical_pdfs.csv", index=False, encoding="utf-8-sig"
    )
    pp_cols = ["id", "product_id", "pdf_id", "sort_order", "created_at"]
    pd.DataFrame(product_pdfs, columns=pp_cols).to_csv(
        script_dir / "product_pdfs.csv", index=False, encoding="utf-8-sig"
    )


def created_stamp_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def total_gallery_count(variants: list[dict[str, Any]]) -> int:
    n = 0
    for v in variants:
        try:
            n += len(json.loads(v.get("gallery_photos") or "[]"))
        except json.JSONDecodeError:
            pass
    return n
