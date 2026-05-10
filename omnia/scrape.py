"""
Scrape Omnia Floor product pages driven by ``links.csv``.

**Row model (``links.csv``)**

- Each non-empty row is one SKU / variant (one row in ``variants.csv``).
- A row with leading columns left blank continues the row above: inherit
  ``Categorie``, ``Subcategorie``, ``Colectie``, ``Nume produs``, and
  ``Link variante`` forward; ``COD REFERINTA`` and ``Nume variante`` are never
  inherited and must be present per variant line.
  Example: Plank on one line, Herringbone on the next with empty leading cells,
  same shade name carried down.

**Product identity**

- One **product** per CSV shade name (``Nume produs``) within each collection
  URL — multiple products share the same page (e.g. each oak tone is its own
  product row); **variants** are format SKUs (Plank / Herringbone / MAT / …).
- ``title`` is the CSV shade name (``Nume produs``) only, not ``h1`` + collection.
- CSV ``Colectie`` maps to ``collection``.
- ``type`` is ``Subcategorie`` from the CSV; ``subtype`` is left empty. ``category``
  is lowercased. Other CSV columns fill ``collection``, etc.; specs still come from the
  site (SPECIFICATIONS ``dl``, hero description).

- Technical PDFs (same URLs as on the site) are written to ``technical_pdfs.csv``
  and linked per product in ``product_pdfs.csv`` (not stored on variants).

Image URLs only — ``gallery_photos`` holds manufacturer image URLs;
``technical_photos`` on variants stays empty (JSON ``[]``).

SPECIFICATIONS at the bottom of each collection page are parsed from ``main dl``
(dt/dd pairs after the SPECIFICATIONS heading).
"""

from __future__ import annotations

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
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scraper_brand_utils import enrich_technical_pdf_title

MANUFACTURER = "Omnia Floor"
LINKS_CSV = "links.csv"
START_PRODUCT_ID = 87
START_VARIANT_ID = 372
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
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

BASE_ORIGIN = "https://www.omniafloor.it"


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


def normalize_collection_url(url: str) -> str:
    u = url.strip().rstrip("/")
    return u + "/" if u else url


def collection_slug_from_url(url: str) -> str:
    parts = [p for p in urlparse(url).path.strip("/").split("/") if p]
    if len(parts) >= 3 and parts[0] == "en" and parts[1] == "collections" and parts[2]:
        return parts[2].replace("-", "_")
    return (parts[-1] if parts else "collection").replace("-", "_")


def _pdf_title_from_url(url: str) -> str:
    path = urlparse(url).path
    base = path.rsplit("/", 1)[-1]
    if base.lower().endswith(".pdf"):
        base = base[:-4]
    t = base.replace("_", " ").replace("-", " ").strip()
    return t or url


def extract_technical_documents(soup: BeautifulSoup) -> list[dict[str, str]]:
    """
    Technical PDFs (and docs under /docs/) linked from ``main``.
    Each item: ``url`` (absolute), ``title`` (anchor text or derived filename).
    Deduped by URL; preserves DOM order.
    """
    root = soup.select_one("main")
    if not root:
        root = soup
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for a in root.select("a[href]"):
        href = (a.get("href") or "").strip()
        if not href:
            continue
        low = href.lower()
        if not (low.endswith(".pdf") or "/docs/" in low.split("?")[0]):
            continue
        abs_url = urljoin(BASE_ORIGIN, href)
        if abs_url in seen:
            continue
        seen.add(abs_url)
        label = a.get_text(" ", strip=True)
        title = label if label else _pdf_title_from_url(abs_url)
        out.append({"url": abs_url, "title": title})
    return out


def parse_dl_specs(soup: BeautifulSoup) -> dict[str, str]:
    out: dict[str, str] = {}
    for dl in soup.select("main dl"):
        for dt in dl.find_all("dt"):
            dd = dt.find_next_sibling("dd")
            if dd:
                key = dt.get_text(" ", strip=True).rstrip(":")
                out[key] = dd.get_text(" ", strip=True)
    return out


