## Procedure
AI, DO NOT READ THIS FILE. THIS IS FOR HUMANS ONLY. (or pițe)

- identify manufacturer with problems
- fix scraper (for botched data) or use upload_photos_r2.py if their website is garbage
- identify what ids the existing data occupies
- make sure new data has unconflicting ids, either:
    - ask cursor to change the ids for the csvs 
    - use the starting id thingy from the scrapers
    - use the upload to cloudflare offset (kinda hard, most ids are not all starting in the same spot)
- merge csvs (delete old merged ones, `python merge_csvs.py "fantini sanitare"`)
- upload to D1 **with --id-offset 0**
- go into any admin page and refresh caches

## Commands to start from when cleaning up only one manufacturer from the db:
```sql
PRAGMA foreign_keys = ON;

DELETE FROM product_pdfs
WHERE product_id IN (SELECT id FROM products WHERE manufacturer = 'Rosa Splendiani');

DELETE FROM variants
WHERE product_id IN (SELECT id FROM products WHERE manufacturer = 'Rosa Splendiani');

DELETE FROM products
WHERE manufacturer = 'Rosa Splendiani';

DELETE FROM technical_pdfs
WHERE url LIKE '%rosasplendiani.it%';
```

## Commands to delete based on ids:

```sql
DELETE FROM product_pdfs WHERE product_id BETWEEN 2500 AND 2516;
DELETE FROM variants WHERE id BETWEEN 10000 AND 10036;
DELETE FROM products WHERE id BETWEEN 2500 AND 2516;
DELETE FROM technical_pdfs WHERE id IN (1221, 1233, 1244, 1255, 1266);
DELETE FROM product_pdfs WHERE id IN (1328, 1340, 1352, 1364, 1376, 1388);
```

## Commands to find out the ids:
```sql
SELECT * FROM products WHERE manufacturer = 'Rosa Splendiani';

SELECT * 
FROM variants
WHERE product_id IN (SELECT id FROM products WHERE manufacturer = 'Rosa Splendiani');

SELECT *
FROM variants
WHERE url_on_manufacturer_website LIKE '%rosasplendiani.it%';

SELECT *
FROM product_pdfs
WHERE product_id IN (SELECT id FROM products WHERE manufacturer = 'Rosa Splendiani');

SELECT *
FROM technical_pdfs
WHERE url LIKE '%rosasplendiani.it%';
```