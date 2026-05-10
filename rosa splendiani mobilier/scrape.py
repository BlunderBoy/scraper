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
    """First 1-2 ``<p>`` paragraphs that follow the H1 -- not the entire page text.

    The site has nested wrapper ``<div>`` elements containing the full content; we
    skip those and only pick leaf ``<p>`` paragraphs to keep the description short.
    """
    h1 = main.select_one("h1")
    if h1 is None:
        return ""
    parts: list[str] = []
    seen: set[str] = set()
    nxt = h1
    for _ in range(120):
        nxt = nxt.find_next()
        if nxt is None:
            break
        if not isinstance(nxt, Tag) or nxt.name != "p":
            continue
        if nxt.find(["h2", "h3", "h4", "h5"]):
            continue
        t = normalize_space(nxt.get_text(" ", strip=True))
        if not t or len(t) < 40 or len(t) > 1500:
            continue
        low = t.lower()
        if "p.iva" in low or "p.i. and c.f." in low or "zona industriale" in low:
            continue
        if "all rights reserved" in low or "©" in t:
            continue
        if "cookie" in low and len(t) < 200:
            continue
        if t in seen:
            continue
        seen.add(t)
        parts.append(t)
        if len(parts) >= 2:
            break
    return "\n\n".join(parts)


_RO_TO_EN_MODULE_KEYWORDS: dict[str, list[str]] = {
    "simplu": ["single"],
    "simpla": ["single"],
    "colt": ["corner"],
    "coltz": ["corner"],
    "dublu": ["double"],
    "dubla": ["double"],
    "mare": ["large", "big"],
    "mic": ["small"],
    "puf": ["pouff", "pouf", "pouffe", "ottoman", "footstool"],
    "rotund": ["round"],
    "rotunda": ["round"],
    "patrat": ["square"],
    "patrata": ["square"],
    "dreptunghiular": ["rectangular"],
    "central": ["central", "middle"],
    "spatar": ["backrest", "back"],
    "perna": ["pillow", "cushion"],
    "perne": ["pillows", "cushions"],
    "decorative": ["decorative"],
    "fotoliu": ["armchair"],
    "canapea": ["sofa"],
    "scaun": ["chair"],
    "modular": ["modular"],
    "set": ["set"],
}

_HIGH_VALUE_TOKENS = {
    "single", "double", "corner", "large", "big", "small", "round", "square",
    "pouff", "pouf", "pouffe", "ottoman", "footstool", "armchair", "chair", "sofa",
    "backrest", "pillow", "cushion", "decorative", "modular", "central", "rectangular",
}


def _strip_diacritics(s: str) -> str:
    s = re.sub(r"[\u0218\u0219]", "s", s)
    s = re.sub(r"[\u021a\u021b]", "t", s)
    s = re.sub(r"[âă]", "a", s)
    s = re.sub(r"î", "i", s)
    return s


def _variant_keywords(label: str) -> set[str]:
    """Translate Romanian variant labels (or English H4 labels) into a comparable
    set of English keywords + dimension tokens.
    """
    s = _strip_diacritics((label or "").lower())
    out: set[str] = set()
    for tok in re.findall(r"[a-z]+", s):
        if tok in _RO_TO_EN_MODULE_KEYWORDS:
            out.update(_RO_TO_EN_MODULE_KEYWORDS[tok])
        elif len(tok) >= 4:
            out.add(tok)
    for n in re.findall(r"\d{2,4}", label or ""):
        out.add(n)
    return out


def build_module_label_to_image(main: Tag) -> dict[str, str]:
    """Walk DOM in order. On site, each module image precedes its H4 label.

    Tracks the most recent product image and pairs it with the next ``<h4>``.
    Stops harvesting after a "Matching and related" / similar section heading.
    """
    out: dict[str, str] = {}
    last_img: str | None = None
    for el in main.descendants:
        if not isinstance(el, Tag):
            continue
        if el.name == "img":
            src = (el.get("src") or "").strip()
            if not src:
                continue
            low = src.lower()
            if "wp-content/uploads" not in low:
                continue
            if "favicon" in low or "logo" in low or "brandbook" in low:
                continue
            if re.search(r"-\d+x\d+(?=\.\w+$)", src):
                src = re.sub(r"-\d+x\d+(?=\.\w+$)", "", src)
            last_img = src
        elif el.name in ("h2", "h3", "h4", "h5"):
            text = normalize_space(el.get_text(" ", strip=True))
            if not text or len(text) > 200:
                continue
            low_t = text.lower()
            if any(needle in low_t for needle in SECTION_CUTS):
                break
            # Skip very generic / unrelated headings
            if any(s in low_t for s in ("contacts", "policy", "outdoor", "interiors", "company")):
                continue
            if last_img is not None:
                key = norm_key(text)
                if key not in out:
                    out[key] = last_img
                last_img = None
    return out


def match_variant_image(
    variant_label: str,
    module_map: dict[str, str],
) -> str:
    """Pick the module image whose H4 text shares the most distinctive keywords with ``variant_label``.

    High-value tokens (single/double/corner/round/etc.) and numeric dimensions count
    extra so module-specific modifiers win over generic shared words.
    """
    if not module_map or not variant_label:
        return ""
    var_kw = _variant_keywords(variant_label)
    if not var_kw:
        return ""
    best_score = -1.0
    best_url = ""
    for label, url in module_map.items():
        lab_kw = _variant_keywords(label)
        if not lab_kw:
            continue
        common = var_kw & lab_kw
        score = 0.0
        for k in common:
            if k.isdigit():
                score += 3.0
            elif k in _HIGH_VALUE_TOKENS:
                score += 2.0
            else:
                score += 0.25
        if score > best_score:
            best_score = score
            best_url = url
    if best_score >= 1.5:
        return best_url
    return ""


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
        module_map: dict[str, str] = {}
        if url:
            soup = get_soup(url)
            if soup is not None:
                trimmed = trim_after_related(soup)
                description = page_description(trimmed)
                gallery = gallery_urls(trimmed)
                pdfs = collect_pdfs(soup, url)
                module_map = build_module_label_to_image(trimmed)
        else:
            print(f"  no URL for {np_!r} -- product page skipped")

        dim_info = parse_dimensions_from_text(description) if description else {}

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
            "material": "",
            "shape": "",
            "cut": "",
            "diameter": dim_info.get("diameter", ""),
            "length": dim_info.get("length", ""),
            "width": dim_info.get("width", ""),
            "height": dim_info.get("height", ""),
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
            raw_color = variant_color(r)
            color = default_color(raw_color)
            sku = variant_sku(SKU_PREFIX, v_id, clean_cell(r.get("COD REFERINTA")))
            row_url = clean_cell(r.get("Link variante"))
            if not row_url.startswith("http"):
                row_url = url

            module_label = normalize_space(clean_cell(r.get("Nume variante")))
            variant_gallery = list(gallery)
            if module_map and module_label:
                matched = match_variant_image(module_label, module_map)
                if matched:
                    variant_gallery = dedupe_urls([matched] + gallery)

            variants_db.append({
                "id": v_id,
                "product_id": p_id,
                "sku": sku,
                "color": color,
                "url": row_url,
                "gallery_photos": json.dumps(variant_gallery, ensure_ascii=False),
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
