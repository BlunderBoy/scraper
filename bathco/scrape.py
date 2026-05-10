"""
Scrape Bathco (The Bath Collection) product pages driven by ``links.csv``.

**Row model**

- Each CSV row with ``Link variante`` + ``COD REFERINTA`` is **one variant**
  (one row in ``variants.csv``).
- Leading blank cells inherit from the row above for:
  ``Categorie``, ``Subcategorie``, ``SUB-SUBCATEGORIE``, ``Colectie``, ``Nume produs``, ``Variante culori``.
  ``Link variante``, ``COD REFERINTA`` are always taken from the row itself.

**Product identity**

- One **product** per distinct logical model while walking the sheet:
  tuple ``(Categorie, Subcategorie, SUB-SUBCATEGORIE, Colectie, Nume produs)``
  after forward-fill. Multiple URLs / finishes for the same model are **variants**.

**CSV → schema**

- ``Colectie`` → ``collection``
- ``COD REFERINTA`` → variant ``sku``
- ``Categorie`` → ``category`` (lower-cased)
- ``Subcategorie`` → ``type``
- ``SUB-SUBCATEGORIE`` → ``subtype``
- ``Variante culori`` → variant ``color`` (title case only)

Product ``description`` comes only from the **Description** accordion
(``data-hash="descripcion"``), not from Downloads / login notices.
``sizes`` and variant ``technical_photos`` come from the **Dimensions** accordion
(``data-hash="dimensiones"``); diagram images are excluded from ``gallery_photos``.
PDFs / ``?pdf=`` technical-spec links are taken from the **Downloads** accordion only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup, Tag

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scraper_brand_utils import (
    aggregate_unique_column,
    default_color,
    enrich_technical_pdf_title,
    normalize_category,
    parse_dimensions_from_text,
)

MANUFACTURER = "Bathco"
LINKS_CSV = "links.csv"
BASE_ORIGIN = "https://www.thebathcollection.com"

START_PRODUCT_ID = 400
START_VARIANT_ID = 1200
START_TECH_PDF_ID = 1
START_PRODUCT_PDF_ID = 1

PRODUCT_CSV_COLUMNS = [
    "id",
    "title",
    "description",
    "category",
    "type",
    "collection",
    "is_new",
    "subtype",
    "manufacturer",
    "catalog_id",
    "finishes",
    "position",
    "sizes",
    "thickness",
    "material",
    "shape",
    "cut",
    "diameter",
    "length",
    "width",
    "height",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

RE_WP_SIZE = re.compile(r"-\d+x\d+(?=\.[a-zA-Z]{2,5}(?:\?|$))")
RE_STUCK_DIM_LABEL = re.compile(r"(?i)\b(length|width|height|high|diameter|depth)(\d)")


def sanitize_filename(text: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", text).strip()


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


def normalize_space(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def norm_key(s: str) -> str:
    return normalize_space(s).casefold()


def normalize_product_url(url: str) -> str:
    """Fix known CSV typos and strip fragments for fetching."""
    u = clean_cell(url)
    u = u.replace("theBAT_hcollection.com", "thebathcollection.com")
    u = u.replace("thebat_hcollection.com", "thebathcollection.com")
    if not u:
        return u
    u = u.split("#")[0].strip()
    if not u.startswith("http"):
        return u
    p = urlparse(u)
    host = (p.netloc or "").lower()
    if host.endswith("thebathcollection.com"):
        path = p.path or "/"
        if path != "/" and not path.endswith("/"):
            path = path + "/"
        return f"{p.scheme}://{host}{path}"
    return u


def wp_full_size(url: str) -> str:
    """Drop WordPress thumbnail suffix (-300x225) before extension."""
    base = url.split("?", 1)[0]
    return RE_WP_SIZE.sub("", base)


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


def hero_title(soup: BeautifulSoup) -> str:
    h2 = soup.select_one("header.head h2")
    if h2:
        return h2.get_text(" ", strip=True)
    t = soup.title.string if soup.title else ""
    if t:
        return normalize_space(t.replace(" - Bathco", "").replace("- Bathco", ""))
    return ""


def collapse_section_for_hash(soup: BeautifulSoup, data_hash: str) -> Tag | None:
    """Return the collapsible panel ``#id`` targeted by ``a.collapse-link[data-hash=...]``."""
    trigger = soup.select_one(f'a.collapse-link[data-hash="{data_hash}"]')
    if not trigger:
        return None
    href = (trigger.get("href") or "").strip()
    if href.startswith("#"):
        return soup.select_one(href)
    return None


