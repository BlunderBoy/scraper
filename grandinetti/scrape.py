"""
Grandinetti — scrape WooCommerce product pages from ``links.csv``.

SKU: ``COD REFERINTA`` when set; otherwise ``GN_<variant_id>``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scraper_brand_utils import (
    clean_cell,
    created_stamp_now,
    dedupe_urls,
    default_color,
    enrich_technical_pdf_title,
    join_unique_csv,
    norm_key,
    normalize_category,
    normalize_space,
    total_gallery_count,
    variant_sku,
    write_brand_outputs,
)

MANUFACTURER = "Grandinetti"
SKU_PREFIX = "GN"
LINKS_CSV = "links.csv"
BASE = "https://www.grandinetti.it"

# Introductory technical PDF linked from many product pages (embedded URLs are sometimes malformed).
KNOWLEDGE_PDF_GRANIGLIA_EN = "https://www.grandinetti.it/pdf/conoscere_la_graniglia_eng.pdf"

START_PRODUCT_ID = 500
START_VARIANT_ID = 2000
START_TECH_PDF_ID = 1
START_PRODUCT_PDF_ID = 1

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def fetch_soup(url: str, session: requests.Session) -> BeautifulSoup | None:
    try:
        r = session.get(url, timeout=25)
        if r.status_code != 200:
            print(f"  HTTP {r.status_code} for {url}")
            return None
        return BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        print(f"  Request failed {url}: {e}")
        return None


def page_title(soup: BeautifulSoup) -> str:
    h1 = soup.select_one("h1.product_title") or soup.select_one("h1")
    if h1:
        return h1.get_text(" ", strip=True)
    if soup.title and soup.title.string:
        return normalize_space(soup.title.string.split("|")[0].strip())
    return ""


def gallery_urls(soup: BeautifulSoup) -> list[str]:
    out: list[str] = []
    og = soup.select_one('meta[property="og:image"]')
    if og and og.get("content"):
        out.append(og["content"].strip())
    for img in soup.select('[class*="woocommerce-product-gallery"] img'):
        src = None
        for attr in ("data-large_image", "data-src", "src"):
            v = img.get(attr)
            if v and "wp-content" in v:
                src = v
                break
        if src:
            from urllib.parse import urljoin

            out.append(urljoin(BASE, src))
    return dedupe_urls(out)


def technical_documents(soup: BeautifulSoup) -> list[dict[str, str]]:
    from urllib.parse import urljoin

    root = soup.select_one("main") or soup
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for a in root.select("a[href]"):
        href = (a.get("href") or "").strip()
        fixed = normalize_grandinetti_pdf_url(href)
        if not fixed or ".pdf" not in fixed.casefold():
            continue
        u = urljoin(BASE, fixed) if fixed.startswith("/") else fixed
        if u in seen:
            continue
        seen.add(u)
        label = a.get_text(" ", strip=True)
        out.append({"url": u, "title": label or u.rsplit("/", 1)[-1]})
    return out


def normalize_grandinetti_pdf_url(raw: str) -> str | None:
    """Recover ``https://…/*.pdf`` (or site-relative ``…/*.pdf``) from broken attributes."""
    from urllib.parse import unquote

    if not raw:
        return None
    s = unquote(raw.strip())
    for q in ('"', "'", "\u201c", "\u201d", "\u2018", "\u2019"):
        s = s.replace(q, "")
    if s.startswith("/") and re.search(r"\.pdf$", s, re.I):
        return s
    m = re.search(r"(https?://[^\s<>\"]+\.(?:pdf|pd)f?)", s, re.I)
    if not m:
        m = re.search(r"(https?://[^\s<>\"]+\.pd)\b", s, re.I)
    if not m:
        return None
    u = m.group(1)
    if u.lower().endswith(".pd") and not u.lower().endswith(".pdf"):
        u = u + "f"
    return u


def pdf_links_from_short_description(soup: BeautifulSoup) -> list[dict[str, str]]:
    from urllib.parse import urljoin

    sd = soup.select_one(".woocommerce-product-details__short-description")
    if not sd:
        return []
    out: list[dict[str, str]] = []
    for a in sd.select("a[href]"):
        fixed = normalize_grandinetti_pdf_url(a.get("href") or "")
        if not fixed:
            continue
        u = urljoin(BASE, fixed) if fixed.startswith("/") else fixed
        label = clean_cell(a.get_text(" ", strip=True)) or u.rsplit("/", 1)[-1].replace(".pdf", "")
        out.append({"url": u, "title": label})
    return out


