"""
Euval -- scrape product pages from ``links.csv``.

Each row in ``links.csv`` is a separate product (one variant each); every row
has a direct product URL like
``https://euval.com/en/collections/<collection>/<slug>/``.

Pages are Elementor (WordPress). The product hero (description + images +
Technical data sheet table) sits before the ``Last projects`` / ``Other
products`` sections at the bottom -- we cut everything after those headings to
exclude unrelated thumbnails.

SKU: ``COD REFERINTA`` when set, else ``EV_<variant_id>``.
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
    default_color,
    enrich_technical_pdf_title,
    norm_key,
    normalize_category,
    normalize_space,
    parse_dimensions_from_text,
    total_gallery_count,
    variant_sku,
    write_brand_outputs,
)

MANUFACTURER = "Euval"
SKU_PREFIX = "EV"
LINKS_CSV = "links.csv"
BASE_ORIGIN = "https://euval.com"

START_PRODUCT_ID = 1000
START_VARIANT_ID = 6500
START_TECH_PDF_ID = 1
START_PRODUCT_PDF_ID = 1

# euval.com 403s on Chrome desktop UAs; minimal Mozilla string is accepted.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

CUT_HEADINGS = (
    "last projects",
    "other products",
    "find out how to customize your project",
)


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


def trim_after_unrelated_sections(soup: BeautifulSoup) -> Tag:
    """Drop everything from the first 'Last projects' / 'Other products' heading onward."""
    main = soup.select_one("main") or soup.find("body") or soup
    for h in main.select("h2, h3, h4"):
        text = (h.get_text(" ", strip=True) or "").lower()
        if any(needle in text for needle in CUT_HEADINGS):
            for sib in list(h.find_all_next()):
                sib.extract()
            h.extract()
            break
    return main


def page_titles(soup: BeautifulSoup) -> tuple[str, str]:
    h1s = soup.select("h1")
    code = h1s[0].get_text(" ", strip=True) if h1s else ""
    subtitle = h1s[1].get_text(" ", strip=True) if len(h1s) > 1 else ""
    return code, subtitle


def page_description(main: Tag) -> str:
    parts: list[str] = []
    for p in main.select("p"):
        text = p.get_text(" ", strip=True)
        if not text or len(text) < 30:
            continue
        low = text.lower()
        if "via domenico da lugo" in low or "p.iva" in low or "euragglo" in low:
            continue
        if "home |" in low and "collections" in low:
            continue
        parts.append(text)
    return "\n\n".join(dict.fromkeys(parts))


def gallery_urls(main: Tag, soup: BeautifulSoup) -> list[str]:
    out: list[str] = []
    og = soup.select_one('meta[property="og:image"]')
    if og and og.get("content"):
        out.append(og["content"].strip())
    for img in main.select('img[src*="wp-content/uploads"]'):
        src = (img.get("src") or "").strip()
        low = src.lower()
        if any(x in low for x in ("/logo", "logo-euval", "/flags/", "-icon", "icon.png", "elementor/thumbs/logo")):
            continue
        if low.endswith(".svg"):
            continue
        out.append(src)
    return dedupe_urls(out)


def specs_table(main: Tag) -> dict[str, str]:
    out: dict[str, str] = {}
    for tr in main.select("table tr"):
        cells = tr.select("td, th")
        if len(cells) < 2:
            continue
        key = normalize_space(cells[0].get_text(" ", strip=True)).rstrip(":")
        val = normalize_space(cells[1].get_text(" ", strip=True))
        if key and val:
            out[key] = val
    return out


_GENERIC_PDF_BLACKLIST = re.compile(
    r"(voci[\-_ ]di[\-_ ]capitolato|specification[\-_ ]en|specifications?[-_ ]?en)",
    re.I,
)


def pdf_documents(main: Tag, page_url: str) -> list[dict[str, str]]:
    """Per-product PDF (e.g. ``92.50-Carbi.pdf``); strips the generic catalogue boilerplate
    PDFs (``Voci-di-capitolato.pdf`` / ``Specification-en.pdf``) that every Euval page links."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for a in main.select("a[href]"):
        href = (a.get("href") or "").strip()
        if not href.lower().endswith(".pdf"):
            continue
        u = urljoin(page_url, href)
        if u in seen:
            continue
        seen.add(u)
        if _GENERIC_PDF_BLACKLIST.search(u):
            continue
        label = a.get_text(" ", strip=True) or u.rsplit("/", 1)[-1].replace(".pdf", "")
        out.append({"url": u, "title": label})
    return out