def description_from_descripcion_panel(soup: BeautifulSoup) -> str:
    """Copy from the Description accordion only (not Downloads / reCAPTCHA / login boilerplate)."""
    root = collapse_section_for_hash(soup, "descripcion")
    if not root:
        return ""
    th = root.select_one(".text-holder")
    if not th:
        return ""
    paras: list[str] = []
    for p in th.select("p"):
        text = p.get_text(" ", strip=True)
        if text and text not in paras:
            paras.append(text)
    if paras:
        return "\n\n".join(paras)
    return normalize_space(th.get_text(" ", strip=True))


def parse_dimensions_panel(soup: BeautifulSoup) -> tuple[str, list[str]]:
    """Dimensions accordion: human-readable sizes text + technical diagram image URLs."""
    root = collapse_section_for_hash(soup, "dimensiones")
    if not root:
        return "", []
    th = root.select_one(".text-holder")
    if not th:
        return "", []
    tech_urls: list[str] = []
    seen_img: set[str] = set()
    for img in th.select("img[src]"):
        src = (img.get("src") or "").strip()
        if not src or "wp-content/uploads" not in src:
            continue
        u = wp_full_size(urljoin(BASE_ORIGIN, src))
        if u not in seen_img:
            seen_img.add(u)
            tech_urls.append(u)
    bits: list[str] = []
    for ul in th.select("ul"):
        for li in ul.select("li"):
            t = li.get_text(" ", strip=True)
            if t:
                bits.append(t)
    for p in th.select("p"):
        t = p.get_text(" ", strip=True)
        if t:
            bits.append(t)
    sizes = " | ".join(bits) if bits else ""
    sizes = RE_STUCK_DIM_LABEL.sub(
        lambda m: f"{m.group(1).title()}: {m.group(2)}", normalize_space(sizes)
    )
    return normalize_space(sizes), tech_urls


def compose_product_title(soup: BeautifulSoup, nume_produs: str) -> str:
    """Prefer site ``h2`` except when the CSV name is strictly more specific (e.g. ``Luena`` vs ``Luena 1000``)."""
    hero = hero_title(soup)
    np = clean_cell(nume_produs)
    if not np:
        return hero
    if not hero:
        return np
    nh, nn = norm_key(hero), norm_key(np)
    if nh == nn:
        return np
    if nh in nn:
        return np
    if nn in nh:
        return hero
    return hero


def parse_dl_specs(soup: BeautifulSoup) -> dict[str, str]:
    out: dict[str, str] = {}
    root = soup.select_one("main") or soup
    for dl in root.select("dl"):
        for dt in dl.find_all("dt"):
            dd = dt.find_next_sibling("dd")
            if dd:
                key = dt.get_text(" ", strip=True).rstrip(":")
                out[key] = dd.get_text(" ", strip=True)
    return out


def _ancestor_class_contains(tag: Tag, needle: str) -> bool:
    for p in tag.parents:
        if not isinstance(p, Tag):
            continue
        cls = " ".join(p.get("class") or [])
        if needle in cls:
            return True
    return False


def extract_gallery_urls(soup: BeautifulSoup) -> list[str]:
    """Hero and in-page gallery (``figure`` / ``.img``); skip swatches and similar-products."""
    seen: set[str] = set()
    out: list[str] = []

    def push_abs(src: str) -> None:
        if not src or "wp-content/uploads" not in src:
            return
        low = src.lower()
        if "wishlist" in low or "logo.svg" in low:
            return
        u = wp_full_size(urljoin(BASE_ORIGIN, src.strip()))
        if u not in seen:
            seen.add(u)
            out.append(u)

    og = soup.select_one('meta[property="og:image"]')
    if og and og.get("content"):
        push_abs(og["content"].strip())

    main = soup.select_one("main") or soup
    for img in main.select("figure img[src], .img-holder img[src], div.img img[src], header.head img[src]"):
        if _ancestor_class_contains(img, "color-item"):
            continue
        if _ancestor_class_contains(img, "similar-products"):
            continue
        if _ancestor_class_contains(img, "version-list"):
            continue
        push_abs((img.get("src") or "").strip())

    if not out:
        for img in soup.select("img[src]"):
            if _ancestor_class_contains(img, "color-item"):
                continue
            src = (img.get("src") or "").strip()
            push_abs(src)

    return out