def finishes_from_short_description(soup: BeautifulSoup) -> list[str]:
    """Lines under ``FINISHES`` / ``FINITURE`` in the Woo short-description block."""
    sd = soup.select_one(".woocommerce-product-details__short-description")
    if not sd:
        return []
    lines = [normalize_space(x) for x in sd.get_text("\n", strip=True).split("\n")]
    lines = [x for x in lines if x]
    for i, line in enumerate(lines):
        if norm_key(line) in (norm_key("FINISHES"), norm_key("FINITURE")):
            out: list[str] = []
            for j in range(i + 1, len(lines)):
                nk = norm_key(lines[j])
                if nk.startswith(norm_key("DOCUMENT")) or nk == norm_key("DOWNLOAD"):
                    break
                out.append(lines[j])
            return out
    return []


_IT_TO_EN_FINISH = {
    "anticato": "Antique",
    "anticato intenso": "Heavy Brushed",
    "anticato intenso (r10)": "Heavy Brushed (R10)",
    "anticato profondo": "Deep Heavy Brushed",
    "anticato profondo (r11)": "Deep Heavy Brushed (R11)",
    "levigato": "Honed",
    "levigato fine": "Honed",
    "levigato fine e bisellato": "Honed And Bevelled",
    "lucido": "Polished",
    "lucido e bisellato": "Polished And Bevelled",
    "opaco": "Opaque",
    "opaco (da levigare in opera)": "Opaque",
    "opaco (da lucidare in opera)": "Opaque",
    "naturale": "Natural",
}


_PARENTHETICAL_ON_SITE_RE = re.compile(
    r"\s*\((?:to be [^)]*on site|da [^)]*in opera)\)\s*",
    re.I,
)


def _normalize_finish_label(raw: str) -> str:
    """Strip ``(to be X on site)``/``(da X in opera)``, translate Italian, keep R-grade tail."""
    if not raw:
        return ""
    s = normalize_space(raw)
    s = _PARENTHETICAL_ON_SITE_RE.sub(" ", s).strip()
    m = re.search(r"\((R\d{1,2})\)\s*$", s)
    grade = m.group(1) if m else ""
    base = s[: m.start()].rstrip() if m else s
    key = norm_key(base)
    en = _IT_TO_EN_FINISH.get(key, base.title() if base else "")
    if grade:
        return f"{en} ({grade})" if en else f"({grade})"
    return en


def _format_dim(num: str) -> str:
    """``"1-2"`` -> ``"1.2"``; trims trailing zeros."""
    s = num.replace("-", ".").strip().rstrip(".")
    return s


def extract_size_axes(soup: BeautifulSoup) -> tuple[list[tuple[str, str]], list[str]]:
    """Pull ``WxHxT cm`` taxonomy tokens from the WooCommerce product meta.

    Returns ``(face_sizes, thicknesses)`` where ``face_sizes`` is a list of (W, H)
    pairs (without thickness, e.g. ``("20", "20")``) and ``thicknesses`` is the list
    of distinct thickness values in cm (e.g. ``["1.2", "2"]``).
    """
    faces: list[tuple[str, str]] = []
    thicknesses: list[str] = []
    seen_face: set[tuple[str, str]] = set()
    seen_t: set[str] = set()

    def _add(w: str, h: str, t: str) -> None:
        f = (w, h)
        if f not in seen_face:
            seen_face.add(f)
            faces.append(f)
        if t and t not in seen_t:
            seen_t.add(t)
            thicknesses.append(t)

    for a in soup.select(".posted_in a[href], .product_meta a[rel=tag], a[href*='product-category']"):
        href = (a.get("href") or "").strip()
        m = re.search(r"/(?:size/)?(\d+)x(\d+)x([\d-]+)-cm/?", href, re.I)
        if m:
            _add(m.group(1), m.group(2), _format_dim(m.group(3)))
            continue
        txt = a.get_text(" ", strip=True)
        m2 = re.search(r"(\d+)\s*[x×]\s*(\d+)\s*[x×]\s*([\d.]+)\s*cm", txt, re.I)
        if m2:
            _add(m2.group(1), m2.group(2), _format_dim(m2.group(3)))

    faces.sort(key=lambda p: (int(p[0]), int(p[1])))
    thicknesses.sort(key=lambda s: float(s))
    return faces, thicknesses


def format_face_sizes(faces: list[tuple[str, str]]) -> str:
    return ", ".join(f"{w}x{h} cm" for w, h in faces)


