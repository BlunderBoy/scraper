var __defProp = Object.defineProperty;
var __name = (target, value) => __defProp(target, "name", { value, configurable: true });

// Products API Worker — bind D1 as DB, R2 for uploads.
// Secret: PRODUCTS_JWT_SECRET (min 32 chars; must match Next.js env for admin JWT).
// src/index.js
var R2_BUCKET_URL = "https://pub-41f75af66e904ba3ba3fb13241e4c4c9.r2.dev";
var corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization"
};
var cacheHeaders = {
  "Cache-Control": "public, s-maxage=300, stale-while-revalidate=600"
};
var readJsonHeaders = { ...corsHeaders, ...cacheHeaders };
var SKU_MAX_LEN = 64;
var PRODUCTS_JWT_AUDIENCE = "products-worker";
var D1_IN_CHUNK_SIZE = 80;
function base64UrlToBytes(s) {
  let t = String(s).replace(/-/g, "+").replace(/_/g, "/");
  const pad = (4 - t.length % 4) % 4;
  t += "=".repeat(pad);
  const binary = atob(t);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}
__name(base64UrlToBytes, "base64UrlToBytes");


async function verifyMutatingJwtRequest(request, env) {
  const secret = env.PRODUCTS_JWT_SECRET;
  if (!secret || String(secret).length < 32) {
    return Response.json(
      { error: "Worker misconfigured: set PRODUCTS_JWT_SECRET (min 32 chars, same as Next)." },
      { status: 503, headers: corsHeaders }
    );
  }
  const auth = request.headers.get("Authorization") || "";
  const m = /^Bearer\s+(.+)$/i.exec(auth.trim());
  if (!m) {
    return Response.json({ error: "Unauthorized" }, { status: 401, headers: corsHeaders });
  }
  const token = m[1].trim();
  const parts = token.split(".");
  if (parts.length !== 3) {
    return Response.json({ error: "Unauthorized" }, { status: 401, headers: corsHeaders });
  }
  let payload;
  try {
    payload = JSON.parse(new TextDecoder().decode(base64UrlToBytes(parts[1])));
  } catch {
    return Response.json({ error: "Unauthorized" }, { status: 401, headers: corsHeaders });
  }
  const nowSec = Math.floor(Date.now() / 1e3);
  if (typeof payload.exp !== "number" || nowSec >= payload.exp) {
    return Response.json({ error: "Token expired" }, { status: 401, headers: corsHeaders });
  }
  const aud = payload.aud;
  if (aud !== PRODUCTS_JWT_AUDIENCE && (!Array.isArray(aud) || !aud.includes(PRODUCTS_JWT_AUDIENCE))) {
    return Response.json({ error: "Unauthorized" }, { status: 401, headers: corsHeaders });
  }
  try {
    const key = await crypto.subtle.importKey(
      "raw",
      new TextEncoder().encode(String(secret)),
      { name: "HMAC", hash: "SHA-256" },
      false,
      ["verify"]
    );
    const data = new TextEncoder().encode(`${parts[0]}.${parts[1]}`);
    const sig = base64UrlToBytes(parts[2]);
    const ok = await crypto.subtle.verify({ name: "HMAC", hash: "SHA-256" }, key, sig, data);
    if (!ok) {
      return Response.json({ error: "Unauthorized" }, { status: 401, headers: corsHeaders });
    }
  } catch {
    return Response.json({ error: "Unauthorized" }, { status: 401, headers: corsHeaders });
  }
  return null;
}
__name(verifyMutatingJwtRequest, "verifyMutatingJwtRequest");


function parseSkuFromBody(sku) {
  if (sku == null || String(sku).trim() === "") {
    return { ok: false, status: 400, error: "SKU is required" };
  }
  const upper = String(sku).trim().toUpperCase();
  if (upper.length > SKU_MAX_LEN) {
    return { ok: false, status: 400, error: `SKU must be at most ${SKU_MAX_LEN} characters` };
  }
  return { ok: true, value: upper };
}
__name(parseSkuFromBody, "parseSkuFromBody");


function isUniqueConstraintError(err) {
  const m = String(err && err.message || "");
  return m.includes("UNIQUE") || m.includes("unique constraint");
}
__name(isUniqueConstraintError, "isUniqueConstraintError");


function parseJsonColumn(raw, fallback) {
  const s = raw != null && String(raw).trim() !== "" ? String(raw) : null;
  if (s == null) return fallback;
  try {
    return JSON.parse(s);
  } catch {
    return fallback;
  }
}
__name(parseJsonColumn, "parseJsonColumn");


function chunkIdsForInClause(ids) {
  if (!ids || ids.length === 0) return [];
  const chunks = [];
  for (let i = 0; i < ids.length; i += D1_IN_CHUNK_SIZE) {
    chunks.push(ids.slice(i, i + D1_IN_CHUNK_SIZE));
  }
  return chunks;
}
__name(chunkIdsForInClause, "chunkIdsForInClause");