def _pdf_title_from_url(url: str) -> str:
    path = urlparse(url).path
    base = path.rsplit("/", 1)[-1]
    if base.lower().endswith(".pdf"):
        base = base[:-4]
    t = base.replace("_", " ").replace("-", " ").strip()
    return t or url


def extract_technical_documents(soup: BeautifulSoup) -> list[dict[str, str]]:
    """PDFs and ``?pdf=`` technical-spec links from the Downloads accordion only."""
    root = collapse_section_for_hash(soup, "descargas")
    if root is None:
        root = soup.select_one("main") or soup
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for a in root.select("a[href]"):
        href = (a.get("href") or "").strip()
        if not href or href.startswith("#"):
            continue
        low = href.lower()
        query = low.split("?", 1)[-1] if "?" in low else ""
        is_pdf_ext = low.endswith(".pdf")
        is_pdf_query = "pdf=" in query
        if not (is_pdf_ext or is_pdf_query):
            continue
        abs_url = urljoin(BASE_ORIGIN, href)
        if abs_url in seen:
            continue
        seen.add(abs_url)
        label = a.get_text(" ", strip=True)
        low_label = label.casefold()
        if "inicia sesión" in low_label or "regístrate" in low_label or label.lower() in ("login", "register"):
            continue
        title = label if label else _pdf_title_from_url(abs_url)
        out.append({"url": abs_url, "title": title})
    return out


def infer_material(description: str, category: str, type_: str, subtype: str) -> str:
    blob = f"{description} {category} {type_} {subtype}".lower()
    if "porcelain" in blob or "porcelain" in description.lower():
        return "Porcelain"
    if "stone" in blob or "natural stone" in description.lower():
        return "Natural stone"
    if "wood" in blob:
        return "Wood"
    if "metal" in blob or "steel" in blob or "chrome" in blob:
        return "Metal"
    if "sanit" in blob or "faucet" in blob or "basin" in blob or "toilet" in blob:
        return "Sanitary ware"
    return ""


def infer_position(category: str, type_: str, subtype: str) -> str:
    blob = f"{category} {type_} {subtype}".lower()
    if "wall" in blob or "perete" in blob or "suspend" in blob:
        return "Wall"
    if "freestanding" in blob or "floor" in blob or "picior" in blob:
        return "Floor"
    if "blat" in blob or "counter" in blob or "lavoar" in blob:
        return "Countertop"
    return ""


def extract_product_row(
    soup: BeautifulSoup,
    categorie: str,
    subcategorie: str,
    sub_sub: str,
    colectie: str,
    nume_produs: str,
    *,
    csv_title: str = "",
    sizes_csv: str = "",
    description_csv: str = "",
) -> dict[str, Any]:
    t_csv = clean_cell(csv_title)
    title = t_csv or compose_product_title(soup, nume_produs)
    description = description_from_descripcion_panel(soup)
    notes = clean_cell(description_csv)
    if notes:
        description = f"{notes}\n\n{description}".strip() if description else notes
    specs = parse_dl_specs(soup)
    dim_sizes, _ = parse_dimensions_panel(soup)
    s_csv = clean_cell(sizes_csv)
    sizes = s_csv or dim_sizes or (specs.get("Dimensions", "") or specs.get("Size", "") or specs.get("Sizes", ""))
    thickness = specs.get("Thickness", "") or ""

    dim_blob = " ".join(filter(None, [sizes, description]))
    dim_info = parse_dimensions_from_text(dim_blob) if dim_blob else {}

    return {
        "title": title,
        "description": description,
        "category": normalize_category(categorie),
        "type": subcategorie,
        "collection": colectie,
        "is_new": False,
        "subtype": sub_sub,
        "manufacturer": MANUFACTURER,
        "catalog_id": None,
        "finishes": "",
        "position": infer_position(categorie, subcategorie, sub_sub),
        "sizes": sizes,
        "thickness": thickness,
        "material": infer_material(description, categorie, subcategorie, sub_sub),
        "shape": "",
        "cut": "",
        "diameter": dim_info.get("diameter", ""),
        "length": dim_info.get("length", ""),
        "width": dim_info.get("width", ""),
        "height": dim_info.get("height", ""),
    }


def variant_finish_title_case(raw: str) -> str:
    return normalize_space(clean_cell(raw).title())