def format_thicknesses(thicknesses: list[str]) -> str:
    return ", ".join(f"{t} cm" for t in thicknesses)


def merged_technical_documents(soup: BeautifulSoup) -> list[dict[str, str]]:
    """Main-area PDFs + short-description links (fixes malformed hrefs) + graniglia knowledge PDF."""
    seen: set[str] = set()
    ordered: list[dict[str, str]] = []

    def push(doc: dict[str, str]) -> None:
        u = doc["url"]
        if u not in seen:
            seen.add(u)
            ordered.append({"url": u, "title": doc["title"]})

    for doc in technical_documents(soup):
        push(doc)
    for doc in pdf_links_from_short_description(soup):
        push(doc)
    push(
        {
            "url": KNOWLEDGE_PDF_GRANIGLIA_EN,
            "title": "Conoscere la graniglia (EN) — Grandinetti",
        }
    )
    return ordered


def variant_color(row: pd.Series) -> str:
    v = clean_cell(row.get("Variante culori"))
    if v:
        return default_color(normalize_space(v.title()))
    n = clean_cell(row.get("Nume variante/SUBTITLU"))
    if n:
        return default_color(normalize_space(n.title()))
    return "Standard"


def csv_product_name(row: pd.Series) -> str:
    orig = clean_cell(row.get("Nume produs original"))
    np = clean_cell(row.get("Nume produs"))
    return orig if orig else np


def load_links(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, encoding="utf-8-sig")
    for col in (
        "Categorie",
        "Subcategorie",
        "SUB-SUBCATEGORIE",
        "Colectie",
        "Nume produs",
    ):
        if col in df.columns:
            df[col] = df[col].ffill()
    return df


def product_key(row: pd.Series) -> tuple[str, str, str, str, str]:
    return (
        norm_key(clean_cell(row.get("Categorie"))),
        norm_key(clean_cell(row.get("Subcategorie"))),
        norm_key(clean_cell(row.get("SUB-SUBCATEGORIE"))),
        norm_key(clean_cell(row.get("Colectie"))),
        norm_key(clean_cell(row.get("Nume produs"))),
    )


def extract_product(
    soup: BeautifulSoup,
    categorie: str,
    subcategorie: str,
    sub_sub: str,
    colectie: str,
    np: str,
    finishes_labels: list[str],
    *,
    csv_title: str,
) -> dict[str, Any]:
    scraped_title = page_title(soup)
    title = clean_cell(csv_title) or scraped_title or np
    page_finishes = finishes_from_short_description(soup)
    raw_finishes = [
        x for x in (*finishes_labels, *page_finishes) if clean_cell(x)
    ]
    normalized_finishes = [_normalize_finish_label(x) for x in raw_finishes]
    finishes = join_unique_csv(normalized_finishes)
    faces, thicknesses = extract_size_axes(soup)
    sizes = format_face_sizes(faces)
    thickness = format_thicknesses(thicknesses)
    return {
        "title": title,
        "description": "",
        "category": normalize_category(categorie),
        "type": subcategorie,
        "collection": colectie,
        "is_new": False,
        "subtype": sub_sub,
        "manufacturer": MANUFACTURER,
        "catalog_id": None,
        "finishes": finishes,
        "position": "",
        "sizes": sizes,
        "thickness": thickness,
        "material": "",
        "shape": "",
        "cut": "",
        "diameter": "",
        "length": "",
        "width": "",
        "height": "",
    }


