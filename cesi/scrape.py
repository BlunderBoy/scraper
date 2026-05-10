"""
CE.SI. Ceramica di Sirone — scrape tile product pages from ``links.csv``.

The CSV groups rows into three Cesi collections, each with its own variant model:

* ``Astratto 10x10`` (12 rows): one product per row with **one** variant — the
  CSV link is the only variant page (size ``10x10``, color encoded in the name
  e.g. ``Tratto SODIO``).
* ``REVERSO`` (9 rows): one product per row with **two** size variants.
  ``VARIANTE PRODUS`` lists them as ``7,5x15, 7,5x7`` (the site spells the
  second one ``7,5x7,5``). The CSV link goes to the ``7,5x15`` variant; the
  sibling ``7,5x7,5 Reverso`` link is discovered inside the page's
  ``Formati disponibili`` section.
* ``COLORI`` (11 rows): one product per row with **N** size variants
  (``5x5, 5x20, 10x10, 20x20, 10x30, 20x60`` etc., varies per color).
  The CSV link goes to one variant; the other sizes are picked up from the
  ``Formati disponibili`` section, filtering for plain-size entries that
  match ``VARIANTE PRODUS`` (skipping ``su rete``, ``ottagono``, ``diamantato``,
  ``Reverso``…).

Each variant carries its own ``og:image`` as the single gallery photo, and a
SKU pulled from the image filename (e.g. ``5MA050050-8.jpg`` -> ``5MA050050-8``).
CE.SI. doesn't publish per-product technical sheets, so every product links to
the same site-wide ``caratteristichetecniche.pdf`` as a baseline document.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import pandas as pd
import requests
from bs4 import BeautifulSoup

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
    join_unique_csv,
    normalize_category,
    normalize_space,
    total_gallery_count,
    write_brand_outputs,
)

MANUFACTURER = "Cesi"
SKU_PREFIX = "CES"
LINKS_CSV = "links.csv"
BASE_URL = "https://www.cesiceramica.it"

START_PRODUCT_ID = 1300
START_VARIANT_ID = 11000
START_TECH_PDF_ID = 1
START_PRODUCT_PDF_ID = 1

# Cesi does not publish per-product technical sheets; every tile shares this
# global "caratteristiche tecniche" PDF, so we attach it to every product.
SHARED_TECHNICAL_PDF_URL = (
    "https://www.cesiceramica.it/public/elenchi_file/caratteristichetecniche.pdf"
)
SHARED_TECHNICAL_PDF_TITLE = "Cesi – Caratteristiche tecniche"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Words that mark a Formati entry as a *different* collection (skipped for COLORI).
FORMATO_MODIFIERS = (
    "su rete",
    "ottagono",
    "esagono",
    "butterfly",
    "diamantato",
    "reverso",
    "pezzi speciali",
)
SIZE_TOKEN_RE = re.compile(r"^\d+(?:,\d+)?x\d+(?:,\d+)?$")


# ---------------------------------------------------------------------------
# CSV loading

def load_links(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, encoding="utf-8-sig")
    for col in ("Categorie", "Subcategorie", "SUB-SUBCATEGORIE", "Colectie"):
        if col in df.columns:
            df[col] = df[col].ffill()
    return df


# ---------------------------------------------------------------------------
# HTTP

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


# ---------------------------------------------------------------------------
# Page parsing helpers

def _h1_text(soup: BeautifulSoup) -> str:
    h1 = soup.find("h1")
    return normalize_space(h1.get_text(" ", strip=True)) if h1 else ""


def _h2_text(soup: BeautifulSoup) -> str:
    """``<h2>Serie I Colori - Matt</h2>`` -> ``"Serie I Colori - Matt"``."""
    for h2 in soup.find_all("h2"):
        t = normalize_space(h2.get_text(" ", strip=True))
        if t and t.lower().startswith("serie"):
            return t
    h2 = soup.find("h2")
    return normalize_space(h2.get_text(" ", strip=True)) if h2 else ""


def _og_image(soup: BeautifulSoup) -> str:
    og = soup.select_one('meta[property="og:image"]')
    if og and og.get("content"):
        return urljoin(BASE_URL, og["content"].strip())
    img = soup.select_one('img[alt][src*="/public/prodotti/immagini/"]')
    if img and img.get("src"):
        return urljoin(BASE_URL, img["src"].strip())
    return ""


def _sku_from_image(url: str) -> str:
    """``.../5MA050050-8.jpg`` -> ``5MA050050-8``; empty string when unknown."""
    if not url:
        return ""
    name = url.rsplit("/", 1)[-1]
    name = re.sub(r"\?.*$", "", name)
    name = re.sub(r"\.(?:jpe?g|png|webp)$", "", name, flags=re.I)
    return name


def _finish_from_h2(h2_text: str) -> str:
    """``"Serie Reverso - Lucidi"`` -> ``"Lucidi"``."""
    if " - " not in h2_text:
        return ""
    return normalize_space(h2_text.split(" - ", 1)[1])


def _formati_items(soup: BeautifulSoup) -> list[tuple[str, str]]:
    """Return ``[(formato_text, absolute_url)]`` from the Formati disponibili block."""
    out: list[tuple[str, str]] = []
    for h3 in soup.find_all("h3"):
        title = normalize_space(h3.get_text(" ", strip=True)).lower()
        if title != "formati disponibili":
            continue
        wr = h3.find_next_sibling("div", class_="wrFormati")
        if wr is None:
            continue
        for item in wr.find_all("div", class_="wrFormatiItem"):
            a = item.find("a", href=True)
            fmt = item.find("div", class_="formato")
            if not (a and fmt):
                continue
            text = normalize_space(fmt.get_text(" ", strip=True))
            url = urljoin(BASE_URL, a["href"].strip())
            if text and url:
                out.append((text, url))
        break
    return out


def _normalize_size_token(text: str) -> str:
    """``"7,5 x 7,5"`` -> ``"7,5x7,5"``; trailing commas (CSV authors often leave
    ``"5x5, 5x20, 10x10, 20x20,"``) and stray punctuation are scrubbed."""
    s = normalize_space(text).lower()
    s = s.replace("×", "x").replace(" x ", "x").replace(" ", "")
    s = s.strip(" ,;.")
    return s


def _expand_short_size(token: str) -> str:
    """The hints/CSV use ``7,5x7`` for the site's ``7,5x7,5``; treat them as equal."""
    if token == "7,5x7":
        return "7,5x7,5"
    return token


