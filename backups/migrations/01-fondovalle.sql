-- Fondovalle ceramice only (category = 'ceramice'). Mobilier rows (category = 'mobilier') are untouched.
-- Local:  sqlite3 backups\d1-local.db  →  BEGIN IMMEDIATE; .read backups/migrations/01-fondovalle.sql; COMMIT;
-- Remote: npx wrangler d1 execute DB --remote --file=backups/migrations/01-fondovalle.sql
--
-- Remote D1 cannot use the temp schema (CREATE TEMP TABLE → SQLITE_AUTH). Use _mig_* tables in main instead.

DROP TABLE IF EXISTS _mig_fv_pcm;
DROP TABLE IF EXISTS _mig_fv_keeper_override;

CREATE TABLE _mig_fv_keeper_override (
  collection TEXT NOT NULL PRIMARY KEY,
  keeper_product_id INTEGER NOT NULL
);
-- INSERT INTO _mig_fv_keeper_override (collection, keeper_product_id) VALUES
--   ('Homescape', 800);

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

DELETE FROM product_pdfs
WHERE rowid IN (
  SELECT pp.rowid
  FROM product_pdfs pp
  INNER JOIN _mig_fv_pcm ON _mig_fv_pcm.old_product_id = pp.product_id
  WHERE pp.rowid NOT IN (
    SELECT MIN(pp2.rowid)
    FROM product_pdfs pp2
    INNER JOIN _mig_fv_pcm ON _mig_fv_pcm.old_product_id = pp2.product_id
    GROUP BY _mig_fv_pcm.keeper_product_id, pp2.pdf_id
  )
);

UPDATE product_pdfs
SET product_id = (SELECT keeper_product_id FROM _mig_fv_pcm WHERE _mig_fv_pcm.old_product_id = product_pdfs.product_id)
WHERE product_id IN (SELECT old_product_id FROM _mig_fv_pcm);

DELETE FROM product_pdfs
WHERE rowid NOT IN (
  SELECT MIN(rowid) FROM product_pdfs GROUP BY product_id, pdf_id
);

UPDATE products
SET title = collection
WHERE id IN (SELECT DISTINCT keeper_product_id FROM _mig_fv_pcm);

DELETE FROM products
WHERE id IN (
  SELECT old_product_id FROM _mig_fv_pcm WHERE old_product_id != keeper_product_id
);

DROP TABLE _mig_fv_pcm;
DROP TABLE _mig_fv_keeper_override;
