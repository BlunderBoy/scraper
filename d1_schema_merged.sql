-- Run once on a new D1 database (e.g. wrangler d1 execute DB --remote --file=d1_schema_merged.sql).
-- Matches merged_products.csv / merged_variants.csv when using the merge script with --source.
-- Composite primary keys (source, id) avoid collisions when Bathco id=1 and other brands reuse small integers.
-- Photo URLs live inline on `variants` as JSON arrays (TEXT columns parsed client-side).

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS products (
  id INTEGER NOT NULL,
  title TEXT,
  description TEXT,
  category TEXT,
  type TEXT,
  collection TEXT,
  is_new INTEGER NOT NULL DEFAULT 0,
  subtype TEXT,
  manufacturer TEXT,
  catalog_id TEXT,
  finishes TEXT,
  position TEXT,
  sizes TEXT,
  thickness TEXT,
  material TEXT,
  shape TEXT,
  cut TEXT,
  diameter TEXT,
  length TEXT,
  width TEXT,
  height TEXT,
  source TEXT NOT NULL,
  PRIMARY KEY (source, id)
);

CREATE TABLE IF NOT EXISTS variants (
  id INTEGER NOT NULL,
  product_id INTEGER NOT NULL,
  sku TEXT,
  color TEXT,
  url TEXT,
  gallery_photos TEXT NOT NULL DEFAULT '[]',
  technical_photos TEXT NOT NULL DEFAULT '[]',
  source TEXT NOT NULL,
  PRIMARY KEY (source, id),
  FOREIGN KEY (source, product_id) REFERENCES products (source, id)
);

CREATE INDEX IF NOT EXISTS idx_variants_product ON variants (source, product_id);
