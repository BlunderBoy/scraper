# D1 migration: collection → product, color → variant

Human-only runbook. Migrate live D1 data so **one manufacturer collection = one `products` row**, and **each former color/shade = one `variants` row** under that product — matching 41zero42, Sodai, Quintessenza, Cesi, and the updated scrapers for Fondovalle, ABK/Moooi, and Casalgrande Padana.

**Scope of this guide:** in-place SQL migration on D1. CSVs in the repo are **not** source of truth if the website was edited manually.

**Strategy:** backup → rehearse on a local SQLite copy (with transactions) → apply to live via Wrangler → verify → refresh caches.

---

## 1. Target data model

### Before (legacy)

| Table | Shape |
|--------|--------|
| `products` | One row per **color** (e.g. product title `"Sugar"`, `collection` = `"Homescape"`) |
| `variants` | Usually **one** variant per product (`color` = `"Standard"` or = product title) |
| `product_pdfs` | Many rows per collection (same `pdf_id`, different `product_id`) |
| `technical_pdfs` | Unchanged (no `product_id`; linked via `product_pdfs`) |

### After (target)

| Table | Shape |
|--------|--------|
| `products` | One row per **`collection`** (`title` = collection name, e.g. `"Homescape"`) |
| `variants` | One row per **old product** (shade); `color` = old product `title` (or `"{title} {sub}"` for multi-variant cases) |
| `product_pdfs` | `product_id` remapped to keeper product; dedupe `(product_id, pdf_id)` |
| `technical_pdfs` | **No change** (still collection-level PDF metadata) |

### Variant `color` rules

| Situation | New `color` |
|-----------|-------------|
| Old variant `color` is empty or `"Standard"` | Old product `title` (e.g. `"Sugar"`) |
| Old variant `color` already equals old product `title` (typical ABK) | Keep as-is |
| Multiple variants per old product (e.g. Royal Travertino Vein/Cross) | `"{old_product_title} {old_variant_color}"` (e.g. `"Bianco Vein"`) |

### Manufacturers in scope

| Brand | `products` filter |
|--------|-------------------|
| Fondovalle ceramice | `manufacturer = 'Fondovalle' AND category = 'ceramice'` |
| Casalgrande Padana | `manufacturer = 'Casalgrande Padana'` |
| ABK + Moooi | `manufacturer IN ('ABK', 'MOOOI BY ABK')` |

Fondovalle **mobilier** shares `manufacturer = 'Fondovalle'` but uses `category = 'mobilier'` — exclude it from session 1 (no separate migration in this runbook).

Migrate **one manufacturer per session** so Time Travel rollback stays understandable.

---

## 2. Prerequisites

### 2.1 Tools

