#!/usr/bin/env python3
"""Reorganize manufacturer CSVs: one product per collection, colors as variants."""

import argparse
import csv
import shutil
from collections import OrderedDict
from pathlib import Path

PRODUCTS_HEADER = [
    "id", "title", "description", "category", "type", "collection", "is_new",
    "subtype", "manufacturer", "catalog_id", "finishes", "position", "sizes",
    "thickness", "material", "shape", "cut", "diameter", "length", "width", "height",
]
VARIANTS_HEADER = [
    "id", "product_id", "sku", "color", "url", "gallery_photos", "technical_photos",
]
PRODUCT_PDFS_HEADER = ["id", "product_id", "pdf_id", "sort_order", "created_at"]

MERGE_COLUMNS = ("finishes", "sizes", "thickness")
FIRST_ROW_COLUMNS = (
    "description", "category", "type", "is_new", "subtype", "manufacturer", "material",
    "shape", "cut", "diameter", "length", "width", "height", "catalog_id", "position",
)


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = [dict(row) for row in reader]
        return list(reader.fieldnames or []), rows


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=fieldnames,
            quoting=csv.QUOTE_MINIMAL,
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def backup(path: Path) -> None:
    bak = path.with_suffix(path.suffix + ".bak")
    shutil.copy2(path, bak)


def merge_comma_field(values: list[str]) -> str:
    seen: set[str] = set()
    parts: list[str] = []
    for value in values:
        if not value or not value.strip():
            continue
        for item in value.split(", "):
            item = item.strip()
            if item and item not in seen:
                seen.add(item)
                parts.append(item)
    return ", ".join(parts)


def transform_products(old_rows: list[dict[str, str]]) -> tuple[list[dict[str, str]], dict[str, str], dict[str, str]]:
    """Return new products, old_id->collection, collection->new_id."""
    groups: OrderedDict[str, list[dict[str, str]]] = OrderedDict()
    old_id_to_collection: dict[str, str] = {}
    old_id_to_title: dict[str, str] = {}

    for row in old_rows:
        collection = row.get("collection", "").strip()
        old_id = row["id"]
        old_id_to_collection[old_id] = collection
        old_id_to_title[old_id] = row.get("title", "").strip()
        groups.setdefault(collection, []).append(row)

    new_rows: list[dict[str, str]] = []
    collection_to_new_id: dict[str, str] = {}

    for new_id, (collection, group) in enumerate(groups.items(), start=1):
        first = group[0]
        new_id_str = str(new_id)
        collection_to_new_id[collection] = new_id_str

        new_row = {col: first.get(col, "") for col in PRODUCTS_HEADER}
        new_row["id"] = new_id_str
        new_row["title"] = collection
        new_row["collection"] = collection

        for col in MERGE_COLUMNS:
            new_row[col] = merge_comma_field([r.get(col, "") for r in group])

        new_rows.append(new_row)

    return new_rows, old_id_to_collection, old_id_to_title, collection_to_new_id


def variant_color(old_title: str, old_color: str) -> str:
    if old_color == "Standard":
        return old_title
    if old_color == old_title:
        return old_color
    return f"{old_title} {old_color}"


def transform_variants(
    old_rows: list[dict[str, str]],
    old_id_to_collection: dict[str, str],
    old_id_to_title: dict[str, str],
    collection_to_new_id: dict[str, str],
) -> list[dict[str, str]]:
    new_rows: list[dict[str, str]] = []
    for new_id, old in enumerate(old_rows, start=1):
        old_product_id = old["product_id"]
        collection = old_id_to_collection.get(old_product_id, "")
        new_product_id = collection_to_new_id.get(collection, "")
        old_title = old_id_to_title.get(old_product_id, "")

        new_rows.append({
            "id": str(new_id),
            "product_id": new_product_id,
            "sku": old.get("sku", ""),
            "color": variant_color(old_title, old.get("color", "").strip()),
            "url": old.get("url", ""),
            "gallery_photos": old.get("gallery_photos", ""),
            "technical_photos": old.get("technical_photos", ""),
        })
    return new_rows


def transform_product_pdfs(
    old_rows: list[dict[str, str]],
    old_id_to_collection: dict[str, str],
    collection_to_new_id: dict[str, str],
) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    deduped: list[dict[str, str]] = []

    for old in old_rows:
        old_product_id = old["product_id"]
        collection = old_id_to_collection.get(old_product_id, "")
        new_product_id = collection_to_new_id.get(collection, "")
        pdf_id = old.get("pdf_id", "")
        key = (new_product_id, pdf_id)
        if key in seen:
            continue
        seen.add(key)
        deduped.append({
            "product_id": new_product_id,
            "pdf_id": pdf_id,
            "sort_order": old.get("sort_order", ""),
            "created_at": old.get("created_at", ""),
        })

    for new_id, row in enumerate(deduped, start=1):
        row["id"] = str(new_id)

    return deduped


def reorganize(manufacturer_dir: Path) -> None:
    products_path = manufacturer_dir / "products.csv"
    variants_path = manufacturer_dir / "variants.csv"
    product_pdfs_path = manufacturer_dir / "product_pdfs.csv"

    _, old_products = read_csv(products_path)
    _, old_variants = read_csv(variants_path)

    old_product_count = len(old_products)
    old_variant_count = len(old_variants)

    new_products, old_id_to_collection, old_id_to_title, collection_to_new_id = transform_products(
        old_products
    )
    new_variants = transform_variants(
        old_variants, old_id_to_collection, old_id_to_title, collection_to_new_id
    )

    backup(products_path)
    backup(variants_path)
    write_csv(products_path, PRODUCTS_HEADER, new_products)
    write_csv(variants_path, VARIANTS_HEADER, new_variants)

    if product_pdfs_path.exists():
        _, old_product_pdfs = read_csv(product_pdfs_path)
        old_pdf_count = len(old_product_pdfs)
        new_product_pdfs = transform_product_pdfs(
            old_product_pdfs, old_id_to_collection, collection_to_new_id
        )
        backup(product_pdfs_path)
        write_csv(product_pdfs_path, PRODUCT_PDFS_HEADER, new_product_pdfs)
        print(
            f"  product_pdfs: {old_pdf_count} -> {len(new_product_pdfs)} "
            f"(deduplicated by collection)"
        )

    print(f"{manufacturer_dir.name}:")
    print(f"  products: {old_product_count} -> {len(new_products)}")
    print(f"  variants: {old_variant_count} -> {len(new_variants)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Reorganize manufacturer CSVs by collection.")
    parser.add_argument(
        "--manufacturer-dir",
        required=True,
        help='Manufacturer folder name (e.g. "fondovalle ceramice")',
    )
    args = parser.parse_args()

    manufacturer_dir = Path(args.manufacturer_dir)
    if not manufacturer_dir.is_dir():
        manufacturer_dir = Path(__file__).parent / args.manufacturer_dir
    if not manufacturer_dir.is_dir():
        raise SystemExit(f"Manufacturer directory not found: {args.manufacturer_dir}")

    reorganize(manufacturer_dir)


if __name__ == "__main__":
    main()