def parse_product(url: str, soup: BeautifulSoup) -> dict[str, Any]:
    main = trim_after_unrelated_sections(soup)
    code, subtitle = page_titles(soup)
    description_parts = []
    if subtitle:
        description_parts.append(subtitle)
    body = page_description(main)
    if body:
        description_parts.append(body)
    description = "\n\n".join(description_parts)

    specs = specs_table(main)
    finishes = specs.get("Classification of the anti-slip properties") or ""

    return {
        "title_code": code,
        "subtitle": subtitle,
        "description": description,
        "gallery": gallery_urls(main, soup),
        "pdfs": pdf_documents(main, url),
        "finishes": finishes,
        "specs": specs,
    }


def load_links(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, encoding="utf-8-sig")
    for col in ("Categorie", "Subcategorie", "SUB-SUBCATEGORIE", "Colectie"):
        if col in df.columns:
            df[col] = df[col].ffill()
    return df


def scrape(*, limit_rows: int | None = None) -> None:
    script_dir = Path(__file__).resolve().parent
    df = load_links(script_dir / LINKS_CSV)

    data_rows: list[pd.Series] = []
    for _, row in df.iterrows():
        link = clean_cell(row.get("Link variante"))
        np_ = clean_cell(row.get("Nume produs"))
        if not link.startswith("http") or not np_:
            continue
        data_rows.append(row)

    if limit_rows is not None:
        data_rows = data_rows[: max(0, limit_rows)]

    session = requests.Session()
    session.headers.update(HEADERS)

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

    for row in data_rows:
        url = clean_cell(row.get("Link variante"))
        np_ = clean_cell(row.get("Nume produs"))
        soup = fetch_soup(url, session)
        if not soup:
            print(f"  SKIP {np_!r} -- fetch failed")
            continue
        parsed = parse_product(url, soup)

        # The spreadsheet name (``92.50 Carbi``) is the canonical title; the Euval H1
        # subtitle is keyword-stuffed marketing copy and is intentionally dropped.
        title = np_

        categorie = clean_cell(row.get("Categorie"))
        subcategorie = clean_cell(row.get("Subcategorie"))
        sub_sub = clean_cell(row.get("SUB-SUBCATEGORIE"))
        colectie = clean_cell(row.get("Colectie"))

        product = {
            "id": p_id,
            "title": title,
            "description": parsed["description"],
            "category": normalize_category(categorie),
            "type": subcategorie,
            "collection": colectie,
            "is_new": False,
            "subtype": sub_sub,
            "manufacturer": MANUFACTURER,
            "catalog_id": None,
            "finishes": parsed["finishes"],
            "position": "",
            "sizes": "",
            "thickness": "",
            "material": "Terrazzo",
            "shape": "",
            "diameter": "",
            "length": "",
            "width": "",
            "height": "",
        }
        products_db.append(product)

        for sort_i, doc in enumerate(parsed["pdfs"]):
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

        sku = variant_sku(SKU_PREFIX, v_id, clean_cell(row.get("COD REFERINTA")))
        variants_db.append({
            "id": v_id,
            "product_id": p_id,
            "sku": sku,
            "color": "Standard",
            "url": url,
            "gallery_photos": json.dumps(parsed["gallery"], ensure_ascii=False),
            "technical_photos": json.dumps([], ensure_ascii=False),
        })

        print(f"  product id={p_id} variant id={v_id} | {title!r} | imgs={len(parsed['gallery'])} | pdfs={len(parsed['pdfs'])}")
        p_id += 1
        v_id += 1
        time.sleep(0.08)

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
