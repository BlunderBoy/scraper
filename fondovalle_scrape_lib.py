"""
HTML parsing for Fondovalle (fondovalle.it) product pages.

Same theme patterns as Bathco: accordions (``data-hash``), single-product layout.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from scraper_brand_utils import clean_cell, norm_key, normalize_space

BASE_ORIGIN = "https://fondovalle.it"

RE_WP_SIZE = re.compile(r"-\d+x\d+(?=\.[a-zA-Z]{2,5}(?:\?|$))")

# ``WxH cm`` on product pages; thickness = last ``… mm`` before each pair (inch fractions may sit between).
RE_FV_WH_CM = re.compile(r"(?P<w>\d+)\s*[x×]\s*(?P<h>\d+)\s*cm", re.I)
RE_FV_MM_TOKEN = re.compile(r"(\d+[,.]?\d*)\s*mm", re.I)


def _fv_mm_token_to_cm_str(mm_raw: str) -> str:
    s = mm_raw.replace(",", ".")
    try:
        v = float(s) / 10.0
    except ValueError:
        return mm_raw
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    t = f"{v:.3f}".rstrip("0").rstrip(".")
    return t


def extract_sizes_wxh_thickness_cm_from_text(blob: str) -> list[str]:
    """Strip finish headings; keep ``WxHxT cm`` (thickness from the last ``mm`` token before each ``WxH cm``)."""
    out: list[str] = []
    seen: set[str] = set()
    text = blob or ""
    for m in RE_FV_WH_CM.finditer(text):
        w, h = m.group("w"), m.group("h")
        start = max(0, m.start() - 140)
        prefix = text[start : m.start()]
        mm_matches = list(RE_FV_MM_TOKEN.finditer(prefix))
        if not mm_matches:
            continue
        th = mm_matches[-1].group(1)
        tcm = _fv_mm_token_to_cm_str(th)
        tok = f"{w}x{h}x{tcm} cm"
        if tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


def extract_sizes_wxh_thickness_cm_from_soup(soup: BeautifulSoup) -> list[str]:
    root = soup.select_one(".color-det") or soup.select_one("body") or soup
    return extract_sizes_wxh_thickness_cm_from_text(root.get_text(" ", strip=True))


def _fmt_mm_str(mm_raw: str) -> str:
    s = (mm_raw or "").replace(",", ".")
    try:
        v = float(s)
    except ValueError:
        return (mm_raw or "").strip()
    if abs(v - round(v)) < 1e-9:
        return f"{int(round(v))} mm"
    t = f"{v:.3f}".rstrip("0").rstrip(".")
    return f"{t} mm"


def extract_sizes_and_thicknesses_cm_from_text(blob: str) -> tuple[list[str], list[str]]:
    """Return ``(["120x60 cm", ...], ["8.5 mm", ...])`` from a Fondovalle text blob.

    ``sizes`` lists ``WxH cm`` formats with no thickness suffix; ``thicknesses``
    lists each unique ``mm`` value found right before a ``WxH cm`` token.
    """
    sizes: list[str] = []
    thicks: list[str] = []
    seen_size: set[str] = set()
    seen_thick: set[str] = set()
    text = blob or ""
    for m in RE_FV_WH_CM.finditer(text):
        w, h = m.group("w"), m.group("h")
        start = max(0, m.start() - 140)
        prefix = text[start : m.start()]
        mm_matches = list(RE_FV_MM_TOKEN.finditer(prefix))
        size_tok = f"{w}x{h} cm"
        if size_tok not in seen_size:
            seen_size.add(size_tok)
            sizes.append(size_tok)
        if mm_matches:
            th_raw = mm_matches[-1].group(1)
            th = _fmt_mm_str(th_raw)
            if th and th not in seen_thick:
                seen_thick.add(th)
                thicks.append(th)
    return sizes, thicks


def extract_sizes_and_thicknesses_cm_from_soup(
    soup: BeautifulSoup,
) -> tuple[list[str], list[str]]:
    root = soup.select_one(".color-det") or soup.select_one("body") or soup
    return extract_sizes_and_thicknesses_cm_from_text(root.get_text(" ", strip=True))


def decorate_fondovalle_technical_pdf_titles(
    docs: list[dict[str, str]],
    *,
    product_title: str,
    collection: str,
) -> None:
    """In-place: stable titles (collection vs product context for generic anchors)."""
    pt = clean_cell(product_title)
    col = clean_cell(collection)
    for doc in docs:
        url = (doc.get("url") or "").strip()
        url_l = url.lower()
        raw = normalize_space(doc.get("title", ""))
        if "download-document" in url_l:
            if col:
                doc["title"] = f"Technical data sheet — {col}"
            elif pt:
                doc["title"] = f"Technical data sheet — {pt}"
            else:
                doc["title"] = raw or "Technical data sheet"
            continue
        if raw and pt and pt.casefold() not in raw.casefold():
            doc["title"] = f"{raw} — {pt}"
        elif raw:
            doc["title"] = raw
        elif pt:
            doc["title"] = f"{col} — {pt}" if col else pt
        else:
            doc["title"] = _pdf_title_from_url(url)

_BAD_FV_PDF_BLOB = (
    "privacy",
    "cookie",
    "gdpr",
    "ethics",
    "sales conditions",
    "general_terms",
    "disclaimer",
    "copywright",
    "copyright",
    "italcer-spa_codi",
    "lettera_autorizz",
    "suppliers & clients",
)


def _is_junk_fondovalle_pdf(url: str, title: str) -> bool:
    blob = f"{url} {title}".lower()
    return any(x in blob for x in _BAD_FV_PDF_BLOB)


def collection_parent_url(page_url: str) -> str | None:
    """
    Parent collection URL when ``page_url`` is a variant (one extra segment under ``/…/products/``).

    Example: ``…/products/homescape/sugar-homescape-standard/`` → ``…/products/homescape/``.
    """
    raw = (page_url or "").strip().split("#", 1)[0].strip()
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        return None
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    try:
        idx = next(i for i, p in enumerate(parts) if p.casefold() == "products")
    except StopIteration:
        return None
    tail = parts[idx + 1 :]
    if len(tail) < 2:
        return None
    parent_parts = parts[: idx + 1] + tail[:-1]
    new_path = "/" + "/".join(parent_parts) + "/"
    return parsed._replace(path=new_path, query="", fragment="").geturl()


def parse_tech_info(soup: BeautifulSoup | None) -> dict[str, str]:
    """Key/value rows from ``.tech-info`` (collection pages, fondovalle.it)."""
    if soup is None:
        return {}
    out: dict[str, str] = {}
    for it in soup.select(".tech-info-item"):
        lab_el = it.select_one(".tech-info-item__label")
        val_el = it.select_one(".tech-info-item__value")
        if not lab_el:
            continue
        k = lab_el.get_text(" ", strip=True).rstrip(":")
        v = val_el.get_text(" ", strip=True) if val_el else ""
        if k:
            out[k] = v
    return out


def description_collection_hero(soup: BeautifulSoup) -> str:
    """Marketing paragraphs on ``body.coll-det`` collection pages."""
    root = soup.select_one("section.coll-det__hero") or soup.select_one(".coll-det__hero")
    if root:
        paras: list[str] = []
        for p in root.select("p"):
            t = normalize_space(p.get_text(" ", strip=True))
            if t and t not in paras:
                paras.append(t)
        if paras:
            return "\n\n".join(paras)
    body = soup.select_one("body") or soup
    classes = " ".join(body.get("class") or [])
    if "coll-det" not in classes:
        return ""
    for sec in body.select("section"):
        chunks: list[str] = []
        for p in sec.select("p"):
            t = normalize_space(p.get_text(" ", strip=True))
            if len(t) > 60:
                chunks.append(t)
        if chunks:
            return "\n\n".join(dict.fromkeys(chunks))
    return ""


def extract_download_document_technical(soup: BeautifulSoup) -> list[dict[str, str]]:
    """``/downloads/download-document/…`` anchors (often PDF responses) linked as technical sheets."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    needles = (
        "technical",
        "scheda",
        "schede",
        "datasheet",
        "data-sheet",
        "ficha",
        "certificat",
        "ce-marking",
    )
    for a in soup.select('a[href*="download-document"]'):
        href = (a.get("href") or "").strip()
        if "/downloads/download-document/" not in href:
            continue
        label = normalize_space(a.get_text(" ", strip=True))
        low = f"{label} {href}".lower()
        if not any(n in low for n in needles):
            continue
        abs_url = urljoin(BASE_ORIGIN, href)
        if abs_url in seen or _is_junk_fondovalle_pdf(abs_url, label):
            continue
        seen.add(abs_url)
        out.append({"url": abs_url, "title": label or "Technical document"})
    return out