def hero_description(soup: BeautifulSoup) -> str:
    parts: list[str] = []
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        parts.append(meta["content"].strip())
    first_hero = soup.select_one("main .hero")
    if first_hero:
        for p in first_hero.select("p"):
            t = p.get_text(strip=True)
            if t and t not in parts:
                parts.append(t)
    return "\n\n".join(dict.fromkeys(parts))


def infer_material(description: str, title: str, categorie: str) -> str:
    low = (description + " " + title + " " + categorie).lower()
    if "spc" in low or "vinyl" in low or "rigid" in low:
        return "SPC"
    if "straw" in low or "jangal" in low:
        return "Natural fiber"
    if "wood" in low and "urban" in low:
        return "Wood"
    if "tile" in low:
        return "Tile"
    return ""


def _omnia_variant_label_is_format_only(label: str) -> bool:
    nk = norm_key(label)
    if not nk:
        return True
    if "plank" in nk or "herringbone" in nk:
        return True
    if nk in (norm_key("MAT"), norm_key("Lucios")):
        return True
    return False


def infer_position(page_url: str, colectie: str) -> str:
    u = page_url.casefold()
    c = colectie.casefold()
    if "revesta" in u or "wall" in c:
        return "Perete"
    return "Podea"


def extract_product_row(
    soup: BeautifulSoup,
    page_url: str,
    shade_name: str,
    categorie: str,
    colectie: str,
    subcategorie: str,
    variant_type_labels: list[str],
) -> dict[str, Any]:
    title_el = soup.select_one("main .hero h1") or soup.select_one("main h1")
    base_title = title_el.get_text(" ", strip=True) if title_el else ""
    title = clean_cell(shade_name) or base_title
    description = hero_description(soup)
    specs = parse_dl_specs(soup)

    sizes = (
        clean_cell(specs.get("Dimensions", ""))
        or clean_cell(specs.get("Dimension", ""))
        or clean_cell(specs.get("Size", ""))
        or clean_cell(specs.get("Sizes", ""))
    )
    if not sizes:
        blob = (soup.select_one("main") or soup).get_text(" ", strip=True)
        found = re.findall(
            r"\d+(?:[.,]\d+)?\s*[x×]\s*\d+(?:[.,]\d+)?(?:\s*[x×]\s*\d+(?:[.,]\d+)?)?\s*(?:mm|cm)?",
            blob,
            flags=re.I,
        )
        if found:
            sizes = " | ".join(sorted(set(found), key=str.casefold))
    wear = specs.get("Wear Layer (overlay)", "") or specs.get("Wear Layer", "")
    ixpe = specs.get("Integrated IXPE Underlay", "")
    thickness = " | ".join(x for x in [wear, ixpe] if x)

    non_format = [x for x in variant_type_labels if not _omnia_variant_label_is_format_only(x)]
    if non_format:
        finishes = ", ".join(non_format)
    else:
        finishes = clean_cell(specs.get("Finish", "")) or clean_cell(specs.get("Surface", ""))

    return {
        "title": title,
        "description": description,
        "category": categorie.lower() if categorie else "",
        "type": subcategorie,
        "collection": colectie,
        "is_new": False,
        "subtype": "",
        "manufacturer": MANUFACTURER,
        "catalog_id": None,
        "finishes": finishes,
        "position": infer_position(page_url, colectie),
        "sizes": sizes,
        "thickness": thickness,
        "material": infer_material(description, title, categorie),
        "shape": "",
    }


def color_slug_from_catalog_src(src: str) -> str:
    m = re.search(r"/colors/([^/]+)/", src)
    return m.group(1) if m else ""


