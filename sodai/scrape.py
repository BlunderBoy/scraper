"""
Sodai Design — product variant pages from ``links.csv`` (sodaidesign.com).

Each spreadsheet row is one variant URL. Rows sharing the same ``Colectie`` + ``Nume produs``
(after forward-fill) become **one** ``products.csv`` row and multiple ``variants.csv`` rows.
Product copy/specs/PDFs come from the first variant URL in each group that loads successfully;
each variant URL is scraped for its own gallery.

- Hero texture: ``.panzoom-container .panzoom-content .background`` (CSS background)
- Description: ``.collection-description``
- Face sizes + thickness: ``.cad-textures.mb-5`` (metric only; imperial after ``/`` ignored)
- Tech line (material / cut / finishes): ``.info-tech`` (noise like ``R10 A+B`` stripped)
- Downloads: ``.download-links a.text-center`` — technical PDFs + CAD ``.zip``; marketing
  ``PDF catalogue`` / season catalogue URLs omitted
- Extra gallery: ``.collection-slider .carousel-cell a.img`` (``href`` = full image)

SKUs: ``SOD-PPVV`` (product index PP from ``links.csv`` groups, variant index VV within product).

``sizes`` is ``WxHxT cm`` per face (comma-separated when several). ``width``, ``height``,
``thickness`` list metric values per face where parsed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from itertools import groupby
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
    format_csv_title,
    normalize_category,
    normalize_space,
    total_gallery_count,
    write_brand_outputs,
)

MANUFACTURER = "Sodai Design"
LINKS_CSV = "links.csv"

START_PRODUCT_ID = 1
START_VARIANT_ID = 17000
START_TECH_PDF_ID = 4000
START_PRODUCT_PDF_ID = 1300

GALLERY_LIMIT = 8

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

_RE_DIM_IN_LINE = re.compile(r"(\d+(?:[.,]\d+)?)\s*[x×]\s*(\d+(?:[.,]\d+)?)", re.I)
_RE_THICK_LINE = re.compile(r"Thickness\s*(\d+(?:[.,]\d+)?)\s*(mm|cm)\b", re.I)
_RE_BG_URL = re.compile(r"background-image\s*:\s*url\(\s*([^)]+)\s*\)", re.I)
_RE_INFO_R_AB = re.compile(r"\bR\d+\s*A\+B\b", re.I)
_RE_RECTIFIED_TAIL = re.compile(r"[\.,]?\s*Rectified\s*$", re.I)


def normalize_page_url(url: str) -> str:
    u = clean_cell(url).split("#")[0].strip()
    return u


def upgrade_http_localhost(u: str) -> str:
    if u.startswith("http://www.sodaidesign.com"):
        return "https://www.sodaidesign.com" + u[len("http://www.sodaidesign.com") :]
    if u.startswith("http://sodaidesign.com"):
        return "https://sodaidesign.com" + u[len("http://sodaidesign.com") :]
    return u


def normalize_asset_url(page_url: str, raw: str) -> str:
    raw = (raw or "").strip().strip('\'"')
    if not raw:
        return ""
    u = urljoin(page_url, raw)
    p = urlparse(u)
    u = p._replace(fragment="").geturl()
    return upgrade_http_localhost(u)


def fetch_soup(url: str, session: requests.Session) -> BeautifulSoup | None:
    try:
        r = session.get(url, timeout=30)
        if r.status_code != 200:
            print(f"  HTTP {r.status_code} for {url}")
            return None
        return BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        print(f"  Request failed {url}: {e}")
        return None


def page_main(soup: BeautifulSoup) -> BeautifulSoup | Tag:
    return soup.select_one("main#primary") or soup.select_one("main") or soup.body or soup


def css_background_url(style: str) -> str:
    m = _RE_BG_URL.search(style or "")
    return (m.group(1) or "").strip().strip('\'"') if m else ""


def hero_image_url(main_el: BeautifulSoup | Tag, page_url: str) -> str:
    div = main_el.select_one(".panzoom-container .panzoom-content .background")
    if not div:
        return ""
    return normalize_asset_url(page_url, css_background_url(div.get("style") or ""))


def extract_description(main_el: BeautifulSoup | Tag) -> str:
    el = main_el.select_one(".collection-description")
    if not el:
        return ""
    return normalize_space(el.get_text("\n", strip=True))


def scrub_info_tech_raw(text: str) -> str:
    """Drop slip rating noise (e.g. ``R10 A+B``) from ``.info-tech``."""
    t = normalize_space(text)
    t = _RE_INFO_R_AB.sub("", t)
    return normalize_space(t)


def parse_info_tech(main_el: BeautifulSoup | Tag) -> tuple[str, str, str]:
    """``material / cut / finishes`` from ``.info-tech``."""
    el = main_el.select_one(".info-tech")
    if not el:
        return "", "", ""
    t = scrub_info_tech_raw(el.get_text(" ", strip=True))
    parts = [normalize_space(p) for p in t.split("/")]
    parts = [p for p in parts if p]
    if len(parts) >= 3:
        return parts[0], parts[1], " / ".join(parts[2:])
    if len(parts) == 2:
        left, right = parts[0], parts[1]
        # Majesty-style: ``… porcelain. Rectified / Glossy finish``
        if re.search(r"Rectified\s*$", left, re.I):
            material = _RE_RECTIFIED_TAIL.sub("", left).strip()
            return material, "Rectified", right
        return left, "", right
    if parts:
        return parts[0].rstrip(" ."), "", ""
    return "", "", ""


def _thick_mm_to_cm_str(val: float, unit: str) -> str:
    if unit.lower() == "mm":
        cm = val / 10.0
    else:
        cm = val
    s = f"{cm:g}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return f"{s} cm"


def parse_cad_textures(main_el: BeautifulSoup | Tag) -> dict[str, str]:
    """Metric faces from ``.cad-textures`` → sizes, width, height, thickness strings."""
    el = main_el.select_one(".cad-textures.mb-5") or main_el.select_one(".cad-textures")
    out = {"sizes": "", "width": "", "height": "", "thickness": ""}
    if not el:
        return out
    lines = [normalize_space(x) for x in el.get_text("\n").split("\n")]
    lines = [x for x in lines if x]

    faces: list[tuple[str, str, str]] = []  # w, h, t_cm_display

    i = 0
    while i < len(lines):
        metric_blob = lines[i].split("/")[0]
        dm = _RE_DIM_IN_LINE.search(metric_blob)
        if not dm:
            i += 1
            continue
        w = dm.group(1).replace(",", ".")
        h = dm.group(2).replace(",", ".")
        t_cm = ""
        j = i + 1
        while j < len(lines):
            next_metric = lines[j].split("/")[0]
            if j > i and _RE_DIM_IN_LINE.search(next_metric):
                break
            tm = _RE_THICK_LINE.search(lines[j])
            if tm:
                val = float(tm.group(1).replace(",", "."))
                unit = tm.group(2)
                t_cm = _thick_mm_to_cm_str(val, unit)
                j += 1
                break
            j += 1
        faces.append((w, h, t_cm))
        i = j if j > i else i + 1

    if not faces:
        return out

    size_parts: list[str] = []
    widths: list[str] = []
    heights: list[str] = []
    thicks: list[str] = []
    for w, h, t in faces:
        widths.append(f"{w} cm")
        heights.append(f"{h} cm")
        if t:
            thicks.append(t)
            size_parts.append(f"{w}x{h}x{t.replace(' cm', '').strip()} cm")
        else:
            size_parts.append(f"{w}x{h} cm")

    uniq_thick = []
    seen_t: set[str] = set()
    for t in thicks:
        if t not in seen_t:
            seen_t.add(t)
            uniq_thick.append(t)

    out["sizes"] = ", ".join(size_parts)
    out["width"] = ", ".join(widths)
    out["height"] = ", ".join(heights)
    out["thickness"] = ", ".join(uniq_thick) if uniq_thick else ""
    return out


def carousel_image_urls(main_el: BeautifulSoup | Tag, page_url: str) -> list[str]:
    urls: list[str] = []
    for a in main_el.select(".collection-slider .carousel-cell a.img[href]"):
        href = (a.get("href") or "").strip()
        if not href:
            continue
        u = normalize_asset_url(page_url, href)
        if u:
            urls.append(u)
    return urls


def _is_marketing_catalogue(label: str, url: str) -> bool:
    """Season / marketing catalogue PDFs — not technical specs."""
    lab = label.casefold()
    ul = url.casefold()
    if "catalogue" in lab or "catalog " in lab:
        return True
    if "limerence" in ul and "season" in ul:
        return True
    return False


def sodai_asset_csv_title(url: str, product_title: str) -> str:
    """Human-readable titles: ``Technical info - {product}`` / ``CAD textures - {product}``."""
    pt = clean_cell(product_title) or "Product"
    if url.casefold().endswith(".zip"):
        return f"CAD textures - {pt}"
    return f"Technical info - {pt}"


def technical_download_links(main_el: BeautifulSoup | Tag, page_url: str) -> list[dict[str, str]]:
    """Technical PDFs + CAD zips from ``.download-links``; skips catalogue PDFs."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for a in main_el.select(".download-links a.text-center[href]"):
        href = (a.get("href") or "").strip()
        hlow = href.casefold()
        if not (hlow.endswith(".pdf") or hlow.endswith(".zip")):
            continue
        u = normalize_asset_url(page_url, href)
        if not u or u in seen:
            continue
        label = normalize_space(a.get_text(" ", strip=True))
        if hlow.endswith(".pdf") and _is_marketing_catalogue(label, href):
            continue
        seen.add(u)
        fname = u.rsplit("/", 1)[-1]
        base_title = label or fname.rsplit(".", 1)[0]
        out.append({"url": u, "title": base_title})
    return out


