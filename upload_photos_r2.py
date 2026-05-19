#!/usr/bin/env python3
"""
Download variant photos from manufacturer URLs, upload to Cloudflare R2, and
rewrite the brand CSVs: CDN URLs in ``variants.csv``, and stable numeric ids
for D1 upload (products, variants, technical_pdfs, product_pdfs).

Manufacturer slug in R2 keys comes from ``products.csv`` column ``manufacturer``
only — never from the folder name.

Edit ``START_*_ID`` below before each manufacturer reload so ids match free
slots in D1 (no re-scrape required).

Prerequisites
-------------
  pip install -r requirements-upload.txt

Environment (``.env`` at project root):
  CLOUDFLARE_ACCOUNT_ID
  R2_ACCESS_KEY_ID
  R2_SECRET_ACCESS_KEY
  R2_BUCKET_NAME
  Optional: R2_PUBLIC_BASE_URL (default CDN: https://cdn.altrodesign.ro)

Usage
-----
  python upload_photos_r2.py "rosa splendiani"
  python upload_photos_r2.py "rosa splendiani" --dry-run
  python upload_photos_r2.py "rosa splendiani" --concurrency 4
"""

from __future__ import annotations

import argparse
import csv
import json
import mimetypes
import os
import re
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlparse

import requests

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:
    boto3 = None  # type: ignore[assignment]
    ClientError = Exception  # type: ignore[misc, assignment]

_ROOT_DIR = Path(__file__).resolve().parent

DEFAULT_CDN_BASE = "https://cdn.altrodesign.ro"

# D1 id starts — change manually before each brand reload.
START_PRODUCT_ID = 1500
START_VARIANT_ID = 9000
START_TECH_PDF_ID = 6000
START_PRODUCT_PDF_ID = 6000

IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif"})

DOWNLOAD_RETRIES = 3
UPLOAD_RETRIES = 2

DOWNLOAD_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "image/*,*/*;q=0.8",
}


def _load_env_stdlib(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if not key:
            continue
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        os.environ[key] = val


def _load_project_env() -> None:
    env_path = _ROOT_DIR / ".env"
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path, override=True)
    except ImportError:
        _load_env_stdlib(env_path)


_load_project_env()


def env_required(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise SystemExit(f"Missing environment variable: {name}")
    return v


def strip_diacritics(s: str) -> str:
    s = re.sub(r"[\u0218\u0219]", "s", s)
    s = re.sub(r"[\u021a\u021b]", "t", s)
    s = re.sub(r"[âă]", "a", s)
    s = re.sub(r"î", "i", s)
    s = re.sub(r"[șş]", "s", s)
    s = re.sub(r"[țţ]", "t", s)
    return s


def slugify(text: str, *, max_len: int = 80) -> str:
    """Lowercase ASCII slug for R2 path segments."""
    s = strip_diacritics((text or "").strip().lower())
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    if not s:
        s = "unknown"
    return s[:max_len].strip("-") or "unknown"


def parse_url_list(raw: str) -> list[str]:
    if not raw or not str(raw).strip():
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    out: list[str] = []
    for entry in parsed:
        if isinstance(entry, str):
            u = entry.strip()
            if u:
                out.append(u)
    return out


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            return [], []
        header = list(reader.fieldnames)
        rows = [{k: (row.get(k) or "") for k in header} for row in reader]
    return header, rows


def write_csv(path: Path, header: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=header, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in header})


def filename_from_url(url: str) -> str:
    path = urlparse(url).path
    name = unquote(PurePosixPath(path).name or "image.jpg")
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", name).strip(". ")
    if not name:
        name = "image.jpg"
    return name


def extension_ok(filename: str) -> bool:
    ext = PurePosixPath(filename).suffix.lower()
    return ext in IMAGE_EXTENSIONS