def dedupe_urls(urls: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def extract_color_blocks(soup: BeautifulSoup) -> list[dict[str, Any]]:
    """
    COLORS grid: each column has .color-card__label (h3 or p), then catalog images.
    Premium lines include upside.webp (plank) and upsideH.webp (herringbone).
    """
    blocks: list[dict[str, Any]] = []
    wrap = soup.select_one("main div.flex.flex-wrap")
    if not wrap:
        return blocks

    for cell in wrap.select(":scope > div.mb-4"):
        lab = cell.select_one(".color-card__label")
        if not lab:
            continue
        color_name = lab.get_text(" ", strip=True)
        if not color_name:
            continue

        raw_rel: list[str] = []
        for img in cell.select("img[src]"):
            src = (img.get("src") or "").strip()
            if not src:
                continue
            if "omnialogo" in src.lower() or "logo" in src.lower():
                continue
            if "/catalog/" in src or src.endswith((".webp", ".jpg", ".jpeg", ".png")):
                raw_rel.append(src)

        raw_abs = [urljoin(BASE_ORIGIN, r).strip() for r in raw_rel]
        raw_abs = dedupe_urls(raw_abs)

        room_urls = [u for u in raw_abs if re.search(r"/room\.(webp|jpg|jpeg|png)", u, re.I)]
        plank_sw = [
            u
            for u in raw_abs
            if re.search(r"/upside\.(webp|jpg|jpeg|png)(?:\?|$)", u, re.I)
            and not re.search(r"/upsideH\.(webp|jpg|jpeg|png)(?:\?|$)", u, re.I)
        ]
        herr_sw = [u for u in raw_abs if re.search(r"/upsideH\.(webp|jpg|jpeg|png)(?:\?|$)", u, re.I)]

        room_u = room_urls[0] if room_urls else ""

        plank_gallery: list[str] = []
        if plank_sw:
            plank_gallery = [plank_sw[0]] + ([room_u] if room_u else [])
        herr_gallery: list[str] = []
        if herr_sw:
            herr_gallery = [herr_sw[0]] + ([room_u] if room_u else [])

        if not plank_gallery and raw_abs:
            plank_gallery = [
                u for u in raw_abs if not re.search(r"/room\.(webp|jpg)", u, re.I)
            ] + [u for u in raw_abs if re.search(r"/room\.(webp|jpg)", u, re.I)]
            plank_gallery = dedupe_urls(plank_gallery)

        slug_src = next((u for u in raw_abs if "upside" in u.casefold() or "/colors/" in u), raw_abs[0])
        folder_slug = color_slug_from_catalog_src(slug_src) or re.sub(
            r"[^\w]+", "_", color_name.lower()
        ).strip("_")

        blocks.append(
            {
                "color_name": color_name,
                "folder_slug": folder_slug,
                "plank_gallery": plank_gallery,
                "herringbone_gallery": herr_gallery,
            }
        )

    return blocks


def load_links_csv(path: Path) -> pd.DataFrame:
    """
    Forward-fill inherited columns only. ``COD REFERINTA`` and ``Nume variante``
    are not filled from above — each variant row keeps its own code and format.
    """
    if not path.is_file():
        raise FileNotFoundError(f"Missing {path}")
    df = pd.read_csv(path, encoding="utf-8-sig")
    for col in ("Categorie", "Subcategorie", "Colectie", "Nume produs", "Link variante"):
        if col in df.columns:
            df[col] = df[col].ffill()
    return df


def variant_color_from_csv(row: pd.Series) -> str:
    """``Nume variante`` from the sheet only; ``Standard`` when blank (no em dash / no shade prefix)."""
    return clean_cell(row.get("Nume variante")) or "Standard"


def gallery_for_variant(block: dict[str, Any], nume_variante: str) -> list[str]:
    nv = norm_key(nume_variante)
    if not nv or nv in ("mat", "lucios"):
        g = block["plank_gallery"]
        return g if g else block.get("herringbone_gallery", [])
    if "herringbone" in nv:
        h = block["herringbone_gallery"]
        if h:
            return h
        return block["plank_gallery"]
    if nv == "plank":
        return block["plank_gallery"]
    return block["plank_gallery"]


def find_color_block(
    blocks: list[dict[str, Any]], nume_produs: str
) -> dict[str, Any] | None:
    nk = norm_key(nume_produs)
    for b in blocks:
        if norm_key(b["color_name"]) == nk:
            return b
    for b in blocks:
        if nk and nk in norm_key(b["color_name"]):
            return b
    return None


def canonical_collection_url(soup: BeautifulSoup, request_url: str) -> str:
    canon = soup.select_one('link[rel="canonical"]')
    if canon and canon.get("href"):
        return normalize_collection_url(urljoin(BASE_ORIGIN, canon["href"]))
    return normalize_collection_url(request_url)


def scrape() -> None:
    script_dir = Path(__file__).resolve().parent
    csv_path = script_dir / LINKS_CSV
    df = load_links_csv(csv_path)

    required = ("Link variante", "COD REFERINTA", "Nume produs")
    for c in required:
        if c not in df.columns:
            raise SystemExit(f"{LINKS_CSV} must include column: {c}")

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
    created_stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    page_cache: dict[str, tuple[BeautifulSoup, list[dict[str, Any]]]] = {}
    product_key_to_id: dict[tuple[str, str], int] = {}

    data_rows: list[pd.Series] = []
    for _, row in df.iterrows():
        link = clean_cell(row.get("Link variante"))
        cod = clean_cell(row.get("COD REFERINTA"))
        if not link or not cod:
            continue
        data_rows.append(row)

    unique_raw = list(
        dict.fromkeys(
            normalize_collection_url(normalize_space(str(r["Link variante"]).strip()))
            for r in data_rows
        )
    )

    for req_url in unique_raw:
        if req_url in page_cache:
            continue
        soup = fetch_soup(req_url, session)
        if not soup:
            continue
        final_url = canonical_collection_url(soup, req_url)
        blocks = extract_color_blocks(soup)
        page_cache[req_url] = (soup, blocks)
        if final_url != req_url:
            page_cache[final_url] = (soup, blocks)

    url_groups: dict[str, list[pd.Series]] = {}
    for row in data_rows:
        raw_u = normalize_collection_url(clean_cell(row.get("Link variante")).strip())
        final_u = raw_u
        if raw_u in page_cache:
            soup_guess = page_cache[raw_u][0]
            final_u = canonical_collection_url(soup_guess, raw_u)
        url_groups.setdefault(final_u, []).append(row)

    for page_url, rows in url_groups.items():
        print(f"\n=== {page_url} ===")
        if page_url not in page_cache:
            print("  skip (fetch failed)")
            continue
        soup, blocks = page_cache[page_url]

        if not blocks:
            print("  No color blocks found (check layout).")
            time.sleep(0.1)
            continue

        technical_docs = extract_technical_documents(soup)
        if technical_docs:
            print(f"  technical docs: {len(technical_docs)}")

        for row in rows:
            np = clean_cell(row.get("Nume produs"))
            nv = clean_cell(row.get("Nume variante"))
            cod = clean_cell(row.get("COD REFERINTA"))
            key = (page_url, norm_key(np))

            if key not in product_key_to_id:
                categorie = clean_cell(row.get("Categorie"))
                colectie = clean_cell(row.get("Colectie"))
                subcategorie = clean_cell(row.get("Subcategorie"))
                shade_rows = [
                    r
                    for r in rows
                    if norm_key(clean_cell(r.get("Nume produs"))) == norm_key(np)
                ]
                variant_type_labels = sorted(
                    {
                        clean_cell(r.get("Nume variante"))
                        for r in shade_rows
                        if clean_cell(r.get("Nume variante"))
                    },
                    key=lambda s: s.casefold(),
                )
                product_row = extract_product_row(
                    soup,
                    page_url,
                    np,
                    categorie,
                    colectie,
                    subcategorie,
                    variant_type_labels,
                )
                product_row["id"] = p_id
                products_db.append(product_row)
                product_key_to_id[key] = p_id
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
                print(f"  product id={p_id} | shade {np!r}")
                p_id += 1

            current_pid = product_key_to_id[key]

            blk = find_color_block(blocks, np)
            if not blk:
                print(f"  WARN no swatch for shade {np!r} (COD {cod})")
                gallery_urls: list[str] = []
            else:
                gallery_urls = gallery_for_variant(blk, nv)

            sku = sanitize_filename(cod.replace("  ", " "))
            color_disp = variant_color_from_csv(row)

            variants_db.append(
                {
                    "id": v_id,
                    "product_id": current_pid,
                    "sku": sku,
                    "color": color_disp,
                    "url": page_url,
                    "gallery_photos": json.dumps(gallery_urls, ensure_ascii=False),
                    "technical_photos": json.dumps([], ensure_ascii=False),
                }
            )
            print(f"  variant {sku} | {color_disp} | {len(gallery_urls)} imgs")
            v_id += 1
            time.sleep(0.05)

        time.sleep(0.1)

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


if __name__ == "__main__":
    scrape()