def _is_plain_size(text: str) -> bool:
    """``"5x5"`` / ``"7,5x15"`` -> True; ``"5x5 su rete"`` / ``"7,5x15 Reverso"`` -> False."""
    low = text.lower()
    if any(m in low for m in FORMATO_MODIFIERS):
        return False
    return bool(SIZE_TOKEN_RE.match(_normalize_size_token(text)))


def _parse_variante_produs(s: str) -> list[str]:
    """Split ``"7,5x15, 7,5x7"`` correctly even though ``,`` doubles as the
    Italian decimal separator: list entries are always followed by whitespace."""
    parts = re.split(r"\s*[,;]\s+|\s*;\s*", s or "")
    return [_expand_short_size(_normalize_size_token(p)) for p in parts if p.strip()]


# ---------------------------------------------------------------------------
# Variant resolution per collection style

def _resolve_variants(
    colectie: str,
    csv_url: str,
    csv_soup: BeautifulSoup,
    variante_produs: str,
) -> list[tuple[str, str]]:
    """Return ``[(size_label, page_url)]`` -- the canonical size label first.

    ``size_label`` is the user-facing dimensions string (e.g. ``"10x10"``); we keep
    the original site spelling so it round-trips into the product's ``sizes`` field.
    """
    items = _formati_items(csv_soup)
    col_lower = (colectie or "").strip().lower()

    if col_lower == "astratto 10x10":
        # 10x10 is fixed; CSV URL is the only variant page.
        return [("10x10", csv_url)]

    if col_lower == "reverso":
        # Target ``7,5x15 Reverso`` and ``7,5x7,5 Reverso``; CSV's ``7,5x7`` -> ``7,5x7,5``.
        wanted = set(_parse_variante_produs(variante_produs) or ["7,5x15", "7,5x7,5"])
        result: dict[str, str] = {}
        for text, url in items:
            low = text.lower()
            if "reverso" not in low:
                continue
            size_part = _normalize_size_token(low.replace("reverso", ""))
            if size_part in wanted:
                result.setdefault(size_part, url)
        # Always include the CSV URL itself even if Formati doesn't enumerate it.
        own_size = _normalize_size_token(_h1_text(csv_soup).split(" ", 1)[0])
        if own_size and own_size in wanted:
            result.setdefault(own_size, csv_url)
        return [(size, url) for size, url in result.items()]

    # COLORI (and any other plain-size collection): pick Formati entries whose
    # text is a pure ``WxH`` token matching the CSV's listed sizes.
    wanted = set(_parse_variante_produs(variante_produs))
    result_co: dict[str, str] = {}
    for text, url in items:
        if not _is_plain_size(text):
            continue
        size = _normalize_size_token(text)
        if size in wanted:
            result_co.setdefault(size, url)
    own_size = _normalize_size_token(_h1_text(csv_soup).split(" ", 1)[0])
    if own_size and own_size in wanted:
        result_co.setdefault(own_size, csv_url)
    return [(size, url) for size, url in result_co.items()]