def guess_content_type(filename: str, response_ct: str | None) -> str:
    if response_ct and response_ct.split(";")[0].strip().lower().startswith("image/"):
        return response_ct.split(";")[0].strip()
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def load_products(products_path: Path) -> tuple[str, dict[int, str]]:
    """Return (manufacturer_name, product_id -> title)."""
    if not products_path.is_file():
        raise SystemExit(f"Missing {products_path}")

    _, rows = read_csv(products_path)
    if not rows:
        raise SystemExit(f"Empty {products_path}")

    manufacturer = ""
    titles: dict[int, str] = {}

    for row in rows:
        mfr = (row.get("manufacturer") or "").strip()
        if mfr and not manufacturer:
            manufacturer = mfr

        pid_raw = (row.get("id") or "").strip()
        title = (row.get("title") or "").strip()
        if not pid_raw:
            continue
        try:
            pid = int(pid_raw)
        except ValueError:
            continue
        if title:
            titles[pid] = title

    if not manufacturer:
        raise SystemExit(
            f"No manufacturer name in {products_path} — "
            "set the ``manufacturer`` column (do not use the folder name)."
        )
    if not titles:
        raise SystemExit(f"No product titles found in {products_path}")

    return manufacturer, titles


@dataclass
class UrlJob:
    source_url: str
    r2_key: str
    cdn_url: str


@dataclass
class RunStats:
    unique_urls: int = 0
    skipped_non_image: int = 0
    downloaded: int = 0
    uploaded: int = 0
    already_in_r2: int = 0
    failed: int = 0
    variants_updated: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)


def allocate_r2_key(
    manufacturer_slug: str,
    product_slug: str,
    source_url: str,
    used_per_product: dict[str, set[str]],
) -> str:
    """Build R2 key; disambiguate duplicate basenames within one product folder."""
    filename = filename_from_url(source_url)
    if not extension_ok(filename):
        base, ext = PurePosixPath(filename).stem, PurePosixPath(filename).suffix
        if not ext:
            filename = f"{base}.jpg"

    bucket_key = f"{product_slug}"
    used = used_per_product.setdefault(bucket_key, set())
    candidate = filename
    if candidate in used:
        stem = PurePosixPath(filename).stem
        ext = PurePosixPath(filename).suffix or ".jpg"
        n = 2
        while candidate in used:
            candidate = f"{stem}-{n}{ext}"
            n += 1
    used.add(candidate)
    return f"{manufacturer_slug}/{product_slug}/{candidate}"


def build_url_jobs(
    variants: list[dict[str, str]],
    product_titles: dict[int, str],
    manufacturer_slug: str,
) -> tuple[list[UrlJob], int]:
    """
    Collect unique source URLs and assign R2 keys.
    Returns (jobs, count_skipped_non_image).
    """
    url_to_product: dict[str, int] = {}
    skipped = 0

    for row in variants:
        pid_raw = (row.get("product_id") or "").strip()
        try:
            product_id = int(pid_raw)
        except ValueError:
            continue

        for col in ("gallery_photos", "technical_photos"):
            for url in parse_url_list(row.get(col, "")):
                if url not in url_to_product:
                    url_to_product[url] = product_id

    used_per_product: dict[str, set[str]] = {}
    jobs: list[UrlJob] = []
    seen_urls: set[str] = set()

    for url, product_id in url_to_product.items():
        if url in seen_urls:
            continue
        seen_urls.add(url)

        fn = filename_from_url(url)
        if not extension_ok(fn):
            skipped += 1
            continue

        title = product_titles.get(product_id)
        if not title:
            raise SystemExit(
                f"Variant references product_id={product_id} but no title in products.csv"
            )
        product_slug = slugify(title)
        r2_key = allocate_r2_key(manufacturer_slug, product_slug, url, used_per_product)
        jobs.append(UrlJob(source_url=url, r2_key=r2_key, cdn_url=""))

    return jobs, skipped


def make_s3_client(account_id: str, access_key: str, secret_key: str):
    if boto3 is None:
        raise SystemExit("boto3 is required. Install: pip install -r requirements-upload.txt")
    return boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
    )


