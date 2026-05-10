"""
GSG (Ceramica GSG) -- scrape product pages from ``links.csv``.

Site is WooCommerce. Each CSV row is one variant; many variants share the
same product page URL. ``data-product_variations`` is dropped from the static
HTML (loaded via AJAX), so per-variant images are not available -- the
single-color hero image of the page is used as the gallery for every variant
of that product.

SKU comes from ``COD REFERINTA`` for Petra rows; for Cruise / Like rows that
column is empty so it falls back to ``GSG_<variant_id>`` per the plan.

Product grouping key: ``(Categorie, Subcategorie, SUB-SUBCATEGORIE, Colectie, Nume produs)``.
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

MANUFACTURER = "GSG"
SKU_PREFIX = "GSG"
LINKS_CSV = "links.csv"

START_PRODUCT_ID = 1300
START_VARIANT_ID = 8000
START_TECH_PDF_ID = 1
START_PRODUCT_PDF_ID = 1

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


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
    return og.get("content", "").strip() if og else ""


def hero_gallery(soup: BeautifulSoup) -> list[str]:
    out: list[str] = []
    og = og_image(soup)
    if og:
        out.append(og)
    seen = set(out)
    for el in soup.select(".product-gallery img, .woocommerce-product-gallery img"):
        for attr in ("data-large_image", "data-src", "src"):
            v = (el.get(attr) or "").strip()
            if v and "wp-content" in v and "logo" not in v.lower() and v not in seen:
                # Prefer the full-resolution path (drop ``-NNNxNNN`` suffix if present).
                stripped = re.sub(r"-\d+x\d+(?=\.\w+$)", "", v)
                if stripped not in seen:
                    seen.add(stripped)
                    out.append(stripped)
    return dedupe_urls(out)


def product_description(soup: BeautifulSoup) -> str:
    parts: list[str] = []
    summary = soup.select_one(".entry-summary, .product-summary, .summary") or soup
    for p in summary.find_all("p", recursive=True):
        txt = normalize_space(p.get_text(" ", strip=True))
        if not txt or len(txt) < 30:
            continue
        low = txt.lower()
        if "all rights reserved" in low or "any reproduction" in low:
            continue
        if "p.iva" in low or "iscr. reg" in low:
            continue
        parts.append(txt)
    if not parts:
        for s in soup.select('script[type="application/ld+json"]'):
            try:
                data = json.loads(s.string or "")
            except Exception:
                continue
            graph = data.get("@graph", [data]) if isinstance(data, dict) else [data]
            for n in graph if isinstance(graph, list) else []:
                if isinstance(n, dict) and n.get("@type") == "Product":
                    d = clean_cell(n.get("description"))
                    if d:
                        parts.append(d)
    return "\n\n".join(dict.fromkeys(parts))


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


def derive_variant_image(default_image: str, default_sku: str, variant_sku_text: str) -> str:
    """Best-effort URL hack: replace the default SKU stem in the og:image URL with the variant's stem."""
    if not default_image or not default_sku or not variant_sku_text:
        return ""
    def stem(s: str) -> str:
        return re.sub(r"\s*-\s*", "-", s).strip()
    a, b = stem(default_sku), stem(variant_sku_text)
    if a and b and a != b and a in default_image:
        return default_image.replace(a, b)
    return ""


_COLOR_PARTICLES = re.compile(
    r"\b(?:cod\.?|lucido|matt|matte|opaco|satinato|lucida)\b",
    re.I,
)


def _color_label_key(s: str) -> str:
    """Strip ``cod.NNN``, finish keywords, and punctuation so swatch titles and CSV
    color names compare equal (e.g. ``"Bianco matt cod.001"`` <-> ``"Bianco matt cod.001"`` 
    or ``"Bicolore Bianco Matt"`` <-> ``"Bicolore bianco matt"``)."""
    s = (s or "").strip()
    s = re.sub(r"cod\.?\s*\d+", "", s, flags=re.I)
    s = _COLOR_PARTICLES.sub(" ", s)
    s = re.sub(r"[^a-zA-Z0-9\s]", " ", s)
    return norm_key(s)


def parse_variation_attribute(soup: BeautifulSoup) -> tuple[str, str, dict[str, str]]:
    """Extract WooCommerce variation product id, attribute name, and {csv_label_key: slug}.

    Returns ``(product_id, attribute_name, label_to_slug)`` or ``("", "", {})`` if the
    page has no variations form.
    """
    form = soup.select_one("form.variations_form, form[data-product_id]")
    if form is None:
        return "", "", {}
    pid = (form.get("data-product_id") or "").strip()
    label_to_slug: dict[str, str] = {}
    attr_name = ""
    for li in soup.select(".thwvsf-wrapper-item-li[data-attribute_name][data-value]"):
        attr = (li.get("data-attribute_name") or "").strip()
        slug = (li.get("data-value") or "").strip()
        title = (li.get("title") or li.get("aria-label") or "").strip()
        title = re.sub(r"\s+(?:color\s+option|product\s+option)$", "", title, flags=re.I).strip()
        if not slug or not title:
            continue
        if not attr_name:
            attr_name = attr
        key = _color_label_key(title)
        if key and key not in label_to_slug:
            label_to_slug[key] = slug
    if not attr_name:
        for sel in soup.select("form.variations_form select[name^='attribute_']"):
            attr_name = (sel.get("name") or "").strip()
            for opt in sel.select("option"):
                slug = (opt.get("value") or "").strip()
                title = opt.get_text(" ", strip=True)
                key = _color_label_key(title)
                if slug and key and key not in label_to_slug:
                    label_to_slug[key] = slug
            if attr_name:
                break
    return pid, attr_name, label_to_slug