def collect_variant_gallery(main_el: BeautifulSoup | Tag, page_url: str, *, limit: int) -> list[str]:
    hero = hero_image_url(main_el, page_url)
    carousel = carousel_image_urls(main_el, page_url)
    merged = dedupe_urls(([hero] if hero else []) + carousel)
    return merged[:limit]


def load_links(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, encoding="utf-8-sig")
    for col in ("Categorie", "Subcategorie", "Colectie", "Nume produs"):
        if col in df.columns:
            df[col] = df[col].ffill()
    return df


def csv_product_title(row: pd.Series) -> str:
    return format_csv_title(clean_cell(row.get("Nume produs")), clean_cell(row.get("Colectie")))


def product_group_key(row: pd.Series) -> tuple[str, str]:
    return (clean_cell(row.get("Colectie")), clean_cell(row.get("Nume produs")))


def sod_skus_for_rows(rows: list[pd.Series]) -> list[str]:
    """``SOD-PPVV``: PP = product group (``Colectie`` + ``Nume produs``), VV = variant within group."""
    skus: list[str] = []
    prod_idx = 0
    var_idx = 0
    prev_key: tuple[str, str] | None = None
    for row in rows:
        key = (clean_cell(row.get("Colectie")), clean_cell(row.get("Nume produs")))
        if key != prev_key:
            prod_idx += 1
            var_idx = 0
            prev_key = key
        var_idx += 1
        skus.append(f"SOD-{prod_idx:02d}{var_idx:02d}")
    return skus