def r2_object_exists(s3, bucket: str, key: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def download_image(session: requests.Session, url: str) -> tuple[bytes, str]:
    last_err: Exception | None = None
    for attempt in range(DOWNLOAD_RETRIES):
        try:
            resp = session.get(url, timeout=60, headers=DOWNLOAD_HEADERS)
            resp.raise_for_status()
            filename = filename_from_url(url)
            ct = guess_content_type(filename, resp.headers.get("Content-Type"))
            return resp.content, ct
        except Exception as e:
            last_err = e
            if attempt < DOWNLOAD_RETRIES - 1:
                time.sleep(2**attempt)
    raise RuntimeError(str(last_err) if last_err else "download failed")


def process_one_url(
    job: UrlJob,
    *,
    session: requests.Session,
    s3,
    bucket: str,
    cdn_base: str,
    dry_run: bool,
    skip_existing: bool,
) -> tuple[str, str | None, str]:
    """
    Returns (source_url, cdn_url_or_none, status).
    status: dry_run | skipped_exists | uploaded | failed
    """
    cdn_url = f"{cdn_base.rstrip('/')}/{job.r2_key}"

    if dry_run:
        return job.source_url, cdn_url, "dry_run"

    try:
        if skip_existing and r2_object_exists(s3, bucket, job.r2_key):
            return job.source_url, cdn_url, "skipped_exists"

        body, content_type = download_image(session, job.source_url)

        last_err: Exception | None = None
        for attempt in range(UPLOAD_RETRIES):
            try:
                s3.put_object(
                    Bucket=bucket,
                    Key=job.r2_key,
                    Body=body,
                    ContentType=content_type,
                )
                return job.source_url, cdn_url, "uploaded"
            except Exception as e:
                last_err = e
                if attempt < UPLOAD_RETRIES - 1:
                    time.sleep(2**attempt)
        raise RuntimeError(str(last_err) if last_err else "upload failed")
    except Exception as e:
        return job.source_url, None, f"failed: {e}"


def rewrite_photo_columns(
    rows: list[dict[str, str]],
    url_to_cdn: dict[str, str],
) -> int:
    """Replace URLs in gallery/technical columns. Returns count of rows touched."""
    touched = 0
    for row in rows:
        changed = False
        for col in ("gallery_photos", "technical_photos"):
            urls = parse_url_list(row.get(col, ""))
            if not urls:
                continue
            new_urls = [url_to_cdn.get(u, u) for u in urls]
            if new_urls != urls:
                row[col] = json.dumps(new_urls, ensure_ascii=False)
                changed = True
        if changed:
            touched += 1
    return touched


def _parse_int(cell: str) -> int | None:
    try:
        return int((cell or "").strip())
    except ValueError:
        return None


def _rows_sorted_by_id(rows: list[dict[str, str]], id_col: str = "id") -> list[dict[str, str]]:
    return sorted(
        rows,
        key=lambda r: (_parse_int(r.get(id_col, "")) is None, _parse_int(r.get(id_col, "")) or 0),
    )


def _remap_primary_ids(
    rows: list[dict[str, str]],
    *,
    start_id: int,
    id_col: str = "id",
) -> tuple[list[dict[str, str]], dict[int, int]]:
    """Assign contiguous ids from ``start_id`` in ascending old-id order."""
    old_to_new: dict[int, int] = {}
    next_id = start_id
    out: list[dict[str, str]] = []
    for row in _rows_sorted_by_id(rows, id_col):
        r = dict(row)
        old = _parse_int(r.get(id_col, ""))
        if old is None:
            out.append(r)
            continue
        old_to_new[old] = next_id
        r[id_col] = str(next_id)
        out.append(r)
        next_id += 1
    return out, old_to_new


def remap_brand_csv_ids(brand_dir: Path, *, dry_run: bool = False) -> dict[str, dict[int, int]]:
    """
    Renumber ids in all four brand CSVs so they match ``START_*_ID`` constants.
    Preserves row order by sorting on the existing id column before assigning.
    """
    paths = {
        "products": brand_dir / "products.csv",
        "variants": brand_dir / "variants.csv",
        "technical_pdfs": brand_dir / "technical_pdfs.csv",
        "product_pdfs": brand_dir / "product_pdfs.csv",
    }
    maps: dict[str, dict[int, int]] = {}

    if not paths["products"].is_file():
        raise SystemExit(f"Missing {paths['products']}")
    if not paths["variants"].is_file():
        raise SystemExit(f"Missing {paths['variants']}")

    p_header, p_rows = read_csv(paths["products"])
    p_rows, product_map = _remap_primary_ids(p_rows, start_id=START_PRODUCT_ID)
    maps["products"] = product_map

    v_header, v_rows = read_csv(paths["variants"])
    v_rows, variant_map = _remap_primary_ids(v_rows, start_id=START_VARIANT_ID)
    for row in v_rows:
        old_pid = _parse_int(row.get("product_id", ""))
        if old_pid is not None and old_pid in product_map:
            row["product_id"] = str(product_map[old_pid])
    maps["variants"] = variant_map

    pdf_map: dict[int, int] = {}
    if paths["technical_pdfs"].is_file():
        t_header, t_rows = read_csv(paths["technical_pdfs"])
        t_rows, pdf_map = _remap_primary_ids(t_rows, start_id=START_TECH_PDF_ID)
        maps["technical_pdfs"] = pdf_map
    else:
        t_header, t_rows = [], {}

    if paths["product_pdfs"].is_file():
        pp_header, pp_rows = read_csv(paths["product_pdfs"])
        pp_rows, pp_map = _remap_primary_ids(pp_rows, start_id=START_PRODUCT_PDF_ID)
        for row in pp_rows:
            old_pid = _parse_int(row.get("product_id", ""))
            if old_pid is not None and old_pid in product_map:
                row["product_id"] = str(product_map[old_pid])
            old_pdf = _parse_int(row.get("pdf_id", ""))
            if old_pdf is not None and old_pdf in pdf_map:
                row["pdf_id"] = str(pdf_map[old_pdf])
        maps["product_pdfs"] = pp_map
    else:
        pp_header, pp_rows = [], {}

    print(
        f"\nID remap (starts: products={START_PRODUCT_ID}, variants={START_VARIANT_ID}, "
        f"technical_pdfs={START_TECH_PDF_ID}, product_pdfs={START_PRODUCT_PDF_ID})"
    )
    for label, m in maps.items():
        if not m:
            continue
        olds = sorted(m)
        print(
            f"  {label}: {len(m)} row(s), "
            f"old {olds[0]}..{olds[-1]} -> new {m[olds[0]]}..{m[olds[-1]]}"
        )

    if dry_run:
        print("  (dry-run: CSV files not rewritten for id remap)")
        return maps

    for name, path, header, rows in (
        ("products", paths["products"], p_header, p_rows),
        ("variants", paths["variants"], v_header, v_rows),
        ("technical_pdfs", paths["technical_pdfs"], t_header, t_rows),
        ("product_pdfs", paths["product_pdfs"], pp_header, pp_rows),
    ):
        if not rows and name in ("technical_pdfs", "product_pdfs"):
            continue
        backup = path.with_suffix(path.suffix + ".bak")
        if not backup.exists() and path.is_file():
            shutil.copy2(path, backup)
        write_csv(path, header, rows)
        print(f"  wrote {path.name}")

    return maps


def resolve_brand_dir(arg: str) -> Path:
    p = Path(arg).expanduser()
    if not p.is_absolute():
        p = (_ROOT_DIR / p).resolve()
    else:
        p = p.resolve()
    if not p.is_dir():
        raise SystemExit(f"Not a directory: {p}")
    return p


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "brand_folder",
        help="Brand folder containing variants.csv and products.csv",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned uploads; do not download, upload, or rewrite CSV",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=4,
        metavar="N",
        help="Parallel download/upload workers (default: 4)",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        default=True,
        help="Skip R2 put when object key already exists (default: on)",
    )
    parser.add_argument(
        "--no-skip-existing",
        action="store_false",
        dest="skip_existing",
        help="Re-upload even if the R2 key already exists",
    )
    parser.add_argument(
        "--cdn-base",
        default=None,
        help=f"Public CDN base URL (default: R2_PUBLIC_BASE_URL or {DEFAULT_CDN_BASE})",
    )
    parser.add_argument(
        "--skip-id-remap",
        action="store_true",
        help="Do not renumber ids in brand CSVs (only upload photos / rewrite URLs)",
    )
    args = parser.parse_args()

    brand_dir = resolve_brand_dir(args.brand_folder)
    variants_path = brand_dir / "variants.csv"
    products_path = brand_dir / "products.csv"

    if not variants_path.is_file():
        raise SystemExit(f"Missing {variants_path}")

    if not args.skip_id_remap:
        remap_brand_csv_ids(brand_dir, dry_run=args.dry_run)

    manufacturer, product_titles = load_products(products_path)
    manufacturer_slug = slugify(manufacturer)

    header, variant_rows = read_csv(variants_path)
    for col in ("gallery_photos", "technical_photos"):
        if col not in header:
            raise SystemExit(f"{variants_path} missing column {col!r}")

    jobs, skipped_non_image = build_url_jobs(
        variant_rows, product_titles, manufacturer_slug
    )

    cdn_base = (
        (args.cdn_base or os.environ.get("R2_PUBLIC_BASE_URL") or DEFAULT_CDN_BASE).strip()
    )
    cdn_base = cdn_base.rstrip("/")

    stats = RunStats(unique_urls=len(jobs), skipped_non_image=skipped_non_image)

    print(f"Brand folder: {brand_dir}")
    print(f"Manufacturer (from products.csv): {manufacturer!r} -> slug {manufacturer_slug!r}")
    print(f"CDN base: {cdn_base}")
    print(f"Unique image URLs: {len(jobs)}")
    if skipped_non_image:
        print(f"Skipped (non-image extension): {skipped_non_image}")

    if args.dry_run:
        print("\n[dry-run] Planned uploads:")
        for job in jobs[:20]:
            print(f"  {job.source_url}")
            print(f"    -> {cdn_base}/{job.r2_key}")
        if len(jobs) > 20:
            print(f"  ... and {len(jobs) - 20} more")
        return 0

    account_id = env_required("CLOUDFLARE_ACCOUNT_ID")
    access_key = env_required("R2_ACCESS_KEY_ID")
    secret_key = env_required("R2_SECRET_ACCESS_KEY")
    bucket = env_required("R2_BUCKET_NAME")

    s3 = make_s3_client(account_id, access_key, secret_key)
    url_to_cdn: dict[str, str] = {}

    concurrency = max(1, args.concurrency)
    print(f"\nProcessing with concurrency={concurrency} ...", flush=True)

    def run_job(job: UrlJob) -> tuple[str, str | None, str]:
        # Each worker gets its own session (not thread-safe to share).
        with requests.Session() as session:
            return process_one_url(
                job,
                session=session,
                s3=s3,
                bucket=bucket,
                cdn_base=cdn_base,
                dry_run=False,
                skip_existing=args.skip_existing,
            )

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(run_job, job): job for job in jobs}
        done = 0
        for fut in as_completed(futures):
            done += 1
            source_url, cdn_url, status = fut.result()
            if cdn_url:
                url_to_cdn[source_url] = cdn_url
            if status == "uploaded":
                stats.uploaded += 1
                stats.downloaded += 1
            elif status == "skipped_exists":
                stats.already_in_r2 += 1
            elif status.startswith("failed:"):
                stats.failed += 1
                stats.failures.append((source_url, status))
            if done % 25 == 0 or done == len(jobs):
                print(f"  progress {done}/{len(jobs)}", flush=True)

    stats.variants_updated = rewrite_photo_columns(variant_rows, url_to_cdn)

    backup_path = variants_path.with_suffix(".csv.bak")
    if not backup_path.exists():
        shutil.copy2(variants_path, backup_path)
        print(f"\nBackup: {backup_path}")
    else:
        print(f"\nBackup already exists (unchanged): {backup_path}")

    write_csv(variants_path, header, variant_rows)
    print(f"Updated: {variants_path}")

    print("\n=== Summary ===")
    print(f"  Unique URLs:     {stats.unique_urls}")
    print(f"  Uploaded:        {stats.uploaded}")
    print(f"  Already in R2:   {stats.already_in_r2}")
    print(f"  Failed:          {stats.failed}")
    print(f"  Variants rows updated: {stats.variants_updated}")

    if stats.failures:
        print("\nFailures (original URLs kept in CSV):")
        for url, err in stats.failures[:30]:
            print(f"  {url}")
            print(f"    {err}")
        if len(stats.failures) > 30:
            print(f"  ... and {len(stats.failures) - 30} more")

    return 1 if stats.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
