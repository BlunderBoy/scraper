"""
Unikolegno -- scrape product pages from ``links.csv``.

CSV row model: one row per wood-finish variant, six unique products
(Tris, Four, Type, Loft Spina, Loft Classic Spina, Twenty). Each row carries
a per-variant ``link Finisaj`` (a direct image URL of the wood swatch) which
is included in that variant's ``gallery_photos`` alongside the room-shot
photos pulled from the product page (``gallerySingleProd``).

Note: this CSV uses ``Sub-subcategorie`` (mixed case) and ``Variante`` as
column names -- different from the standard schema. They are mapped
explicitly.

SKU: empty -> ``UNI_<variant_id>``.
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
    total_gallery_count,
    variant_sku,
    write_brand_outputs,
)

MANUFACTURER = "Unikolegno"
SKU_PREFIX = "UNI"
LINKS_CSV = "links.csv"

START_PRODUCT_ID = 1800
START_VARIANT_ID = 10500
START_TECH_PDF_ID = 1
START_PRODUCT_PDF_ID = 1

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

CATALOG_NOTE_RO = (
    "Pentru mai multe tipuri de lemn și de finisaje puteți consulta catalogul producătorului."
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


def page_title(soup: BeautifulSoup) -> str:
    h1 = soup.select_one("h1")
    return h1.get_text(" ", strip=True) if h1 else ""


def gallery_photos(soup: BeautifulSoup) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for el in soup.select('[class*="gallerySingleProd"]'):
        for img in el.select("img"):
            for attr in ("data-src", "src"):
                v = (img.get(attr) or "").strip()
                if not v or "logo" in v.lower() or v.lower().endswith(".svg"):
                    continue
                if v in seen:
                    continue
                seen.add(v)
                out.append(v)
    return dedupe_urls(out)


def technical_photos(soup: BeautifulSoup) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for img in soup.select("img"):
        for attr in ("data-src", "src", "data-srcset"):
            v = (img.get(attr) or "").strip()
            if "_scheda" in v.lower() and "wp-content/uploads" in v:
                stripped = re.sub(r"-\d+x\d+(?=\.\w+$)", "", v.split(",")[0].split()[0])
                if stripped not in seen:
                    seen.add(stripped)
                    out.append(stripped)
    return dedupe_urls(out)


def technical_data(soup: BeautifulSoup) -> dict[str, str]:
    """Extract the ``TECHNICAL DATA`` table (laying method, thickness, width, length)
    plus the diagram legend (A/B/C/D/E) so the technical photo is self-contained.
    """
    out: dict[str, str] = {
        "laying_method": "",
        "thickness": "",
        "width": "",
        "length": "",
        "construction": "",
        "legend": "",
    }
    h3 = next(
        (h for h in soup.select("h3") if "technical data" in (h.get_text(" ", strip=True) or "").lower()),
        None,
    )
    if h3:
        construction_p = h3.find_next_sibling("p")
        if construction_p:
            out["construction"] = normalize_space(construction_p.get_text(" ", strip=True))
        block = h3.find_parent("div")
        if block is not None:
            text = block.get_text("\n", strip=True)
            for line in text.splitlines():
                m = re.match(r"\s*(Laying method|Thickness|Width\*?|Length)\s*$", line, re.I)
                if not m:
                    continue
            label_map = {
                "laying method": "laying_method",
                "thickness": "thickness",
                "width": "width",
                "width*": "width",
                "length": "length",
            }
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            for i, line in enumerate(lines):
                key = label_map.get(line.lower())
                if key and i + 1 < len(lines):
                    out[key] = lines[i + 1]

    legend_el = soup.select_one('[class*="tec-image-legenda"]')
    if legend_el is not None:
        items: list[str] = []
        text = legend_el.get_text("\n", strip=True)
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        i = 0
        while i < len(lines):
            label = lines[i]
            if re.fullmatch(r"[A-Z]", label) and i + 1 < len(lines):
                items.append(f"{label} {lines[i+1].lstrip('–-').strip()}")
                i += 2
                continue
            i += 1
        out["legend"] = ", ".join(items)
    return out


def product_description(soup: BeautifulSoup, td: dict[str, str]) -> str:
    parts: list[str] = []
    for sel in (".sheet-content", ".description", '[class*="sheet-content"]', '[class*="description"]'):
        for el in soup.select(sel):
            t = normalize_space(el.get_text(" ", strip=True))
            if not t or len(t) < 25:
                continue
            if "tutti i diritti" in t.lower() or "p.iva" in t.lower():
                continue
            parts.append(t)
    if td.get("construction"):
        parts.append(td["construction"])
    if td.get("legend"):
        parts.append(f"Construcție: {td['legend']}.")
    parts.append(CATALOG_NOTE_RO)
    return "\n\n".join(dict.fromkeys(parts))


def _format_mm_value(v: str) -> str:
    s = re.sub(r"\s*\*[^*]*$", "", (v or "").strip())
    s = s.replace("*", "").strip()
    if not s:
        return ""
    if re.fullmatch(r"\d+(?:[.,]\d+)?(?:\s*[/-]\s*\d+(?:[.,]\d+)?)*\s*(?:or\s*\d+(?:\s*/\s*\d+(?:[.,]\d+)?)*)?", s, re.I):
        return f"{s} mm"
    return s


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
    for col in ("Categorie", "Subcategorie", "Sub-subcategorie", "Colectie", "Nume produs", "Link variante"):
        if col in df.columns:
            df[col] = df[col].ffill()
    return df


def product_key(row: pd.Series) -> tuple[str, str]:
    return (
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
        sub_sub = clean_cell(first.get("Sub-subcategorie"))
        colectie = clean_cell(first.get("Colectie"))

        description = ""
        page_gallery: list[str] = []
        tech_imgs: list[str] = []
        pdfs: list[dict[str, str]] = []
        td: dict[str, str] = {}
        if url:
            soup = get_soup(url)
            if soup is not None:
                td = technical_data(soup)
                description = product_description(soup, td)
                page_gallery = gallery_photos(soup)
                tech_imgs = technical_photos(soup)
                pdfs = collect_pdfs(soup, url)

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
            "finishes": "",
            "position": "",
            "sizes": "",
            "thickness": _format_mm_value(td.get("thickness", "")),
            "material": "Wood",
            "shape": "",
            "cut": "",
            "diameter": "",
            "length": _format_mm_value(td.get("length", "")),
            "width": _format_mm_value(td.get("width", "")),
            "height": "",
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
            color = default_color(normalize_space(clean_cell(r.get("Variante"))))
            sku = variant_sku(SKU_PREFIX, v_id, clean_cell(r.get("COD REFERINTA")))
            swatch = clean_cell(r.get("link Finisaj"))

            gallery: list[str] = []
            if swatch.startswith("http"):
                gallery.append(swatch)
            gallery.extend(page_gallery)
            gallery = dedupe_urls(gallery)

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

        print(f"  product id={p_id} | {np_!r} | variants={len(group)} | page_imgs={len(page_gallery)} | tech={len(tech_imgs)} | pdfs={len(pdfs)}")
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