- **Node.js** (for `npx wrangler`)
- **SQLite CLI** (optional; [DB Browser for SQLite](https://sqlitebrowser.org/) also works)
- This repo cloned on the machine you use (e.g. `d:\scraper`)

### 2.2 Configure `wrangler.toml`

Edit [wrangler.toml](wrangler.toml):

- `account_id` — Cloudflare account ID
- `[[d1_databases]]` → `database_name`, `database_id`

The binding name is **`DB`** (used as `wrangler d1 execute DB ...`).  
Export uses **`database_name`** (the label in the D1 dashboard), not the binding.

```
name = "d1-migrate"
compatibility_date = "2025-04-01"
account_id = "YOUR_ACCOUNT_ID"

[[d1_databases]]
binding = "DB"
database_name = "your-db-name-in-dashboard"
database_id = "your-database-uuid"
```

### 2.3 Log in

```powershell
cd d:\scraper
npx wrangler login
npx wrangler whoami
npx wrangler d1 list
```

Confirm your database appears and note `database_name` for exports.

### 2.4 Confirm Time Travel (production storage)

```powershell
npx wrangler d1 info YOUR_D1_DATABASE_NAME
```

Look for `version: production`. Time Travel / bookmarks apply to production-storage DBs.  
Docs: [Time Travel and backups](https://developers.cloudflare.com/d1/reference/time-travel/)

### 2.5 Schema check (once)

Column names differ slightly between merged CSV schema and live Worker API. Live app uses:

- `variants.url_on_manufacturer_website` (not `url`)

```powershell
npx wrangler d1 execute DB --remote --command "PRAGMA table_info(variants);"
npx wrangler d1 execute DB --remote --command "PRAGMA table_info(products);"
```

If `PRAGMA table_info(products)` shows a **`source`** column (merged-upload schema), add  
`AND source = 'fondovalle ceramice'` (or the right folder name) to every `products` / `variants` / `product_pdfs` filter in the SQL below, and include `source` in temp tables. If there is no `source` column, ignore this.

On live D1, Fondovalle ceramice vs mobilier is split by **`category`** (`'ceramice'` / `'mobilier'`) — see session 1 filters in §1 and `01-fondovalle.sql`.

---

## 3. Safety net (before any migration)

### 3.1 Time Travel bookmark (whole database)

**Restore rewinds the entire D1 database**, not one manufacturer. Still essential if live migration goes wrong.

```powershell
# Current bookmark (save the output string somewhere safe)
npx wrangler d1 time-travel info YOUR_D1_DATABASE_NAME
```

Optional: bookmark for a specific time (RFC3339):

```powershell
npx wrangler d1 time-travel info YOUR_D1_DATABASE_NAME --timestamp="2026-05-20T12:00:00+00:00"
```

**Before each manufacturer session:** run `time-travel info` again and save the bookmark.

**If live migration fails:**

```powershell
npx wrangler d1 time-travel restore YOUR_D1_DATABASE_NAME --bookmark=PASTE_BOOKMARK_HERE
```

The CLI prints a **previous bookmark** to undo an over-restore.  
Retention: ~30 days (paid) / ~7 days (free) — see [D1 limits](https://developers.cloudflare.com/d1/platform/limits/).

### 3.2 SQL export (second backup)

```powershell
mkdir backups -Force
npx wrangler d1 export YOUR_D1_DATABASE_NAME --remote -o backups\d1-before-collections-YYYY-MM-DD.sql
```

Keep this file in git **only if it contains no secrets**; otherwise store outside the repo.

---

## 4. Local rehearsal database

### 4.1 Import export into SQLite

```powershell
cd d:\scraper
sqlite3 backups\d1-local.db ".read backups/d1-before-collections-YYYY-MM-DD.sql"
```

Or open `backups\d1-local.db` in DB Browser and execute the `.sql` import.

### 4.2 Baseline counts (remote and local)

Save outputs in a text file for comparison.

```sql
-- Replace filter per brand (Fondovalle ceramice examples below)

SELECT 'products' AS t, COUNT(*) AS n FROM products
WHERE manufacturer = 'Fondovalle' AND category = 'ceramice';
SELECT 'variants' AS t, COUNT(*) AS n
FROM variants v
WHERE v.product_id IN (
  SELECT id FROM products WHERE manufacturer = 'Fondovalle' AND category = 'ceramice'
);
SELECT 'product_pdfs' AS t, COUNT(*) AS n
FROM product_pdfs pp
WHERE pp.product_id IN (
  SELECT id FROM products WHERE manufacturer = 'Fondovalle' AND category = 'ceramice'
);
```

Run on **local**:

```powershell
sqlite3 backups\d1-local.db "SELECT COUNT(*) FROM products WHERE manufacturer = 'Fondovalle' AND category = 'ceramice';"
```

Run on **remote**:

```powershell
npx wrangler d1 execute DB --remote --command "SELECT COUNT(*) AS n FROM products WHERE manufacturer = 'Fondovalle' AND category = 'ceramice';"
```

---

## 5. Discovery (local copy)

Run in `sqlite3 backups\d1-local.db` or DB Browser.

### 5.1 Collections that need merging

```sql
SELECT collection, manufacturer, COUNT(*) AS n_products,
       GROUP_CONCAT(id) AS product_ids,
       GROUP_CONCAT(title) AS titles
FROM products
WHERE manufacturer = 'Fondovalle' AND category = 'ceramice'   -- change per session
  AND TRIM(COALESCE(collection, '')) != ''
GROUP BY collection, manufacturer
HAVING COUNT(*) > 1
ORDER BY n_products DESC;
```

### 5.2 Rows that cannot auto-group

```sql
SELECT id, title, collection, manufacturer, category
FROM products
WHERE manufacturer = 'Fondovalle' AND category = 'ceramice'
  AND (collection IS NULL OR TRIM(collection) = '');
```

Fix these manually before migrating (assign a `collection` or exclude from automation).

### 5.3 Variants per product (spot Royal Travertino–style cases)

```sql
SELECT p.collection, p.id AS product_id, p.title AS product_title,
       v.id AS variant_id, v.color
FROM products p
JOIN variants v ON v.product_id = p.id
WHERE p.manufacturer = 'Fondovalle' AND p.category = 'ceramice'
ORDER BY p.collection, p.id, v.id;
```

### 5.4 PDF links that will dedupe

```sql
SELECT p.collection, pp.pdf_id, COUNT(DISTINCT pp.product_id) AS n_products
FROM product_pdfs pp
JOIN products p ON p.id = pp.product_id
WHERE p.manufacturer = 'Fondovalle' AND p.category = 'ceramice'
GROUP BY p.collection, pp.pdf_id
HAVING n_products > 1;
```

### 5.5 Keeper product per collection (important)

Default SQL uses **`MIN(id)`** as the keeper product per collection. If a colleague edited a specific shade’s product row, that id should be the keeper instead.

Build overrides (example):

| collection | keeper_product_id |
|------------|-------------------|
| Homescape | 8050 |
| Royal Travertino | 858 |

You will plug these into `keeper_override` in the migration script (§6).

---

## 6. Migration SQL (per manufacturer)

Create one file per brand under `backups/migrations/`, e.g.:

- `backups/migrations/01-fondovalle.sql`
- `backups/migrations/02-abk-moooi.sql`
- `backups/migrations/03-casalgrande.sql`

**Edit the brand filter** in the `_mig_*_pcm` query (manufacturer, and `category` for Fondovalle ceramice) and optional `_mig_*_keeper_override` inserts.

### 6.0 Local SQLite vs remote D1

| Feature | Local `sqlite3` / DB Browser | Remote `wrangler d1 execute --remote --file=...` |
|---------|------------------------------|--------------------------------------------------|
| `CREATE TEMP TABLE` | Works (`temp` schema) | **Fails** — D1 is sandboxed and cannot use the temp schema → `SQLITE_AUTH` |
| `BEGIN` / `COMMIT` in the file | Optional (wrap in §6.5) | **Fails** — use wrangler batch only |
| Staging tables | Use `_mig_*` in `main` for scripts shared with remote | Same |

Migration files under `backups/migrations/` use **`CREATE TABLE _mig_*`** and **`DROP TABLE`** at the end so one file works both places.

### 6.1 Fondovalle ceramice template

```sql
DROP TABLE IF EXISTS _mig_fv_pcm;
DROP TABLE IF EXISTS _mig_fv_keeper_override;

-- Optional: override MIN(id) keepers (uncomment INSERTs; table is empty by default)
CREATE TABLE _mig_fv_keeper_override (
  collection TEXT NOT NULL PRIMARY KEY,
  keeper_product_id INTEGER NOT NULL
);
-- INSERT INTO _mig_fv_keeper_override (collection, keeper_product_id) VALUES
--   ('Homescape', 800),
--   ('Royal Travertino', 858);

CREATE TABLE _mig_fv_pcm AS
SELECT
  p.id AS old_product_id,
  p.title AS old_title,
  TRIM(p.collection) AS collection,
  p.manufacturer,
  COALESCE(
    (SELECT ko.keeper_product_id FROM _mig_fv_keeper_override ko
     WHERE ko.collection = TRIM(p.collection)),
    (SELECT MIN(p2.id) FROM products p2
     WHERE p2.manufacturer = p.manufacturer
       AND p2.category = 'ceramice'
       AND TRIM(p2.collection) = TRIM(p.collection))
  ) AS keeper_product_id
FROM products p
WHERE p.manufacturer = 'Fondovalle'
  AND p.category = 'ceramice'
  AND TRIM(COALESCE(p.collection, '')) != '';

-- 1) Reassign variants + fix color
UPDATE variants
SET
  product_id = (SELECT keeper_product_id FROM _mig_fv_pcm WHERE _mig_fv_pcm.old_product_id = variants.product_id),
  color = (
    SELECT CASE
      WHEN TRIM(COALESCE(variants.color, '')) IN ('', 'Standard') THEN _mig_fv_pcm.old_title
      WHEN variants.color = _mig_fv_pcm.old_title THEN variants.color
      ELSE _mig_fv_pcm.old_title || ' ' || variants.color
    END
    FROM _mig_fv_pcm
    WHERE _mig_fv_pcm.old_product_id = variants.product_id
  )
WHERE product_id IN (SELECT old_product_id FROM _mig_fv_pcm);

-- 2) Drop duplicate PDF links before remap (UNIQUE on product_id + pdf_id)
DELETE FROM product_pdfs
WHERE rowid IN (
  SELECT pp.rowid FROM product_pdfs pp
  INNER JOIN _mig_fv_pcm ON _mig_fv_pcm.old_product_id = pp.product_id
  WHERE pp.rowid NOT IN (
    SELECT MIN(pp2.rowid) FROM product_pdfs pp2
    INNER JOIN _mig_fv_pcm ON _mig_fv_pcm.old_product_id = pp2.product_id
    GROUP BY _mig_fv_pcm.keeper_product_id, pp2.pdf_id
  )
);

-- 3) Remap product_pdfs to keeper
UPDATE product_pdfs
SET product_id = (SELECT keeper_product_id FROM _mig_fv_pcm WHERE _mig_fv_pcm.old_product_id = product_pdfs.product_id)
WHERE product_id IN (SELECT old_product_id FROM _mig_fv_pcm);

-- 4) Dedupe product_pdfs (safety net)
DELETE FROM product_pdfs
WHERE rowid NOT IN (
  SELECT MIN(rowid) FROM product_pdfs GROUP BY product_id, pdf_id
);

-- 5) Collection-level product title (does not merge finishes/sizes — see §6.4)
UPDATE products
SET title = collection
WHERE id IN (SELECT DISTINCT keeper_product_id FROM _mig_fv_pcm);

-- 6) Remove redundant per-color product rows
DELETE FROM products
WHERE id IN (
  SELECT old_product_id FROM _mig_fv_pcm WHERE old_product_id != keeper_product_id
);

DROP TABLE _mig_fv_pcm;
DROP TABLE _mig_fv_keeper_override;
```

(`02-abk-moooi.sql` / `03-casalgrande.sql` use `_mig_abk_*` / `_mig_cg_*` names.)

### 6.2 ABK + Moooi

Same script; change filters to:

```sql
WHERE p.manufacturer IN ('ABK', 'MOOOI BY ABK')
```

Run as **one session** (both share the same migration file) or split if you prefer smaller blast radius.

### 6.3 Casalgrande Padana

```sql
WHERE p.manufacturer = 'Casalgrande Padana'
```

### 6.4 Merging `finishes` / `sizes` / `thickness` (optional)

The template **only** sets `title = collection` on keepers. It does **not** union descriptive fields across siblings. If the live site already has correct merged text on one keeper row, leave it.

To union comma-separated fields locally (SQLite), run a separate one-off per field after inspecting duplicates — only if you need parity with [reorganize_csvs.py](reorganize_csvs.py) behavior. Skipping is safer when data was hand-edited.

### 6.5 Run on local copy

Same migration file as remote (no `TEMP` tables). For one atomic transaction locally:

```powershell
sqlite3 backups\d1-local.db
```

```sql
BEGIN IMMEDIATE;
.read backups/migrations/01-fondovalle.sql
COMMIT;
```

If SQLite reports an error inside `BEGIN`, run `ROLLBACK;` then re-import from `d1-before-...sql` if needed.

---

## 7. Validate locally

```sql
-- One product per collection?
SELECT collection, manufacturer, COUNT(*) AS n
FROM products
WHERE manufacturer = 'Fondovalle' AND category = 'ceramice'
GROUP BY collection, manufacturer
HAVING COUNT(*) > 1;

-- Orphan variants?
SELECT v.id, v.product_id
FROM variants v
LEFT JOIN products p ON p.id = v.product_id
WHERE p.id IS NULL;

-- Orphan or duplicate PDF links?
SELECT pp.product_id, pp.pdf_id, COUNT(*) AS n
FROM product_pdfs pp
LEFT JOIN products p ON p.id = pp.product_id
WHERE p.id IS NULL
GROUP BY pp.product_id, pp.pdf_id;

SELECT product_id, pdf_id, COUNT(*) AS n
FROM product_pdfs
GROUP BY product_id, pdf_id
HAVING COUNT(*) > 1;

-- SKU collisions?
SELECT sku, COUNT(*) AS n FROM variants GROUP BY sku HAVING COUNT(*) > 1;

-- Variants per collection product
SELECT p.id, p.title, p.collection, COUNT(v.id) AS n_variants
FROM products p
LEFT JOIN variants v ON v.product_id = p.id
WHERE p.manufacturer = 'Fondovalle' AND p.category = 'ceramice'
GROUP BY p.id
ORDER BY p.collection;
```

**Expect (order of magnitude, from scraper-era data):**

| Manufacturer | ~products after | ~variants (unchanged count) |
|--------------|-----------------|-----------------------------|
| Fondovalle ceramice | ~13 | ~80 |
| ABK + Moooi | ~16–22 | ~65 |
| Casalgrande | ~11–15 | ~63 |

Exact counts depend on live DB and manual edits.

Spot-check:

- **Homescape** — variants `Sugar`, `Clay`, `Coal`, … under one product
- **Royal Travertino** — `Bianco Vein`, `Bianco Cross`, …
- **ABK Blend** — colors like `Concrete Ash` unchanged
- **Casalgrande Elements Pebbles** — variant colors like `Elements Pebbles Beige`

---

## 8. Apply to live D1 (Wrangler)

### 8.1 Pre-flight per manufacturer

1. Save Time Travel bookmark (`§3.1`).
2. Optional: another export after prior brands migrated:  
   `npx wrangler d1 export YOUR_D1_DATABASE_NAME --remote -o backups\d1-before-fondovalle.sql`
3. Re-run baseline counts on **remote** (§4.2).

### 8.2 Execute migration file on remote

Use the `backups/migrations/*.sql` files as written: **`_mig_*` tables only**, no `CREATE TEMP TABLE`, no `BEGIN`/`COMMIT` (see §6.0). Wrangler runs the file as one batch against remote D1.

```powershell
npx wrangler d1 execute DB --remote --file=backups\migrations\01-fondovalle.sql
```

Repeat for `02-abk-moooi.sql`, `03-casalgrande.sql`.

If execution fails partway through, use Time Travel restore (`§3.1`). **`SQLITE_AUTH`** almost always means a disallowed statement (temp schema, transaction SQL, or unsupported `PRAGMA`) — not a Wrangler login problem.

**Alternative:** split the file and run step-by-step only if the combined file fails parsing.

### 8.3 Post-flight on remote

Re-run the same validation queries from §7 via:

```powershell
npx wrangler d1 execute DB --remote --command "SELECT collection, COUNT(*) AS n FROM products WHERE manufacturer = 'Fondovalle' AND category = 'ceramice' GROUP BY collection HAVING COUNT(*) > 1;"
```

Compare counts to §4.2 baseline:

- **products** count should drop (many → one per collection).
- **variants** count should stay the same.
- **product_pdfs** count should drop (deduped).
- **technical_pdfs** count should be unchanged.

### 8.4 Site check

- Open admin / public pages for 2–3 collections per migrated brand.
- Confirm PDFs still download (product_pdfs → technical_pdfs join).
- **Refresh caches** on admin pages (see [cleanup_manufacturer.md](cleanup_manufacturer.md) procedure).

### 8.5 Rollback

```powershell
npx wrangler d1 time-travel restore YOUR_D1_DATABASE_NAME --bookmark=BOOKMARK_FROM_PREFLIGHT
```

This restores the **whole** database, including other manufacturers and any edits after that bookmark.

---

## 9. Suggested session order

| Session | Manufacturer | Migration file |
|---------|--------------|----------------|
| 1 | Fondovalle ceramice | `01-fondovalle.sql` |
| 2 | ABK + Moooi | `02-abk-moooi.sql` |
| 3 | Casalgrande Padana | `03-casalgrande.sql` |

Between sessions: new Time Travel bookmark + spot-check site.

---

## 10. What this migration does *not* do

| Topic | Note |
|--------|------|
| **technical_pdfs** | Not modified; only `product_pdfs.product_id` changes |
| **New product IDs** | Keepers keep existing ids; deleted rows are duplicate per-color **products** only |
| **Other manufacturers / Fondovalle mobilier** | Untouched if filters are correct (`category = 'mobilier'` for Fondovalle furniture) |
| **CSV / scrape reload** | Separate; scrapers already emit collection=product format for future runs |
| **Romanian translations** | Unchanged on `products` unless you edit text |
| **R2 / images** | Variant `gallery_photos` JSON untouched |

---

## 11. Reference: repo CSV transform

If you ever need to reconcile exports with repo logic, [reorganize_csvs.py](reorganize_csvs.py) implements the same grouping for CSVs (not for live D1). Scraper/hints docs:

- [fondovalle ceramice/hints.txt](fondovalle%20ceramice/hints.txt)
- [abk/hints.txt](abk/hints.txt)
- [casalgrande padana/hints.txt](casalgrande%20padana/hints.txt)

---

## 12. Quick command cheat sheet

```powershell
cd d:\scraper

# Auth & config
npx wrangler login
npx wrangler d1 list
npx wrangler d1 info YOUR_D1_DATABASE_NAME

# Backup
npx wrangler d1 time-travel info YOUR_D1_DATABASE_NAME
npx wrangler d1 export YOUR_D1_DATABASE_NAME --remote -o backups\d1-YYYY-MM-DD.sql

# Local import & migrate
sqlite3 backups\d1-local.db ".read backups/d1-YYYY-MM-DD.sql"
sqlite3 backups\d1-local.db < backups\migrations\01-fondovalle.sql

# Live migrate (one manufacturer)
npx wrangler d1 execute DB --remote --file=backups\migrations\01-fondovalle.sql

# Rollback whole DB
npx wrangler d1 time-travel restore YOUR_D1_DATABASE_NAME --bookmark=...
```

---

## 13. Checklist (copy per session)

```
[ ] Time Travel bookmark saved
[ ] wrangler d1 export saved
[ ] Baseline counts recorded (products / variants / product_pdfs)
[ ] Discovery: no empty collection rows (or fixed)
[ ] keeper_override filled (if MIN(id) is wrong for edited rows)
[ ] Migration run on d1-local.db
[ ] Local validation passed
[ ] npx wrangler d1 execute --remote --file=...
[ ] Remote validation passed
[ ] Site spot-check + cache refresh
```

---

*Last updated for collection-as-product migration (Fondovalle ceramice, ABK/Moooi, Casalgrande Padana).*
