"""
More Möbel -- scrape product pages from ``links.csv``.

The CSV has three separate variant columns:
``Variante LEMN/METAL``, ``Variante PIELE`` and ``Variante TEXTIL``.
Per the plan they are merged into a single ``color`` field separated with
" / " so each row becomes one variant. Many products list only leather +
fabric (wood/metal blank); empty columns are dropped.

Product grouping key MUST include ``SUB-SUBCATEGORIE`` because the same
``Nume produs`` (e.g. "OSO") shows up under FOTOLII, SCAUNE and MESE -- those
are different products.

The ``obs`` column carries general display instructions (not product copy)
and is intentionally ignored.

Pages are at ``https://www.more-moebel.de/en/item/<slug>`` and contain the
description, a product details table (UPHOLSTERY / FRAME / OPTIONS), 3-5
photos and a single data-sheet PDF.

SKU: no ``COD REFERINTA`` -> ``MORE_<variant_id>``.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup, Tag

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from scraper_brand_utils import (
    clean_cell,
    created_stamp_now,
    dedupe_urls,
    enrich_technical_pdf_title,
    norm_key,
    normalize_space,
    total_gallery_count,
    variant_sku,
    write_brand_outputs,
)

MANUFACTURER = "More"
SKU_PREFIX = "MORE"
LINKS_CSV = "links.csv"
BASE_ORIGIN = "https://www.more-moebel.de"

START_PRODUCT_ID = 1400
START_VARIANT_ID = 8500
START_TECH_PDF_ID = 1
START_PRODUCT_PDF_ID = 1

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
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
    h1 = soup.select_one("h1")
    txt = h1.get_text(" ", strip=True) if h1 else ""
    # site renders H1 like "OSO Easy Chair – more" -- drop the brand suffix.
    txt = re.sub(r"\s*[\u2013\u2014-]\s*more\s*$", "", txt)
    return normalize_space(txt)


def page_description(soup: BeautifulSoup) -> str:
    parts: list[str] = []
    section = soup.select_one('[class*="section-description"]')
    if section:
        for p in section.find_all(["p"]):
            t = normalize_space(p.get_text(" ", strip=True))
            if t and len(t) > 25:
                parts.append(t)
        if not parts:
            t = normalize_space(section.get_text(" ", strip=True))
            if t and len(t) > 25:
                parts.append(t)

    details = soup.select_one('[class*="section-product-details"]')
    if details:
        rows: list[str] = []
        for el in details.find_all(["dt", "dd", "li", "td", "th", "p", "h3", "strong"]):
            t = normalize_space(el.get_text(" ", strip=True))
            if not t or t.lower() in ("product video", "product details"):
                continue
            rows.append(t)
        joined = " ".join(rows).strip()
        if joined:
            parts.append(joined)
    return "\n\n".join(dict.fromkeys(parts))


def hero_gallery(soup: BeautifulSoup, page_url: str) -> list[str]:
    out: list[str] = []
    og = soup.select_one('meta[property="og:image"]')
    if og and og.get("content"):
        out.append(urljoin(page_url, og["content"].strip()))
    for img in soup.select('img[src*="/upload/"]'):
        src = (img.get("src") or "").strip()
        if not src or "logo" in src.lower():
            continue
        out.append(urljoin(page_url, src))
    return dedupe_urls(out)


def collect_pdfs(soup: BeautifulSoup, page_url: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for a in soup.select('a[href$=".pdf"]'):
        href = (a.get("href") or "").strip()
        if not href:
            continue
        u = urljoin(page_url, href)
        if u in seen:
            continue
        seen.add(u)
        label = normalize_space(a.get_text(" ", strip=True)) or u.rsplit("/", 1)[-1].replace(".pdf", "")
        out.append({"url": u, "title": label})
    return out


def merged_variant_color(row: pd.Series) -> str:
    parts: list[str] = []
    for col in ("Variante LEMN/METAL", "Variante PIELE", "Variante TEXTIL"):
        v = clean_cell(row.get(col))
        if v:
            parts.append(normalize_space(v))
    return " / ".join(parts)


def load_links(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, encoding="utf-8-sig")
    for col in ("Categorie", "Subcategorie", "SUB-SUBCATEGORIE", "Colectie", "Nume produs", "Link variante"):
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


def scrape(*, limit_rows: int | None = None) -> None:
    script_dir = Path(__file__).resolve().parent
    df = load_links(script_dir / LINKS_CSV)

    data_rows: list[pd.Series] = []
    for _, row in df.iterrows():
        link = clean_cell(row.get("Link variante"))
        np_ = clean_cell(row.get("Nume produs"))
        if not link.startswith("http") or not np_:
            continue
        if not merged_variant_color(row):
            # Skip rows that have no finishes; can be a header artefact.
            continue
        data_rows.append(row)

    if limit_rows is not None:
        data_rows = data_rows[: max(0, limit_rows)]

    session = requests.Session()
    session.headers.update(HEADERS)

    cache: dict[str, BeautifulSoup] = {}

    def get_soup(url: str) -> BeautifulSoup | None:
        if url in cache:
            return cache[url]
        soup = fetch_soup(url, session)
        if soup is not None:
            cache[url] = soup
        time.sleep(0.08)
        return soup

    key_order: list[tuple] = []
    key_to_rows: dict[tuple, list[pd.Series]] = {}
    for r in data_rows:
        k = product_key(r)
        if k not in key_to_rows:
            key_order.append(k)
            key_to_rows[k] = []
        key_to_rows[k].append(r)

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

    for k in key_order:
        group = key_to_rows[k]
        first = group[0]
        url = clean_cell(first.get("Link variante"))
        soup = get_soup(url)
        if soup is None:
            print(f"  SKIP product (fetch failed) {url}")
            continue

        np_ = clean_cell(first.get("Nume produs"))
        categorie = clean_cell(first.get("Categorie"))
        subcategorie = clean_cell(first.get("Subcategorie"))
        sub_sub = clean_cell(first.get("SUB-SUBCATEGORIE"))
        colectie = clean_cell(first.get("Colectie"))

        page_title_text = page_title(soup)
        # The site renders some pages with a placeholder H1 of just "more"; fall back to the CSV name.
        if not page_title_text or page_title_text.lower() == "more":
            title = np_
        else:
            title = page_title_text

        description = page_description(soup)
        gallery = hero_gallery(soup, url)
        pdfs = collect_pdfs(soup, url)

        finishes_set: list[str] = []
        seen_fin: set[str] = set()
        for r in group:
            for col in ("Variante LEMN/METAL", "Variante PIELE", "Variante TEXTIL"):
                v = normalize_space(clean_cell(r.get(col)))
                if v and v not in seen_fin:
                    seen_fin.add(v)
                    finishes_set.append(v)
        finishes = ", ".join(finishes_set)

        product = {
            "id": p_id,
            "title": title,
            "description": description,
            "category": categorie.lower() if categorie else "",
            "type": subcategorie,
            "collection": colectie,
            "is_new": False,
            "subtype": sub_sub,
            "manufacturer": MANUFACTURER,
            "catalog_id": None,
            "finishes": finishes,
            "position": "",
            "sizes": "",
            "thickness": "",
            "material": "",
            "shape": "",
        }
        products_db.append(product)

        for sort_i, doc in enumerate(pdfs):
            u = doc["url"]
            if u not in pdf_url_to_id:
                pdf_url_to_id[u] = pdf_id_counter
                technical_pdfs_db.append({
                    "id": pdf_id_counter,
                    "title": enrich_technical_pdf_title(doc["title"], product_title=title, collection=colectie),
                    "r2_key": "",
                    "url": u,
                    "created_at": stamp,
                })
                pdf_id_counter += 1
            product_pdfs_db.append({
                "id": pp_id_counter,
                "product_id": p_id,
                "pdf_id": pdf_url_to_id[u],
                "sort_order": sort_i,
                "created_at": stamp,
            })
            pp_id_counter += 1

        for r in group:
            color = merged_variant_color(r) or "Standard"
            sku = variant_sku(SKU_PREFIX, v_id, clean_cell(r.get("COD REFERINTA")))
            variants_db.append({
                "id": v_id,
                "product_id": p_id,
                "sku": sku,
                "color": color,
                "url": url,
                "gallery_photos": json.dumps(gallery, ensure_ascii=False),
                "technical_photos": json.dumps([], ensure_ascii=False),
            })
            v_id += 1

        print(f"  product id={p_id} | {title!r} | variants={len(group)} | imgs={len(gallery)} | pdfs={len(pdfs)}")
        p_id += 1

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
