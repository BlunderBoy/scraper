-- Fondovalle ceramice: collection -> product, shade -> variant
-- Run on LOCAL copy first, then: wrangler d1 execute DB --remote --file=backups/migrations/01-fondovalle.sql

PRAGMA foreign_keys = ON;

BEGIN IMMEDIATE;

-- Optional: override MIN(id) keepers when a colleague edited a specific product row
CREATE TEMP TABLE keeper_override (
  collection TEXT NOT NULL PRIMARY KEY,
  keeper_product_id INTEGER NOT NULL
);
-- INSERT INTO keeper_override (collection, keeper_product_id) VALUES
--   ('Homescape', 800);

CREATE TEMP TABLE pcm AS
SELECT
  p.id AS old_product_id,
  p.title AS old_title,
  TRIM(p.collection) AS collection,
  p.manufacturer,
  COALESCE(
    (SELECT ko.keeper_product_id FROM keeper_override ko
     WHERE ko.collection = TRIM(p.collection)),
    (SELECT MIN(p2.id) FROM products p2
     WHERE p2.manufacturer = p.manufacturer
       AND TRIM(p2.collection) = TRIM(p.collection))
  ) AS keeper_product_id
FROM products p
WHERE p.manufacturer = 'Fondovalle'
  AND TRIM(COALESCE(p.collection, '')) != '';

UPDATE variants
SET
  product_id = (SELECT keeper_product_id FROM pcm WHERE pcm.old_product_id = variants.product_id),
  color = (
    SELECT CASE
      WHEN TRIM(COALESCE(variants.color, '')) IN ('', 'Standard') THEN pcm.old_title
      WHEN variants.color = pcm.old_title THEN variants.color
      ELSE pcm.old_title || ' ' || variants.color
    END
    FROM pcm
    WHERE pcm.old_product_id = variants.product_id
  )
WHERE product_id IN (SELECT old_product_id FROM pcm);

UPDATE product_pdfs
SET product_id = (SELECT keeper_product_id FROM pcm WHERE pcm.old_product_id = product_pdfs.product_id)
WHERE product_id IN (SELECT old_product_id FROM pcm);

DELETE FROM product_pdfs
WHERE rowid NOT IN (
  SELECT MIN(rowid) FROM product_pdfs GROUP BY product_id, pdf_id
);

UPDATE products
SET title = collection
WHERE id IN (SELECT DISTINCT keeper_product_id FROM pcm);

DELETE FROM products
WHERE id IN (
  SELECT old_product_id FROM pcm WHERE old_product_id != keeper_product_id
);

COMMIT;
