"""
41zero42 — collection pages from ``links.csv`` (41zero42.com).

Each spreadsheet row is one variant; rows sharing ``Colectie`` + ``Nume produs`` (after ffill)
produce one ``products.csv`` row and multiple ``variants.csv`` rows.

- Gallery images: all ``a.immagine-lightbox[href]`` under ``article.collezioni .entry-content``
  (main flexslider plus masonry/gallery tiles — not only ``#slider`` slides).
- Copy: ``article.collezioni .entry-content p``
- Specs: ``div.scheda-tecnica`` — Technology → ``material``, Finish → ``finishes``, Thickness → ``thickness``
- Technical assets: ``div.scheda-tecnica div.downloads a.info-tech`` — Info-tech PDF + CAD ``.zip`` only
  (skips catalogues, videos, external configurators)

Variant galleries: lead with CDN hero ``cdn.altrodesign.ro/products/{name}.jpg`` when the filename is known,
then all lightbox images from the page (same set for every variant of that product unless a variant URL differs).

SKUs: ``41Z-XXXX`` — if ``SKU`` column is set, output ``41Z-{value}``; else ``41Z-{PP}{VV}`` (product index,
variant index within product, two digits each).

All numeric CSV ids start at **80000** for this scraper.
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

MANUFACTURER = "41zero42"
LINKS_CSV = "links.csv"

COL_BRAND = "SUB-SUBCATEGORIE/BRAND"
COL_VARIANT = "Nume variante/SUBTITLU"
COL_SKU = "SKU"

START_ID = 80000
START_PRODUCT_ID = START_ID
START_VARIANT_ID = START_ID
START_TECH_PDF_ID = START_ID
START_PRODUCT_PDF_ID = START_ID

CDN_BASE = "https://cdn.altrodesign.ro/products/"

# Filenames from ``hints.txt`` (CDN/R2); ``nano gap-*.jpg`` uses a space after ``nano``.
_R2_FILENAMES_RAW = (
    "biscuit-bianco.jpg,biscuit-bordeaux.jpg,biscuit-notte.jpg,biscuit-powder.jpg,biscuit-salvia.jpg,"
    "biscuit-terra.jpg,cosmo-brick-lux-blu.jpg,cosmo-brick-lux-cotto.jpg,cosmo-brick-lux-grigio.jpg,"
    "cosmo-brick-lux-nero.jpg,cosmo-brick-lux-verde.jpg,cosmo-brick-matte-blu.jpg,cosmo-brick-matte-cotto.jpg,"
    "cosmo-brick-matte-grigio.jpg,cosmo-brick-matte-nero.jpg,cosmo-brick-matte-verde.jpg,futura-bird.jpg,"
    "futura-black.jpg,futura-blue.jpg,futura-drop-black.jpg,futura-drop-blue.jpg,futura-drop-rose.jpg,"
    "futura-drop-white.jpg,futura-grey.jpg,futura-grid-black.jpg,futura-grid-blue.jpg,futura-grid-rose.jpg,"
    "futura-grid-white.jpg,futura-half-black.jpg,futura-half-blue.jpg,futura-half-rose.jpg,futura-half-white.jpg,"
    "futura-microchip.jpg,futura-rose.jpg,futura-t-blue.jpg,futura-triangles.jpg,futura-t-rose.jpg,"
    "futura-tubes.jpg,futura-t-white.jpg,futura-white.jpg,milano70-cacao.jpg,milano70-cognac.jpg,"
    "milano70-gold.jpg,milano70-olive.jpg,milano70-peacock.jpg,mou-butter-glossy.jpg,mou-butter-matte.jpg,"
    "mou-butter-mix-glossy.jpg,mou-butter-mix-matte.jpg,mou-caramel-glossy.jpg,mou-caramel-matte.jpg,"
    "mou-caramel-mix-glossy.jpg,mou-caramel-mix-matte.jpg,mou-milk-glossy.jpg,mou-milk-matte.jpg,"
    "mou-milk-mix-glossy.jpg,mou-milk-mix-matte.jpg,nano gap-bianco.jpg,nano gap-grigio.jpg,nano gap-nero.jpg,"
    "nok-ebony.jpg,nok-grains-ebony.jpg,nok-grains-ivory.jpg,nok-grains-taupe.jpg,nok-grains-terra.jpg,"
    "nok-ivory.jpg,nok-snake-ebony.jpg,nok-snake-ivory.jpg,nok-snake-taupe.jpg,nok-snake-terra.jpg,nok-taupe.jpg,"
    "nok-terra.jpg,nok-totem-ebony.jpg,nok-totem-ivory.jpg,nok-totem-taupe.jpg,nok-totem-terra.jpg,"
    "pixel41-almond.jpg,pixel41-antrax.jpg,pixel41-black.jpg,pixel41-blush.jpg,pixel41-bordeaux.jpg,"
    "pixel41-celadon.jpg,pixel41-cerulean.jpg,pixel41-cloud.jpg,pixel41-coral.jpg,pixel41-curry.jpg,"
    "pixel41-frog.jpg,pixel41-grey.jpg,pixel41-khaki.jpg,pixel41-lemon.jpg,pixel41-lobster.jpg,"
    "pixel41-marine.jpg,pixel41-military.jpg,pixel41-mint.jpg,pixel41-mud.jpg,pixel41-musk.jpg,"
    "pixel41-notte.jpg,pixel41-nude.jpg,pixel41-nut.jpg,pixel41-ocean.jpg,pixel41-peacock.jpg,pixel41-pearl.jpg,"
    "pixel41-pool.jpg,pixel41-powder.jpg,pixel41-purple.jpg,pixel41-red.jpg,pixel41-rose.jpg,pixel41-salvia.jpg,"
    "pixel41-sand.jpg,pixel41-sky.jpg,pixel41-strawberry.jpg,pixel41-terra.jpg,pixel41-tobacco.jpg,"
    "pixel41-tuareg.jpg,pixel41-vanilla.jpg,pixel41-violet.jpg,pixel41-white.jpg,rigo-black.jpg,rigo-grey.jpg,"
    "rigo-mud.jpg,rigo-white.jpg,stories-caravan.jpg,stories-costiera.jpg,stories-dance.jpg,stories-eddie.jpg,"
    "stories-white.jpg,wigwag-black.jpg,wigwag-grey.jpg,wigwag-mud.jpg,wigwag-white.jpg"
)

R2_KNOWN_FILENAMES = frozenset(normalize_space(x) for x in _R2_FILENAMES_RAW.split(",") if normalize_space(x))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def slug_part(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.casefold().strip()).strip("-")


def variant_r2_jpg_candidates(colectie: str, variante: str) -> list[str]:
    """Hyphen filename and ``nano gap-bianco``-style variant with space in collection prefix."""
    sc = slug_part(colectie)
    sv = slug_part(variante)
    if sv.startswith(sc + "-"):
        sv = sv[len(sc) + 1 :]
    hyphen = f"{sc}-{sv}.jpg"
    spaced = f"{sc.replace('-', ' ')}-{sv}.jpg"
    if hyphen == spaced:
        return [hyphen]
    return [hyphen, spaced]


def mou_r2_filename(variante: str) -> str | None:
    """Map CSV titles like ``MILK MATTE MIX`` to R2 names ``mou-milk-mix-matte.jpg``."""
    parts = [p.casefold() for p in re.split(r"\s+", clean_cell(variante)) if p.strip()]
    if not parts:
        return None
    flavor = next((f for f in ("milk", "butter", "caramel") if f in parts), None)
    if not flavor:
        return None
    has_mix = "mix" in parts
    finish = "matte" if "matte" in parts else "glossy" if "glossy" in parts else None
    if not finish:
        return None
    if has_mix:
        name = f"mou-{flavor}-mix-{finish}.jpg"
    else:
        name = f"mou-{flavor}-{finish}.jpg"
    return name if name in R2_KNOWN_FILENAMES else None


def cdn_hero_url(colectie: str, variante: str) -> str | None:
    if slug_part(colectie) == "mou":
        mou_fn = mou_r2_filename(variante)
        if mou_fn:
            return CDN_BASE + mou_fn
    for name in variant_r2_jpg_candidates(colectie, variante):
        if name in R2_KNOWN_FILENAMES:
            return CDN_BASE + name
    return None


def normalize_page_url(url: str) -> str:
    return clean_cell(url).split("#")[0].strip()


def normalize_asset_url(page_url: str, raw: str) -> str:
    raw = (raw or "").strip().strip("'\"")
    if not raw:
        return ""
    u = urljoin(page_url, raw)
    return urlparse(u)._replace(fragment="").geturl()


def fetch_soup(url: str, session: requests.Session) -> BeautifulSoup | None:
    try:
        r = session.get(url, timeout=35)
        if r.status_code != 200:
            print(f"  HTTP {r.status_code} for {url}")
            return None
        return BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        print(f"  Request failed {url}: {e}")
        return None


def page_main(soup: BeautifulSoup) -> BeautifulSoup | Tag:
    return soup.select_one("main#main") or soup.select_one("main") or soup.body or soup


def slider_image_urls(main_el: BeautifulSoup | Tag, page_url: str) -> list[str]:
    """Every product lightbox in the article (flexslider + masonry), in DOM order."""
    root = main_el.select_one("article.collezioni .entry-content") or main_el
    out: list[str] = []
    for a in root.select("a.immagine-lightbox[href]"):
        href = (a.get("href") or "").strip()
        if href:
            out.append(normalize_asset_url(page_url, href))
    return dedupe_urls(out)


def extract_description(soup: BeautifulSoup) -> str:
    ps = soup.select("article.collezioni .entry-content p")
    texts = [
        normalize_space(p.get_text(" ", strip=True))
        for p in ps
        if normalize_space(p.get_text(" ", strip=True))
    ]
    return "\n\n".join(texts[:8])


def parse_scheda_tecnica(scheda: Tag | None) -> tuple[str, str, str, str]:
    """material (Technology), finishes (Finish), thickness; collection title from ``h3.titolo-collezione``."""
    if not scheda:
        return "", "", "", ""
    tit = scheda.select_one("h3.titolo-collezione")
    coll_t = normalize_space(tit.get_text(" ", strip=True)) if tit else ""
    material = finishes = thickness = ""
    for h3 in scheda.select("h3.intestazione-campo"):
        label = normalize_space(h3.get_text(" ", strip=True)).casefold()
        sp = h3.find_next_sibling("span", class_="specifica")
        val = normalize_space(sp.get_text(" ", strip=True)) if sp else ""
        if label == "technology":
            material = val
        elif label == "finish":
            finishes = val
        elif label == "thickness":
            thickness = val
    return material, finishes, thickness, coll_t


def technical_download_links(scheda: Tag | None, page_url: str) -> list[dict[str, str]]:
    """Info-tech PDF + CAD zip only."""
    if not scheda:
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for a in scheda.select("div.downloads a.info-tech[href]"):
        href = (a.get("href") or "").strip()
        low = href.casefold()
        if not low.startswith("http"):
            continue
        if "41zero42.com" not in urlparse(href).netloc.casefold():
            continue
        info_el = a.select_one("span.info-text")
        label = normalize_space(info_el.get_text(" ", strip=True)) if info_el else ""
        lc = label.casefold()
        if "catalogue" in lc or lc == "digital catalogue" or lc == "general catalogue":
            continue
        if low.endswith(".mp4"):
            continue
        if low.endswith(".pdf"):
            if "info tech" not in lc and "info-tech" not in low and "info_tech" not in low:
                continue
        elif low.endswith(".zip"):
            if "cad" not in lc and "cad" not in low and "texture" not in lc:
                continue
        else:
            continue
        u = normalize_asset_url(page_url, href)
        if not u or u in seen:
            continue
        seen.add(u)
        out.append({"url": u, "label": label})
    return out


def asset_csv_title(url: str, product_title: str) -> str:
    pt = clean_cell(product_title) or "Product"
    if url.casefold().endswith(".zip"):
        return f"CAD textures - {pt}"
    return f"Technical info - {pt}"


def variant_gallery_urls(
    colectie: str,
    variante: str,
    slider_urls: list[str],
    *,
    limit: int = 48,
) -> list[str]:
    hero = cdn_hero_url(colectie, variante)
    merged = dedupe_urls(([hero] if hero else []) + slider_urls)
    return merged[:limit]


def load_links(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, encoding="utf-8-sig")
    for col in ("Categorie", "Subcategorie", COL_BRAND, "Colectie", "Nume produs", "Link variante"):
        if col in df.columns:
            df[col] = df[col].ffill()
    return df


def csv_product_title(row: pd.Series) -> str:
    return format_csv_title(clean_cell(row.get("Nume produs")), clean_cell(row.get("Colectie")))


def product_group_key(row: pd.Series) -> tuple[str, str]:
    return (clean_cell(row.get("Colectie")), clean_cell(row.get("Nume produs")))


def sku_for_variant(coll_seq: int, var_seq: int, csv_sku_raw: Any) -> str:
    raw = clean_cell(csv_sku_raw)
    if raw:
        if raw.upper().startswith("41Z-"):
            return raw
        return f"41Z-{raw}"
    return f"41Z-{coll_seq:02d}{var_seq:02d}"


def skus_for_rows(rows: list[pd.Series]) -> list[str]:
    out: list[str] = []
    coll_seq = 0
    var_seq = 0
    prev_key: tuple[str, str] | None = None
    for row in rows:
        key = product_group_key(row)
        if key != prev_key:
            coll_seq += 1
            var_seq = 0
            prev_key = key
        var_seq += 1
        sku_raw = row.get(COL_SKU) if COL_SKU in row.index else ""
        out.append(sku_for_variant(coll_seq, var_seq, sku_raw))
    return out


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

    row_skus = skus_for_rows(rows_out)

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
        for cand in group:
            u = normalize_page_url(clean_cell(cand.get("Link variante")))
            s = fetch_soup(u, session)
            time.sleep(0.06)
            if s:
                soup0 = s
                url0 = u
                break

        if not soup0:
            print(
                f"\n=== SKIP product group {clean_cell(base_row.get('Colectie'))!r} / "
                f"{clean_cell(base_row.get('Nume produs'))!r} (no working URL) ==="
            )
            row_flat_index += len(group)
            continue

        main0 = page_main(soup0)
        scheda = main0.select_one("div.scheda-tecnica")
        material, finishes, thickness, _coll_scheda = parse_scheda_tecnica(scheda)

        categorie = clean_cell(base_row.get("Categorie"))
        subcategorie = clean_cell(base_row.get("Subcategorie"))
        colectie = clean_cell(base_row.get("Colectie"))

        row_dict: dict[str, Any] = {
            "title": csv_product_title(base_row),
            "description": extract_description(soup0),
            "category": normalize_category(categorie),
            "type": subcategorie,
            "collection": colectie,
            "is_new": False,
            "subtype": "",
            "manufacturer": MANUFACTURER,
            "catalog_id": None,
            "finishes": finishes,
            "position": "",
            "sizes": "",
            "thickness": thickness,
            "material": material,
            "shape": "",
            "cut": "",
            "diameter": "",
            "length": "",
            "width": "",
            "height": "",
            "id": p_id,
        }
        products_db.append(row_dict)

        docs = technical_download_links(scheda, url0)
        pt = clean_cell(row_dict.get("title", ""))
        for sort_i, doc in enumerate(docs):
            u = doc["url"]
            if u not in pdf_url_to_id:
                pdf_url_to_id[u] = pdf_id_counter
                pdf_title = asset_csv_title(u, pt)
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

        slider_urls = slider_image_urls(main0, url0)

        print(
            f"\n  product id={p_id} | {row_dict['title']!r} | {len(group)} variants | "
            f"slider imgs={len(slider_urls)} | tech downloads={len(docs)}"
        )

        for row in group:
            sku = row_skus[row_flat_index]
            row_flat_index += 1
            variante = clean_cell(row.get(COL_VARIANT))
            url = normalize_page_url(clean_cell(row.get("Link variante")))
            col_name = default_color(variante.strip().title() if variante else "")

            if url == url0:
                main_v = main0
            else:
                soup_v = fetch_soup(url, session)
                time.sleep(0.06)
                main_v = page_main(soup_v) if soup_v else None

            if main_v and url != url0:
                slider_v = slider_image_urls(main_v, url)
                imgs = variant_gallery_urls(colectie, variante, slider_v if slider_v else slider_urls)
            else:
                imgs = variant_gallery_urls(colectie, variante, slider_urls)

            variants_db.append(
                {
                    "id": v_id,
                    "product_id": p_id,
                    "sku": sku,
                    "color": col_name,
                    "url": url,
                    "gallery_photos": json.dumps(imgs, ensure_ascii=False),
                    "technical_photos": json.dumps([], ensure_ascii=False),
                }
            )
            print(f"    variant {sku} | {col_name} | imgs={len(imgs)} | cdn={'yes' if cdn_hero_url(colectie, variante) else 'no'}")
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
            "Close open CSV exports and retry.\n"
            f"Target folder: {out_dir}\n"
            f"System message: {err}",
            file=sys.stderr,
        )
        raise SystemExit(1) from err

    print(
        f"\nDone. {len(products_db)} products, {len(variants_db)} variants, "
        f"{len(technical_pdfs_db)} technical rows, {total_gallery_count(variants_db)} gallery URLs -> {out_dir}"
    )


def main() -> None:
    script_dir = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--output-dir", type=Path, default=None, metavar="DIR")
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
