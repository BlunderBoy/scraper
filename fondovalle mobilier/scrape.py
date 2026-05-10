"""
Fondovalle furniture / shapes — product pages from ``links.csv`` (fondovalle.it).

SKU: ``COD REFERINTA`` when set; otherwise ``FV_<variant_id>``.
"""

from __future__ import annotations

import argparse
import json
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

from fondovalle_scrape_lib import (
    collection_parent_url,
    decorate_fondovalle_technical_pdf_titles,
    extract_gallery_urls,
    extract_product_row_common,
    fondovalle_variant_color,
    merge_technical_documents,
    parse_dimensions_panel,
)
from scraper_brand_utils import (
    aggregate_unique_column,
    clean_cell,
    created_stamp_now,
    default_color,
    norm_key,
    normalize_category,
    total_gallery_count,
    variant_sku,
    write_brand_outputs,
)

MANUFACTURER = "Fondovalle"
SKU_PREFIX = "FV"
LINKS_CSV = "links.csv"

START_PRODUCT_ID = 700
START_VARIANT_ID = 4000
START_TECH_PDF_ID = 1
START_PRODUCT_PDF_ID = 1

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def normalize_url(url: str) -> str:
    u = clean_cell(url).split("#")[0].strip()
    if not u.startswith("http"):
        return u
    from urllib.parse import urlparse

    p = urlparse(u)
    host = (p.netloc or "").lower()
    path = p.path or "/"
    if host.endswith("fondovalle.it") and path != "/" and not path.endswith("/"):
        path = path + "/"
    return f"{p.scheme}://{host}{path}"


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


def scrape(*, limit_rows: int | None = None) -> None:
    script_dir = Path(__file__).resolve().parent
    df = load_links(script_dir / LINKS_CSV)

    session = requests.Session()
    session.headers.update(HEADERS)

    data_rows: list[pd.Series] = []
    for _, row in df.iterrows():
        link = normalize_url(clean_cell(row.get("Link variante")))
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

    def get_soup(raw: str) -> BeautifulSoup | None:
        req_u = normalize_url(raw)
        if req_u in cache:
            return cache[req_u]
        soup = fetch_soup(req_u, session)
        if soup:
            cache[req_u] = soup
            time.sleep(0.07)
        return soup

    product_key_to_id: dict[tuple[str, str, str, str, str], int] = {}

    for k in key_order:
        group = key_to_rows[k]
        finishes_labels = sorted(
            {clean_cell(r.get("Variante culori")) for r in group if clean_cell(r.get("Variante culori"))},
            key=lambda s: s.casefold(),
        )
        first = group[0]
        first_url = normalize_url(clean_cell(first.get("Link variante")))
        soup = get_soup(first_url)
        if not soup:
            print(f"\n=== SKIP product {k!r} ===")
            continue

        coll_url = collection_parent_url(first_url)
        soup_c = (
            get_soup(coll_url)
            if coll_url and coll_url.rstrip("/").casefold() != first_url.rstrip("/").casefold()
            else None
        )

        categorie = clean_cell(first.get("Categorie"))
        subcategorie = clean_cell(first.get("Subcategorie"))
        sub_sub = clean_cell(first.get("SUB-SUBCATEGORIE"))
        colectie = clean_cell(first.get("Colectie"))
        np = clean_cell(first.get("Nume produs"))
        csv_title = clean_cell(first.get("Nume produs original")) or np
        sizes_agg = aggregate_unique_column(group, "Dimensiuni")

        row_dict = extract_product_row_common(
            soup,
            categorie,
            subcategorie,
            sub_sub,
            colectie,
            np,
            finishes_labels,
            manufacturer=MANUFACTURER,
            title_csv=csv_title,
            sizes_csv=sizes_agg or None,
            description_csv=None,
            collection_soup=soup_c,
        )
        row_dict["category"] = normalize_category(categorie)
        row_dict["id"] = p_id
        products_db.append(row_dict)
        product_key_to_id[k] = p_id

        docs = merge_technical_documents(soup, soup_c)
        decorate_fondovalle_technical_pdf_titles(
            docs,
            product_title=clean_cell(row_dict.get("title", "")),
            collection=colectie,
        )
        for sort_i, doc in enumerate(docs):
            u = doc["url"]
            if u not in pdf_url_to_id:
                pdf_url_to_id[u] = pdf_id_counter
                technical_pdfs_db.append(
                    {
                        "id": pdf_id_counter,
                        "title": doc["title"],
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
        soup = get_soup(clean_cell(row.get("Link variante")))
        req_u = normalize_url(clean_cell(row.get("Link variante")))
        if soup:
            _, dim_imgs = parse_dimensions_panel(soup)
            dim_set = set(dim_imgs)
            gurls = [u for u in extract_gallery_urls(soup) if u not in dim_set]
        else:
            dim_imgs = []
            gurls = []

        sku = variant_sku(SKU_PREFIX, v_id, clean_cell(row.get("COD REFERINTA")))
        col = default_color(fondovalle_variant_color(row))

        variants_db.append(
            {
                "id": v_id,
                "product_id": pid,
                "sku": sku,
                "color": col,
                "url": req_u,
                "gallery_photos": json.dumps(gurls, ensure_ascii=False),
                "technical_photos": json.dumps(dim_imgs if soup else [], ensure_ascii=False),
            }
        )
        print(f"  variant {sku} | {col} | {len(gurls)} imgs | {len(dim_imgs)} tech")
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
