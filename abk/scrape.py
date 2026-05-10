"""
ABK Group — scrape collection pages from ``links.csv`` (same row model as Milano:
forward-filled categories + one URL row per block; continuation lines add color names).

Sources:

- ``https://www.abk.it/en/collection/...`` — intro, sizes, finishes, color swatches,
  optional ``hiddenGallery`` room shots keyed by color name.
- ``https://moooiceramicsurfaces.com/en/surface/...`` — Moooi by ABK: ``minimali``
  color grid, spec list, catalogue PDF.

**Product**: one row in ``products.csv`` per spreadsheet line. The title is the
raw ``Nume produs`` from the CSV (e.g. ``Concrete Ash``); collection copy, sizes,
and finishes come from the scraped page for that row’s URL.

**Variants**: exactly **one** variant per product (the same shade); ``gallery_photos``
matches that name to the site’s colour grid. Duplicate display titles (e.g. two
``STRIPES`` lines) get `` (2)``, `` (3)``, … on the product title. ``COD REFERINTA``
→ ``sku`` when present, else ``ABK_<variant_id>``.
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
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scraper_brand_utils import (
    created_stamp_now,
    dedupe_urls,
    default_color,
    enrich_technical_pdf_title,
    norm_key,
    normalize_category,
    normalize_space,
    total_gallery_count,
    variant_sku,
    write_brand_outputs,
)

MANUFACTURER = "ABK"
SKU_PREFIX = "ABK"
LINKS_CSV = "links.csv"

START_PRODUCT_ID = 650
START_VARIANT_ID = 4500
START_TECH_PDF_ID = 1
START_PRODUCT_PDF_ID = 1

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

FFILL_COLUMNS = (
    "Categorie",
    "Subcategorie",
    "SUB-SUBCATEGORIE/BRAND",
    "Colectie",
    "Link variante",
)


def clean_cell(val: object) -> str:
    try:
        if val is None or pd.isna(val):
            return ""
    except (TypeError, ValueError):
        pass
    s = normalize_space(str(val))
    if s.lower() == "nan":
        return ""
    return s


def load_links_csv(path: Path) -> pd.DataFrame:
    if not path.is_file():
        raise FileNotFoundError(f"Missing {path}")
    df = pd.read_csv(path, encoding="utf-8-sig")
    for col in FFILL_COLUMNS:
        if col in df.columns:
            df[col] = df[col].ffill()
    return df


def subtype_column_name(df: pd.DataFrame) -> str:
    for c in df.columns:
        if norm_key(str(c)) in (
            norm_key("SUB-SUBCATEGORIE/BRAND"),
            norm_key("SUB-SUBCATEGORIE"),
        ):
            return str(c)
    return "SUB-SUBCATEGORIE/BRAND"


def normalize_page_url(raw: str) -> str:
    """Normalize URL for cache keys and requests. ABK returns 404 if a trailing ``/`` is added."""
    u = clean_cell(raw)
    if not u.startswith("http"):
        return u
    u = u.split("#")[0].strip().rstrip("/")
    p = urlparse(u)
    host = (p.netloc or "").lower()
    if "moooiceramicsurfaces.com" in host:
        return f"{p.scheme}://moooiceramicsurfaces.com{p.path or '/'}"
    return u


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


def _pdf_title_from_url(url: str) -> str:
    path = urlparse(url).path
    base = path.rsplit("/", 1)[-1]
    if base.lower().endswith(".pdf"):
        base = base[:-4]
    t = base.replace("_", " ").replace("-", " ").strip()
    return t or url


def extract_pdfs_abk(soup: BeautifulSoup, origin: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    root = soup.select_one("main") or soup
    for a in root.select("a[href]"):
        href = (a.get("href") or "").strip()
        if not href.lower().endswith(".pdf"):
            continue
        abs_u = urljoin(origin + "/", href)
        if abs_u in seen:
            continue
        seen.add(abs_u)
        label = a.get_text(" ", strip=True)
        title = label if label else _pdf_title_from_url(abs_u)
        out.append({"url": abs_u, "title": title})
    return out


def extract_pdfs_moooi(soup: BeautifulSoup, page_url: str) -> list[dict[str, str]]:
    origin = f"{urlparse(page_url).scheme}://{urlparse(page_url).netloc}"
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for a in soup.select("main a[href]"):
        href = (a.get("href") or "").strip()
        if not href or ".pdf" not in href.lower():
            continue
        abs_u = urljoin(page_url, href)
        if abs_u in seen:
            continue
        seen.add(abs_u)
        label = a.get_text(" ", strip=True)
        title = label if label else _pdf_title_from_url(abs_u)
        out.append({"url": abs_u, "title": title})
    return out


def parse_abk_collection_page(soup: BeautifulSoup, page_url: str) -> dict[str, Any]:
    origin = f"{urlparse(page_url).scheme}://{urlparse(page_url).netloc}"
    main = soup.select_one("main")
    title = ""
    h1 = soup.select_one("h1.h1introCaption") or soup.select_one("main h1")
    if h1:
        title = h1.get_text(" ", strip=True)

    desc_parts: list[str] = []
    left = soup.select_one(".col-lg-7.collezioneInfoContent")
    if left:
        tag = left.select_one(".h3")
        if tag:
            t = tag.get_text(" ", strip=True)
            if t:
                desc_parts.append(t)

    finishes: list[str] = []
    sizes: list[str] = []
    material = ""
    right = soup.select_one(".col-lg-4.offset-lg-1.collezioneInfoContent")
    if right:
        for cap in right.select("p.collectionCapitol"):
            lab = cap.get_text(" ", strip=True)
            if lab and lab.casefold() not in ("surfaces", "size") and not material:
                material = lab
        h4s = right.select("h4")
        if len(h4s) >= 1:
            for sp in h4s[0].select("span"):
                t = sp.get_text(" ", strip=True)
                if t:
                    finishes.append(t)
        if len(h4s) >= 2:
            for sp in h4s[1].select("span"):
                t = sp.get_text(" ", strip=True)
                if t:
                    sizes.append(t)

    desc = "\n\n".join(dict.fromkeys(desc_parts))

    color_galleries: dict[str, list[str]] = {}
    for div in soup.select("section.collectionSliderGallery .col-6, section.collectionSliderGallery .col-sm-3"):
        cap = div.select_one("p.text-center")
        if not cap:
            continue
        label = cap.get_text(" ", strip=True)
        if not label:
            continue
        urls: list[str] = []
        for img in div.select("a.glightbox img[src], .boxed img[src]"):
            src = (img.get("src") or "").strip()
            if src:
                urls.append(urljoin(origin + "/", src))
        nk = norm_key(label)
        if nk not in color_galleries:
            color_galleries[nk] = []
        color_galleries[nk].extend(urls)

    for a in soup.select(".hiddenGallery a[href]"):
        href = (a.get("href") or "").strip()
        gal = (a.get("data-gallery") or "").strip()
        if not href or not gal:
            continue
        nk = norm_key(gal)
        if nk not in color_galleries:
            color_galleries[nk] = []
        color_galleries[nk].append(urljoin(origin + "/", href))

    for k in list(color_galleries.keys()):
        color_galleries[k] = dedupe_urls(color_galleries[k])

    pdfs = extract_pdfs_abk(soup, origin)

    slider = soup.select_one("section.collectionSliderGallery")
    thick_blob = slider.get_text(" ", strip=True) if slider else ""
    thick_hints = re.findall(r"\b(\d+(?:[.,]\d+)?)\s*mm\b", thick_blob, flags=re.I)
    thickness = ", ".join(sorted({x.replace(",", ".") for x in thick_hints})) if thick_hints else ""

    return {
        "title": title,
        "description": desc,
        "finishes": ", ".join(dict.fromkeys(finishes)),
        "sizes": ", ".join(dict.fromkeys(sizes)),
        "material": material,
        "thickness": thickness,
        "color_galleries": color_galleries,
        "pdfs": pdfs,
    }


def parse_moooi_surface_page(soup: BeautifulSoup, page_url: str) -> dict[str, Any]:
    main = soup.select_one("main")
    title = ""
    h1 = soup.select_one(".topSlide h1.h1") or soup.select_one("main h1")
    if h1:
        title = h1.get_text(" ", strip=True)

    desc_parts: list[str] = []
    intro = soup.select_one(".prodDetail .h3")
    if intro:
        desc_parts.append(intro.get_text(" ", strip=True))
    for p in soup.select(".prodDetail .txt-sang-light p"):
        t = p.get_text(" ", strip=True)
        if t:
            desc_parts.append(t)
    desc = "\n\n".join(dict.fromkeys(desc_parts))

    sizes = ""
    thickness = ""
    for li in soup.select(".prodDetail ul li"):
        name_el = li.select_one("span.name")
        cod_el = li.select_one("span.cod")
        if not name_el or not cod_el:
            continue
        k = name_el.get_text(" ", strip=True).casefold()
        v = cod_el.get_text(" ", strip=True)
        if k == "format":
            sizes = v
        elif "thick" in k:
            thickness = v

    origin = f"{urlparse(page_url).scheme}://{urlparse(page_url).netloc}"

    def _resolve_moooi_asset(href: str) -> str:
        """Moooi's ``../public/prodcolors/...`` hrefs are intended to resolve to
        ``https://moooiceramicsurfaces.com/public/...`` (the page's ``../`` count
        does not match the actual depth, so urljoin would emit ``/en/public/...``)."""
        s = (href or "").strip()
        if not s:
            return ""
        if s.startswith("http"):
            return s
        s = re.sub(r"^(?:\.\./)+", "", s)
        if s.startswith("/"):
            return urljoin(origin + "/", s.lstrip("/"))
        return urljoin(origin + "/", s)

    color_galleries: dict[str, list[str]] = {}
    for li in soup.select(".minimali ul li"):
        a = li.select_one("a.glightbox[href]")
        cap = li.select_one("p.h6, p")
        if not a:
            continue
        label = ""
        if cap:
            label = cap.get_text(" ", strip=True)
        if not label:
            alt = a.select_one("img[alt]")
            if alt:
                label = (alt.get("alt") or "").strip()
        if not label:
            continue
        href = (a.get("href") or "").strip()
        nk = norm_key(label)
        if nk not in color_galleries:
            color_galleries[nk] = []
        if href:
            color_galleries[nk].append(_resolve_moooi_asset(href))
        for img in a.select("img[src]"):
            src = (img.get("src") or "").strip()
            if src and "-thumb" not in src.casefold():
                color_galleries[nk].append(_resolve_moooi_asset(src))

    for k in list(color_galleries.keys()):
        color_galleries[k] = dedupe_urls(color_galleries[k])

    pdfs = extract_pdfs_moooi(soup, page_url)

    return {
        "title": title,
        "description": desc,
        "finishes": "",
        "sizes": sizes,
        "material": "Porcelain",
        "thickness": thickness,
        "color_galleries": color_galleries,
        "pdfs": pdfs,
    }


def parse_page(soup: BeautifulSoup, page_url: str) -> dict[str, Any]:
    host = (urlparse(page_url).netloc or "").lower()
    if "moooiceramicsurfaces.com" in host:
        return parse_moooi_surface_page(soup, page_url)
    return parse_abk_collection_page(soup, page_url)


_IMPERIAL_PARENS_RE = re.compile(r"\s*\(\s*\d[^)]*[\"”][^)]*\)\s*", re.I)


def _clean_size_token(s: str) -> str:
    """Strip the trailing imperial parenthetical, e.g. ``120×120 (48"x48")`` -> ``120x120``."""
    s = _IMPERIAL_PARENS_RE.sub("", s).strip()
    s = s.replace("×", "x").replace("X", "x")
    return s.strip()


def _clean_sizes_csv(raw: str) -> str:
    if not raw:
        return ""
    parts = [_clean_size_token(p) for p in re.split(r"[,;]", raw)]
    return ", ".join(p for p in dict.fromkeys(parts) if p)


def gallery_for_color(name: str, color_galleries: dict[str, list[str]]) -> list[str]:
    nk = norm_key(name)
    if not nk:
        return []
    if nk in color_galleries:
        return color_galleries[nk]
    for k, urls in color_galleries.items():
        if nk == k or nk in k or k in nk:
            return urls
    for k, urls in color_galleries.items():
        parts = k.split()
        if len(parts) >= 2 and all(p in nk for p in parts if len(p) > 2):
            return urls
    return []


def infer_position(categorie: str, sub: str) -> str:
    blob = f"{categorie} {sub}".lower()
    if "outdoor" in blob or "deck" in blob:
        return "Floor"
    if "wall" in blob or "decor" in blob or "décor" in blob:
        return "Wall"
    return ""


def scrape(*, limit_rows: int | None = None) -> None:
    script_dir = Path(__file__).resolve().parent
    df = load_links_csv(script_dir / LINKS_CSV)
    subtype_col = subtype_column_name(df)

    data_rows: list[pd.Series] = []
    for _, row in df.iterrows():
        link = normalize_page_url(clean_cell(row.get("Link variante")))
        np = clean_cell(row.get("Nume produs"))
        if not link.startswith("http") or not np:
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

    soup_cache: dict[str, BeautifulSoup] = {}

    def get_soup(u: str) -> tuple[BeautifulSoup | None, str]:
        nu = normalize_page_url(u)
        if nu in soup_cache:
            return soup_cache[nu], nu
        soup = fetch_soup(nu, session)
        if soup:
            soup_cache[nu] = soup
        time.sleep(0.08)
        return soup, nu

    title_occurrences: dict[str, int] = {}

    for row in data_rows:
        u = normalize_page_url(clean_cell(row.get("Link variante")))
        soup, final_url = get_soup(u)
        if not soup:
            print(f"\n=== SKIP (fetch failed) {final_url} ===")
            continue

        parsed = parse_page(soup, final_url)
        site_h1 = clean_cell(parsed.get("title", ""))
        nume = clean_cell(row.get("Nume produs"))
        colectie = clean_cell(row.get("Colectie"))
        base_title = nume
        nk_base = norm_key(base_title)
        title_occurrences[nk_base] = title_occurrences.get(nk_base, 0) + 1
        n_dup = title_occurrences[nk_base]
        title = base_title if n_dup == 1 else f"{base_title} ({n_dup})"

        desc = clean_cell(parsed.get("description", ""))

        finishes = clean_cell(parsed.get("finishes", ""))
        sizes = _clean_sizes_csv(clean_cell(parsed.get("sizes", "")))
        thickness = clean_cell(parsed.get("thickness", ""))
        material = clean_cell(parsed.get("material", ""))
        if not material:
            blob = f"{desc} {site_h1}".lower()
            if "porcelain" in blob:
                material = "Porcelain"

        categorie = clean_cell(row.get("Categorie"))
        subcategorie = clean_cell(row.get("Subcategorie"))
        brand = clean_cell(row.get(subtype_col))

        manufacturer = MANUFACTURER
        if "moooi" in brand.casefold():
            manufacturer = "MOOOI BY ABK"

        products_db.append(
            {
                "id": p_id,
                "title": title,
                "description": desc,
                "category": normalize_category(categorie),
                "type": subcategorie,
                "collection": colectie,
                "is_new": False,
                "subtype": brand,
                "manufacturer": manufacturer,
                "catalog_id": None,
                "finishes": finishes,
                "position": infer_position(categorie, subcategorie),
                "sizes": sizes,
                "thickness": thickness,
                "material": material,
                "shape": "",
                "cut": "",
                "diameter": "",
                "length": "",
                "width": "",
                "height": "",
            }
        )

        color_map: dict[str, list[str]] = parsed.get("color_galleries") or {}
        for sort_i, doc in enumerate(parsed.get("pdfs") or []):
            doc_u = doc["url"]
            if doc_u not in pdf_url_to_id:
                pdf_url_to_id[doc_u] = pdf_id_counter
                technical_pdfs_db.append(
                    {
                        "id": pdf_id_counter,
                        "title": enrich_technical_pdf_title(
                            doc["title"],
                            product_title=site_h1 or colectie,
                            collection=colectie,
                        ),
                        "r2_key": "",
                        "url": doc_u,
                        "created_at": stamp,
                    }
                )
                pdf_id_counter += 1
            product_pdfs_db.append(
                {
                    "id": pp_id_counter,
                    "product_id": p_id,
                    "pdf_id": pdf_url_to_id[doc_u],
                    "sort_order": sort_i,
                    "created_at": stamp,
                }
            )
            pp_id_counter += 1

        name = nume
        sku = variant_sku(SKU_PREFIX, v_id, clean_cell(row.get("COD REFERINTA")))
        gurls = gallery_for_color(name, color_map)
        col_label = default_color(name.title() if name else "")

        variants_db.append(
            {
                "id": v_id,
                "product_id": p_id,
                "sku": sku,
                "color": col_label,
                "url": final_url,
                "gallery_photos": json.dumps(gurls, ensure_ascii=False),
                "technical_photos": json.dumps([], ensure_ascii=False),
            }
        )

        print(
            f"\n  product id={p_id} variant id={v_id} | {title!r} | "
            f"PDFs={len(parsed.get('pdfs') or [])} | imgs={len(gurls)}"
        )
        p_id += 1
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
    ap.add_argument(
        "--limit-rows",
        type=int,
        default=None,
        help="Process only the first N data rows from links.csv (for testing).",
    )
    args = ap.parse_args()
    scrape(limit_rows=args.limit_rows)


if __name__ == "__main__":
    main()