def wp_full_size(url: str) -> str:
    base = url.split("?", 1)[0]
    return RE_WP_SIZE.sub("", base)


def _is_product_image_url(src: str) -> bool:
    low = src.lower()
    if "wishlist" in low or "logo.svg" in low or "lazy_placeholder" in low:
        return False
    return (
        "wp-content/uploads" in low
        or "fondovalle-media" in low
        or "storage.googleapis.com" in low
    )


def hero_title(soup: BeautifulSoup) -> str:
    h2 = soup.select_one("header.head h2")
    if h2:
        return h2.get_text(" ", strip=True)
    h1 = soup.select_one("main h1")
    if h1:
        return h1.get_text(" ", strip=True)
    h1b = soup.select_one(".color-det h1") or soup.select_one("body.color-det h1")
    if h1b:
        return h1b.get_text(" ", strip=True)
    h1c = soup.select_one("h1")
    if h1c:
        return h1c.get_text(" ", strip=True)
    t = soup.title.string if soup.title else ""
    if t:
        return normalize_space(t.split("|")[0].replace(" | Ceramica Fondovalle", "").strip())
    return ""


def collapse_section_for_hash(soup: BeautifulSoup, data_hash: str) -> Tag | None:
    trigger = soup.select_one(f'a.collapse-link[data-hash="{data_hash}"]')
    if not trigger:
        return None
    href = (trigger.get("href") or "").strip()
    if href.startswith("#"):
        return soup.select_one(href)
    return None