# ---------------------------------------------------------------------------
# Sizes synthesis

def _format_sizes(sizes: list[str]) -> str:
    return join_unique_csv([f"{s} cm" for s in sizes if s])


# ---------------------------------------------------------------------------
# Main scrape loop

def scrape(*, limit_rows: int | None = None) -> None:
    script_dir = Path(__file__).resolve().parent
    df = load_links(script_dir / LINKS_CSV)

    rows: list[pd.Series] = []
    for _, row in df.iterrows():
        link = clean_cell(row.get("Link variante"))
        nume = clean_cell(row.get("Nume produs"))
        if not link.startswith("http") or not nume:
            continue
        rows.append(row)

    if limit_rows is not None:
        rows = rows[: max(0, limit_rows)]

    session = requests.Session()
    session.headers.update(HEADERS)

    products_db: list[dict[str, Any]] = []
    variants_db: list[dict[str, Any]] = []
    technical_pdfs_db: list[dict[str, Any]] = []
    product_pdfs_db: list[dict[str, Any]] = []

    p_id = START_PRODUCT_ID
    v_id = START_VARIANT_ID
    tech_pdf_id = START_TECH_PDF_ID
    pp_id = START_PRODUCT_PDF_ID
    stamp = created_stamp_now()

    # Register the shared technical PDF once; every product links to this same id.
    technical_pdfs_db.append({
        "id": tech_pdf_id,
        "title": SHARED_TECHNICAL_PDF_TITLE,
        "r2_key": "",
        "url": SHARED_TECHNICAL_PDF_URL,
        "created_at": stamp,
    })
    shared_pdf_id = tech_pdf_id
    tech_pdf_id += 1

    soup_cache: dict[str, BeautifulSoup] = {}

    def _get_soup(url: str) -> BeautifulSoup | None:
        if url in soup_cache:
            return soup_cache[url]
        s = fetch_soup(url, session)
        if s is not None:
            soup_cache[url] = s
            time.sleep(0.08)
        return s

    for row in rows:
        csv_url = clean_cell(row.get("Link variante"))
        nume = clean_cell(row.get("Nume produs"))
        categorie = clean_cell(row.get("Categorie"))
        subcategorie = clean_cell(row.get("Subcategorie"))
        sub_sub = clean_cell(row.get("SUB-SUBCATEGORIE"))
        colectie = clean_cell(row.get("Colectie"))
        variante_produs = clean_cell(row.get("VARIANTE PRODUS"))

        csv_soup = _get_soup(csv_url)
        if csv_soup is None:
            print(f"  SKIP fetch failed: {csv_url}")
            continue

        finishes = _finish_from_h2(_h2_text(csv_soup))

        variant_specs = _resolve_variants(colectie, csv_url, csv_soup, variante_produs)
        if not variant_specs:
            print(f"  WARN no variants resolved for {csv_url!r} (colectie={colectie!r})")
            continue

        sizes_label = _format_sizes([size for size, _ in variant_specs])

        product = {
            "id": p_id,
            "title": nume,
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
            "sizes": sizes_label,
            "thickness": "",
            "material": "Ceramics",
            "shape": "",
            "cut": "",
            "diameter": "",
            "length": "",
            "width": "",
            "height": "",
        }
        products_db.append(product)

        product_pdfs_db.append({
            "id": pp_id,
            "product_id": p_id,
            "pdf_id": shared_pdf_id,
            "sort_order": 0,
            "created_at": stamp,
        })
        pp_id += 1

        for size_label, page_url in variant_specs:
            v_soup = _get_soup(page_url)
            if v_soup is None:
                print(f"    SKIP variant fetch failed: {page_url}")
                continue
            hero = _og_image(v_soup)
            sku = _sku_from_image(hero) or f"{SKU_PREFIX}_{v_id}"
            variants_db.append({
                "id": v_id,
                "product_id": p_id,
                "sku": sku,
                "color": "Standard",
                "url": page_url,
                "gallery_photos": json.dumps([hero] if hero else [], ensure_ascii=False),
                "technical_photos": json.dumps([], ensure_ascii=False),
            })
            print(
                f"    variant id={v_id} sku={sku!r} size={size_label!r} url={page_url}"
            )
            v_id += 1

        print(
            f"  product id={p_id} | {nume!r} ({colectie!r}) | sizes={sizes_label!r} | "
            f"finishes={finishes!r}"
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
        f"{len(technical_pdfs_db)} PDFs ({len(product_pdfs_db)} product-pdf links), "
        f"{total_gallery_count(variants_db)} gallery URLs -> {script_dir}"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="Process at most N CSV rows.")
    args = ap.parse_args()
    scrape(limit_rows=args.limit)


if __name__ == "__main__":
    main()
