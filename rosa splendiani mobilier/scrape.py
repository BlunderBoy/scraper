"""
Rosa Splendiani -- scrape product pages from ``links.csv``.

Site is WordPress + ``qode-advanced-image-gallery``. Each row is one variant
(either a colour finish or a module sub-part for the BAHIA / Tosca modular
sofas). Some rows have ``Variante culori`` populated, others have only
``Nume variante`` (module name) -- both populate the variant ``color`` field
in the output, joined with " / " when both are present.

To avoid pulling in unrelated photos from the "Altri elementi della
collezione" / "Matching and related products" / "Other related products"
sections at the bottom of the page, the parser truncates the DOM at the first
of those headings before harvesting gallery images.

SKU comes from ``COD REFERINTA`` (already prefixed ``ROSA_`` in some rows);
fallback ``ROSA_<variant_id>``.
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

MANUFACTURER = "Rosa Splendiani"
SKU_PREFIX = "ROSA"
LINKS_CSV = "links.csv"

START_PRODUCT_ID = 1500
START_VARIANT_ID = 9000
START_TECH_PDF_ID = 1
START_PRODUCT_PDF_ID = 1

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

SECTION_CUTS = (
    "altri elementi della collezione",
    "matching and related products",
    "other related products",
    "altri prodotti",
    "you may also like",
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


def trim_after_related(soup: BeautifulSoup) -> Tag:
    main = soup.find("body") or soup
    for h in main.select("h2, h3, h4"):
        text = (h.get_text(" ", strip=True) or "").lower()
        if any(needle in text for needle in SECTION_CUTS):
            for sib in list(h.find_all_next()):
                sib.extract()
            h.extract()
            break
    return main


def page_title(soup: BeautifulSoup) -> str:
    h1 = soup.select_one("h1")
    return h1.get_text(" ", strip=True) if h1 else ""


def page_description(main: Tag) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for el in main.find_all(["p", "h3", "h4"]):
        t = normalize_space(el.get_text(" ", strip=True))
        if not t or len(t) < 30:
            continue
        low = t.lower()
        if "p.iva" in low or "p.i. and c.f." in low or "zona industriale" in low or "rosa splendiani" in low and "©" in t:
            continue
        if t in seen:
            continue
        seen.add(t)
        parts.append(t)
    return "\n\n".join(parts)


def gallery_urls(main: Tag) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()

    for a in main.select('a[href*="wp-content/uploads"]'):
        href = (a.get("href") or "").strip().lower()
        if not href.endswith((".jpg", ".jpeg", ".png", ".webp")):
            continue
        url = a.get("href").strip()
        if "favicon" in url.lower() or "logo" in url.lower():
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append(url)

    if not out:
        for img in main.select('img[src*="wp-content/uploads"]'):
            src = (img.get("src") or "").strip()
            if not src or "favicon" in src.lower() or "logo" in src.lower():
                continue
            stripped = re.sub(r"-\d+x\d+(?=\.\w+$)", "", src)
            if stripped not in seen:
                seen.add(stripped)
                out.append(stripped)
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


def variant_color(row: pd.Series) -> str:
    nv = normalize_space(clean_cell(row.get("Nume variante")))
    vc = normalize_space(clean_cell(row.get("Variante culori")))
    parts = [x for x in (nv, vc) if x]
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
        np_ = clean_cell(row.get("Nume produs"))
        if not np_:
            continue
        data_rows.append(row)

    if limit_rows is not None:
        data_rows = data_rows[: max(0, limit_rows)]

    session = requests.Session()
    session.headers.update(HEADERS)

    cache: dict[str, BeautifulSoup] = {}

    def get_soup(url: str) -> BeautifulSoup | None:
        if not url.startswith("http"):
            return None
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
        url = ""
        for r in group:
            u = clean_cell(r.get("Link variante"))
            if u.startswith("http"):
                url = u
                break

        np_ = clean_cell(first.get("Nume produs"))
        categorie = clean_cell(first.get("Categorie"))
        subcategorie = clean_cell(first.get("Subcategorie"))
        sub_sub = clean_cell(first.get("SUB-SUBCATEGORIE"))
        colectie = clean_cell(first.get("Colectie"))

        description = ""
        gallery: list[str] = []
        pdfs: list[dict[str, str]] = []
        if url:
            soup = get_soup(url)
            if soup is not None:
                trimmed = trim_after_related(soup)
                description = page_description(trimmed)
                gallery = gallery_urls(trimmed)
                pdfs = collect_pdfs(soup, url)
        else:
            print(f"  no URL for {np_!r} -- product page skipped")

        finishes_set: list[str] = []
        seen_fin: set[str] = set()
        for r in group:
            for col in ("Variante culori", "Nume variante"):
                v = normalize_space(clean_cell(r.get(col)))
                if v and v not in seen_fin:
                    seen_fin.add(v)
                    finishes_set.append(v)
        finishes = ", ".join(finishes_set)

        product = {
            "id": p_id,
            "title": np_,
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
            "material": "Aluminium / Olefin rope",
            "shape": "",
        }
        products_db.append(product)

        for sort_i, doc in enumerate(pdfs):
            u = doc["url"]
            if u not in pdf_url_to_id:
                pdf_url_to_id[u] = pdf_id_counter
                technical_pdfs_db.append({
                    "id": pdf_id_counter,
                    "title": enrich_technical_pdf_title(doc["title"], product_title=np_, collection=colectie),
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
            color = variant_color(r) or "Standard"
            sku = variant_sku(SKU_PREFIX, v_id, clean_cell(r.get("COD REFERINTA")))
            row_url = clean_cell(r.get("Link variante"))
            if not row_url.startswith("http"):
                row_url = url
            variants_db.append({
                "id": v_id,
                "product_id": p_id,
                "sku": sku,
                "color": color,
                "url": row_url,
                "gallery_photos": json.dumps(gallery, ensure_ascii=False),
                "technical_photos": json.dumps([], ensure_ascii=False),
            })
            v_id += 1

        print(f"  product id={p_id} | {np_!r} | variants={len(group)} | imgs={len(gallery)} | pdfs={len(pdfs)}")
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
