"""
Fir Italia -- scrape product pages from ``links.csv``.

Each row is one product (Stainless Steel only, one variant). The CleoSteel 48
collection. Pages live at ``https://www.fir-italia.it/eng/products_<code>_<slug>``
and contain a single hero photo, a one-line description and a download area
with technical drawing, technical sheet, installation manual and catalogue.

The site rejects Python's TLS fingerprint, so HTML is fetched via
``curl.exe``. Description is intentionally kept short -- just the product
subtitle (``itemprop="description"``) -- with the website's "Complementary
necessary articles" appended on a new line when present (richer/more accurate
than the CSV column, which occasionally has the wrong article code). The CSV
``obs`` and ``Articole complementare necesare`` columns are used as fallbacks
when the page does not list any compatibility info.

SKU comes from the CSV's ``COD REFERINTA``.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import pandas as pd
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

MANUFACTURER = "Fir Italia"
SKU_PREFIX = "FIR"
LINKS_CSV = "links.csv"

START_PRODUCT_ID = 1200
START_VARIANT_ID = 7500
START_TECH_PDF_ID = 1
START_PRODUCT_PDF_ID = 1


def fetch_html(url: str, *, timeout: int = 30) -> str | None:
    """Fetch via curl.exe (Python's TLS fingerprint is blocked)."""
    try:
        result = subprocess.run(
            [
                "curl.exe",
                "-sL",
                "--max-time",
                str(timeout),
                "-A",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                url,
            ],
            capture_output=True,
            text=False,
            timeout=timeout + 5,
        )
        if result.returncode != 0 or not result.stdout:
            print(f"  curl exit={result.returncode} for {url}")
            return None
        return result.stdout.decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  curl failed {url}: {e}")
        return None


def fetch_soup(url: str) -> BeautifulSoup | None:
    html = fetch_html(url)
    if html is None:
        return None
    return BeautifulSoup(html, "html.parser")


def og_image(soup: BeautifulSoup) -> str:
    og = soup.select_one('meta[property="og:image"]')
    return (og.get("content", "").strip() if og else "")


def page_description(soup: BeautifulSoup) -> str:
    parts: list[str] = []
    desc = soup.select_one('[itemprop="description"]')
    if desc:
        text = normalize_space(desc.get_text(" ", strip=True))
        if text and len(text) > 4:
            parts.append(text)
    cover = soup.select_one('[class*="product-cover"]')
    if cover:
        for p in cover.select("p"):
            t = normalize_space(p.get_text(" ", strip=True))
            if t and len(t) > 4:
                parts.append(t)
    return "\n\n".join(dict.fromkeys(parts))


def complementary_articles_items(soup: BeautifulSoup) -> list[tuple[str, str]]:
    """Return ``[(code, description)]`` from the website's compatibility section.

    The H2 "Complementary necessary articles" sits above a card list with one
    ``itemListElement`` per required article. Each card holds an ``Art. XXXXXXX``
    code in the first ``span.code`` and the article's English subtitle in the
    following one. This is more reliable than the CSV ``Articole complementare
    necesare`` column, which occasionally references the wrong product code.
    """
    for h2 in soup.find_all("h2"):
        if "Complementary necessary articles" not in h2.get_text(" ", strip=True):
            continue
        items: list[tuple[str, str]] = []
        seen: set[str] = set()
        for card in h2.find_all_next(
            "div", attrs={"itemprop": "itemListElement"}, limit=40
        ):
            h3 = card.find("h3", attrs={"itemprop": "name"})
            if h3 is None:
                continue
            code_span = h3.find("span", class_="code")
            code = normalize_space(code_span.get_text(" ", strip=True)) if code_span else ""
            spans = card.find_all("span", class_="code")
            desc_text = ""
            for span in spans[1:]:
                desc_text = normalize_space(span.get_text(" ", strip=True))
                if desc_text:
                    break
            if not code:
                continue
            key = code.lower()
            if key in seen:
                continue
            seen.add(key)
            items.append((code, desc_text))
        return items
    return []


def complementary_articles_text(soup: BeautifulSoup) -> str:
    """Format the website's compatibility list as a Romanian-headed block."""
    items = complementary_articles_items(soup)
    if not items:
        return ""
    lines = ["Articole complementare necesare:"]
    for code, desc in items:
        if desc:
            lines.append(f"- {code} - {desc}")
        else:
            lines.append(f"- {code}")
    return "\n".join(lines)


_GLOBAL_PDF_BLACKLIST = re.compile(r"lookbook|brochure_generale|listino", re.IGNORECASE)


def collect_pdfs(soup: BeautifulSoup, base_url: str) -> list[dict[str, str]]:
    """Find PDF links in the Download Area, dedupe direct vs ``download_file.php`` wrappers."""
    out: list[dict[str, str]] = []
    seen_keys: set[str] = set()
    candidates: list[tuple[str, str]] = []

    for a in soup.select("a[href]"):
        href = (a.get("href") or "").strip()
        if not href:
            continue
        low = href.lower()
        is_pdf = False
        if low.endswith(".pdf"):
            is_pdf = True
        elif "/download_file.php" in low:
            m_f = re.search(r"f=([^&]+)", href)
            if m_f and m_f.group(1).lower().endswith(".pdf"):
                is_pdf = True
        if not is_pdf:
            continue
        if _GLOBAL_PDF_BLACKLIST.search(href):
            continue
        u = urljoin(base_url, href)
        label = normalize_space(a.get_text(" ", strip=True))
        if not label:
            ctx_parent = a.find_parent(["h4", "li", "div"])
            if ctx_parent:
                label = normalize_space(ctx_parent.get_text(" ", strip=True))[:80]
        candidates.append((u, label or u.rsplit("/", 1)[-1]))

    # dedupe by filename token
    def key_of(u: str) -> str:
        m = re.search(r"f=([^&]+)", u)
        if m:
            return m.group(1).lower()
        return urlparse(u).path.rsplit("/", 1)[-1].lower()

    for u, label in candidates:
        k = key_of(u)
        if not k:
            continue
        if k in seen_keys:
            for prev in out:
                if key_of(prev["url"]) == k:
                    if not prev["title"] or prev["title"].lower() in ("download", ""):
                        prev["title"] = label
                    break
            continue
        seen_keys.add(k)
        out.append({"url": u, "title": label})
    return out


def _upgrade_to_big(url: str) -> str:
    """Replace ``/img_medium/`` (low-res) with ``/img_big/`` (high-res) in fir-italia URLs."""
    return re.sub(r"/img_(?:medium|small)/", "/img_big/", url)


def hero_gallery(soup: BeautifulSoup, base_url: str) -> list[str]:
    out: list[str] = []
    cover = soup.select_one('[class*="product-cover"]')
    if cover:
        for img in cover.select("img[src], img[data-interchange]"):
            src = (img.get("src") or "").strip()
            if src and not src.endswith(".png") and "logo" not in src.lower():
                out.append(_upgrade_to_big(urljoin(base_url, src)))
            di = (img.get("data-interchange") or "").strip()
            if di:
                # data-interchange is "[url, small], [url, medium], [url, large]"; pick the large.
                for u, label in re.findall(r"\[([^,\[\]]+),\s*([^\]]+)\]", di):
                    label = label.strip().lower()
                    if label in ("large", "big"):
                        out.append(_upgrade_to_big(urljoin(base_url, u.strip())))
                        break
                else:
                    for u, _ in re.findall(r"\[([^,\[\]]+),\s*([^\]]+)\]", di):
                        out.append(_upgrade_to_big(urljoin(base_url, u.strip())))

    text_html = str(soup)
    for u in re.findall(r"https?://[^\"'\s]+/upload/prodotti/img/imgc2/[^\"'\s]+", text_html):
        out.append(u)

    if not out:
        og = og_image(soup)
        if og:
            out.append(_upgrade_to_big(og))

    return dedupe_urls([u for u in out if u])


def load_links(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, encoding="utf-8-sig")
    for col in ("Categorie", "Subcategorie", "SUB-SUBCATEGORIE", "Colectie"):
        if col in df.columns:
            df[col] = df[col].ffill()
    return df


def scrape(*, limit_rows: int | None = None) -> None:
    script_dir = Path(__file__).resolve().parent
    df = load_links(script_dir / LINKS_CSV)

    data_rows: list[pd.Series] = []
    for _, row in df.iterrows():
        link = clean_cell(row.get("Link variante"))
        if link.startswith("http"):
            data_rows.append(row)

    if limit_rows is not None:
        data_rows = data_rows[: max(0, limit_rows)]

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

    for row in data_rows:
        url = clean_cell(row.get("Link variante"))
        soup = fetch_soup(url)
        if soup is None:
            print(f"  SKIP fetch failed: {url}")
            continue

        np_ = clean_cell(row.get("Nume produs"))
        nv = clean_cell(row.get("Nume variante/subtitlu"))
        title = np_ or nv

        categorie = clean_cell(row.get("Categorie"))
        subcategorie = clean_cell(row.get("Subcategorie"))
        sub_sub = clean_cell(row.get("SUB-SUBCATEGORIE"))
        colectie = clean_cell(row.get("Colectie"))
        comp_articles = clean_cell(row.get("Articole complementare necesare"))
        obs = clean_cell(row.get("obs"))

        description_parts: list[str] = []
        page_desc = page_description(soup)
        if page_desc:
            description_parts.append(page_desc)
        if obs:
            description_parts.append(f"Observații: {obs}")
        comp_block = complementary_articles_text(soup)
        if not comp_block and comp_articles:
            comp_block = f"Articole complementare necesare:\n- {comp_articles}"
        if comp_block:
            description_parts.append(comp_block)
        description = "\n\n".join(description_parts)

        gallery = hero_gallery(soup, url)
        pdfs = collect_pdfs(soup, url)

        dims = parse_dimensions_from_text(description)
        product = {
            "id": p_id,
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
            "position": "",
            "sizes": "",
            "thickness": "",
            "material": "Stainless Steel AISI 316L",
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
                    "title": enrich_technical_pdf_title(doc["title"], product_title=title, collection=colectie),
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

        sku = variant_sku(SKU_PREFIX, v_id, clean_cell(row.get("COD REFERINTA")))
        color = default_color(normalize_space(clean_cell(row.get("Variante culori"))).title())
        variants_db.append({
            "id": v_id,
            "product_id": p_id,
            "sku": sku,
            "color": color,
            "url": url,
            "gallery_photos": json.dumps(gallery, ensure_ascii=False),
            "technical_photos": json.dumps([], ensure_ascii=False),
        })

        print(f"  product id={p_id} variant id={v_id} sku={sku} | {title!r} | imgs={len(gallery)} | pdfs={len(pdfs)}")
        p_id += 1
        v_id += 1
        time.sleep(0.1)

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