def description_from_descripcion_panel(soup: BeautifulSoup) -> str:
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
        if not src or not _is_product_image_url(src):
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
    return normalize_space(sizes), tech_urls


def compose_product_title(soup: BeautifulSoup, nume_produs: str) -> str:
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
    """Gallery URLs from OG meta, hero figures, and CDN images (GCS + WP uploads)."""
    seen: set[str] = set()
    out: list[str] = []

    def push_abs(src: str) -> None:
        src = (src or "").strip()
        if not src or not _is_product_image_url(src):
            return
        u = wp_full_size(urljoin(BASE_ORIGIN, src))
        if u not in seen:
            seen.add(u)
            out.append(u)

    og = soup.select_one('meta[property="og:image"]')
    if og and og.get("content"):
        push_abs(og["content"].strip())

    main = soup.select_one("main") or soup
    for img in main.select("figure img, .img-holder img, div.img img, header.head img"):
        if _ancestor_class_contains(img, "color-item"):
            continue
        if _ancestor_class_contains(img, "similar-products"):
            continue
        if _ancestor_class_contains(img, "version-list"):
            continue
        src = (img.get("src") or img.get("data-src") or "").strip()
        push_abs(src)

    if not out:
        for img in soup.select("img"):
            if _ancestor_class_contains(img, "color-item"):
                continue
            src = (img.get("src") or img.get("data-src") or "").strip()
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
    """
    Technical PDFs / document URLs only — never the whole ``main`` tree (footer legal PDFs).

    Uses: ``download-document`` technical links (new theme) and the legacy ``descargas`` accordion.
    """
    out: list[dict[str, str]] = []
    seen: set[str] = set()

    def push(abs_url: str, title: str) -> None:
        if abs_url in seen or _is_junk_fondovalle_pdf(abs_url, title):
            return
        seen.add(abs_url)
        out.append({"url": abs_url, "title": title})

    for doc in extract_download_document_technical(soup):
        push(doc["url"], doc["title"])

    root = collapse_section_for_hash(soup, "descargas")
    if root is not None:
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
            label = a.get_text(" ", strip=True)
            low_label = label.casefold()
            if "inicia sesión" in low_label or "regístrate" in low_label or label.lower() in ("login", "register"):
                continue
            title = label if label else _pdf_title_from_url(abs_url)
            if _is_junk_fondovalle_pdf(abs_url, title):
                continue
            push(abs_url, title)
    return out


def merge_technical_documents(
    variant_soup: BeautifulSoup,
    collection_soup: BeautifulSoup | None,
) -> list[dict[str, str]]:
    """Deduped union of technical docs from variant and collection pages."""
    seen: set[str] = set()
    merged: list[dict[str, str]] = []
    for src in (variant_soup, collection_soup):
        if src is None:
            continue
        for doc in extract_technical_documents(src):
            u = doc["url"]
            if u not in seen:
                seen.add(u)
                merged.append(doc)
    return merged


def infer_material(description: str, category: str, type_: str, subtype: str) -> str:
    blob = f"{description} {category} {type_} {subtype}".lower()
    if "porcelain" in blob or "porcelain" in description.lower():
        return "Porcelain"
    if "stone" in blob or "ceramic" in blob:
        return "Ceramic"
    if "wood" in blob:
        return "Wood"
    if "metal" in blob:
        return "Metal"
    return ""


def infer_position(category: str, type_: str, subtype: str) -> str:
    blob = f"{category} {type_} {subtype}".lower()
    if "wall" in blob:
        return "Wall"
    if "floor" in blob:
        return "Floor"
    return ""


def normalize_fondovalle_ceramice_finishes_separators(finishes: str) -> str:
    """Ceramice ``finishes``: comma-separated only (hyphen/en-dash used as separators → ``, ``)."""
    if not clean_cell(finishes):
        return ""
    parts = re.split(r"\s*[,;]\s*|\s+-\s+|\s*[–—]\s*", finishes)
    bits = [normalize_space(p) for p in parts if clean_cell(normalize_space(p))]
    return ", ".join(dict.fromkeys(bits))


