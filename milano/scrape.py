"""
Milano Parquet — scrape product pages from ``links.csv``.

Milano blocks requests that advertise ``AppleWebKit`` in User-Agent; use a minimal UA.
URLs must use ``https://www.milano-parquet.com`` (non-www often returns 403).

SKU: ``COD REFERINTA`` when set; otherwise ``MI_<variant_id>``.
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
    join_unique_csv,
    norm_key,
    normalize_category,
    normalize_space,
    parse_dimensions_from_text,
    split_dimension_token,
    total_gallery_count,
    variant_sku,
    write_brand_outputs,
)

MANUFACTURER = "Milano Parquet"
SKU_PREFIX = "MI"
LINKS_CSV = "links.csv"

START_PRODUCT_ID = 600
START_VARIANT_ID = 3000
START_TECH_PDF_ID = 1
START_PRODUCT_PDF_ID = 1

# Full Chrome UA triggers 403 on this host (bot filter).
MILANO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def normalize_milano_url(url: str) -> str:
    u = clean_cell(url)
    u = u.replace("http://", "https://")
    u = u.replace("https://milano-parquet.com", "https://www.milano-parquet.com")
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


def page_title(soup: BeautifulSoup) -> str:
    h1 = soup.select_one("h1")
    if h1:
        return h1.get_text(" ", strip=True)
    if soup.title and soup.title.string:
        return normalize_space(soup.title.string.split("-")[0].strip())
    return ""


def page_description(soup: BeautifulSoup) -> str:
    """Milano product pages have no usable long-form marketing description in the markup we scrape."""
    return ""


RE_MILANO_DIM_3 = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*[x×]\s*(\d+(?:[.,]\d+)?)\s*[x×]\s*(\d+(?:[.,]\d+)?)\s*(mm|cm)?",
    re.I,
)
RE_MILANO_DIM_2 = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*[x×]\s*(\d+(?:[.,]\d+)?)\s*(mm|cm)?",
    re.I,
)


def _milano_parse_f(s: str) -> float:
    return float(s.replace(",", "."))


def _milano_fmt_cm_from_mm(v_mm: float) -> str:
    cm = v_mm / 10.0
    if abs(cm - round(cm)) < 1e-9:
        return str(int(round(cm)))
    return f"{cm:.2f}".rstrip("0").rstrip(".")


def _milano_fmt_cm_val(v_cm: float) -> str:
    if abs(v_cm - round(v_cm)) < 1e-9:
        return str(int(round(v_cm)))
    return f"{v_cm:.2f}".rstrip("0").rstrip(".")


def _normalize_one_milano_size_segment(seg: str) -> str | None:
    seg = normalize_space(seg)
    if not seg or not re.search(r"\d", seg):
        return None
    m = RE_MILANO_DIM_3.search(seg)
    if m:
        a, b, c = m.group(1), m.group(2), m.group(3)
        u = (m.group(4) or "mm").lower()
        na, nb, nc = _milano_parse_f(a), _milano_parse_f(b), _milano_parse_f(c)
        if u == "cm":
            w, h, t = na, nb, nc
            return f"{_milano_fmt_cm_val(w)}x{_milano_fmt_cm_val(h)}x{_milano_fmt_cm_val(t)} cm"
        t_m, w_m, h_m = na, nb, nc
        return f"{_milano_fmt_cm_from_mm(w_m)}x{_milano_fmt_cm_from_mm(h_m)}x{_milano_fmt_cm_from_mm(t_m)} cm"
    m2 = RE_MILANO_DIM_2.search(seg)
    if m2:
        a, b = m2.group(1), m2.group(2)
        u = (m2.group(3) or "mm").lower()
        na, nb = _milano_parse_f(a), _milano_parse_f(b)
        if u == "mm":
            return f"{_milano_fmt_cm_from_mm(na)}x{_milano_fmt_cm_from_mm(nb)} cm"
        return f"{_milano_fmt_cm_val(na)}x{_milano_fmt_cm_val(nb)} cm"
    return None


def normalize_milano_sizes_string(raw: str) -> str:
    """``TxWxH mm`` (site) → ``WxHxT cm``; multiple options joined with ``, ``."""
    if not clean_cell(raw):
        return ""
    t = raw.replace("·", ",").replace("•", ",").replace("|", ",").replace(";", ",")
    segments = [normalize_space(x) for x in t.split(",") if normalize_space(x)]
    out: list[str] = []
    seen: set[str] = set()
    for seg in segments:
        conv = _normalize_one_milano_size_segment(seg)
        if conv and conv not in seen:
            seen.add(conv)
            out.append(conv)
    return ", ".join(out)


def clean_milano_finishes_display(s: str) -> str:
    if not clean_cell(s):
        return ""
    t = normalize_space(s.replace("·", ",").replace("•", ",").replace(";", ","))
    parts = [normalize_space(p) for p in t.split(",") if normalize_space(p)]
    return ", ".join(dict.fromkeys(parts))


def formato_technical_image_urls(urls: list[str]) -> list[str]:
    """Dimension / format diagrams (``formato`` in URL) belong in ``technical_photos``."""
    out: list[str] = []
    for u in urls:
        path = urlparse(u).path.casefold()
        if "formato" in path or "formato" in u.casefold():
            out.append(u)
    return dedupe_urls(out)


_NON_PRODUCT_IMAGE_PATTERNS = re.compile(
    r"/(?:logo|icon|wishlist|avatar|favicon|"
    r"frame[-_]?\d+|"
    r"guida[-_]|gids?[-_]|news[-_]|articolo[-_]|"
    r"superficie[-_])",
    re.I,
)


def gallery_urls(soup: BeautifulSoup, page_url: str) -> list[str]:
    """Real product/lifestyle photos only. Surface swatches, format diagrams, icon-frames,
    and blog/guide headers are excluded so they do not pollute the gallery."""
    origin = f"{urlparse(page_url).scheme}://{urlparse(page_url).netloc}"
    out: list[str] = []
    og = soup.select_one('meta[property="og:image"]')
    if og and og.get("content"):
        cand = urljoin(origin, og["content"].strip())
        if not _NON_PRODUCT_IMAGE_PATTERNS.search(cand):
            out.append(cand)
    for img in soup.select('img[src*="wp-content/uploads"]'):
        src = (img.get("src") or "").strip()
        if not src:
            continue
        cand = urljoin(origin, src)
        if _NON_PRODUCT_IMAGE_PATTERNS.search(cand):
            continue
        parent_class = " ".join(c for el in (img.parent, img.parent.parent if img.parent else None) if el is not None for c in (el.get("class") or []))
        if "elementor-image-box-img" in parent_class or "tbl-cont-ct" in parent_class:
            continue
        out.append(cand)
    return dedupe_urls(out)


def technical_image_urls(soup: BeautifulSoup, page_url: str) -> list[str]:
    """Surface-texture swatches and ``Formato`` diagrams on the Milano page."""
    origin = f"{urlparse(page_url).scheme}://{urlparse(page_url).netloc}"
    out: list[str] = []
    for img in soup.select('img[src*="wp-content/uploads"]'):
        src = (img.get("src") or "").strip()
        if not src:
            continue
        cand = urljoin(origin, src)
        low = cand.casefold()
        if "formato" in low or "/superficie" in low or "superficie-" in low:
            out.append(cand)
    return dedupe_urls(out)


def technical_documents(soup: BeautifulSoup, origin: str) -> list[dict[str, str]]:
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    root = soup.select_one("main") or soup
    for a in root.select('a[href$=".pdf"], a[href*=".pdf"]'):
        href = (a.get("href") or "").strip()
        if not href.lower().endswith(".pdf"):
            continue
        u = urljoin(origin, href)
        if u in seen:
            continue
        seen.add(u)
        label = a.get_text(" ", strip=True)
        out.append({"url": u, "title": label or u.rsplit("/", 1)[-1]})
    return out


def finisaj_column_key(row: pd.Series) -> str | None:
    for name in row.index:
        if norm_key(str(name)) == norm_key("Finisaj"):
            return str(name)
    return None


def column_key_matching(row: pd.Series, *candidates: str) -> str | None:
    """Resolve actual CSV header when spelling/diacritics match ``candidates``."""
    targets = {norm_key(c) for c in candidates}
    for name in row.index:
        if norm_key(str(name)) in targets:
            return str(name)
    return None


def variant_color(row: pd.Series, finisaj_effective: str) -> str:
    """Prefer ``Variante`` from the sheet; if empty use ``Finisaj`` (not combined)."""
    var_key = None
    for name in row.index:
        if norm_key(str(name)) == norm_key("Variante"):
            var_key = str(name)
            break
    v = clean_cell(row.get(var_key)) if var_key else ""
    if v:
        return normalize_space(v.title())
    f = normalize_space((finisaj_effective or "").title())
    return f if f else "Standard"


def listone_standard_technical_image_urls(soup: BeautifulSoup, page_url: str) -> list[str]:
    """Diagram / format images under the ``Listone Standard`` heading (Elementor)."""
    origin = f"{urlparse(page_url).scheme}://{urlparse(page_url).netloc}"
    out: list[str] = []
    seen: set[str] = set()
    for h in soup.select("h4, h5, .elementor-heading-title"):
        if "listone standard" not in h.get_text(" ", strip=True).casefold():
            continue
        box = h.find_parent("div", class_=re.compile(r"e-con"))
        if box is None:
            box = h.parent
        for img in box.select(".elementor-widget-text-editor img[src]"):
            src = (img.get("src") or "").strip()
            if not src or "wp-content" not in src.casefold():
                continue
            u = urljoin(origin, src)
            low = u.casefold()
            if any(x in low for x in ("logo", "icon", "placeholder", "avatar")):
                continue
            if u not in seen:
                seen.add(u)
                out.append(u)
        break
    return out


def listone_standard_dimension_line(soup: BeautifulSoup) -> str:
    """Reads the ``Listone Standard`` column (thickness / length text widgets)."""
    for h in soup.select("h4, h5, .elementor-heading-title"):
        if "listone standard" not in h.get_text(" ", strip=True).casefold():
            continue
        box = h.find_parent("div", class_=re.compile(r"e-con"))
        if box is None:
            box = h.parent
        bits: list[str] = []
        for ed in box.select(".elementor-widget-text-editor .elementor-widget-container"):
            tx = normalize_space(ed.get_text(" ", strip=True))
            if tx and re.search(r"\d", tx):
                bits.append(tx)
        if bits:
            return ", ".join(bits)
    return ""


def supplementary_sizes_from_html(soup: BeautifulSoup) -> str:
    """When ``Dimensiuni`` is empty in the sheet, pull dimension-like strings from the page."""
    bits: list[str] = []
    for tr in soup.select("table tr"):
        row = tr.get_text(" ", strip=True)
        if re.search(r"\d+\s*[x×]\s*\d+", row, re.I) and len(row) < 160:
            bits.append(normalize_space(row))
    if bits:
        return ", ".join(sorted(set(bits), key=str.casefold))
    blob = (soup.select_one("main") or soup).get_text(" ", strip=True)
    found = re.findall(
        r"\d{2,4}\s*[x×]\s*\d{2,4}(?:\s*[x×]\s*\d{2,4})?\s*(?:mm|cm)?",
        blob,
        flags=re.I,
    )
    if found:
        return ", ".join(sorted(set(found), key=str.casefold))
    return ""


def load_links(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, encoding="utf-8-sig")
    sub_col = None
    for name in df.columns:
        if norm_key(str(name)) == norm_key("Sub-subcategorie"):
            sub_col = name
            break
    for col in ("Categorie", "Subcategorie", "Colectie", "Nume produs"):
        if col in df.columns:
            df[col] = df[col].ffill()
    if sub_col:
        df[sub_col] = df[sub_col].ffill()
    fin_col = None
    for name in df.columns:
        if norm_key(str(name)) == norm_key("Finisaj"):
            fin_col = str(name)
            break
    if fin_col:
        df[fin_col] = df[fin_col].ffill()
    return df


def product_key(row: pd.Series) -> tuple[str, str, str, str, str]:
    sub_col = None
    for name in row.index:
        if norm_key(str(name)) == norm_key("Sub-subcategorie"):
            sub_col = name
            break
    sub_sub = clean_cell(row.get(sub_col)) if sub_col else ""
    return (
        norm_key(clean_cell(row.get("Categorie"))),
        norm_key(clean_cell(row.get("Subcategorie"))),
        norm_key(sub_sub),
        norm_key(clean_cell(row.get("Colectie"))),
        norm_key(clean_cell(row.get("Nume produs"))),
    )


def extract_product(
    soup: BeautifulSoup,
    categorie: str,
    subcategorie: str,
    sub_sub: str,
    colectie: str,
    np: str,
    finishes_labels: list[str],
    sizes_hint: str,
    *,
    csv_title: str,
    cut_csv: str,
    material_csv: str,
) -> dict[str, Any]:
    title = clean_cell(csv_title) or page_title(soup) or np
    description = page_description(soup)
    finishes = clean_milano_finishes_display(
        ", ".join(normalize_space(x).title() for x in finishes_labels if clean_cell(x))
    )
    sizes_raw = (
        clean_cell(sizes_hint)
        or listone_standard_dimension_line(soup)
        or supplementary_sizes_from_html(soup)
    )
    sizes_normalized = normalize_milano_sizes_string(sizes_raw)
    sizes_str = sizes_normalized
    if not sizes_str and sizes_raw:
        sizes_str = normalize_space(
            sizes_raw.replace("|", ",").replace(";", ",").replace("·", ",").replace("•", ",")
        )

    # If the cleaned size is a single ``WxLxT cm`` token, lift the thickness out into its own column.
    thickness = ""
    width = ""
    length_v = ""
    if sizes_str and "," not in sizes_str:
        parts = split_dimension_token(sizes_str)
        if parts:
            width = parts.get("width", "")
            length_v = parts.get("length", "")
            thickness = parts.get("height", "")
            sizes_str = ", ".join(p for p in (width, length_v) if p)

    cut_clean = _normalize_milano_cut(cut_csv)
    mat = clean_cell(material_csv) or "Wood"
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
        "finishes": finishes,
        "position": "",
        "sizes": sizes_str,
        "thickness": thickness,
        "material": mat,
        "shape": "",
        "cut": cut_clean,
        "diameter": "",
        "length": length_v,
        "width": width,
        "height": "",
    }


def _normalize_milano_cut(raw: str) -> str:
    """``"PLACI; Spina 90° ; Chevron 45°"`` -> ``"Placi, Spina 90°, Chevron 45°"``."""
    if not raw:
        return ""
    parts = re.split(r"[;,]", raw)
    cleaned: list[str] = []
    for p in parts:
        s = normalize_space(p)
        if not s:
            continue
        if s.upper() == "PLACI":
            s = "Placi"
        cleaned.append(s)
    return join_unique_csv(cleaned)


def scrape(*, limit_rows: int | None = None) -> None:
    script_dir = Path(__file__).resolve().parent
    df = load_links(script_dir / LINKS_CSV)

    session = requests.Session()
    session.headers.update(MILANO_HEADERS)

    data_rows: list[pd.Series] = []
    for _, row in df.iterrows():
        link = normalize_milano_url(clean_cell(row.get("Link variante")))
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

    def get_soup(raw: str) -> tuple[BeautifulSoup | None, str]:
        u = normalize_milano_url(raw)
        if u in cache:
            return cache[u], u
        soup = fetch_soup(u, session)
        if soup:
            cache[u] = soup
        time.sleep(0.06)
        return soup, u

    product_key_to_id: dict[tuple[str, str, str, str, str], int] = {}

    fin_key_global = finisaj_column_key(data_rows[0]) if data_rows else None

    for k in key_order:
        group = key_to_rows[k]
        finishes_labels = sorted(
            {
                clean_cell(r.get(fin_key_global))
                for r in group
                if fin_key_global and clean_cell(r.get(fin_key_global))
            },
            key=lambda s: s.casefold(),
        )
        first = group[0]
        soup, final_url = get_soup(clean_cell(first.get("Link variante")))
        if not soup:
            print(f"\n=== SKIP product (fetch failed) {k!r} ===")
            continue

        origin = f"{urlparse(final_url).scheme}://{urlparse(final_url).netloc}"
        categorie = clean_cell(first.get("Categorie"))
        subcategorie = clean_cell(first.get("Subcategorie"))
        sub_key = None
        for name in first.index:
            if norm_key(str(name)) == norm_key("Sub-subcategorie"):
                sub_key = name
                break
        sub_sub = clean_cell(first.get(sub_key)) if sub_key else ""
        colectie = clean_cell(first.get("Colectie"))
        np = clean_cell(first.get("Nume produs"))
        dim_agg = aggregate_unique_column(group, "Dimensiuni", sep=", ")
        opt_key = column_key_matching(
            first,
            "Opțiune de montaj / format",
            "Optiune de montaj / format",
        )
        filtru_key = column_key_matching(first, "Filtru")
        cut_agg = aggregate_unique_column(group, opt_key) if opt_key else ""
        material_agg = aggregate_unique_column(group, filtru_key) if filtru_key else ""

        row_dict = extract_product(
            soup,
            categorie,
            subcategorie,
            sub_sub,
            colectie,
            np,
            finishes_labels,
            dim_agg,
            csv_title=np,
            cut_csv=cut_agg,
            material_csv=material_agg,
        )
        row_dict["id"] = p_id
        products_db.append(row_dict)
        product_key_to_id[k] = p_id

        docs = technical_documents(soup, origin)
        for sort_i, doc in enumerate(docs):
            u = doc["url"]
            if u not in pdf_url_to_id:
                pdf_url_to_id[u] = pdf_id_counter
                technical_pdfs_db.append(
                    {
                        "id": pdf_id_counter,
                        "title": enrich_technical_pdf_title(
                            doc["title"],
                            product_title=clean_cell(row_dict.get("title", "")),
                            collection=clean_cell(row_dict.get("collection", "")),
                        ),
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
        soup, final_url = get_soup(clean_cell(row.get("Link variante")))
        gurls: list[str] = []
        tech_urls: list[str] = []
        if soup:
            gurls = gallery_urls(soup, final_url)
            tech_urls = dedupe_urls(
                listone_standard_technical_image_urls(soup, final_url)
                + technical_image_urls(soup, final_url)
            )
            tech_set = set(tech_urls)
            gurls = [u for u in gurls if u not in tech_set]

        sku = variant_sku(SKU_PREFIX, v_id, clean_cell(row.get("COD REFERINTA")))
        fin_row = (
            clean_cell(row.get(fin_key_global)) if fin_key_global else ""
        )
        col = default_color(variant_color(row, fin_row))

        variants_db.append(
            {
                "id": v_id,
                "product_id": pid,
                "sku": sku,
                "color": col,
                "url": normalize_milano_url(clean_cell(row.get("Link variante"))),
                "gallery_photos": json.dumps(gurls, ensure_ascii=False),
                "technical_photos": json.dumps(tech_urls, ensure_ascii=False),
            }
        )
        print(f"  variant {sku} | {col} | {len(gurls)} imgs")
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