async function expandProductRows(productRows, env) {
  if (!productRows || productRows.length === 0) {
    return [];
  }
  const ids = productRows.map((p) => p.id);
  const variants = [];
  for (const chunk of chunkIdsForInClause(ids)) {
    const ph = chunk.map(() => "?").join(",");
    const { results: part } = await env.DB.prepare(
      `SELECT * FROM variants WHERE product_id IN (${ph})`
    ).bind(...chunk).all();
    variants.push(...part);
  }
  const variantsByProductId = /* @__PURE__ */ new Map();
  for (const v of variants) {
    v.gallery_photos = parseJsonColumn(v.gallery_photos, []);
    v.technical_photos = parseJsonColumn(v.technical_photos, []);
    v.tags = parseJsonColumn(v.tags, []);
    v.extra_data = parseJsonColumn(v.extra_data, {});
    const list = variantsByProductId.get(v.product_id) || [];
    list.push(v);
    variantsByProductId.set(v.product_id, list);
  }
  const pdfsByProductId = /* @__PURE__ */ new Map();
  if (ids.length > 0) {
    for (const chunk of chunkIdsForInClause(ids)) {
      const ph = chunk.map(() => "?").join(",");
      const { results: pdfJoinRows } = await env.DB.prepare(
        `SELECT pp.product_id, pp.pdf_id, pp.sort_order, tp.id AS tp_id, tp.title, tp.url, tp.r2_key, tp.created_at
			 FROM product_pdfs pp
			 INNER JOIN technical_pdfs tp ON tp.id = pp.pdf_id
			 WHERE pp.product_id IN (${ph})
			 ORDER BY pp.product_id, pp.sort_order ASC, pp.id ASC`
      ).bind(...chunk).all();
      for (const row of pdfJoinRows) {
        const list = pdfsByProductId.get(row.product_id) || [];
        list.push({
          id: row.tp_id,
          title: row.title,
          url: row.url,
          r2_key: row.r2_key,
          sort_order: row.sort_order,
          created_at: row.created_at
        });
        pdfsByProductId.set(row.product_id, list);
      }
    }
  }
  const catalogIds = [...new Set(productRows.map((p) => p.catalog_id).filter((cid) => cid != null && cid !== ""))].map((cid) => Number(cid)).filter((n) => !Number.isNaN(n));
  const catalogById = /* @__PURE__ */ new Map();
  if (catalogIds.length > 0) {
    for (const chunk of chunkIdsForInClause(catalogIds)) {
      const ph = chunk.map(() => "?").join(",");
      const { results: catRows } = await env.DB.prepare(
        `SELECT id, title, manufacturer, r2_key, url, created_at FROM manufacturer_catalogs WHERE id IN (${ph})`
      ).bind(...chunk).all();
      for (const c of catRows) {
        catalogById.set(c.id, c);
      }
    }
  }
  return productRows.map((p) => {
    const cid = p.catalog_id != null && p.catalog_id !== "" ? Number(p.catalog_id) : null;
    const catalog = cid != null && !Number.isNaN(cid) ? catalogById.get(cid) || null : null;
    return {
      ...p,
      variants: variantsByProductId.get(p.id) || [],
      pdfs: pdfsByProductId.get(p.id) || [],
      catalog
    };
  });
}
__name(expandProductRows, "expandProductRows");


function nullableText(raw) {
  if (raw == null) return null;
  const s = String(raw).trim();
  return s === "" ? null : s;
}
__name(nullableText, "nullableText");


async function uploadPdfToR2(file, env) {
  const name = file.name || "document.pdf";
  const ext = String(name.split(".").pop() || "pdf").toLowerCase();
  const safeExt = ext === "pdf" ? "pdf" : "pdf";
  const uniqueId = Math.random().toString(36).substring(7);
  const key = `pdfs/${Date.now()}-${uniqueId}.${safeExt}`;
  const contentType = file.type && String(file.type).toLowerCase().includes("pdf") ? file.type : "application/pdf";
  await env.R2.put(key, file.stream(), { httpMetadata: { contentType } });
  return { key, url: `${R2_BUCKET_URL}/${key}` };
}
__name(uploadPdfToR2, "uploadPdfToR2");


async function uploadCatalogPdfToR2(file, env) {
  const uniqueId = Math.random().toString(36).substring(7);
  const key = `catalogs/${Date.now()}-${uniqueId}.pdf`;
  const contentType = file.type && String(file.type).toLowerCase().includes("pdf") ? file.type : "application/pdf";
  await env.R2.put(key, file.stream(), { httpMetadata: { contentType } });
  return { key, url: `${R2_BUCKET_URL}/${key}` };
}
__name(uploadCatalogPdfToR2, "uploadCatalogPdfToR2");


async function catalogExists(env, catalogId) {
  const row = await env.DB.prepare(`SELECT id FROM manufacturer_catalogs WHERE id = ?`).bind(catalogId).first();
  return !!row;
}
__name(catalogExists, "catalogExists");


async function handleListProducts(request, env) {
  try {
    const { searchParams } = new URL(request.url);
    const category = searchParams.get("category");
    const subtype = searchParams.get("subtype");
    const manufacturer = searchParams.get("manufacturer");
    const expandRaw = searchParams.get("expand");
    const expand = expandRaw === "1" || expandRaw === "true";
    const conditions = [];
    const binds = [];
    if (category) {
      conditions.push("category = ?");
      binds.push(category);
    }
    if (subtype) {
      conditions.push("subtype = ?");
      binds.push(subtype);
    }
    if (manufacturer) {
      conditions.push("manufacturer = ?");
      binds.push(manufacturer);
    }
    let query = `SELECT * FROM products`;
    if (conditions.length) {
      query += ` WHERE ${conditions.join(" AND ")}`;
    }
    query += ` ORDER BY id DESC`;
    const { results } = await env.DB.prepare(query).bind(...binds).all();
    if (expand) {
      const expanded = await expandProductRows(results, env);
      return Response.json(expanded, { headers: readJsonHeaders });
    }
    return Response.json(results, { headers: readJsonHeaders });
  } catch (err) {
    console.error("[products-worker] handleListProducts", request.url, err);
    const message = err instanceof Error ? err.message : String(err);
    const stack = err instanceof Error ? err.stack : void 0;
    return Response.json({ error: message, stack }, { status: 500, headers: corsHeaders });
  }
}
__name(handleListProducts, "handleListProducts");