def fetch_variant_image(
    session: requests.Session,
    page_url: str,
    product_id: str,
    attr_name: str,
    slug: str,
) -> str:
    """Hit the WooCommerce ``get_variation`` AJAX endpoint and return the full-size image URL."""
    if not product_id or not attr_name or not slug:
        return ""
    parsed = urlparse(page_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    lang_prefix = ""
    parts = [p for p in (parsed.path or "").split("/") if p]
    if parts and parts[0] in ("en", "it", "fr", "de", "es"):
        lang_prefix = "/" + parts[0]
    ajax_url = f"{origin}{lang_prefix}/?wc-ajax=get_variation"
    try:
        r = session.post(
            ajax_url,
            data={"product_id": product_id, attr_name: slug},
            timeout=20,
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        if r.status_code != 200 or not r.text:
            return ""
        data = json.loads(r.text)
    except (requests.RequestException, json.JSONDecodeError):
        return ""
    img = (data or {}).get("image") or {}
    full = clean_cell(img.get("full_src")) or clean_cell(img.get("url")) or clean_cell(img.get("src"))
    return full


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
        link = clean_cell(row.get("Link variante"))
        np_ = clean_cell(row.get("Nume produs"))
        if not link.startswith("http") or not np_:
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
        url = clean_cell(first.get("Link variante"))
        soup = get_soup(url)
        if soup is None:
            print(f"  SKIP product (fetch failed) {url}")
            continue

        np_ = clean_cell(first.get("Nume produs"))
        categorie = clean_cell(first.get("Categorie"))
        subcategorie = clean_cell(first.get("Subcategorie"))
        sub_sub = clean_cell(first.get("SUB-SUBCATEGORIE"))
        colectie = clean_cell(first.get("Colectie"))

        description = product_description(soup)

        finishes_set = sorted(
            {clean_cell(r.get("Variante culori")) for r in group if clean_cell(r.get("Variante culori"))},
            key=lambda s: s.casefold(),
        )
        finishes = ", ".join(finishes_set)

        gallery_default = hero_gallery(soup)
        default_sku = clean_cell(first.get("COD REFERINTA"))
        product_wc_id, attr_name, label_to_slug = parse_variation_attribute(soup)

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
            "finishes": "",
            "position": "",
            "sizes": "",
            "thickness": "",
            "material": "Ceramica",
            "shape": "",
            "cut": "",
            "diameter": dims.get("diameter", ""),
            "length": dims.get("length", ""),
            "width": dims.get("width", ""),
            "height": dims.get("height", ""),
        }
        products_db.append(product)

        for sort_i, doc in enumerate(collect_pdfs(soup, url)):
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

        slug_image_cache: dict[str, str] = {}

        for r in group:
            sku = variant_sku(SKU_PREFIX, v_id, clean_cell(r.get("COD REFERINTA")))
            csv_color = normalize_space(clean_cell(r.get("Variante culori")))
            color = default_color(csv_color)

            variant_image = ""
            slug = label_to_slug.get(_color_label_key(csv_color))
            if slug and product_wc_id and attr_name:
                if slug in slug_image_cache:
                    variant_image = slug_image_cache[slug]
                else:
                    variant_image = fetch_variant_image(session, url, product_wc_id, attr_name, slug)
                    slug_image_cache[slug] = variant_image
                    time.sleep(0.05)

            gallery: list[str] = []
            if variant_image:
                gallery.append(variant_image)
            gallery.extend(gallery_default)

            row_sku = clean_cell(r.get("COD REFERINTA"))
            if not variant_image and row_sku and default_sku and row_sku != default_sku and gallery_default:
                hacked = derive_variant_image(gallery_default[0], default_sku, row_sku)
                if hacked and hacked != gallery_default[0]:
                    gallery = [hacked] + gallery

            variants_db.append({
                "id": v_id,
                "product_id": p_id,
                "sku": sku,
                "color": color,
                "url": url,
                "gallery_photos": json.dumps(dedupe_urls(gallery), ensure_ascii=False),
                "technical_photos": json.dumps([], ensure_ascii=False),
            })
            v_id += 1

        matched = sum(1 for k in slug_image_cache.values() if k)
        print(f"  product id={p_id} | {np_!r} | variants={len(group)} | imgs(default)={len(gallery_default)} | per-variant_imgs={matched}")
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