def scrape(*, limit_rows: int | None = None, output_dir: Path | None = None) -> None:
    script_dir = Path(__file__).resolve().parent
    out_dir = output_dir if output_dir is not None else script_dir
    df = load_links(script_dir / LINKS_CSV)

    session = requests.Session()
    session.headers.update(HEADERS)

    rows_out: list[pd.Series] = []
    for _, row in df.iterrows():
        link = normalize_page_url(clean_cell(row.get("Link variante")))
        np = clean_cell(row.get("Nume produs")) or clean_cell(row.get("Colectie"))
        if not link.startswith("http") or not np:
            continue
        rows_out.append(row)

    if limit_rows is not None:
        rows_out = rows_out[: max(0, limit_rows)]

    row_skus = sod_skus_for_rows(rows_out)

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

    row_flat_index = 0

    for _key, group_iter in groupby(rows_out, key=product_group_key):
        group = list(group_iter)
        base_row = group[0]

        soup0: BeautifulSoup | None = None
        url0 = ""
        rep_row = base_row
        for cand in group:
            u = normalize_page_url(clean_cell(cand.get("Link variante")))
            s = fetch_soup(u, session)
            time.sleep(0.05)
            if s:
                soup0 = s
                url0 = u
                rep_row = cand
                break

        if not soup0:
            print(f"\n=== SKIP product group (no page loaded) {clean_cell(base_row.get('Colectie'))!r} / {clean_cell(base_row.get('Nume produs'))!r} ===")
            row_flat_index += len(group)
            continue

        main0 = page_main(soup0)
        categorie = clean_cell(rep_row.get("Categorie"))
        subcategorie = clean_cell(rep_row.get("Subcategorie"))
        colectie = clean_cell(rep_row.get("Colectie"))

        desc = extract_description(main0)
        material, cut, finishes_it = parse_info_tech(main0)
        cad = parse_cad_textures(main0)

        row_dict: dict[str, Any] = {
            "title": csv_product_title(base_row),
            "description": desc,
            "category": normalize_category(categorie),
            "type": subcategorie,
            "collection": colectie,
            "is_new": False,
            "subtype": "",
            "manufacturer": MANUFACTURER,
            "catalog_id": None,
            "finishes": finishes_it,
            "position": "",
            "sizes": cad["sizes"],
            "thickness": cad["thickness"],
            "material": material,
            "shape": "",
            "cut": cut,
            "diameter": "",
            "length": "",
            "width": cad["width"],
            "height": cad["height"],
            "id": p_id,
        }
        products_db.append(row_dict)

        docs = technical_download_links(main0, url0)
        pt = clean_cell(row_dict.get("title", ""))
        for sort_i, doc in enumerate(docs):
            u = doc["url"]
            if u not in pdf_url_to_id:
                pdf_url_to_id[u] = pdf_id_counter
                pdf_title = sodai_asset_csv_title(u, pt)
                technical_pdfs_db.append(
                    {
                        "id": pdf_id_counter,
                        "title": pdf_title,
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

        print(
            f"\n  product id={p_id} | {row_dict['title']!r} | "
            f"{len(group)} variants | PDFs={len(docs)} (specs from first loaded URL)"
        )

        for row in group:
            sku = row_skus[row_flat_index]
            row_flat_index += 1
            nume_variant = clean_cell(row.get("Nume variante"))
            url = normalize_page_url(clean_cell(row.get("Link variante")))

            if url == url0:
                soup_v = soup0
                main_v = main0
            else:
                soup_v = fetch_soup(url, session)
                time.sleep(0.05)
                main_v = page_main(soup_v) if soup_v else None

            if main_v:
                gurls = collect_variant_gallery(main_v, url, limit=GALLERY_LIMIT)
            else:
                print(f"    WARN variant fetch failed {url!r}")
                gurls = []

            col_name = default_color(nume_variant.strip().title() if nume_variant else "")

            variants_db.append(
                {
                    "id": v_id,
                    "product_id": p_id,
                    "sku": sku,
                    "color": col_name,
                    "url": url,
                    "gallery_photos": json.dumps(gurls, ensure_ascii=False),
                    "technical_photos": json.dumps([], ensure_ascii=False),
                }
            )
            print(f"    variant {sku} | {col_name} | imgs={len(gurls)}")
            v_id += 1

        p_id += 1

    try:
        write_brand_outputs(
            out_dir,
            products=products_db,
            variants=variants_db,
            technical_pdfs=technical_pdfs_db,
            product_pdfs=product_pdfs_db,
        )
    except PermissionError as err:
        print(
            "\nCannot save CSV files: permission denied.\n"
            "Close products.csv, variants.csv, technical_pdfs.csv, and product_pdfs.csv if open in Excel, "
            "then run again.\n"
            f"Target folder: {out_dir}\n"
            f"System message: {err}",
            file=sys.stderr,
        )
        raise SystemExit(1) from err

    print(
        f"\nDone. {len(products_db)} products, {len(variants_db)} variants, "
        f"{len(technical_pdfs_db)} PDFs, {total_gallery_count(variants_db)} gallery URLs -> {out_dir}"
    )


def main() -> None:
    script_dir = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        metavar="DIR",
        help="Write CSVs here (default: folder containing scrape.py).",
    )
    args = ap.parse_args()
    od = args.output_dir
    if od is not None:
        out_dir = od.resolve() if od.is_absolute() else (script_dir / od).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
    else:
        out_dir = None
    scrape(limit_rows=args.limit, output_dir=out_dir)


if __name__ == "__main__":
    main()
