"""
Quintessenza Ceramiche — collection pages from ``links.csv`` (quintessenzaceramiche.it).

Each CSV row is one ``collezioni`` URL (treated as one product). Variants are the
Elementor image-box tiles: color title, manufacturer SKU in the
description line (variant line size is ignored), and a ``gallery_photos`` list of up to
five URLs (variant tile first when unique, then og:image, hero/layout images, then
image-box grid). Product ``sizes`` are lowercased with a space before ``cm``/``mm``.
Thickness is normalized to ``Xmm, Ymm``. Marketing description drops pipe-separated
collection teasers and stub intros that only introduce further paragraphs.
Sizes come from the Elementor size heading (metric only; see ``extract_sizes_field``). Product thickness,
finish, and recycled material come from the ordered icon-box row before downloads (slots
0 / 2 / 3; slot 1 is texture and ignored). Trims (``ALL*`` SKUs) and cross-collection tiles
are dropped. PDFs: ``QC MORE`` + leaflet anchors only; titles
``{product title} - Technical details`` / ``{product title} - Leaflet``. Shared Färgblock leaflet URLs use the stable title
``Färgblock - Leaflet`` so output does not depend on which format row is scraped first.

SKU: manufacturer code from the variant tile when present; otherwise ``QC_<variant_id>``.
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
    join_unique_csv,
    normalize_category,
    normalize_space,
    total_gallery_count,
    variant_sku,
    write_brand_outputs,
)

try:
    from deep_translator import GoogleTranslator
except ImportError:
    GoogleTranslator = None  # type: ignore[misc, assignment]

MANUFACTURER = "Quintessenza Ceramiche"
SKU_PREFIX = "QC"
LINKS_CSV = "links.csv"

START_PRODUCT_ID = 2100
START_VARIANT_ID = 11000
START_TECH_PDF_ID = 1
START_PRODUCT_PDF_ID = 1

# SiteGround / WAF returns HTTP 403 for a full Chrome ``User-Agent`` string from Python;
# a minimal UA matches browser behaviour that still receives ``200`` (verified).
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
}

# Manufacturer SKU token at end of variant description (e.g. ``10x40 TEM101M``, ``4x33,4 COL104L``).
_RE_VARIANT_SKU = re.compile(r"^([A-Z]{2,}\d[A-Z0-9]*)\s*$")

# Stop collecting product-detail icon rows when we reach downloads / QC MORE.
_STOP_PRODUCT_DETAIL_ICON = re.compile(
    r"^(?:qc\s*more)$|info\s+tecniche|\bscarica\b|\bcatalogo\b|\bleaflet\b",
    re.I,
)

_RE_IMPERIAL_SIZE = re.compile(r'["\u201d\u201c]|\b\d+\s*"\s*[x×]|\bin(?:ch)?\b', re.I)

_RE_LEAFLET_PDF = re.compile(r"leaflet|flyer|volantino", re.I)

_RE_MATERIAL_RECYCLED_IT = re.compile(r"^(\d+)\s*%\s*Materiale\s+riciclato\s*$", re.I)

_PHOTO_URL_EXT = re.compile(r"\.(jpe?g|png|webp)(\?|$)", re.I)


def format_pdf_title(product_title: str, url: str, anchor_label: str) -> str:
    pt = clean_cell(product_title)
    blob = f"{url} {anchor_label}"
    if _RE_LEAFLET_PDF.search(blob):
        # Both Färgblock collection pages point at the same leaflet PDF; title must not depend on scrape order.
        if "fargblock" in urlparse(url).path.casefold():
            return "Färgblock - Leaflet"
        return f"{pt} - Leaflet"
    return f"{pt} - Technical details"


def collection_slug_from_url(url: str) -> str:
    parts = [p for p in urlparse(url).path.strip("/").split("/") if p]
    if len(parts) >= 2 and parts[0].casefold() == "collezioni":
        return parts[1].casefold()
    if len(parts) >= 3 and parts[0].casefold() == "en" and parts[1].casefold() == "collezioni":
        return parts[2].casefold()
    return parts[-1].casefold() if parts else ""


def _pgm_bucket(sku_u: str) -> str | None:
    m = re.match(r"^PGM(\d{3})", sku_u)
    if not m:
        return None
    n = int(m.group(1))
    if 112 <= n <= 122:
        return "pigmento10"
    if (101 <= n <= 111) or (301 <= n <= 311):
        return "pigmento"
    return None


def sku_allowed_for_collection(sku: str, slug: str) -> bool:
    """Keep only SKUs that belong to this collection page (drops trims ALL*, cross-sells COL*, …)."""
    s = sku.strip().upper()
    slug_l = slug.casefold()
    if s.startswith("ALL"):
        return False
    if slug_l == "tempo":
        return s.startswith("TEM")
    if slug_l == "terrae":
        return s.startswith("TER") or bool(re.match(r"^ER\d", s))
    if slug_l == "amarcord":
        return s.startswith("AMA")
    if slug_l == "colors":
        return s.startswith("COL")
    if slug_l == "fluid":
        return s.startswith("FLU")
    if slug_l == "marea":
        return s.startswith("MAR")
    if slug_l in ("fargblock-15x15", "fargblock-matt"):
        return s.startswith("FGB")
    if slug_l == "confetti":
        return s.startswith("CNF")
    if slug_l == "oltre":
        return s.startswith("OTR")
    if slug_l == "pigmento10":
        return _pgm_bucket(s) == "pigmento10"
    if slug_l == "pigmento":
        return _pgm_bucket(s) == "pigmento"
    return False


def extract_sizes_field(root: BeautifulSoup | Tag) -> str:
    """Metric face size only (no imperial ``h2`` lines). Primary: Elementor hint selector."""

    def is_metric_size_line(text: str) -> bool:
        s = normalize_space(text)
        if len(s) > 42 or len(s) < 3:
            return False
        if _RE_IMPERIAL_SIZE.search(s):
            return False
        if not re.match(r"^\d", s):
            return False
        if not re.search(r"[x×]", s, re.I):
            return False
        if re.search(r"(cm|mm)", s, re.I):
            return True
        return bool(re.fullmatch(r"\d[\d.,]*\s*[x×]\s*\d[\d.,]*", s, re.I))

    h2_hint = root.select_one(".elementor-element-1ad3c71 > div:nth-child(1) > h2:nth-child(1)")
    if h2_hint:
        t = normalize_space(h2_hint.get_text(" ", strip=True))
        if is_metric_size_line(t):
            return standardize_sizes_field(t)
    candidates: list[str] = []
    seen: set[str] = set()
    for h2 in root.select("h2"):
        t = normalize_space(h2.get_text(" ", strip=True))
        if not is_metric_size_line(t):
            continue
        k = t.casefold()
        if k in seen:
            continue
        seen.add(k)
        candidates.append(t)
    return standardize_sizes_field(join_unique_csv(candidates))


def standardize_sizes_field(raw: str) -> str:
    """Lowercase; space before ``cm``/``mm`` when glued; EU decimal commas → dots (``6,5`` → ``6.5``); commas between sizes stay."""
    s = normalize_space(raw).lower()
    if not s:
        return ""
    s = re.sub(r"(\d)(cm|mm)\b", r"\1 \2", s)
    s = re.sub(r"(?<=\d),(?=\d)", ".", s)
    return normalize_space(s)


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


def collection_root(soup: BeautifulSoup) -> BeautifulSoup | Tag:
    """Main ``collezioni`` post body (avoids header/footer PDF noise)."""
    root = soup.select_one('div[data-elementor-post-type="collezioni"]')
    if root:
        return root
    return soup


# Sidebar / spec-sheet paragraphs to exclude from marketing description (Italian UI).
_IT_DESCRIPTION_JUNK = re.compile(
    r"\bNCS\b|chituri|Ambalare\b|Caratteristiche\s+tecniche|Modelli\s+di\s+posa|"
    r"Modelli\s+di\s+installazione|Packaging\b",
    re.I,
)


def _is_collection_teaser_nav(text: str) -> bool:
    """Footer / mega-menu style line: many short segments separated by ``|``."""
    return text.count("|") >= 4


def _is_stub_intro_paragraph(text: str) -> bool:
    """Short lead-in that only introduces other blocks (often ends with ``:``)."""
    t = text.strip()
    return len(t) < 160 and t.endswith(":")


def extract_description_it(soup: BeautifulSoup) -> str:
    root = collection_root(soup)
    parts: list[str] = []

    hint_p = root.select_one(".elementor-element-789ca4d4 .elementor-widget-container > p")
    if hint_p:
        t = normalize_space(hint_p.get_text(" ", strip=True))
        if len(t) > 20 and not _IT_DESCRIPTION_JUNK.search(t):
            parts.append(t)

    for te in root.select(".elementor-widget-text-editor"):
        for p in te.select("p"):
            t = normalize_space(p.get_text(" ", strip=True))
            if len(t) < 25:
                continue
            if _IT_DESCRIPTION_JUNK.search(t):
                continue
            parts.append(t)

    paras = list(dict.fromkeys(parts))
    paras = [p for p in paras if not _is_collection_teaser_nav(p)]
    while paras and _is_collection_teaser_nav(paras[-1]):
        paras.pop()
    while len(paras) > 1 and _is_stub_intro_paragraph(paras[0]):
        paras.pop(0)

    if not paras:
        return ""

    if len(paras) >= 2:
        last, first = paras[-1], paras[0]
        if len(last) >= 180 and _is_stub_intro_paragraph(first):
            return last

    return "\n\n".join(paras)


def translate_it_ro(text: str, *, enabled: bool) -> str:
    if not enabled or not normalize_space(text):
        return text
    if GoogleTranslator is None:
        print("  [translate] deep-translator not installed; leaving Italian.")
        return text
    translator = GoogleTranslator(source="it", target="ro")
    chunks: list[str] = []
    remaining = text.strip()
    max_chunk = 4200
    while remaining:
        if len(remaining) <= max_chunk:
            piece = remaining
            remaining = ""
        else:
            cut = remaining.rfind("\n\n", 0, max_chunk)
            if cut < max_chunk // 2:
                cut = remaining.rfind(". ", 0, max_chunk)
            if cut < max_chunk // 2:
                cut = max_chunk
            piece = remaining[:cut].strip()
            remaining = remaining[cut:].strip()
        if not piece:
            break
        try:
            chunks.append(translator.translate(piece))
            time.sleep(0.35)
        except Exception as e:
            print(f"  [translate] failed ({e}); keeping Italian fragment.")
            chunks.append(piece)
    return "\n\n".join(chunks)


def translate_material_it_ro(text: str, *, enabled: bool) -> str:
    """When translation is on: ``… Materiale riciclato`` → ``… materiale reciclate`` (RO)."""
    s = normalize_space(text)
    if not s:
        return ""
    if not enabled:
        return s
    m = _RE_MATERIAL_RECYCLED_IT.match(s)
    if m:
        return f"{m.group(1)}% materiale reciclate"
    if GoogleTranslator is None:
        return s
    try:
        return GoogleTranslator(source="it", target="ro").translate(s)
    except Exception:
        return s


def parse_variant_line(desc: str) -> tuple[str, str]:
    """Split ``size_part manufacturer_sku`` from the image-box description."""
    desc = normalize_space(desc)
    if not desc:
        return "", ""
    tokens = desc.split()
    if not tokens:
        return "", ""
    last = tokens[-1]
    if _RE_VARIANT_SKU.match(last):
        size_part = " ".join(tokens[:-1])
        return size_part, last
    return desc, ""


def standardize_thickness_mm(raw: str) -> str:
    """Normalize thickness to ``8mm, 12mm`` (comma-separated, no space before ``mm``)."""
    s = normalize_space(raw).lower()
    if not s or not re.search(r"\d", s):
        return ""
    s = re.sub(r"\bmm\b", " ", s)
    chunks: list[str] = []
    for part in re.split(r"\s*[,/]\s*", s):
        part = normalize_space(part)
        if not part:
            continue
        m = re.match(r"^([\d.,]+)", part)
        if not m:
            continue
        chunks.append(f"{m.group(1)}mm")
    return ", ".join(chunks)


def product_detail_icon_titles(root: BeautifulSoup | Tag) -> list[str]:
    """Icon-box titles in the specs strip, stopping before QC MORE / scarica / leaflet."""
    rows: list[str] = []
    for ib in root.select(".elementor-widget-icon-box"):
        title_el = ib.select_one(".elementor-icon-box-title")
        t = normalize_space(title_el.get_text(" ", strip=True) if title_el else "")
        if not t:
            continue
        if _STOP_PRODUCT_DETAIL_ICON.search(t):
            break
        rows.append(t)
    return rows


def product_detail_specs(root: BeautifulSoup | Tag) -> tuple[str, str, str]:
    """Thickness (slot 0), finishes (slot 2). Slot 1 is texture (ignored). Slot 3 = material."""
    titles = product_detail_icon_titles(root)
    thickness = standardize_thickness_mm(titles[0]) if titles else ""
    finishes = titles[2] if len(titles) > 2 else ""
    material = titles[3] if len(titles) > 3 else ""
    return thickness, finishes, material


def qc_catalog_documents(root: BeautifulSoup | Tag, page_url: str) -> list[dict[str, str]]:
    """Technical PDF = ``QC MORE`` anchor only; leaflet = PDF anchor whose label mentions ``leaflet``."""
    tech_u = ""
    leaflet_u = ""
    leaflet_label = ""
    for ib in root.select(".elementor-widget-icon-box"):
        tit_el = ib.select_one(".elementor-icon-box-title")
        box_title = normalize_space(tit_el.get_text(" ", strip=True) if tit_el else "")
        bt = box_title.casefold()
        for a in ib.select("a[href]"):
            href = (a.get("href") or "").strip()
            if ".pdf" not in href.casefold():
                continue
            u = urljoin(page_url, href)
            link_txt = normalize_space(a.get_text(" ", strip=True))
            lt = link_txt.casefold()
            if bt == "qc more" or lt == "qc more":
                tech_u = u
            if "leaflet" in lt or "leaflet" in bt:
                leaflet_u = u
                leaflet_label = link_txt or "Leaflet"
    docs: list[dict[str, str]] = []
    if tech_u:
        docs.append({"url": tech_u, "title": "QC MORE"})
    if leaflet_u and leaflet_u != tech_u:
        docs.append({"url": leaflet_u, "title": leaflet_label})
    return docs


def extract_variants(root: BeautifulSoup | Tag, page_url: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for box in root.select(".elementor-widget-image-box"):
        title_el = box.select_one(".elementor-image-box-title")
        desc_el = box.select_one(".elementor-image-box-description")
        title = normalize_space(title_el.get_text(" ", strip=True) if title_el else "")
        desc = normalize_space(desc_el.get_text(" ", strip=True) if desc_el else "")
        size_part, sku = parse_variant_line(desc)
        if not sku:
            continue
        img = box.select_one("img")
        src = ""
        if img:
            src = (img.get("data-src") or img.get("src") or "").strip()
        if src and not src.startswith("http"):
            src = urljoin(page_url, src)
        out.append(
            {
                "color": title,
                "size_hint": size_part,
                "sku": sku,
                "image": src,
            }
        )
    return out


def normalize_image_url(page_url: str, src: str) -> str:
    src = (src or "").strip()
    if not src:
        return ""
    u = urljoin(page_url, src)
    p = urlparse(u)
    return p._replace(query="", fragment="").geturl()


def _img_src(img: Tag) -> str:
    return (img.get("data-src") or img.get("data-lazy-src") or img.get("src") or "").strip()


def _inside_image_box(img: Tag) -> bool:
    for par in img.parents:
        if isinstance(par, Tag) and "elementor-widget-image-box" in (par.get("class") or []):
            return True
    return False


def _is_product_photo_url(url: str) -> bool:
    if not url or url.startswith("data:"):
        return False
    sl = url.casefold()
    if "wp-content/uploads" not in sl:
        return False
    if any(x in sl for x in ("logo", "icon", "placeholder", "avatar")):
        return False
    if "leaflet" in sl or "volantino" in sl or "flyer" in sl:
        return False
    base = sl.rsplit("/", 1)[-1].split("?")[0]
    if re.match(r"^\d+x\d+\.(png|jpe?g|webp)$", base):
        return False
    return bool(_PHOTO_URL_EXT.search(url))


def collect_page_product_images(
    soup: BeautifulSoup, root: BeautifulSoup | Tag, page_url: str, *, limit: int = 5
) -> list[str]:
    """Hero / layout images first (outside image-box widgets), then variant grid tiles, after ``og:image``."""
    urls: list[str] = []
    seen: set[str] = set()

    def push(raw: str) -> None:
        u = normalize_image_url(page_url, raw)
        if not u or not _is_product_photo_url(u):
            return
        if u in seen:
            return
        seen.add(u)
        urls.append(u)

    og = soup.select_one('meta[property="og:image"]')
    if og and og.get("content"):
        push(og["content"])

    for img in root.select("img"):
        if _inside_image_box(img):
            continue
        push(_img_src(img))
        if len(urls) >= limit:
            return urls[:limit]

    for img in root.select(".elementor-widget-image-box img"):
        push(_img_src(img))
        if len(urls) >= limit:
            break

    return urls[:limit]


def csv_product_title(row: pd.Series) -> str:
    np = clean_cell(row.get("Nume produs"))
    col = clean_cell(row.get("Colectie"))
    if np:
        return np
    return col


def load_links(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, encoding="utf-8-sig")
    for col in ("Categorie", "Subcategorie", "Colectie", "Nume produs"):
        if col in df.columns:
            df[col] = df[col].ffill()
    return df


def normalize_url(url: str) -> str:
    u = clean_cell(url).split("#")[0].strip()
    if not u.startswith("http"):
        return u
    return u.rstrip("/") + "/" if "quintessenzaceramiche.it" in u.casefold() and not u.endswith("/") else u


def scrape(*, limit_rows: int | None = None, translate: bool = True, output_dir: Path | None = None) -> None:
    script_dir = Path(__file__).resolve().parent
    out_dir = output_dir if output_dir is not None else script_dir
    df = load_links(script_dir / LINKS_CSV)

    session = requests.Session()
    session.headers.update(HEADERS)

    rows_out: list[pd.Series] = []
    for _, row in df.iterrows():
        link = normalize_url(clean_cell(row.get("Link variante")))
        np = clean_cell(row.get("Nume produs")) or clean_cell(row.get("Colectie"))
        if not link.startswith("http") or not np:
            continue
        rows_out.append(row)

    if limit_rows is not None:
        rows_out = rows_out[: max(0, limit_rows)]

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

    def get_soup(raw_url: str) -> BeautifulSoup | None:
        u = normalize_url(raw_url)
        if u in cache:
            return cache[u]
        soup = fetch_soup(u, session)
        if soup:
            cache[u] = soup
        time.sleep(0.08)
        return soup

    translate_ok = translate and GoogleTranslator is not None
    if translate and GoogleTranslator is None:
        print("Install deep-translator for IT→RO description translation: pip install deep-translator")

    for row in rows_out:
        url = normalize_url(clean_cell(row.get("Link variante")))
        soup = get_soup(url)
        if not soup:
            print(f"\n=== SKIP (fetch failed) {url!r} ===")
            continue

        root = collection_root(soup)
        slug = collection_slug_from_url(url)
        variants_all = extract_variants(root, url)
        variants = [v for v in variants_all if sku_allowed_for_collection(v["sku"], slug)]
        if not variants:
            print(f"\n=== SKIP (no variants after collection filter) {url!r} ===")
            continue

        categorie = clean_cell(row.get("Categorie"))
        subcategorie = clean_cell(row.get("Subcategorie"))
        colectie = clean_cell(row.get("Colectie"))

        thickness, finishes, material = product_detail_specs(root)

        sizes = extract_sizes_field(root)
        page_gallery = collect_page_product_images(soup, root, url, limit=5)

        desc_it = extract_description_it(soup)
        desc_ro = translate_it_ro(desc_it, enabled=translate_ok)
        material_out = translate_material_it_ro(material, enabled=translate_ok)

        row_dict: dict[str, Any] = {
            "title": csv_product_title(row),
            "description": desc_ro,
            "category": normalize_category(categorie),
            "type": subcategorie,
            "collection": colectie,
            "is_new": False,
            "subtype": "",
            "manufacturer": MANUFACTURER,
            "catalog_id": None,
            "finishes": finishes,
            "position": "",
            "sizes": sizes,
            "thickness": thickness,
            "material": material_out,
            "shape": "",
            "cut": "",
            "diameter": "",
            "length": "",
            "width": "",
            "height": "",
            "id": p_id,
        }
        products_db.append(row_dict)

        docs = qc_catalog_documents(root, url)
        for sort_i, doc in enumerate(docs):
            u = doc["url"]
            if u not in pdf_url_to_id:
                pdf_url_to_id[u] = pdf_id_counter
                pdf_title = format_pdf_title(
                    clean_cell(row_dict.get("title", "")),
                    u,
                    doc["title"],
                )
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

        print(f"\n  product id={p_id} | {row_dict.get('title')!r} | variants={len(variants)} | PDFs={len(docs)}")

        seen_skus: set[str] = set()
        for v in variants:
            sku_code = clean_cell(v["sku"])
            cod_for_variant = sku_code
            if sku_code and sku_code in seen_skus:
                cod_for_variant = ""
            if sku_code:
                seen_skus.add(sku_code)
            sku = variant_sku(SKU_PREFIX, v_id, cod_for_variant)
            col = default_color(normalize_space(v["color"]).title())
            v_img = normalize_image_url(url, v["image"]) if v["image"] else ""
            gurls = dedupe_urls(([v_img] if v_img else []) + page_gallery)[:5]

            variants_db.append(
                {
                    "id": v_id,
                    "product_id": p_id,
                    "sku": sku,
                    "color": col,
                    "url": url,
                    "gallery_photos": json.dumps(gurls, ensure_ascii=False),
                    "technical_photos": json.dumps([], ensure_ascii=False),
                }
            )
            print(f"    variant {sku} | {col} | imgs={len(gurls)}")
            v_id += 1

        p_id += 1
        time.sleep(0.06)

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
            "Most often one of these is still open in Excel, LibreOffice, or another editor — "
            "close products.csv, variants.csv, technical_pdfs.csv, and product_pdfs.csv, "
            "then run the scraper again.\n"
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
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--no-translate", action="store_true", help="Keep Italian description text.")
    ap.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        metavar="DIR",
        help=(
            "Optional folder for CSV output (default: same folder as scrape.py). "
            "Use only if you want outputs elsewhere; if saving fails, close the CSVs in Excel first."
        ),
    )
    args = ap.parse_args()
    od = args.output_dir
    if od is not None:
        out_dir = od.resolve() if od.is_absolute() else (script_dir / od).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
    else:
        out_dir = None
    scrape(limit_rows=args.limit, translate=not args.no_translate, output_dir=out_dir)


if __name__ == "__main__":
    main()
