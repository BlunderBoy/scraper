"""
Gommaire -- scrape product pages from ``links.csv``.

One product per ``Nume produs`` (category columns forward-filled). Variants come
from ``Nume variante`` and/or ``Variante culori``; when both are empty the
variant ``color`` is ``Standard``. Product titles and SKUs come from the CSV,
not the website H1.

Pages are Next.js at ``https://gommaire.com/product/<slug>``. Hero, gallery and
technical images are decoded from ``/_next/image`` URLs. Specs (measurements,
material) live in the ``grid.grid-cols-2.my-2.justify-center`` label/value
paragraph pairs under ``main``.

Special cases (see ``hints.txt``):
  * **Dining Oval Table Imen** -- one URL per colour finish; product ``material``
    stays empty; measurements shared across variants.
  * **Round Side Table Phil** -- one URL and one photo set for Small/Medium/Large;
    product ``sizes`` concatenates all measurement lines from the page.

Rows with only forward-filled product names (no link, SKU, or variant label) are
skipped -- e.g. blank lines after Castor in the spreadsheet.

PDFs are not scraped: the site only links generic catalog PDFs (not tied to a
product or collection), so ``technical_pdfs.csv`` and ``product_pdfs.csv`` stay
empty.

SKU: ``COD REFERINTA`` when set; else ``GOM_<variant_id>``.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse, parse_qs

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
    join_unique_csv,
    norm_key,
    normalize_category,
    normalize_space,
    parse_dimensions_from_text,
    total_gallery_count,
    variant_sku,
    write_brand_outputs,
)

MANUFACTURER = "Gommaire"
SKU_PREFIX = "GOM"
LINKS_CSV = "links.csv"

START_PRODUCT_ID = 2200
START_VARIANT_ID = 18000

PHIL_PRODUCT_NAME = "Round Side Table Phil"
IMEN_PRODUCT_NAME = "Dining Oval Table Imen"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def decode_image_url(src: str) -> str:
    src = (src or "").strip()
    if not src:
        return ""
    if "/_next/image" in src:
        q = parse_qs(urlparse(src).query)
        if "url" in q:
            return unquote(q["url"][0])
    return src


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


def _spec_grids(main: Tag) -> list[Tag]:
    return main.select("div.grid.grid-cols-2.my-2.justify-center")


def _grid_to_dict(grid: Tag) -> dict[str, str]:
    ps = grid.select("p")
    out: dict[str, str] = {}
    i = 0
    while i < len(ps):
        label = normalize_space(ps[i].get_text(" ", strip=True))
        if label.endswith(":"):
            key = label[:-1].strip()
            if i + 1 < len(ps):
                out[key] = normalize_space(ps[i + 1].get_text(" ", strip=True))
                i += 2
                continue
        i += 1
    return out


def parse_spec_blocks(soup: BeautifulSoup) -> list[dict[str, str]]:
    main = soup.select_one("main") or soup
    return [_grid_to_dict(g) for g in _spec_grids(main)]


def hero_image_url(soup: BeautifulSoup) -> str:
    main = soup.select_one("main") or soup
    img = main.select_one("div.relative img.object-contain.object-center")
    if img is None:
        img = main.select_one("div.relative img.object-contain")
    return decode_image_url((img.get("src") or "").strip()) if img else ""


def technical_image_url(soup: BeautifulSoup) -> str:
    main = soup.select_one("main") or soup
    for img in main.select("img.h-20.md\\:h-32.w-auto, img.h-20"):
        u = decode_image_url((img.get("src") or "").strip())
        if u:
            return u
    for img in main.select("img"):
        cls = " ".join(img.get("class") or [])
        if "h-20" in cls or "md:h-32" in cls:
            u = decode_image_url((img.get("src") or "").strip())
            if u:
                return u
    return ""


def gallery_image_urls(soup: BeautifulSoup) -> list[str]:
    main = soup.select_one("main") or soup
    out: list[str] = []
    seen: set[str] = set()
    for img in main.select(
        "div.mt-16 img, "
        "div.grid.grid-cols-3.gap-5 div.relative.overflow-hidden img, "
        "div.grid.grid-cols-3.gap-5 img"
    ):
        u = decode_image_url((img.get("src") or "").strip())
        if not u or u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def build_photo_sets(soup: BeautifulSoup) -> tuple[list[str], list[str]]:
    """Return ``(gallery_photos, technical_photos)`` with hero first in gallery."""
    hero = hero_image_url(soup)
    gallery = gallery_image_urls(soup)
    tech = technical_image_url(soup)

    combined: list[str] = []
    if hero:
        combined.append(hero)
    for u in gallery:
        if u != hero:
            combined.append(u)
    combined = dedupe_urls(combined)

    tech_list = [tech] if tech else []
    return combined, tech_list


def measurements_from_blocks(blocks: list[dict[str, str]]) -> list[str]:
    return [b["Measurements"] for b in blocks if b.get("Measurements")]


def material_from_blocks(blocks: list[dict[str, str]]) -> str:
    for b in blocks:
        m = b.get("Material", "").strip()
        if m:
            return m
    return ""


def phil_concatenated_sizes(blocks: list[dict[str, str]]) -> str:
    return join_unique_csv(measurements_from_blocks(blocks), sep=" | ")


def variant_color(row: pd.Series) -> str:
    nv = normalize_space(clean_cell(row.get("Nume variante")))
    vc = normalize_space(clean_cell(row.get("Variante culori")))
    parts = [x for x in (nv, vc) if x]
    return " / ".join(parts)


def is_data_row(row: pd.Series) -> bool:
    if not clean_cell(row.get("Nume produs")):
        return False
    link = clean_cell(row.get("Link variante"))
    cod = clean_cell(row.get("COD REFERINTA"))
    nv = clean_cell(row.get("Nume variante"))
    vc = clean_cell(row.get("Variante culori"))
    return link.startswith("http") or bool(cod) or bool(nv) or bool(vc)


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


def group_primary_url(group: list[pd.Series]) -> str:
    for r in group:
        u = clean_cell(r.get("Link variante"))
        if u.startswith("http"):
            return u
    return ""


def scrape(*, limit_products: int | None = None) -> None:
    script_dir = Path(__file__).resolve().parent
    df = load_links(script_dir / LINKS_CSV)

    data_rows: list[pd.Series] = []
    for _, row in df.iterrows():
        if is_data_row(row):
            data_rows.append(row)

    key_order: list[tuple[str, str, str, str, str]] = []
    key_to_rows: dict[tuple[str, str, str, str, str], list[pd.Series]] = {}
    for row in data_rows:
        k = product_key(row)
        if k not in key_to_rows:
            key_order.append(k)
            key_to_rows[k] = []
        key_to_rows[k].append(row)

    if limit_products is not None:
        key_order = key_order[: max(0, limit_products)]

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

    products_db: list[dict[str, Any]] = []
    variants_db: list[dict[str, Any]] = []
    technical_pdfs_db: list[dict[str, Any]] = []
    product_pdfs_db: list[dict[str, Any]] = []

    p_id = START_PRODUCT_ID
    v_id = START_VARIANT_ID
    stamp = created_stamp_now()

    for k in key_order:
        group = key_to_rows[k]
        first = group[0]
        np_ = clean_cell(first.get("Nume produs"))
        categorie = clean_cell(first.get("Categorie"))
        subcategorie = clean_cell(first.get("Subcategorie"))
        sub_sub = clean_cell(first.get("SUB-SUBCATEGORIE"))
        colectie = clean_cell(first.get("Colectie"))
        is_phil = norm_key(np_) == norm_key(PHIL_PRODUCT_NAME)
        is_imen = norm_key(np_) == norm_key(IMEN_PRODUCT_NAME)

        primary_url = group_primary_url(group)
        blocks: list[dict[str, str]] = []
        shared_gallery: list[str] = []
        shared_technical: list[str] = []
        if primary_url:
            soup = get_soup(primary_url)
            if soup is not None:
                blocks = parse_spec_blocks(soup)
                if is_phil:
                    shared_gallery, shared_technical = build_photo_sets(soup)

        if is_imen:
            material = ""
        elif is_phil:
            material = material_from_blocks(blocks)
        else:
            material = material_from_blocks(blocks)

        if is_phil and blocks:
            sizes = phil_concatenated_sizes(blocks)
        else:
            meas = measurements_from_blocks(blocks)
            sizes = meas[0] if meas else ""

        dim_info = parse_dimensions_from_text(sizes) if sizes else {}

        finish_labels = sorted(
            {variant_color(r) for r in group if variant_color(r)},
            key=lambda s: s.casefold(),
        )
        finishes = join_unique_csv(finish_labels)

        product = {
            "id": p_id,
            "title": np_,
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
            "thickness": "",
            "material": material,
            "shape": "",
            "cut": "",
            "diameter": dim_info.get("diameter", ""),
            "length": dim_info.get("length", ""),
            "width": dim_info.get("width", ""),
            "height": dim_info.get("height", ""),
        }
        products_db.append(product)

        variant_count = 0
        for r in group:
            raw_color = variant_color(r)
            color = default_color(raw_color)
            sku = variant_sku(SKU_PREFIX, v_id, clean_cell(r.get("COD REFERINTA")))

            row_url = clean_cell(r.get("Link variante"))
            if not row_url.startswith("http"):
                row_url = primary_url

            if is_phil:
                gallery, technical = shared_gallery, shared_technical
            else:
                soup_v = get_soup(row_url) if row_url else None
                if soup_v is not None:
                    gallery, technical = build_photo_sets(soup_v)
                    if not blocks and not is_imen:
                        blocks = parse_spec_blocks(soup_v)
                else:
                    gallery, technical = [], []

            variants_db.append(
                {
                    "id": v_id,
                    "product_id": p_id,
                    "sku": sku,
                    "color": color,
                    "url": row_url,
                    "gallery_photos": json.dumps(gallery, ensure_ascii=False),
                    "technical_photos": json.dumps(technical, ensure_ascii=False),
                }
            )
            v_id += 1
            variant_count += 1

        print(
            f"  product id={p_id} | {np_!r} | variants={variant_count} | "
            f"sizes={sizes!r} | material={material!r}"
        )
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
        f"{total_gallery_count(variants_db)} gallery URLs -> {script_dir}"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max number of products to scrape (for testing)",
    )
    args = ap.parse_args()
    scrape(limit_products=args.limit)


if __name__ == "__main__":
    main()
