"""
Fantini -- scrape product variant pages from ``links.csv``.

Row model: each CSV row is one variant (a finish), with its own URL like
``https://www.fantini.it/en-us/product/49-3864-5002e904wu`` and its own
``COD REFERINTA``. Variants in the same model share design / collection /
feature list / PDF documents, so product-level data is taken from the first
row of each ``(Categorie, Subcategorie, SUB-SUBCATEGORIE, Colectie, Nume
produs)`` group; per-variant ``og:image`` (``IMM_..._<sku>.jpg``) becomes the
variant's gallery.

SKU: ``COD REFERINTA`` from the CSV when set, else ``FAN_<variant_id>``. The
spreadsheet sometimes already prefixes ``FAN_`` -- left as-is.
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
    aggregate_unique_column,
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

MANUFACTURER = "Fantini"
SKU_PREFIX = "FAN"
LINKS_CSV = "links.csv"

START_PRODUCT_ID = 1100
START_VARIANT_ID = 7000
START_TECH_PDF_ID = 1
START_PRODUCT_PDF_ID = 1

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# PDF anchors that come from the global footer / company info, not the product.
GLOBAL_PDF_LABEL_BLACKLIST = re.compile(r"code of ethics|organizational model|modello di organizzazione|codice etico", re.I)
GLOBAL_PDF_URL_BLACKLIST = re.compile(r"Codice_Etico|MODELLO_DI_ORGANIZZAZIONE|Fratelli_Fantini", re.I)


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


def og_image(soup: BeautifulSoup) -> str:
    og = soup.select_one('meta[property="og:image"]')
    if og and og.get("content"):
        return og["content"].strip()
    return ""


def jsonld_image(soup: BeautifulSoup) -> str:
    for s in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(s.string or "")
        except Exception:
            continue
        if isinstance(data, dict):
            img = data.get("image")
            if isinstance(img, list) and img:
                return clean_cell(img[0])
            if isinstance(img, str):
                return clean_cell(img)
    return ""


def hero_gallery(soup: BeautifulSoup) -> list[str]:
    out: list[str] = []
    og = og_image(soup)
    if og:
        out.append(og)
    j = jsonld_image(soup)
    if j and j not in out:
        out.append(j)
    return dedupe_urls([u for u in out if u])


def header_block(soup: BeautifulSoup) -> Tag | None:
    return soup.select_one('[class*="component-block-header-product"]')


def collection_name(soup: BeautifulSoup) -> str:
    h1 = soup.select_one("h1")
    return h1.get_text(" ", strip=True) if h1 else ""


def feature_subtitle(soup: BeautifulSoup) -> str:
    h3 = soup.select_one('[class*="component-block-header-product"] h3, [class*="component-details-product"] h3, h3')
    return h3.get_text(" ", strip=True) if h3 else ""


def design_and_collection(soup: BeautifulSoup) -> dict[str, str]:
    """Pulled from the ``Design:`` / ``Collection:`` definition list."""
    out: dict[str, str] = {}
    block = soup.select_one('[class*="component-details-product"]')
    if not block:
        return out
    text = block.get_text("\n", strip=True)
    lines = [normalize_space(x) for x in text.split("\n") if normalize_space(x)]
    for i, line in enumerate(lines[:-1]):
        low = line.casefold().rstrip(":")
        if low in ("design", "designer"):
            out["design"] = lines[i + 1]
        elif low == "collection":
            out["collection"] = lines[i + 1]
    return out


def feature_bullets(soup: BeautifulSoup) -> list[str]:
    """Free-text feature list under header (4 1/2'' projection, 1.2 gpm, ...)."""
    block = header_block(soup)
    if block is None:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for el in block.find_all(["li", "p"]):
        t = normalize_space(el.get_text(" ", strip=True))
        if not t or len(t) < 4:
            continue
        if t.lower().startswith(("art.", "finish", "favourite", "add to list")):
            continue
        if t.lower() in ("design:", "collection:"):
            continue
        if t in seen:
            continue
        seen.add(t)
        found.append(t)
    return found[:30]


def description_text(soup: BeautifulSoup) -> str:
    """Subtitle + bullet feature list. The page's meta/JSON-LD descriptions repeat the
    same phrase three times across sources, so we keep just the structured content."""
    parts: list[str] = []
    subtitle = feature_subtitle(soup)
    if subtitle:
        parts.append(subtitle)
    bullets = feature_bullets(soup)
    if bullets:
        parts.append("\n".join(f"- {b}" for b in bullets))
    return "\n\n".join(dict.fromkeys(parts))


def collect_pdfs(soup: BeautifulSoup, page_url: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for a in soup.select("a[href]"):
        href = (a.get("href") or "").strip()
        if not href.lower().endswith(".pdf"):
            continue
        u = urljoin(page_url, href)
        if u in seen:
            continue
        seen.add(u)
        label = normalize_space(a.get_text(" ", strip=True)) or u.rsplit("/", 1)[-1].replace(".pdf", "")
        if GLOBAL_PDF_LABEL_BLACKLIST.search(label) or GLOBAL_PDF_URL_BLACKLIST.search(u):
            continue
        out.append({"url": u, "title": label})
    return out


def load_links(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, encoding="utf-8-sig")
    for col in ("Categorie", "Subcategorie", "SUB-SUBCATEGORIE", "Colectie", "Nume produs"):
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


def variant_color(row: pd.Series) -> str:
    return normalize_space(clean_cell(row.get("Variante culori")).title())


def scrape(*, limit_rows: int | None = None) -> None:
    script_dir = Path(__file__).resolve().parent
    df = load_links(script_dir / LINKS_CSV)

    data_rows: list[pd.Series] = []
    for _, row in df.iterrows():
        link = clean_cell(row.get("Link variante"))
        if not link.startswith("http"):
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
        time.sleep(0.06)
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
    product_key_to_id: dict[tuple, int] = {}

    for k in key_order:
        group = key_to_rows[k]
        first = group[0]
        first_url = clean_cell(first.get("Link variante"))
        soup = get_soup(first_url)
        if soup is None:
            print(f"  SKIP product (fetch failed) {first_url}")
            continue

        np_ = clean_cell(first.get("Nume produs"))
        categorie = clean_cell(first.get("Categorie"))
        subcategorie = clean_cell(first.get("Subcategorie"))
        sub_sub = clean_cell(first.get("SUB-SUBCATEGORIE"))
        colectie = clean_cell(first.get("Colectie"))

        dc = design_and_collection(soup)

        description = description_text(soup)
        if dc.get("design") and "Design:" not in description:
            description = (description + f"\n\nDesign: {dc['design']}").strip()

        # ``finishes`` would just repeat the per-variant colour codes; leave blank.
        finishes = ""

        dims = parse_dimensions_from_text(description)

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
            "material": "Brass",
            "shape": "",
            "cut": "",
            "diameter": dims.get("diameter", ""),
            "length": dims.get("length", ""),
            "width": dims.get("width", ""),
            "height": dims.get("height", ""),
        }
        products_db.append(product)
        product_key_to_id[k] = p_id

        for sort_i, doc in enumerate(collect_pdfs(soup, first_url)):
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

        print(f"  product id={p_id} | {np_!r} | pdfs={len(collect_pdfs(soup, first_url))}")
        p_id += 1

    for row in data_rows:
        k = product_key(row)
        if k not in product_key_to_id:
            continue
        pid = product_key_to_id[k]
        url = clean_cell(row.get("Link variante"))
        soup = get_soup(url)
        gallery: list[str] = []
        if soup is not None:
            gallery = hero_gallery(soup)

        # The Fantini website exposes a single article number per finish (e.g. ``5002E904WU``).
        # The spreadsheet sometimes pre-pends ``FAN_``; strip it so every SKU is uniform.
        cod_raw = clean_cell(row.get("COD REFERINTA"))
        if cod_raw.upper().startswith("FAN_"):
            cod_raw = cod_raw[4:]
        sku = variant_sku(SKU_PREFIX, v_id, cod_raw)
        color = default_color(variant_color(row))

        variants_db.append({
            "id": v_id,
            "product_id": pid,
            "sku": sku,
            "color": color,
            "url": url,
            "gallery_photos": json.dumps(gallery, ensure_ascii=False),
            "technical_photos": json.dumps([], ensure_ascii=False),
        })
        print(f"  variant id={v_id} sku={sku} | {color} | imgs={len(gallery)}")
        v_id += 1

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