def scrape(*, limit_rows: int | None = None) -> None:
    script_dir = Path(__file__).resolve().parent
    csv_path = script_dir / LINKS_CSV
    df = load_links(csv_path)

    session = requests.Session()
    session.headers.update(HEADERS)

    data_rows: list[pd.Series] = []
    for _, row in df.iterrows():
        link = clean_cell(row.get("Link variante"))
        np = clean_cell(row.get("Nume produs"))
        if not link.startswith("http") or not np:
            continue
        data_rows.append(row)

    if limit_rows is not None:
        data_rows = data_rows[: max(0, limit_rows)]

    key_order: list[tuple[str, str, str, str, str]] = []
    key_to_rows: dict[tuple[str, str, str, str, str], list[pd.Series]] = {}
    for row in data_rows:
        k = product_key(row)
        if k not in key_to_rows:
            key_order.append(k)
            key_to_rows[k] = []
        key_to_rows[k].append(row)

    products_db: list[dict[str, Any]] = []
    variants_db: list[dict[str, Any]] = []
    technical_pdfs_db: list[dict[str, Any]] = []
    product_pdfs_db: list[dict[str, Any]] = []
    pdf_url_to_id: dict[str, int] = {}

    p_id = START_PRODUCT_ID
    v_id = START_VARIANT_ID
    pdf_id_counter = START_TECH_PDF_ID
    pp_id_counter = START_PRODUCT_PDF_ID
    stamp = created_stamp_now()

    cache: dict[str, BeautifulSoup] = {}

    def get_soup(u: str) -> BeautifulSoup | None:
        if u in cache:
            return cache[u]
        soup = fetch_soup(u, session)
        if soup:
            cache[u] = soup
        time.sleep(0.06)
        return soup

    product_key_to_id: dict[tuple[str, str, str, str, str], int] = {}

    for k in key_order:
        group = key_to_rows[k]
        finishes_labels = sorted(
            {clean_cell(r.get("Variante culori")) for r in group if clean_cell(r.get("Variante culori"))},
            key=lambda s: s.casefold(),
        )
        first = group[0]
        soup = get_soup(clean_cell(first.get("Link variante")))
        if not soup:
            print(f"\n=== SKIP product (fetch failed) {k!r} ===")
            continue

        categorie = clean_cell(first.get("Categorie"))
        subcategorie = clean_cell(first.get("Subcategorie"))
        sub_sub = clean_cell(first.get("SUB-SUBCATEGORIE"))
        colectie = clean_cell(first.get("Colectie"))
        np = clean_cell(first.get("Nume produs"))

        row_dict = extract_product(
            soup,
            categorie,
            subcategorie,
            sub_sub,
            colectie,
            np,
            finishes_labels,
            csv_title=csv_product_name(first),
        )
        row_dict["id"] = p_id
        products_db.append(row_dict)
        product_key_to_id[k] = p_id

        docs = merged_technical_documents(soup)
        for sort_i, doc in enumerate(docs):
            u = doc["url"]
            if u not in pdf_url_to_id:
                pdf_url_to_id[u] = pdf_id_counter
                if u == KNOWLEDGE_PDF_GRANIGLIA_EN:
                    pdf_title = "Conoscere la graniglia (EN) — Grandinetti"
                else:
                    pdf_title = enrich_technical_pdf_title(
                        doc["title"],
                        product_title=clean_cell(row_dict.get("title", "")),
                        collection=clean_cell(row_dict.get("collection", "")),
                    )
                technical_pdfs_db.append(
                    {
                        "id": pdf_id_counter,
                        "title": pdf_title,
                        "r2_key": "",
                        "url": u,
                        "created_at": stamp,
                    }
                )
                pdf_id_counter += 1
            product_pdfs_db.append(
                {
                    "id": pp_id_counter,
                    "product_id": p_id,
                    "pdf_id": pdf_url_to_id[u],
                    "sort_order": sort_i,
                    "created_at": stamp,
                }
            )
            pp_id_counter += 1

        print(f"\n  product id={p_id} | {row_dict.get('title')!r} | PDFs: {len(docs)}")
        p_id += 1

    for row in data_rows:
        k = product_key(row)
        if k not in product_key_to_id:
            continue
        pid = product_key_to_id[k]
        link = clean_cell(row.get("Link variante"))
        soup = get_soup(link)
        gurls: list[str] = []
        if soup:
            gurls = gallery_urls(soup)

        sku = variant_sku(SKU_PREFIX, v_id, clean_cell(row.get("COD REFERINTA")))
        col = variant_color(row)

        variants_db.append(
            {
                "id": v_id,
                "product_id": pid,
                "sku": sku,
                "color": col,
                "url": link,
                "gallery_photos": json.dumps(gurls, ensure_ascii=False),
                "technical_photos": json.dumps([], ensure_ascii=False),
            }
        )
        print(f"  variant {sku} | {col} | {len(gurls)} imgs")
        v_id += 1
        time.sleep(0.05)

    write_brand_outputs(
        script_dir,
        products=products_db,
        variants=variants_db,
        technical_pdfs=technical_pdfs_db,
        product_pdfs=product_pdfs_db,
    )
    print(
        f"\nDone. {len(products_db)} products, {len(variants_db)} variants, "
        f"{len(technical_pdfs_db)} PDFs, {total_gallery_count(variants_db)} gallery URLs -> {script_dir}"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    scrape(limit_rows=args.limit)


if __name__ == "__main__":
    main()