def extract_product_row_common(
    soup: BeautifulSoup,
    categorie: str,
    subcategorie: str,
    sub_sub: str,
    colectie: str,
    nume_produs: str,
    finishes_labels: list[str],
    *,
    manufacturer: str,
    title_csv: str | None = None,
    sizes_csv: str | None = None,
    description_csv: str | None = None,
    collection_soup: BeautifulSoup | None = None,
    fondovalle_ceramice: bool = False,
) -> dict[str, Any]:
    t_csv = clean_cell(title_csv) if title_csv is not None else ""
    title = t_csv or compose_product_title(soup, nume_produs)
    description = description_from_descripcion_panel(soup)
    sheet_notes = clean_cell(description_csv) if description_csv is not None else ""
    if sheet_notes:
        description = (
            f"{sheet_notes}\n\n{description}".strip() if description else sheet_notes
        )
    if collection_soup is not None:
        ch = description_collection_hero(collection_soup)
        if ch:
            if description and ch not in description and description not in ch:
                description = f"{ch}\n\n{description}".strip()
            elif not description:
                description = ch
    specs = parse_dl_specs(soup)
    specs_c = parse_tech_info(collection_soup) if collection_soup is not None else {}
    dim_sizes, _ = parse_dimensions_panel(soup)
    sizes_csv_clean = clean_cell(sizes_csv) if sizes_csv is not None else ""
    formats_c = clean_cell(specs_c.get("Formats", ""))

    if fondovalle_ceramice:
        size_tokens, thick_tokens = extract_sizes_and_thicknesses_cm_from_soup(soup)
        if not size_tokens:
            size_tokens, thick_tokens = extract_sizes_and_thicknesses_cm_from_text(
                " ".join(x for x in (sizes_csv_clean, dim_sizes) if x)
            )
        if not size_tokens:
            specs_dims = " ".join(
                x
                for x in (
                    clean_cell(specs.get("Dimensions", "")),
                    clean_cell(specs.get("Size", "")),
                    clean_cell(specs.get("Sizes", "")),
                )
                if x
            )
            size_tokens, thick_tokens = extract_sizes_and_thicknesses_cm_from_text(specs_dims)
        sizes = ", ".join(dict.fromkeys(size_tokens))
        ceramice_thickness = ", ".join(dict.fromkeys(thick_tokens))
        sub_out = ""
    else:
        size_candidates = [
            sizes_csv_clean,
            dim_sizes,
            formats_c,
            clean_cell(specs.get("Dimensions", "")),
            clean_cell(specs.get("Size", "")),
            clean_cell(specs.get("Sizes", "")),
        ]
        size_candidates = [normalize_space(x) for x in size_candidates if clean_cell(x)]
        sizes = " | ".join(list(dict.fromkeys(size_candidates)))
        sub_out = sub_sub

    if fondovalle_ceramice:
        thickness = (
            ceramice_thickness
            or clean_cell(specs.get("Thickness", ""))
            or clean_cell(specs_c.get("Thickness", ""))
            or ""
        )
    else:
        thickness = (
            clean_cell(specs.get("Thickness", ""))
            or clean_cell(specs_c.get("Thickness", ""))
            or ""
        )

    surface_s = clean_cell(specs_c.get("Surface", "")) or clean_cell(parse_tech_info(soup).get("Surface", ""))
    csv_fin_list = [normalize_space(x).title() for x in finishes_labels if clean_cell(x)]
    if fondovalle_ceramice:
        fin_parts: list[str] = []
        if surface_s:
            fin_parts.append(surface_s)
        surf_low = surface_s.casefold()
        for bit in csv_fin_list:
            if bit and bit.casefold() not in surf_low:
                fin_parts.append(bit)
        finishes = normalize_fondovalle_ceramice_finishes_separators(
            ", ".join(dict.fromkeys(fin_parts))
        )
    else:
        finishes = ", ".join(csv_fin_list)

    return {
        "title": title,
        "description": description,
        "category": categorie.lower() if categorie else "",
        "type": subcategorie,
        "collection": colectie,
        "is_new": False,
        "subtype": sub_out,
        "manufacturer": manufacturer,
        "catalog_id": None,
        "finishes": finishes,
        "position": infer_position(categorie, subcategorie, sub_sub),
        "sizes": sizes,
        "thickness": thickness,
        "material": infer_material(description, categorie, subcategorie, sub_sub),
        "shape": "",
        "cut": "",
        "diameter": "",
        "length": "",
        "width": "",
        "height": "",
    }


def fondovalle_variant_color(row: Any) -> str:
    """Title case from ``Variante culori`` or ``Nume variante/SUBTITLU``."""
    vc = clean_cell(row.get("Variante culori"))
    if vc:
        return normalize_space(vc.title())
    nv = clean_cell(row.get("Nume variante/SUBTITLU"))
    if nv:
        return normalize_space(nv.title())
    return "Standard"