def load_links_csv(path: Path) -> pd.DataFrame:
    if not path.is_file():
        raise FileNotFoundError(f"Missing {path}")
    df = pd.read_csv(path, encoding="utf-8-sig")
    for col in (
        "Categorie",
        "Subcategorie",
        "SUB-SUBCATEGORIE",
        "Colectie",
        "Nume produs",
        "Variante culori",
    ):
        if col in df.columns:
            df[col] = df[col].ffill()
    return df


def product_key_from_row(row: pd.Series) -> tuple[str, str, str, str, str]:
    return (
        norm_key(clean_cell(row.get("Categorie"))),
        norm_key(clean_cell(row.get("Subcategorie"))),
        norm_key(clean_cell(row.get("SUB-SUBCATEGORIE"))),
        norm_key(clean_cell(row.get("Colectie"))),
        norm_key(clean_cell(row.get("Nume produs"))),
    )


def variant_color_label(row: pd.Series) -> str:
    """``Variante culori`` only, title-cased (variant ``color`` column)."""
    return variant_finish_title_case(clean_cell(row.get("Variante culori")))


def scrape(*, limit_rows: int | None = None) -> None:
    script_dir = Path(__file__).resolve().parent
    csv_path = script_dir / LINKS_CSV
    df = load_links_csv(csv_path)

    required = ("Link variante", "COD REFERINTA", "Nume produs")
    for c in required:
        if c not in df.columns:
            raise SystemExit(f"{LINKS_CSV} must include column: {c}")

    session = requests.Session()
    session.headers.update(HEADERS)

    data_rows: list[pd.Series] = []
    for _, row in df.iterrows():
        link = normalize_product_url(clean_cell(row.get("Link variante")))
        cod = clean_cell(row.get("COD REFERINTA"))
        np_ = clean_cell(row.get("Nume produs"))
        if not link or not cod or not np_:
            continue
        data_rows.append(row)

    if limit_rows is not None:
        data_rows = data_rows[: max(0, limit_rows)]

    key_to_rows: dict[tuple[str, str, str, str, str], list[pd.Series]] = {}
    key_order: list[tuple[str, str, str, str, str]] = []
    for row in data_rows:
        k = product_key_from_row(row)
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
    created_stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    page_cache: dict[str, BeautifulSoup] = {}

    def get_soup(raw_url: str) -> BeautifulSoup | None:
        """Cache keyed by requested URL only.

        Many Bathco ``/en/products/...`` pages share one ``rel=canonical`` URL; aliasing
        by canonical would merge different products (e.g. high vs low basin faucet).
        """
        req_u = normalize_product_url(raw_url)
        if req_u in page_cache:
            return page_cache[req_u]
        soup = fetch_soup(req_u, session)
        if not soup:
            return None
        page_cache[req_u] = soup
        time.sleep(0.08)
        return soup

    product_key_to_id: dict[tuple[str, str, str, str, str], int] = {}

    for k in key_order:
        group = key_to_rows[k]
        first = group[0]
        soup = get_soup(clean_cell(first.get("Link variante")))
        if not soup:
            print(f"\n=== PRODUCT SKIP (fetch failed) key={k!r} ===")
            continue

        categorie = clean_cell(first.get("Categorie"))
        subcategorie = clean_cell(first.get("Subcategorie"))
        sub_sub = clean_cell(first.get("SUB-SUBCATEGORIE"))
        colectie = clean_cell(first.get("Colectie"))

        np_row = clean_cell(first.get("Nume produs"))
        orig = clean_cell(first.get("Nume produs original"))
        csv_title = orig if orig else np_row
        sizes_agg = aggregate_unique_column(group, "Dimensiuni")
        sheet_notes = ""
        if "OBSERVATII" in first.index:
            sheet_notes = aggregate_unique_column(group, "OBSERVATII", sep="\n---\n")
        product_row = extract_product_row(
            soup,
            categorie,
            subcategorie,
            sub_sub,
            colectie,
            np_row,
            csv_title=csv_title,
            sizes_csv=sizes_agg,
            description_csv=sheet_notes,
        )
        product_row["id"] = p_id
        products_db.append(product_row)
        product_key_to_id[k] = p_id

        technical_docs = extract_technical_documents(soup)
        if technical_docs:
            print(f"\n  product id={p_id} | {product_row.get('title')!r} | PDFs: {len(technical_docs)}")
        else:
            print(f"\n  product id={p_id} | {product_row.get('title')!r}")

        for sort_i, doc in enumerate(technical_docs):
            u = doc["url"]
            if u not in pdf_url_to_id:
                pdf_url_to_id[u] = pdf_id_counter
                technical_pdfs_db.append(
                    {
                        "id": pdf_id_counter,
                        "title": enrich_technical_pdf_title(
                            doc["title"],
                            product_title=clean_cell(product_row.get("title", "")),
                            collection=clean_cell(product_row.get("collection", "")),
                        ),
                        "r2_key": "",
                        "url": u,
                        "created_at": created_stamp,
                    }
                )
                pdf_id_counter += 1
            pid_pdf = pdf_url_to_id[u]
            product_pdfs_db.append(
                {
                    "id": pp_id_counter,
                    "product_id": p_id,
                    "pdf_id": pid_pdf,
                    "sort_order": sort_i,
                    "created_at": created_stamp,
                }
            )
            pp_id_counter += 1

        p_id += 1
        time.sleep(0.05)

    for row in data_rows:
        k = product_key_from_row(row)
        if k not in product_key_to_id:
            print(f"  WARN variant skipped — missing product for key {k!r} SKU {clean_cell(row.get('COD REFERINTA'))}")
            continue
        current_pid = product_key_to_id[k]
        raw_link = clean_cell(row.get("Link variante"))
        req_u = normalize_product_url(raw_link)
        soup = get_soup(raw_link)
        gallery_urls: list[str] = []
        tech_photo_urls: list[str] = []
        if soup:
            _, dim_imgs = parse_dimensions_panel(soup)
            dim_set = set(dim_imgs)
            tech_photo_urls = list(dim_imgs)
            gallery_urls = [u for u in extract_gallery_urls(soup) if u not in dim_set]
        else:
            print(f"  WARN no page for variant {clean_cell(row.get('COD REFERINTA'))} -> {req_u}")

        sku = sanitize_filename(clean_cell(row.get("COD REFERINTA")).replace("  ", " "))
        color_disp = default_color(variant_color_label(row))

        variants_db.append(
            {
                "id": v_id,
                "product_id": current_pid,
                "sku": sku,
                "color": color_disp,
                "url": req_u,
                "gallery_photos": json.dumps(gallery_urls, ensure_ascii=False),
                "technical_photos": json.dumps(tech_photo_urls, ensure_ascii=False),
            }
        )
        print(f"  variant {sku} | {color_disp} | {len(gallery_urls)} imgs | {len(tech_photo_urls)} tech imgs")
        v_id += 1
        time.sleep(0.05)

    df_p = pd.DataFrame(products_db)
    ordered = [c for c in PRODUCT_CSV_COLUMNS if c in df_p.columns]
    extra = [c for c in df_p.columns if c not in ordered]
    df_p = df_p[ordered + extra]
    df_p.to_csv(script_dir / "products.csv", index=False, encoding="utf-8-sig")
    variant_cols = ["id", "product_id", "sku", "color", "url", "gallery_photos", "technical_photos"]
    pd.DataFrame(variants_db, columns=variant_cols).to_csv(
        script_dir / "variants.csv", index=False, encoding="utf-8-sig"
    )

    tp_cols = ["id", "title", "r2_key", "url", "created_at"]
    pd.DataFrame(technical_pdfs_db, columns=tp_cols).to_csv(
        script_dir / "technical_pdfs.csv", index=False, encoding="utf-8-sig"
    )
    pp_cols = ["id", "product_id", "pdf_id", "sort_order", "created_at"]
    pd.DataFrame(product_pdfs_db, columns=pp_cols).to_csv(
        script_dir / "product_pdfs.csv", index=False, encoding="utf-8-sig"
    )

    total_photos = sum(len(json.loads(v.get("gallery_photos", "[]"))) for v in variants_db)
    print(
        f"\nDone. {len(products_db)} products, {len(variants_db)} variants, "
        f"{len(technical_pdfs_db)} technical PDFs, {len(product_pdfs_db)} product_pdf links, "
        f"{total_photos} gallery URLs -> {script_dir}"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Scrape Bathco product pages from links.csv")
    ap.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Process only the first N data rows (variants) after filtering",
    )
    args = ap.parse_args()
    scrape(limit_rows=args.limit)


if __name__ == "__main__":
    main()