async function handleCreateProduct(request, env) {
  try {
    const body = await request.json();
    const {
      title,
      description,
      category,
      collection,
      is_new,
      subtype,
      manufacturer,
      catalog_id,
      position,
      sizes,
      thickness,
      material,
      shape,
      cut,
      finishes
    } = body;
    const newFlag = is_new === true || is_new === 1 ? 1 : 0;
    const sub = subtype != null && subtype !== "" ? String(subtype) : null;
    const mfr = manufacturer != null && manufacturer !== "" ? String(manufacturer) : null;
    let catalogIdBind = null;
    if (catalog_id != null && catalog_id !== "") {
      const cid = Number(catalog_id);
      if (Number.isNaN(cid)) {
        return Response.json({ error: "Invalid catalog_id" }, { status: 400, headers: corsHeaders });
      }
      if (!(await catalogExists(env, cid))) {
        return Response.json({ error: "catalog_id not found" }, { status: 400, headers: corsHeaders });
      }
      catalogIdBind = cid;
    }
    const result = await env.DB.prepare(
      `INSERT INTO products (title, description, category, collection, subtype, manufacturer, is_new,
			 catalog_id, position, sizes, thickness, material, shape, cut, finishes)
			 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
    ).bind(
      title,
      description || "",
      category || "",
      collection || "",
      sub,
      mfr,
      newFlag,
      catalogIdBind,
      nullableText(position),
      nullableText(sizes),
      nullableText(thickness),
      nullableText(material),
      nullableText(shape),
      nullableText(cut),
      nullableText(finishes)
    ).run();
    return Response.json({ success: true, productId: result.meta.last_row_id }, { headers: corsHeaders });
  } catch (err) {
    return Response.json({ error: err.message }, { status: 500, headers: corsHeaders });
  }
}
__name(handleCreateProduct, "handleCreateProduct");


async function handleUpdateProduct(productId, request, env) {
  try {
    const body = await request.json();
    const existing = await env.DB.prepare(`SELECT * FROM products WHERE id = ?`).bind(productId).first();
    if (!existing) {
      return Response.json({ error: "Not found" }, { status: 404, headers: corsHeaders });
    }
    const merged = { ...existing, ...body };
    const newFlag = merged.is_new === true || merged.is_new === 1 ? 1 : 0;
    const sub = merged.subtype != null && merged.subtype !== "" ? String(merged.subtype) : null;
    const mfr = merged.manufacturer != null && merged.manufacturer !== "" ? String(merged.manufacturer) : null;
    let catalogIdBind = null;
    if (merged.catalog_id != null && String(merged.catalog_id).trim() !== "") {
      const cid = Number(merged.catalog_id);
      if (Number.isNaN(cid)) {
        return Response.json({ error: "Invalid catalog_id" }, { status: 400, headers: corsHeaders });
      }
      if (!(await catalogExists(env, cid))) {
        return Response.json({ error: "catalog_id not found" }, { status: 400, headers: corsHeaders });
      }
      catalogIdBind = cid;
    }
    await env.DB.prepare(
      `UPDATE products
			 SET title = ?,
					 description = ?,
					 category = ?,
					 collection = ?,
					 subtype = ?,
					 manufacturer = ?,
					 is_new = ?,
					 catalog_id = ?,
					 position = ?,
					 sizes = ?,
					 thickness = ?,
					 material = ?,
					 shape = ?,
					 cut = ?,
					 finishes = ?
			 WHERE id = ?`
    ).bind(
      merged.title,
      merged.description,
      merged.category,
      merged.collection,
      sub,
      mfr,
      newFlag,
      catalogIdBind,
      nullableText(merged.position),
      nullableText(merged.sizes),
      nullableText(merged.thickness),
      nullableText(merged.material),
      nullableText(merged.shape),
      nullableText(merged.cut),
      nullableText(merged.finishes),
      productId
    ).run();
    return Response.json({ success: true }, { headers: corsHeaders });
  } catch (err) {
    return Response.json({ error: err.message }, { status: 500, headers: corsHeaders });
  }
}
__name(handleUpdateProduct, "handleUpdateProduct");


async function handleGetFullProduct(productId, env) {
  try {
    const product = await env.DB.prepare(`SELECT * FROM products WHERE id = ?`).bind(productId).first();
    if (!product) {
      return Response.json({ error: "Not found" }, { status: 404, headers: corsHeaders });
    }
    const [full] = await expandProductRows([product], env);
    return Response.json(full, { headers: readJsonHeaders });
  } catch (err) {
    return Response.json({ error: err.message }, { status: 500, headers: corsHeaders });
  }
}
__name(handleGetFullProduct, "handleGetFullProduct");


async function handleDeleteProduct(productId, env) {
  await env.DB.prepare(`DELETE
												FROM products
												WHERE id = ?`).bind(productId).run();
  return Response.json({ success: true }, { headers: corsHeaders });
}
__name(handleDeleteProduct, "handleDeleteProduct");


function normalizeUrlList(raw) {
  if (raw == null) return [];
  let arr = raw;
  if (typeof raw === "string") {
    try {
      arr = JSON.parse(raw);
    } catch {
      return [];
    }
  }
  if (!Array.isArray(arr)) return [];
  const out = [];
  for (const v of arr) {
    if (v == null) continue;
    const s = String(v).trim();
    if (s) out.push(s);
  }
  return out;
}
__name(normalizeUrlList, "normalizeUrlList");


async function handleCreateVariant(request, env) {
  try {
    const body = await request.json();
    const {
      productId, color, tags, extra_data, sku, url_on_manufacturer_website,
      gallery_photos, technical_photos
    } = body;
    const parsed = parseSkuFromBody(sku);
    if (!parsed.ok) {
      return Response.json({ error: parsed.error }, { status: parsed.status, headers: corsHeaders });
    }
    const urlMfr = nullableText(url_on_manufacturer_website);
    const gallery = normalizeUrlList(gallery_photos);
    const technical = normalizeUrlList(technical_photos);
    const res = await env.DB.prepare(
      `INSERT INTO variants (product_id, color, tags, extra_data, sku, url_on_manufacturer_website,
                             gallery_photos, technical_photos)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?)`
    ).bind(
      productId,
      color,
      JSON.stringify(tags || []),
      JSON.stringify(extra_data || {}),
      parsed.value,
      urlMfr,
      JSON.stringify(gallery),
      JSON.stringify(technical)
    ).run();
    return Response.json({ success: true, variantId: res.meta.last_row_id }, { headers: corsHeaders });
  } catch (err) {
    if (isUniqueConstraintError(err)) {
      return Response.json({ error: "SKU already in use" }, { status: 409, headers: corsHeaders });
    }
    return Response.json({ error: err.message }, { status: 500, headers: corsHeaders });
  }
}
__name(handleCreateVariant, "handleCreateVariant");


async function handleUpdateVariant(variantId, request, env) {
  try {
    const body = await request.json();
    const {
      color, tags, extra_data, sku, url_on_manufacturer_website,
      gallery_photos, technical_photos
    } = body;
    const parsed = parseSkuFromBody(sku);
    if (!parsed.ok) {
      return Response.json({ error: parsed.error }, { status: parsed.status, headers: corsHeaders });
    }
    const extraDataString = typeof extra_data === "string" ? extra_data : JSON.stringify(extra_data || {});
    const urlMfr = nullableText(url_on_manufacturer_website);
    const gallery = normalizeUrlList(gallery_photos);
    const technical = normalizeUrlList(technical_photos);
    await env.DB.prepare(
      `UPDATE variants
          SET color = ?,
              tags = ?,
              extra_data = ?,
              sku = ?,
              url_on_manufacturer_website = ?,
              gallery_photos = ?,
              technical_photos = ?
        WHERE id = ?`
    ).bind(
      color,
      JSON.stringify(tags || []),
      extraDataString,
      parsed.value,
      urlMfr,
      JSON.stringify(gallery),
      JSON.stringify(technical),
      variantId
    ).run();
    return Response.json({ success: true }, { headers: corsHeaders });
  } catch (err) {
    if (isUniqueConstraintError(err)) {
      return Response.json({ error: "SKU already in use" }, { status: 409, headers: corsHeaders });
    }
    return Response.json({ error: err.message }, { status: 500, headers: corsHeaders });
  }
}
__name(handleUpdateVariant, "handleUpdateVariant");


async function handleDeleteVariant(variantId, env) {
  await env.DB.prepare(`DELETE
												FROM variants
												WHERE id = ?`).bind(variantId).run();
  return Response.json({ success: true }, { headers: corsHeaders });
}
__name(handleDeleteVariant, "handleDeleteVariant");


async function handleGetNewItems(request, env) {
  try {
    const { searchParams } = new URL(request.url);
    const expandRaw = searchParams.get("expand");
    const expand = expandRaw === "1" || expandRaw === "true";
    const { results: flaggedNew } = await env.DB.prepare(`
			SELECT *
			FROM products
			WHERE is_new = 1
			ORDER BY created_at DESC
			LIMIT 20
		`).all();
    let items = [...flaggedNew];
    if (items.length < 20) {
      const needed = 20 - items.length;
      const ids = items.map((p) => p.id);
      const placeholders = ids.length ? ids.map(() => "?").join(",") : null;
      let query = `SELECT * FROM products`;
      if (ids.length) {
        query += ` WHERE id NOT IN (${placeholders})`;
      }
      query += ` ORDER BY created_at DESC LIMIT ?`;
      const stmt = env.DB.prepare(query);
      const bindValues = ids.length ? [...ids, needed] : [needed];
      const { results: filler } = await stmt.bind(...bindValues).all();
      items = items.concat(filler);
    }
    if (expand) {
      const expanded = await expandProductRows(items, env);
      return Response.json(expanded, { headers: readJsonHeaders });
    }
    return Response.json(items, { headers: readJsonHeaders });
  } catch (err) {
    return Response.json({ error: err.message }, { status: 500, headers: corsHeaders });
  }
}
__name(handleGetNewItems, "handleGetNewItems");


async function handleMetaSubtypesByCategory(env) {
  try {
    const { results } = await env.DB.prepare(
      `SELECT DISTINCT category, subtype FROM products
			 WHERE subtype IS NOT NULL AND TRIM(subtype) != ''`
    ).all();
    const map = {};
    for (const row of results) {
      const cat = row.category ? String(row.category).toLowerCase() : "";
      const sub = row.subtype ? String(row.subtype).trim() : "";
      if (!cat || !sub) continue;
      if (!map[cat]) map[cat] = [];
      if (!map[cat].includes(sub)) map[cat].push(sub);
    }
    for (const k of Object.keys(map)) {
      map[k].sort((a, b) => a.localeCompare(b, "ro", { sensitivity: "base" }));
    }
    return Response.json(map, { headers: readJsonHeaders });
  } catch (err) {
    return Response.json({ error: err.message }, { status: 500, headers: corsHeaders });
  }
}
__name(handleMetaSubtypesByCategory, "handleMetaSubtypesByCategory");


async function handleMetaSubtypesForCategory(request, env) {
  try {
    const category = new URL(request.url).searchParams.get("category");
    if (!category) {
      return Response.json({ error: "Missing category" }, { status: 400, headers: corsHeaders });
    }
    const { results } = await env.DB.prepare(
      `SELECT DISTINCT subtype FROM products
			 WHERE category = ? AND subtype IS NOT NULL AND TRIM(subtype) != ''
			 ORDER BY subtype`
    ).bind(category).all();
    const list = results.map((r) => r.subtype).filter(Boolean);
    return Response.json(list, { headers: readJsonHeaders });
  } catch (err) {
    return Response.json({ error: err.message }, { status: 500, headers: corsHeaders });
  }
}
__name(handleMetaSubtypesForCategory, "handleMetaSubtypesForCategory");


async function handleMetaManufacturers(request, env) {
  try {
    const { searchParams } = new URL(request.url);
    const category = searchParams.get("category");
    const isNew = searchParams.get("is_new");
    let query = `SELECT DISTINCT manufacturer FROM products WHERE manufacturer IS NOT NULL AND TRIM(manufacturer) != ''`;
    const binds = [];
    if (category) {
      query += ` AND category = ?`;
      binds.push(category);
    }
    if (isNew === "1" || isNew === "true") {
      query += ` AND is_new = 1`;
    }
    query += ` ORDER BY manufacturer`;
    const { results } = await env.DB.prepare(query).bind(...binds).all();
    const list = results.map((r) => r.manufacturer).filter(Boolean);
    return Response.json(list, { headers: readJsonHeaders });
  } catch (err) {
    return Response.json({ error: err.message }, { status: 500, headers: corsHeaders });
  }
}
__name(handleMetaManufacturers, "handleMetaManufacturers");


async function handleMetaColors(request, env) {
  try {
    const { searchParams } = new URL(request.url);
    const category = searchParams.get("category");
    const isNew = searchParams.get("is_new");
    let query = `SELECT DISTINCT v.color AS color
			FROM variants v
			INNER JOIN products p ON p.id = v.product_id
			WHERE v.color IS NOT NULL AND TRIM(v.color) != ''`;
    const binds = [];
    if (category) {
      query += ` AND p.category = ?`;
      binds.push(category);
    }
    if (isNew === "1" || isNew === "true") {
      query += ` AND p.is_new = 1`;
    }
    query += ` ORDER BY v.color`;
    const { results } = await env.DB.prepare(query).bind(...binds).all();
    const list = results.map((r) => r.color).filter(Boolean);
    return Response.json(list, { headers: readJsonHeaders });
  } catch (err) {
    return Response.json({ error: err.message }, { status: 500, headers: corsHeaders });
  }
}
__name(handleMetaColors, "handleMetaColors");


async function handleListTechnicalPdfs(env) {
  try {
    const { results } = await env.DB.prepare(`SELECT id, title, r2_key, url, created_at FROM technical_pdfs ORDER BY id DESC`).all();
    return Response.json(results, { headers: readJsonHeaders });
  } catch (err) {
    return Response.json({ error: err.message }, { status: 500, headers: corsHeaders });
  }
}
__name(handleListTechnicalPdfs, "handleListTechnicalPdfs");


async function handleCreateTechnicalPdf(request, env) {
  try {
    const formData = await request.formData();
    const file = formData.get("file");
    const titleRaw = formData.get("title");
    const title = titleRaw != null && String(titleRaw).trim() !== "" ? String(titleRaw).trim() : file && file.name ? String(file.name) : "Document";
    if (!file || typeof file.stream !== "function") {
      return Response.json({ error: "file is required" }, { status: 400, headers: corsHeaders });
    }
    const { key, url } = await uploadPdfToR2(file, env);
    const res = await env.DB.prepare(`INSERT INTO technical_pdfs (title, r2_key, url) VALUES (?, ?, ?)`).bind(title, key, url).run();
    return Response.json({ success: true, pdfId: res.meta.last_row_id, url, title, r2_key: key }, { headers: corsHeaders });
  } catch (err) {
    return Response.json({ error: err.message }, { status: 500, headers: corsHeaders });
  }
}
__name(handleCreateTechnicalPdf, "handleCreateTechnicalPdf");


async function handleLinkOrUploadProductPdfs(productId, request, env) {
  try {
    const product = await env.DB.prepare(`SELECT id FROM products WHERE id = ?`).bind(productId).first();
    if (!product) {
      return Response.json({ error: "Product not found" }, { status: 404, headers: corsHeaders });
    }
    const ct = (request.headers.get("Content-Type") || "").toLowerCase();
    if (ct.includes("application/json")) {
      const body = await request.json();
      const pdfId = Number(body.pdf_id);
      const sortOrder = body.sort_order != null && body.sort_order !== "" ? Number(body.sort_order) : 0;
      if (Number.isNaN(pdfId)) {
        return Response.json({ error: "pdf_id is required" }, { status: 400, headers: corsHeaders });
      }
      const pdfRow = await env.DB.prepare(`SELECT id FROM technical_pdfs WHERE id = ?`).bind(pdfId).first();
      if (!pdfRow) {
        return Response.json({ error: "pdf_id not found" }, { status: 400, headers: corsHeaders });
      }
      try {
        await env.DB.prepare(
          `INSERT INTO product_pdfs (product_id, pdf_id, sort_order) VALUES (?, ?, ?)`
        ).bind(productId, pdfId, Number.isNaN(sortOrder) ? 0 : sortOrder).run();
      } catch (err) {
        if (isUniqueConstraintError(err)) {
          return Response.json({ error: "PDF already linked to this product" }, { status: 409, headers: corsHeaders });
        }
        throw err;
      }
      return Response.json({ success: true }, { headers: corsHeaders });
    }
    const formData = await request.formData();
    const files = formData.getAll("files");
    const titles = formData.getAll("titles");
    const sortOrders = formData.getAll("sort_orders");
    if (!files.length) {
      return Response.json({ error: "files required" }, { status: 400, headers: corsHeaders });
    }
    let uploadedCount = 0;
    for (let i = 0; i < files.length; i++) {
      const file = files[i];
      if (!file || typeof file.stream !== "function") continue;
      uploadedCount++;
      const title = titles[i] != null && String(titles[i]).trim() !== "" ? String(titles[i]).trim() : file.name || "Document";
      const soRaw = sortOrders[i];
      const sortOrder = soRaw != null && String(soRaw).trim() !== "" ? Number(soRaw) : 0;
      const { key, url } = await uploadPdfToR2(file, env);
      const res = await env.DB.prepare(`INSERT INTO technical_pdfs (title, r2_key, url) VALUES (?, ?, ?)`).bind(title, key, url).run();
      const pdfId = res.meta.last_row_id;
      try {
        await env.DB.prepare(`INSERT INTO product_pdfs (product_id, pdf_id, sort_order) VALUES (?, ?, ?)`).bind(
          productId,
          pdfId,
          Number.isNaN(sortOrder) ? 0 : sortOrder
        ).run();
      } catch (err) {
        if (isUniqueConstraintError(err)) {
          return Response.json({ error: "Duplicate PDF link" }, { status: 409, headers: corsHeaders });
        }
        throw err;
      }
    }
    if (uploadedCount === 0) {
      return Response.json({ error: "No valid file uploads" }, { status: 400, headers: corsHeaders });
    }
    return Response.json({ success: true }, { headers: corsHeaders });
  } catch (err) {
    return Response.json({ error: err.message }, { status: 500, headers: corsHeaders });
  }
}
__name(handleLinkOrUploadProductPdfs, "handleLinkOrUploadProductPdfs");


async function handleUnlinkProductPdf(productId, pdfId, env) {
  try {
    const pid = Number(pdfId);
    if (Number.isNaN(pid)) {
      return Response.json({ error: "Invalid pdf id" }, { status: 400, headers: corsHeaders });
    }
    await env.DB.prepare(`DELETE FROM product_pdfs WHERE product_id = ? AND pdf_id = ?`).bind(productId, pid).run();
    return Response.json({ success: true }, { headers: corsHeaders });
  } catch (err) {
    return Response.json({ error: err.message }, { status: 500, headers: corsHeaders });
  }
}
__name(handleUnlinkProductPdf, "handleUnlinkProductPdf");


async function handleListCatalogs(env) {
  try {
    const { results } = await env.DB.prepare(
      `SELECT id, title, manufacturer, r2_key, url, created_at FROM manufacturer_catalogs ORDER BY title ASC, id DESC`
    ).all();
    return Response.json(results, { headers: readJsonHeaders });
  } catch (err) {
    return Response.json({ error: err.message }, { status: 500, headers: corsHeaders });
  }
}
__name(handleListCatalogs, "handleListCatalogs");


async function handleCreateCatalog(request, env) {
  try {
    const formData = await request.formData();
    const file = formData.get("file");
    const titleRaw = formData.get("title");
    const title = titleRaw != null && String(titleRaw).trim() !== "" ? String(titleRaw).trim() : "Catalog";
    const manufacturer = nullableText(formData.get("manufacturer"));
    if (!file || typeof file.stream !== "function") {
      return Response.json({ error: "file is required" }, { status: 400, headers: corsHeaders });
    }
    const { key, url } = await uploadCatalogPdfToR2(file, env);
    const res = await env.DB.prepare(
      `INSERT INTO manufacturer_catalogs (title, manufacturer, r2_key, url) VALUES (?, ?, ?, ?)`
    ).bind(title, manufacturer, key, url).run();
    return Response.json({ success: true, catalogId: res.meta.last_row_id, url, title, r2_key: key }, { headers: corsHeaders });
  } catch (err) {
    return Response.json({ error: err.message }, { status: 500, headers: corsHeaders });
  }
}
__name(handleCreateCatalog, "handleCreateCatalog");


async function handleUpdateCatalog(catalogId, request, env) {
  try {
    const existing = await env.DB.prepare(`SELECT * FROM manufacturer_catalogs WHERE id = ?`).bind(catalogId).first();
    if (!existing) {
      return Response.json({ error: "Not found" }, { status: 404, headers: corsHeaders });
    }
    const body = await request.json();
    const merged = { ...existing, ...body };
    const title = String(merged.title ?? "").trim() || String(existing.title ?? "").trim() || "Catalog";
    await env.DB.prepare(
      `UPDATE manufacturer_catalogs SET title = ?, manufacturer = ? WHERE id = ?`
    ).bind(title, nullableText(merged.manufacturer), catalogId).run();
    return Response.json({ success: true }, { headers: corsHeaders });
  } catch (err) {
    return Response.json({ error: err.message }, { status: 500, headers: corsHeaders });
  }
}
__name(handleUpdateCatalog, "handleUpdateCatalog");


async function handleDeleteCatalog(catalogId, env) {
  try {
    const row = await env.DB.prepare(`SELECT r2_key FROM manufacturer_catalogs WHERE id = ?`).bind(catalogId).first();
    if (!row) {
      return Response.json({ error: "Not found" }, { status: 404, headers: corsHeaders });
    }
    await env.DB.prepare(`DELETE FROM manufacturer_catalogs WHERE id = ?`).bind(catalogId).run();
    try {
      await env.R2.delete(row.r2_key);
    } catch {
    }
    return Response.json({ success: true }, { headers: corsHeaders });
  } catch (err) {
    return Response.json({ error: err.message }, { status: 500, headers: corsHeaders });
  }
}
__name(handleDeleteCatalog, "handleDeleteCatalog");


async function handleListPairings(env) {
  const { results } = await env.DB.prepare(`SELECT *
							FROM pairings
							ORDER BY created_at DESC`).all();
  return Response.json(results, { headers: corsHeaders });
}
__name(handleListPairings, "handleListPairings");


async function handleCreatePairing(request, env) {
  const { tag_left, tag_right } = await request.json();
  const res = await env.DB.prepare(
    `INSERT INTO pairings (tag_left, tag_right)
		 VALUES (?, ?)`
  ).bind(tag_left, tag_right).run();
  return Response.json(
    { success: true, id: res.meta.last_row_id },
    { headers: corsHeaders }
  );
}
__name(handleCreatePairing, "handleCreatePairing");


async function handleUpdatePairing(pairingId, request, env) {
  const { tag_left, tag_right } = await request.json();
  await env.DB.prepare(
    `UPDATE pairings
		 SET tag_left = ?,
				 tag_right = ?
		 WHERE id = ?`
  ).bind(tag_left, tag_right, pairingId).run();
  return Response.json({ success: true }, { headers: corsHeaders });
}
__name(handleUpdatePairing, "handleUpdatePairing");


async function handleDeletePairing(pairingId, env) {
  await env.DB.prepare(`DELETE
												FROM pairings
												WHERE id = ?`).bind(pairingId).run();
  return Response.json({ success: true }, { headers: corsHeaders });
}
__name(handleDeletePairing, "handleDeletePairing");


async function handleGetPairedProducts(productId, env) {
  try {
    const { results: variants } = await env.DB.prepare(
      `SELECT tags FROM variants WHERE product_id = ?`
    ).bind(productId).all();
    const productTags = /* @__PURE__ */ new Set();
    for (const v of variants) {
      const tags = parseJsonColumn(v.tags, []);
      tags.forEach((t) => productTags.add(t.toLowerCase().trim()));
    }
    if (productTags.size === 0) {
      return Response.json([], { headers: corsHeaders });
    }
    const { results: allPairings } = await env.DB.prepare(
      `SELECT tag_left, tag_right FROM pairings`
    ).all();
    const pairedTags = /* @__PURE__ */ new Set();
    for (const p of allPairings) {
      const left = p.tag_left.toLowerCase().trim();
      const right = p.tag_right.toLowerCase().trim();
      if (productTags.has(left)) pairedTags.add(right);
      if (productTags.has(right)) pairedTags.add(left);
    }
    if (pairedTags.size === 0) {
      return Response.json([], { headers: corsHeaders });
    }
    const tagArr = [...pairedTags];
    const likeConditions = tagArr.map(() => `LOWER(v.tags) LIKE ?`).join(" OR ");
    const likeBinds = tagArr.map((t) => `%"${t}"%`);
    const { results: matchedProducts } = await env.DB.prepare(`
			SELECT DISTINCT p.*
			FROM products p
						 JOIN variants v ON v.product_id = p.id
			WHERE p.id != ? AND (${likeConditions})
			LIMIT 12
		`).bind(productId, ...likeBinds).all();
    for (const product of matchedProducts) {
      const { results: pVariants } = await env.DB.prepare(
        `SELECT * FROM variants WHERE product_id = ?`
      ).bind(product.id).all();
      for (const v of pVariants) {
        v.gallery_photos = parseJsonColumn(v.gallery_photos, []);
        v.technical_photos = parseJsonColumn(v.technical_photos, []);
        v.tags = parseJsonColumn(v.tags, []);
        v.extra_data = parseJsonColumn(v.extra_data, {});
      }
      product.variants = pVariants;
    }
    return Response.json(matchedProducts, { headers: corsHeaders });
  } catch (err) {
    return Response.json({ error: err.message }, { status: 500, headers: corsHeaders });
  }
}
__name(handleGetPairedProducts, "handleGetPairedProducts");


function buildProductSearchBlob(product) {
  const parts = [];
  for (const key of Object.keys(product)) {
    if (key === "variants" || key === "pdfs" || key === "catalog") continue;
    const val = product[key];
    if (val == null) continue;
    parts.push(String(val));
  }
  if (product.catalog) {
    parts.push(String(product.catalog.title ?? ""));
  }
  for (const v of product.variants || []) {
    parts.push(
      String(v.id ?? ""),
      String(v.sku ?? ""),
      String(v.color ?? ""),
      String(v.url_on_manufacturer_website ?? "")
    );
    const tags = v.tags || [];
    for (const t of tags) {
      parts.push(String(t));
    }
    parts.push(JSON.stringify(v.extra_data ?? {}));
    for (const url of v.gallery_photos || []) {
      parts.push(String(url));
    }
    for (const url of v.technical_photos || []) {
      parts.push(String(url));
    }
  }
  return parts.join(" ").toLowerCase();
}
__name(buildProductSearchBlob, "buildProductSearchBlob");


async function handleSearchProducts(request, env) {
  try {
    const raw = new URL(request.url).searchParams.get("q");
    const trimmed = (raw || "").trim();
    if (!trimmed) {
      return Response.json([], { headers: readJsonHeaders });
    }
    const needle = trimmed.toLowerCase();
    const { results } = await env.DB.prepare(`SELECT * FROM products ORDER BY id DESC`).all();
    const expanded = await expandProductRows(results, env);
    const filtered = expanded.filter((p) => buildProductSearchBlob(p).includes(needle));
    return Response.json(filtered, { headers: readJsonHeaders });
  } catch (err) {
    return Response.json({ error: err.message }, { status: 500, headers: corsHeaders });
  }
}
__name(handleSearchProducts, "handleSearchProducts");


var index_default = {
  async fetch(request, env) {
    try {
      const { pathname } = new URL(request.url);
      const method = request.method;
      if (method === "OPTIONS") return new Response(null, { headers: corsHeaders });
      const isMutating = method === "POST" || method === "PUT" || method === "DELETE";
      if (isMutating) {
        const authError = await verifyMutatingJwtRequest(request, env);
        if (authError) return authError;
      }
      if (pathname === "/technical-pdfs" && method === "GET") return await handleListTechnicalPdfs(env);
    if (pathname === "/technical-pdfs" && method === "POST") return await handleCreateTechnicalPdf(request, env);
    if (pathname === "/catalogs" && method === "GET") return await handleListCatalogs(env);
    if (pathname === "/catalogs" && method === "POST") return await handleCreateCatalog(request, env);
    const catalogMatch = pathname.match(/^\/catalogs\/(\d+)$/);
    if (catalogMatch) {
      if (method === "PUT") return await handleUpdateCatalog(catalogMatch[1], request, env);
      if (method === "DELETE") return await handleDeleteCatalog(catalogMatch[1], env);
    }
    if (pathname === "/pairings" && method === "GET") return await handleListPairings(env);
    if (pathname === "/pairings" && method === "POST") return await handleCreatePairing(request, env);
    const pairingMatch = pathname.match(/^\/pairings\/(\d+)$/);
    if (pairingMatch) {
      if (method === "PUT")
        return await handleUpdatePairing(pairingMatch[1], request, env);
      if (method === "DELETE")
        return await handleDeletePairing(pairingMatch[1], env);
    }
    if (pathname === "/products/meta/subtypes-by-category" && method === "GET")
      return await handleMetaSubtypesByCategory(env);
    if (pathname === "/products/meta/subtypes" && method === "GET")
      return await handleMetaSubtypesForCategory(request, env);
    if (pathname === "/products/meta/manufacturers" && method === "GET")
      return await handleMetaManufacturers(request, env);
    if (pathname === "/products/meta/colors" && method === "GET")
      return await handleMetaColors(request, env);
    if (pathname === "/products/new" && method === "GET") return await handleGetNewItems(request, env);
    if (pathname === "/products/search" && method === "GET") return await handleSearchProducts(request, env);
    if (pathname === "/products" && method === "GET") return await handleListProducts(request, env);
    if (pathname === "/products" && method === "POST") return await handleCreateProduct(request, env);
    const pairedMatch = pathname.match(/^\/products\/(\d+)\/paired$/);
    if (pairedMatch && method === "GET") return await handleGetPairedProducts(pairedMatch[1], env);
    const productPdfPost = pathname.match(/^\/products\/(\d+)\/pdfs$/);
    if (productPdfPost && method === "POST") return await handleLinkOrUploadProductPdfs(productPdfPost[1], request, env);
    const productPdfDel = pathname.match(/^\/products\/(\d+)\/pdfs\/(\d+)$/);
    if (productPdfDel && method === "DELETE") return await handleUnlinkProductPdf(productPdfDel[1], productPdfDel[2], env);
    const productMatch = pathname.match(/^\/products\/(\d+)$/);
    if (productMatch) {
      if (method === "GET") return await handleGetFullProduct(productMatch[1], env);
      if (method === "PUT") return await handleUpdateProduct(productMatch[1], request, env);
      if (method === "DELETE") return await handleDeleteProduct(productMatch[1], env);
    }
    if (pathname === "/variants" && method === "POST") return await handleCreateVariant(request, env);
    const variantMatch = pathname.match(/^\/variants\/(\d+)$/);
    if (variantMatch) {
      if (method === "PUT") return await handleUpdateVariant(variantMatch[1], request, env);
      if (method === "DELETE") return await handleDeleteVariant(variantMatch[1], env);
    }
    return new Response("Not Found", { status: 404, headers: corsHeaders });
    } catch (err) {
      console.error("[products-worker]", request.method, request.url, err);
      const message = err instanceof Error ? err.message : String(err);
      const stack = err instanceof Error ? err.stack : void 0;
      return Response.json({ error: message, stack }, { status: 500, headers: corsHeaders });
    }
  }
};
export {
  index_default as default
};
//# sourceMappingURL=index.js.map
