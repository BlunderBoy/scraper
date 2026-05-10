"""
Sanycces -- scrape product pages from ``links.csv``.

Pages live at ``https://sanycces.es/en/product/<slug>/``. The ``producto-info``
panel exposes Description / Measures / Awards labels in plain text and the
``producto-carrusel`` element holds the product photos. ``producto-desplegables``
contains Materials / Finishes / Accessories / Technical drawings tabs.

CSV row model: each row is one variant; many rows share one URL.

CRITICAL behaviours from the plan:
  - Filter out rows where ``Variante culori`` is the pseudo-variant
    "varianta disponibila la cerere in orice culoare RAL". Do NOT create a
    variant for these rows.
  - Append the RAL availability note to *every* product description (in
    Romanian) per the hints.
  - "Isla alta | Isla baja" splits its variants by ``Nume variante`` (``blat``
    top vs ``picior`` leg) -- include ``Nume variante`` in the variant ``color``
    field so each sub-part + colour combo is distinct.

SKU: ``COD REFERINTA`` is empty for all rows -> ``SAN_<variant_id>``.
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

MANUFACTURER = "Sanycces"
SKU_PREFIX = "SAN"
LINKS_CSV = "links.csv"

START_PRODUCT_ID = 1600
START_VARIANT_ID = 9500
START_TECH_PDF_ID = 1
START_PRODUCT_PDF_ID = 1

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

RAL_NOTE_RO = (
    "Se pot vopsi în orice culoare RAL. Pentru mai multe informații legate de "
    "produsele SANYCCES vă rugăm să consultați catalogul producătorului."
)
RAL_PSEUDO_RE = re.compile(r"varianta\s+disponibila\s+la\s+cerere", re.I)


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


def info_panel(soup: BeautifulSoup) -> dict[str, str]:
    """Return a dict of section-name -> joined text for ``producto-info``."""
    out: dict[str, list[str]] = {}
    panel = soup.select_one('[class*="producto-info"]')
    if not panel:
        return {}
    current_label: str | None = None
    for el in panel.find_all(["h3", "h4", "h5", "h6", "p", "li"], recursive=True):
        txt = normalize_space(el.get_text(" ", strip=True))
        if not txt:
            continue
        if el.name in ("h3", "h4", "h5", "h6"):
            label = txt.rstrip(":").lower()
            if label in {"description", "measures", "awards", "accessories"}:
                current_label = label
                out.setdefault(current_label, [])
                continue
            current_label = None
            continue
        if current_label is not None:
            out[current_label].append(txt)
    return {k: "\n".join(v).strip() for k, v in out.items() if v}


def materials_section(soup: BeautifulSoup) -> str:
    """First Materials block under producto-desplegables -- name only."""
    panel = soup.select_one('[class*="producto-desplegables"]')
    if not panel:
        return ""
    for h in panel.find_all(["h3", "h4", "h5"]):
        if (h.get_text(" ", strip=True) or "").strip().lower() == "materials":
            nxt = h.find_next(["h3", "h4", "h5"])
            if nxt:
                return normalize_space(nxt.get_text(" ", strip=True))
    return ""


def page_title(soup: BeautifulSoup) -> str:
    h1 = soup.select_one("h1")
    return h1.get_text(" ", strip=True) if h1 else ""


def gallery_urls(soup: BeautifulSoup) -> list[str]:
    """Hero (``producto-info`` marketing shot) + ``producto-carrusel`` slider photos.

    The Sanycces theme renders a marketing photo inside ``section.producto-info`` and a
    secondary slider in ``section.disenosslider.producto-carrusel`` -> ``.col-carrusel``.
    Google Drive ``Designs`` thumbnails (a wide design library carousel) and the
    ``sliderplanos`` technical drawings are excluded.
    """
    out: list[str] = []
    seen: set[str] = set()

    info = soup.select_one('section[class*="producto-info"]')
    if info:
        for img in info.select("img[src]"):
            src = (img.get("src") or "").strip()
            low = src.lower()
            if "sanycces.es/wp-content/uploads" not in low:
                continue
            if any(x in low for x in ("logo", "/grupo-", ".svg", "favicon", "image_", "premioimage_")):
                continue
            if src not in seen:
                seen.add(src)
                out.append(src)

    for el in soup.select('section[class*="producto-carrusel"] .col-carrusel.carruselslider'):
        for img in el.select("img[src]"):
            src = (img.get("src") or "").strip()
            if not src:
                continue
            low = src.lower()
            if "sanycces.es/wp-content/uploads" not in low:
                continue
            if any(x in low for x in ("logo", "/grupo-37", ".svg", "favicon", "_planos", "/planos")):
                continue
            if src not in seen:
                seen.add(src)
                out.append(src)
    return dedupe_urls(out)


def technical_drawings(soup: BeautifulSoup) -> list[str]:
    """``.sliderplanos`` holds the FTC technical drawing PNGs (not in the gallery)."""
    out: list[str] = []
    seen: set[str] = set()
    for el in soup.select(".sliderplanos img[src], .planostecnicos img[src]"):
        src = (el.get("src") or "").strip()
        low = src.lower()
        if not src or "sanycces.es/wp-content/uploads" not in low:
            continue
        if low.endswith(".svg") or "logo" in low or "/grupo-" in low:
            continue
        if src not in seen:
            seen.add(src)
            out.append(src)
    return out


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


def variant_color(row: pd.Series) -> str:
    nv = normalize_space(clean_cell(row.get("Nume variante")))
    vc = normalize_space(clean_cell(row.get("Variante culori")))
    parts = [x for x in (nv, vc) if x]
    return " / ".join(parts)


def is_ral_pseudo_row(row: pd.Series) -> bool:
    vc = clean_cell(row.get("Variante culori"))
    return bool(RAL_PSEUDO_RE.search(vc))


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

        description_parts: list[str] = []
        sizes = ""
        material = ""
        gallery: list[str] = []
        tech_imgs: list[str] = []
        pdfs: list[dict[str, str]] = []

        if url:
            soup = get_soup(url)
            if soup is not None:
                info = info_panel(soup)
                desc = info.get("description", "")
                if desc:
                    description_parts.append(desc)
                sizes = info.get("measures", "")
                material = materials_section(soup)
                gallery = gallery_urls(soup)
                tech_imgs = technical_drawings(soup)
                pdfs = collect_pdfs(soup, url)

        description_parts.append(RAL_NOTE_RO)
        description = "\n\n".join(description_parts)

        # Variants are the colour codes on the Sanycces site; ``finishes`` would just
        # repeat those codes, so it is intentionally left blank per the brief.
        finishes = ""

        dim_blob = " ".join(x for x in (sizes, np_) if x)
        dims = parse_dimensions_from_text(dim_blob)

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
            "sizes": sizes,
            "thickness": "",
            "material": material,
            "shape": "",
            "cut": "",
            "diameter": dims.get("diameter", ""),
            "length": dims.get("length", ""),
            "width": dims.get("width", ""),
            "height": dims.get("height", ""),
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

        real_rows = [r for r in group if not is_ral_pseudo_row(r)]
        if not real_rows:
            real_rows = [group[0]]

        kept_variants = 0
        for r in real_rows:
            color = default_color(variant_color(r))
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
                "technical_photos": json.dumps(tech_imgs, ensure_ascii=False),
            })
            v_id += 1
            kept_variants += 1

        print(f"  product id={p_id} | {np_!r} | variants={kept_variants} (was {len(group)}) | imgs={len(gallery)} | pdfs={len(pdfs)}")
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
