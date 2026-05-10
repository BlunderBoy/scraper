"""
Casalgrande Padana -- scrape collection pages from ``links.csv``.

Each collection URL (e.g. ``/product/elements-pebbles``) renders a Next.js
single-page-app. Product data lives in the ``__NEXT_DATA__`` script as
``props.pageProps.prodotto`` with ``titolo``, ``descrizione`` (HTML),
``mainImage``, ``detailImages``, ``documenti``, ``documentoTabellaMinimale``
and ``attributi`` containing ``colori`` (per-shade swatches), ``decori``,
``pezziSpeciali``, ``formatiSpecifici``, ``superfici``, ``spessoreSpecifico``.

CSV row model: each ``Nume produs`` is a separate product (one variant each).
Many products share one URL (the collection page), so ``Link variante`` is
forward-filled. Every distinct shade / decoro / pezzo speciale named in the
CSV is matched against the collection's ``colori`` / ``decori`` /
``pezziSpeciali`` lists to pull the swatch image; the page's ``mainImage`` and
``detailImages`` are appended for context.

SKU: ``COD REFERINTA`` when set, else ``CP_<variant_id>``.
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

MANUFACTURER = "Casalgrande Padana"
SKU_PREFIX = "CP"
LINKS_CSV = "links.csv"

START_PRODUCT_ID = 900
START_VARIANT_ID = 6000
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


def extract_next_data(soup: BeautifulSoup) -> dict[str, Any]:
    s = soup.select_one("#__NEXT_DATA__")
    if not s or not s.string:
        return {}
    try:
        return json.loads(s.string)
    except json.JSONDecodeError:
        return {}


def html_to_text(html: str) -> str:
    if not html:
        return ""
    sub = BeautifulSoup(html, "html.parser")
    paragraphs: list[str] = []
    for p in sub.find_all(["p", "li"]):
        t = p.get_text(" ", strip=True)
        if t:
            paragraphs.append(t)
    if not paragraphs:
        bare = normalize_space(sub.get_text(" ", strip=True))
        return _trim_marketing_intro(bare)
    paragraphs = [_trim_marketing_intro(x) for x in paragraphs]
    paragraphs = [x for x in paragraphs if x]
    return "\n\n".join(paragraphs)


_MARKETING_PREFIX_PATTERNS = [
    re.compile(r"^\s*(?:Casalgrande\s+Padana|The\s+brand|We)\s+(?:presents?|introduces?|offers?|launch(?:es)?)\b[^.]*\.\s*", re.I),
    re.compile(r"^\s*In\s+particular,\s+[^.]*\.\s*", re.I),
]


def _trim_marketing_intro(text: str) -> str:
    """Drop the boilerplate ``Casalgrande Padana presents …`` openers (incl. follow-up
    cross-sell sentences) common across collections."""
    if not text:
        return ""
    out = text
    for _ in range(8):
        before = out
        for pat in _MARKETING_PREFIX_PATTERNS:
            out = pat.sub("", out, count=1)
        if out == before:
            break
    return out.strip()


def names_index(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for it in items or []:
        nm = clean_cell(it.get("nome"))
        if not nm:
            continue
        out[norm_key(nm)] = it
    return out


def parse_collection(soup: BeautifulSoup) -> dict[str, Any]:
    data = extract_next_data(soup)
    pp = (data.get("props") or {}).get("pageProps") or {}
    prod = pp.get("prodotto") or {}
    attr = prod.get("attributi") or {}

    description = html_to_text(prod.get("descrizione") or "")
    main_image = clean_cell(prod.get("mainImage"))
    detail_images = [clean_cell(u) for u in (prod.get("detailImages") or []) if clean_cell(u)]

    formati_specifici = [clean_cell(x.get("nome")) for x in (attr.get("formatiSpecifici") or []) if clean_cell(x.get("nome"))]
    superfici = [clean_cell(x.get("nome")) for x in (attr.get("superfici") or []) if clean_cell(x.get("nome"))]
    dimensioni = [clean_cell(x.get("nome")) for x in (attr.get("dimensioni") or []) if clean_cell(x.get("nome"))]
    spessori_specifici = [clean_cell(x.get("nome")) for x in (attr.get("spessoreSpecifico") or attr.get("spessori") or []) if clean_cell(x.get("nome"))]

    if not spessori_specifici:
        spessori_specifici = sorted({
            re.search(r"(\d+(?:\.\d+)?\s*mm)", v or "", flags=re.I).group(1)
            for v in dimensioni
            if re.search(r"\d+(?:\.\d+)?\s*mm", v or "", flags=re.I)
        }, key=str.casefold)

    sizes_combined = formati_specifici or dimensioni
    sizes = ", ".join(dict.fromkeys(sizes_combined))
    finishes = ", ".join(dict.fromkeys(superfici))
    thickness = ", ".join(dict.fromkeys(spessori_specifici))

    colori_idx = names_index(attr.get("colori") or [])
    decori_idx = names_index(attr.get("decori") or [])
    pezzi_idx = names_index(attr.get("pezziSpeciali") or [])

    documenti = list(prod.get("documenti") or [])
    tab_min = prod.get("documentoTabellaMinimale") or {}
    if tab_min and tab_min.get("url"):
        documenti.append(tab_min)

    return {
        "titolo": clean_cell(prod.get("titolo")),
        "description": description,
        "main_image": main_image,
        "detail_images": detail_images,
        "sizes": sizes,
        "finishes": finishes,
        "thickness": thickness,
        "colori_idx": colori_idx,
        "decori_idx": decori_idx,
        "pezzi_idx": pezzi_idx,
        "documenti": documenti,
    }


def find_match(name: str, parsed: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    nk = norm_key(name)
    if not nk:
        return None, ""
    for label, idx in (("colore", parsed["colori_idx"]), ("decoro", parsed["decori_idx"]), ("pezzoSpeciale", parsed["pezzi_idx"])):
        if nk in idx:
            return idx[nk], label
    for label, idx in (("colore", parsed["colori_idx"]), ("decoro", parsed["decori_idx"]), ("pezzoSpeciale", parsed["pezzi_idx"])):
        for k, v in idx.items():
            if nk in k or k in nk:
                return v, label
    return None, ""


def normalize_image_url(u: str) -> str:
    u = clean_cell(u)
    if not u:
        return ""
    if u.startswith("//"):
        return "https:" + u
    return u


def variant_gallery_urls(match: dict[str, Any] | None, parsed: dict[str, Any]) -> list[str]:
    out: list[str] = []
    if match and match.get("image"):
        out.append(normalize_image_url(match.get("image")))
    if parsed.get("main_image"):
        out.append(normalize_image_url(parsed["main_image"]))
    out.extend(normalize_image_url(u) for u in parsed.get("detail_images", []))
    return dedupe_urls([u for u in out if u])


def load_links(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, encoding="utf-8-sig")
    for col in ("Categorie", "Subcategorie", "SUB-SUBCATEGORIE", "Colectie", "Link variante"):
        if col in df.columns:
            df[col] = df[col].ffill()
    return df


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

    cache: dict[str, dict[str, Any]] = {}

    def get_parsed(url: str) -> dict[str, Any] | None:
        if url in cache:
            return cache[url]
        soup = fetch_soup(url, session)
        if not soup:
            return None
        parsed = parse_collection(soup)
        cache[url] = parsed
        time.sleep(0.08)
        return parsed

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
        np_ = clean_cell(row.get("Nume produs"))
        categorie = clean_cell(row.get("Categorie"))
        subcategorie = clean_cell(row.get("Subcategorie"))
        sub_sub = clean_cell(row.get("SUB-SUBCATEGORIE"))
        colectie = clean_cell(row.get("Colectie"))

        parsed = get_parsed(url)
        if parsed is None:
            print(f"  SKIP product (fetch failed) {np_!r} from {url}")
            continue

        match, _kind = find_match(np_, parsed)
        gallery = variant_gallery_urls(match, parsed)
        per_color_desc = ""
        if match and match.get("descrizioneDettagliata"):
            per_color_desc = html_to_text(match["descrizioneDettagliata"])

        full_desc_parts: list[str] = []
        if per_color_desc:
            full_desc_parts.append(per_color_desc)
        if parsed.get("description"):
            full_desc_parts.append(parsed["description"])
        description = "\n\n".join(full_desc_parts)

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
            "finishes": parsed.get("finishes", ""),
            "position": "",
            "sizes": parsed.get("sizes", ""),
            "thickness": parsed.get("thickness", ""),
            "material": "Porcelain stoneware",
            "shape": "",
            "cut": "",
            "diameter": "",
            "length": "",
            "width": "",
            "height": "",
        }
        products_db.append(product)

        for sort_i, doc in enumerate(parsed.get("documenti") or []):
            doc_url = clean_cell(doc.get("url"))
            if not doc_url:
                continue
            if doc_url not in pdf_url_to_id:
                title_raw = clean_cell(doc.get("nome")) or clean_cell(doc.get("descrizione")) or doc_url.rsplit("/", 1)[-1]
                pdf_url_to_id[doc_url] = pdf_id_counter
                technical_pdfs_db.append({
                    "id": pdf_id_counter,
                    "title": enrich_technical_pdf_title(title_raw, product_title=np_, collection=colectie),
                    "r2_key": "",
                    "url": doc_url,
                    "created_at": stamp,
                })
                pdf_id_counter += 1
            product_pdfs_db.append({
                "id": pp_id_counter,
                "product_id": p_id,
                "pdf_id": pdf_url_to_id[doc_url],
                "sort_order": sort_i,
                "created_at": stamp,
            })
            pp_id_counter += 1

        sku = variant_sku(SKU_PREFIX, v_id, clean_cell(row.get("COD REFERINTA")))
        variants_db.append({
            "id": v_id,
            "product_id": p_id,
            "sku": sku,
            "color": "Standard",
            "url": url,
            "gallery_photos": json.dumps(gallery, ensure_ascii=False),
            "technical_photos": json.dumps([], ensure_ascii=False),
        })

        print(f"  product id={p_id} variant id={v_id} | {np_!r} | imgs={len(gallery)} | docs={len(parsed.get('documenti') or [])}")
        p_id += 1
        v_id += 1

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
