"""
Terrazzo Italiano -- scrape portfolio pages from ``links.csv``.

Each row is one product (one variant each, no colour variants, no SKU). Pages
are at ``https://www.terrazzoitaliano.com/en/portfolio/<slug>-en/`` (Elementor
WordPress) and contain 3 photos (square, square-tile, lastra), a short
description ("Marble-cement is..." or "Marble-resin is..."), and four surface
finish blocks (polished / brushed / honed) under "surface finishes".

Images are lazy-loaded via ``data-src`` -- both attributes are inspected.

The site rejects full Chrome UAs (403); a minimal "Mozilla/5.0 (Windows NT
10.0; Win64; x64)" UA is accepted (same as Euval).

SKU: empty in CSV -> ``TI_<variant_id>``.
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

MANUFACTURER = "Terrazzo Italiano"
SKU_PREFIX = "TI"
LINKS_CSV = "links.csv"

START_PRODUCT_ID = 1700
START_VARIANT_ID = 10000
START_TECH_PDF_ID = 1
START_PRODUCT_PDF_ID = 1

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

CUT_HEADINGS = (
    "are you interested in this product",
    "login or register to the reserved area",
    "registrati o accedi",
    "tradition and modernity",
)

# Per-product technical sheets sit behind the ``Reserved Area`` login (the site
# explicitly says ``Login or Register to the reserved area for the download``);
# what is openly available is one shared specifications PDF on the public
# technical-area page, applied to every product as a baseline document.
TECHNICAL_AREA_PDF = (
    "https://www.terrazzoitaliano.com/wp-content/uploads/2021/08/"
    "specifiche-tecniche-terrazzo-italiano.pdf"
)
TECHNICAL_AREA_PDF_TITLE = "Terrazzo Italiano – Technical specifications"


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


def trim_after_unrelated(soup: BeautifulSoup) -> Tag:
    main = soup.find("body") or soup
    for h in main.select("h2, h3, h4"):
        text = (h.get_text(" ", strip=True) or "").lower()
        if any(needle in text for needle in CUT_HEADINGS):
            for sib in list(h.find_all_next()):
                sib.extract()
            h.extract()
            break
    return main


def page_description(main: Tag) -> str:
    parts: list[str] = []
    for p in main.find_all(["p"]):
        t = normalize_space(p.get_text(" ", strip=True))
        if not t or len(t) < 30:
            continue
        if "info@terrazzoitaliano.com" in t.lower():
            continue
        parts.append(t)
    return "\n\n".join(dict.fromkeys(parts))


def gallery_urls(main: Tag, soup: BeautifulSoup) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    og = soup.select_one('meta[property="og:image"]')
    if og and og.get("content"):
        out.append(og["content"].strip())
        seen.add(out[-1])
    for img in main.select("img"):
        for attr in ("data-src", "src"):
            v = (img.get(attr) or "").strip()
            if not v or "wp-content/uploads" not in v:
                continue
            low = v.lower()
            if any(x in low for x in ("logo", "flag", "favicon", "/icon", "cropped-tz", "terrazzo-white", "terrazzo-black", "lucidatura", ".woff")):
                continue
            stripped = re.sub(r"-\d+x\d+(?=\.\w+$)", "", v)
            if stripped not in seen:
                seen.add(stripped)
                out.append(stripped)
    return dedupe_urls(out)


def surface_finishes(main: Tag) -> str:
    """List every <h4> sitting under the 'surface finishes' h2."""
    items: list[str] = []
    for h in main.select("h2"):
        text = (h.get_text(" ", strip=True) or "").lower()
        if "surface finishes" in text:
            for sib in h.find_all_next():
                if sib is None:
                    break
                if sib.name == "h2" and "surface finishes" not in (sib.get_text(" ", strip=True) or "").lower():
                    break
                if sib.name == "h4":
                    t = normalize_space(sib.get_text(" ", strip=True))
                    if t and t.lower() not in ("surface finishes:", "what are you interested in?"):
                        items.append(t)
            break
    out: list[str] = []
    seen: set[str] = set()
    for it in items:
        k = it.casefold()
        if k not in seen:
            seen.add(k)
            out.append(it.title())
    return ", ".join(out)


def collect_pdfs(soup: BeautifulSoup, page_url: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for a in soup.select('a[href*=".pdf"]'):
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


def load_links(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, encoding="utf-8-sig")
    for col in ("Categorie", "Subcategorie", "SUB-SUBCATEGORIE", "Colectie", "Link variante"):
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
        soup = fetch_soup(url, session)
        if soup is None:
            print(f"  SKIP fetch failed: {url}")
            continue

        np_ = clean_cell(row.get("Nume produs"))
        categorie = clean_cell(row.get("Categorie"))
        subcategorie = clean_cell(row.get("Subcategorie"))
        sub_sub = clean_cell(row.get("SUB-SUBCATEGORIE"))
        colectie = clean_cell(row.get("Colectie"))

        trimmed = trim_after_unrelated(soup)
        description = page_description(trimmed)
        gallery = gallery_urls(trimmed, soup)
        finishes = surface_finishes(trimmed)
        pdfs = collect_pdfs(soup, url)

        material = "Marble-cement" if (colectie or "").lower() == "marble-cement" else (
            "Marble-resin" if (colectie or "").lower() == "marble-resin" else "Terrazzo"
        )

        product = {
            "id": p_id,
            "title": np_,
            "description": description,
            "category": normalize_category(categorie),
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
            "material": material,
            "shape": "",
            "cut": "",
            "diameter": "",
            "length": "",
            "width": "",
            "height": "",
        }
        products_db.append(product)

        # Always link the shared specifications PDF as a baseline technical doc.
        if TECHNICAL_AREA_PDF not in [d["url"] for d in pdfs]:
            pdfs = [{"url": TECHNICAL_AREA_PDF, "title": TECHNICAL_AREA_PDF_TITLE}, *pdfs]

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

        sku = variant_sku(SKU_PREFIX, v_id, clean_cell(row.get("COD REFERINTA")))
        variants_db.append({
            "id": v_id,
            "product_id": p_id,
            "sku": sku,
            "color": "Standard",
            "url": url,
            "gallery_photos": json.dumps(gallery, ensure_ascii=False),
            "technical_photos": json.dumps([], ensure_ascii=False),
        })

        print(f"  product id={p_id} variant id={v_id} | {np_!r} | imgs={len(gallery)} | finishes={finishes!r}")
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
