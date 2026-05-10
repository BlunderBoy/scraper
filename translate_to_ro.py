#!/usr/bin/env python3
"""
Translate merged_products.csv (and the variants snapshot) to Romanian via
Cloudflare Workers AI.

The merged CSVs mix English, Italian, Spanish and -- inside descriptions --
sentences in two languages at once. This script replaces translatable cells
field-by-field with a context-aware Romanian rewrite (NOT word-for-word MT).
A SQLite cache keyed on ``(field, source_hash, model)`` keeps re-runs cheap.

What gets translated (``merged_products.csv`` only):
  title, description, category, type, subtype, collection, finishes,
  position, material, shape, cut

What is left untouched everywhere:
  ids, manufacturer, catalog_id, sku, url, gallery_photos, technical_photos,
  source, is_new, all numeric/dimension fields (sizes, thickness, diameter,
  length, width, height) and -- in ``merged_variants.csv`` -- every column
  including ``color`` (per project policy: colors and variant names are kept
  in their original form).

The translator only touches ``merged_products.csv``. ``merged_variants.csv``
is snapshotted to ``merged_variants_original.csv`` and otherwise written back
verbatim so downstream tooling sees a consistent ``_original`` pair.

Outputs (always in this directory):
  merged_products_original.csv  -- exact copy of the pre-translation CSV
  merged_variants_original.csv  -- exact copy of the variants CSV
  merged_products.csv           -- Romanian rewrite
  merged_variants.csv           -- unchanged (kept for symmetry with above)

CLI examples:
  python translate_to_ro.py                # default run; uses cache, full file
  python translate_to_ro.py --dry-run      # plan only, no API calls
  python translate_to_ro.py --limit 20     # first 20 product rows
  python translate_to_ro.py --force        # ignore cache, re-translate all
  python translate_to_ro.py --workers 2    # lower concurrency for tight rate limits
  python translate_to_ro.py --model @cf/meta/llama-3.1-8b-instruct-fast
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


_ROOT_DIR = Path(__file__).resolve().parent


def _load_env_stdlib(path: Path) -> None:
    """Same minimal ``KEY=VALUE`` parser as upload_cloudflare.py."""
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
        from dotenv import load_dotenv  # type: ignore[import-untyped]

        load_dotenv(env_path, override=True)
    except ImportError:
        _load_env_stdlib(env_path)


_load_project_env()


# ----------------------------- configuration --------------------------------

DEFAULT_MODEL = "@cf/meta/llama-3.1-8b-instruct-fast"

PRODUCTS_CSV = _ROOT_DIR / "merged_products.csv"
VARIANTS_CSV = _ROOT_DIR / "merged_variants.csv"
PRODUCTS_BACKUP = _ROOT_DIR / "merged_products_original.csv"
VARIANTS_BACKUP = _ROOT_DIR / "merged_variants_original.csv"
CACHE_DB = _ROOT_DIR / "translation_cache.sqlite"

# Columns of ``merged_products.csv`` we send through the LLM. Order here only
# affects logging; values are matched by column name.
PRODUCT_TRANSLATE_FIELDS: tuple[str, ...] = (
    "title",
    "description",
    "finishes",
    "position",
    "material",
    "shape",
    "cut",
)

# A small bilingual glossary the model has to follow. Italian/Spanish/English
# source spellings -> the Romanian phrase we want everywhere. Adding entries
# here is cheaper than fine-tuning: the model copies the mapping directly when
# the source contains the listed word.
GLOSSARY_RO: tuple[tuple[str, str], ...] = (
    # categories
    ("placi ceramice", "ceramice"),
    ("ceramic tile", "placă ceramică"),
    ("ceramic tiles", "placi ceramice"),
    ("piastrelle", "placi ceramice"),
    ("porcellanato", "gresie porțelanată"),
    ("porcelain", "porțelan"),
    # rooms / scopes
    ("bagno", "baie"),
    ("bathroom", "baie"),
    ("cucina", "bucătărie"),
    ("kitchen", "bucătărie"),
    # sanitary
    ("wash basin", "lavoar"),
    ("washbasin", "lavoar"),
    ("lavabo", "lavoar"),
    ("mixer", "baterie"),
    ("miscelatore", "baterie"),
    ("rubinetto", "baterie"),
    ("grifo", "baterie"),
    ("grifería", "baterie"),
    ("single-control", "monocomandă"),
    ("single control", "monocomandă"),
    ("monocomando", "monocomandă"),
    ("monomando", "monocomandă"),
    ("water spout", "pipă"),
    ("spout", "pipă"),
    ("bocca", "pipă"),
    ("caño", "pipă"),
    ("shower", "duș"),
    ("doccia", "duș"),
    ("ducha", "duș"),
    ("bath mixer", "baterie de cadă"),
    ("vasca", "cadă"),
    ("bañera", "cadă"),
    ("bidet", "bideu"),
    ("toilet", "WC"),
    ("vaso", "WC"),
    ("inodoro", "WC"),
    ("countertop", "blat"),
    ("deck-mounted", "cu montare pe blat"),
    ("deck mounted", "cu montare pe blat"),
    ("wall-mounted", "cu montare pe perete"),
    ("wall mounted", "cu montare pe perete"),
    ("floor-mounted", "cu montare pe pardoseală"),
    ("floor mounted", "cu montare pe pardoseală"),
    ("ceiling-mounted", "cu montare în plafon"),
    ("ceiling mounted", "cu montare în plafon"),
    ("freestanding", "freestanding"),
    ("built-in", "încastrat"),
    ("built in", "încastrat"),
    ("incasso", "încastrat"),
    ("empotrado", "încastrat"),
    ("pop-up drain", "ventil clic-clac"),
    ("pop up drain", "ventil clic-clac"),
    ("clic/clac", "clic-clac"),
    ("clic-clac", "clic-clac"),
    ("aerator", "aerator"),
    ("cartridge", "cartuș"),
    # ceramic / tile finishes
    ("matte", "mat"),
    ("matt", "mat"),
    ("opaco", "mat"),
    ("mate", "mat"),
    ("glossy", "lucios"),
    ("lucido", "lucios"),
    ("brillante", "lucios"),
    ("polished", "lustruit"),
    ("brushed", "periat"),
    ("satin", "satinat"),
    ("satinato", "satinat"),
    ("natural", "natural"),
    ("naturale", "natural"),
    ("anti-slip", "antiderapant"),
    ("antislip", "antiderapant"),
    ("structured", "structurat"),
    ("strutturato", "structurat"),
    ("textured", "texturat"),
    ("relief", "relief"),
    ("rectified", "rectificată"),
    ("rettificato", "rectificată"),
    # wood / parquet
    ("oak", "stejar"),
    ("rovere", "stejar"),
    ("roble", "stejar"),
    ("walnut", "nuc"),
    ("noce", "nuc"),
    ("maple", "arțar"),
    ("acero", "arțar"),
    ("ash", "frasin"),
    ("frassino", "frasin"),
    ("plank", "plank"),
    ("planks", "planks"),
    ("herringbone", "spic"),
    ("spina", "spic"),
    ("parquet", "parchet"),
    ("flooring", "pardoseală"),
    ("solid wood", "lemn masiv"),
    ("engineered wood", "lemn stratificat"),
    # materials
    ("stainless steel", "oțel inoxidabil"),
    ("acciaio inox", "oțel inoxidabil"),
    ("acero inoxidable", "oțel inoxidabil"),
    ("solid surface", "Solid Surface"),
    ("marble", "marmură"),
    ("marmo", "marmură"),
    ("mármol", "marmură"),
    ("granite", "granit"),
    ("travertine", "travertin"),
    # cuts / formats
    ("tile", "placă"),
    ("tiles", "placi"),
    ("placă ceramică", "placă ceramică"),
    ("strip", "fâșie"),
    ("plank format", "format plank"),
    # furniture
    ("sofa", "canapea"),
    ("divano", "canapea"),
    ("sillón", "fotoliu"),
    ("armchair", "fotoliu"),
    ("poltrona", "fotoliu"),
    ("chair", "scaun"),
    ("sedia", "scaun"),
    ("silla", "scaun"),
    ("table", "masă"),
    ("tavolo", "masă"),
    ("mesa", "masă"),
    ("coffee table", "masă de cafea"),
    ("side table", "masă laterală"),
    ("modular", "modular"),
    ("modulare", "modular"),
    ("sectional", "modular"),
    # generic
    ("collection", "colecție"),
    ("collezione", "colecție"),
    ("colección", "colecție"),
    ("design", "design"),
    ("designed by", "design"),
    ("dimensions", "dimensiuni"),
    ("dimensioni", "dimensiuni"),
    ("size", "dimensiune"),
    ("sizes", "dimensiuni"),
    ("thickness", "grosime"),
    ("spessore", "grosime"),
    ("espesor", "grosime"),
    ("length", "lungime"),
    ("lunghezza", "lungime"),
    ("longitud", "lungime"),
    ("width", "lățime"),
    ("larghezza", "lățime"),
    ("ancho", "lățime"),
    ("height", "înălțime"),
    ("altezza", "înălțime"),
    ("altura", "înălțime"),
    ("available", "disponibil"),
    ("available in", "disponibil în"),
    ("required", "necesar"),
    ("recommended", "recomandat"),
    ("with", "cu"),
    ("without", "fără"),
)

GLOSSARY_BLOCK = "\n".join(f"- {src} -> {tgt}" for src, tgt in GLOSSARY_RO)


SYSTEM_PROMPT_TEMPLATE = """\
You are a professional Romanian translator for an interior-design e-commerce \
catalogue (ceramic tiles, bathroom fittings, parquet flooring, furniture). \
You will receive a JSON array of items, each with a stable ``id``, a ``field`` \
name (one of: title, description, finishes, position, material, shape, cut) \
and a ``text`` value.

The source language varies per item: English, Italian, Spanish, or a mix of \
two within the same string (e.g., English headings followed by Italian \
paragraphs). Detect the language(s) automatically and produce fluent, \
idiomatic Romanian for every item.

Hard rules -- failure to follow these breaks downstream processing:
1. Return STRICT JSON of shape {{"items": [{{"id": "...", "text": "..."}}]}}. \
The number of items returned must equal the number of items received and every \
``id`` must match exactly.
2. Romanian must use proper diacritics: ă â î ș ț (and their capitals). Never \
leave English/Italian/Spanish words in the output unless rule 3 applies.
3. Preserve verbatim (do NOT translate):
   - Brand names, collection names and designer names (e.g., Fantini, ABK, \
Casalgrande Padana, Moooi, CleoSteel, Nostromo, Flora, AF/21, Davide Mercatali).
   - Material/stone/marble proper names: Carrara, Calacatta, Travertin/Travertino, \
Statuario, Bianco/Nero Marquina, Emperador, Botticino, etc.
   - Article codes (``Art. 0521474``), SKUs, model numbers, percentages, URLs, \
e-mail addresses.
   - Numbers, units and dimensions: ``mm``, ``cm``, ``m``, ``120x60``, ``8.5 mm``, \
``1/2"`` etc. Keep the original ``x`` or ``×`` separator and unit casing.
   - Section labels that already use Romanian text in the source (``Observații:``, \
``Articole complementare necesare:``).
4. Bulletted lists and line breaks (``\\n``, ``\\n\\n``) MUST be preserved \
exactly; only the words on each line change.
5. Use this domain glossary -- apply consistently whenever the source contains \
the listed word/phrase:
{glossary}
6. Keep the meaning, register and approximate length of the source. Do not \
add marketing copy, disclaimers, or sentences that were not in the source. \
Empty input strings stay empty.
7. For very short labels (single-word ``category``/``type``/``shape`` etc.), \
output a single Romanian word/phrase -- not a full sentence.

Respond ONLY with the JSON object, no commentary, no markdown fences."""


# ----------------------------- cache layer ----------------------------------


_CACHE_LOCK = threading.Lock()


def _open_cache(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS translations (
            field TEXT NOT NULL,
            source_hash TEXT NOT NULL,
            model TEXT NOT NULL,
            source TEXT NOT NULL,
            target TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (field, source_hash, model)
        )"""
    )
    conn.commit()
    return conn


def _hash_source(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _cache_get(conn: sqlite3.Connection, field: str, source: str, model: str) -> str | None:
    h = _hash_source(source)
    with _CACHE_LOCK:
        row = conn.execute(
            "SELECT target FROM translations WHERE field=? AND source_hash=? AND model=?",
            (field, h, model),
        ).fetchone()
    return row[0] if row else None


def _cache_put(
    conn: sqlite3.Connection, field: str, source: str, target: str, model: str
) -> None:
    h = _hash_source(source)
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with _CACHE_LOCK:
        conn.execute(
            "INSERT OR REPLACE INTO translations(field, source_hash, model, source, target, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (field, h, model, source, target, ts),
        )
        conn.commit()


# ----------------------------- CF Workers AI --------------------------------


class CloudflareAIError(RuntimeError):
    """Raised when the CF Workers AI call cannot be parsed into a usable response."""


class CloudflareAuthError(CloudflareAIError):
    """HTTP 401/403 from Workers AI -- token missing the Workers AI permission."""


def _cf_chat_completion(
    account_id: str,
    token: str,
    model: str,
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.1,
    max_tokens: int = 8192,
    timeout: int = 120,
    use_json_schema: bool = True,
) -> str:
    """POST to the OpenAI-compatible CF endpoint, return the assistant message content.

    Large models (llama-3.3-70b) honour ``response_format=json_schema`` and
    reliably return a parseable object. The cheaper 8B models occasionally
    refuse strict schema mode (``content: null``, 0 tokens used); the caller
    can disable it via ``use_json_schema=False`` to fall back to plain
    ``json_object`` mode plus prompt-only JSON discipline.
    """
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1/chat/completions"
    body_dict: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if use_json_schema:
        body_dict["response_format"] = {
            "type": "json_schema",
            "schema": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "text": {"type": "string"},
                            },
                            "required": ["id", "text"],
                        },
                    }
                },
                "required": ["items"],
            },
        }
    else:
        body_dict["response_format"] = {"type": "json_object"}
    body = json.dumps(body_dict).encode("utf-8")
    req = Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace") if e.fp else ""
        if e.code in (401, 403):
            raise CloudflareAuthError(
                "Cloudflare auth rejected (HTTP "
                f"{e.code}). The token in .env probably does not include the "
                "'Workers AI' permission. Edit the token at "
                "https://dash.cloudflare.com/profile/api-tokens to add "
                "'Workers AI -> Read' (User scope) and 'Account -> Workers AI Read' "
                "(Account scope), or set CLOUDFLARE_WORKERS_AI_TOKEN in .env to a "
                f"separate token. Response: {detail[:300]}"
            ) from None
        raise CloudflareAIError(f"HTTP {e.code} from CF AI: {detail[:500]}") from None
    except URLError as e:
        raise CloudflareAIError(f"network error: {e}") from None

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise CloudflareAIError(f"non-JSON response: {raw[:300]!r}") from e

    choices = payload.get("choices") if isinstance(payload, dict) else None
    if isinstance(choices, list) and choices:
        msg = choices[0].get("message") or {}
        content = msg.get("content")
        # With `response_format=json_schema`, CF returns `content` as an
        # already-parsed object. Without it, content is a raw string. The
        # 8B model sometimes ignores the schema and returns ``content: null``
        # with 0 tokens consumed -- treat that as a recoverable error so the
        # caller can retry in plain json_object mode.
        if isinstance(content, str) and content:
            return content
        if isinstance(content, dict):
            return json.dumps(content, ensure_ascii=False)
        if content is None and use_json_schema:
            raise CloudflareAIError(
                "model returned null content with json_schema response_format -- "
                "likely a small-model refusal; retrying in json_object mode"
            )

    # Older `/ai/run/{model}` style response: {"result": {"response": "..."}}.
    result = payload.get("result") if isinstance(payload, dict) else None
    if isinstance(result, dict):
        text = result.get("response")
        if isinstance(text, str) and text:
            return text
        if isinstance(text, dict):
            return json.dumps(text, ensure_ascii=False)

    raise CloudflareAIError(f"unexpected response shape: {raw[:300]!r}")


# ----------------------------- translation core -----------------------------


def _make_messages(items: list[dict[str, str]]) -> list[dict[str, str]]:
    user_payload = json.dumps({"items": items}, ensure_ascii=False)
    return [
        {
            "role": "system",
            "content": SYSTEM_PROMPT_TEMPLATE.format(glossary=GLOSSARY_BLOCK),
        },
        {"role": "user", "content": user_payload},
    ]


def _parse_json_loose(raw: str) -> dict[str, Any]:
    """Extract the first JSON object from the model output."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        snippet = raw[start : end + 1]
        try:
            return json.loads(snippet)
        except json.JSONDecodeError as e:
            raise CloudflareAIError(f"could not parse JSON: {snippet[:300]!r}") from e
    raise CloudflareAIError(f"no JSON object in response: {raw[:300]!r}")


# Romanian-specific diacritics; if a source already has several of these and
# no Italian/Spanish/English function words, the text is already Romanian and
# we should keep it verbatim instead of asking the 8B model to "re-translate"
# it (which empirically corrupts diacritics: ă -> â, ț -> ç, etc.).
_RO_DIACRITICS = re.compile(r"[ăâîșțĂÂÎȘȚşţŞŢ]")

# English function words that almost never appear inside Romanian text.
# If the source has any of these AND the target is identical, the model
# probably echoed the input instead of translating it.
_ENGLISH_TELLS = re.compile(
    r"\b(?:the|and|for|with|from|that|this|these|those|which|when|where|"
    r"is|are|was|were|been|being|have|has|had|will|would|could|should|"
    r"shall|may|might|can|cannot|but|than|also|only|all|any|each|every|"
    r"available|design|by|of|to|in|on|at|as|it|its|our)\b",
    re.IGNORECASE,
)

# Italian / Spanish source tells -- same idea, narrower vocab so we don't
# falsely flag mixed-language descriptions that already use RO words.
_ROMANCE_TELLS = re.compile(
    r"\b(?:della|delle|degli|della|nella|nello|nelle|nelli|sulla|sulle|"
    r"con|sin|per|tra|fra|dei|degli|sono|essere|stato|stata|stati|state|"
    r"questo|questa|questi|queste|quello|quella|quelli|quelle|"
    r"la|el|los|las|del|las|para|sobre|entre|este|esta|estos|estas|"
    r"es|son|esta|está|están)\b",
    re.IGNORECASE,
)


def _is_already_romanian(text: str) -> bool:
    """Heuristic: at least 1 RO-specific diacritic and no obvious EN/IT/ES
    function words. Short titles like ``Loop K - Baterie joasă`` count.
    """
    if len(text) < 8:
        return False
    diacritics = len(_RO_DIACRITICS.findall(text))
    if diacritics < 1:
        return False
    if _ENGLISH_TELLS.search(text) or _ROMANCE_TELLS.search(text):
        return False
    return True


def _looks_translated(target: str, source: str) -> bool:
    """Return True when ``target`` is a plausible RO rewrite of ``source``.

    Permissive on purpose: many short labels (``SPC``, ``Podea``, ``Inox``,
    ``Stejar Natural``) are already Romanian/universal, so the model
    correctly returns them unchanged. We only flag a target as "untranslated"
    if it is BYTE-IDENTICAL to a source that clearly contains English or
    Italian/Spanish function words -- that is, a real echo.
    """
    if not source.strip():
        return target == "" or target.strip() == ""
    if not target.strip():
        return False
    if not any(c.isalpha() for c in source):
        return True
    if source.strip().casefold() != target.strip().casefold():
        return True
    if _ENGLISH_TELLS.search(source) or _ROMANCE_TELLS.search(source):
        return False
    return True


def _translate_batch(
    account_id: str,
    token: str,
    model: str,
    items: list[dict[str, str]],
    *,
    max_attempts: int = 3,
) -> dict[str, str]:
    """Run one model call; return ``{id: translated_text}`` for every input id.

    On the first attempt we request strict ``response_format=json_schema``. The
    smaller (cheaper) llama-3.1-8B model occasionally refuses that mode and
    returns an empty completion -- subsequent attempts drop down to plain
    ``json_object`` mode which the small model handles reliably.
    """
    last_err: CloudflareAIError | None = None
    for attempt in range(1, max_attempts + 1):
        use_schema = attempt == 1
        last_attempt = attempt == max_attempts
        try:
            content = _cf_chat_completion(
                account_id,
                token,
                model,
                _make_messages(items),
                temperature=0.1 if attempt == 1 else 0.0,
                use_json_schema=use_schema,
            )
            parsed = _parse_json_loose(content)
            out_items = parsed.get("items") if isinstance(parsed, dict) else None
            if not isinstance(out_items, list):
                raise CloudflareAIError(f"missing 'items' array in {parsed!r}")

            by_id: dict[str, str] = {}
            for it in out_items:
                if not isinstance(it, dict):
                    continue
                key = it.get("id")
                txt = it.get("text", "")
                if isinstance(key, str) and isinstance(txt, str):
                    by_id[key] = txt

            missing = [it["id"] for it in items if it["id"] not in by_id]
            if missing:
                raise CloudflareAIError(
                    f"missing ids in response: {missing[:5]} (returned "
                    f"{len(by_id)} of {len(items)})"
                )

            # Echo detection: penultimate attempt re-rolls if any cell looks
            # untranslated. On the LAST attempt we accept whatever came back
            # so 1 bad cell doesn't kill 19 good ones -- the source CSV may
            # contain text the 8B model just cannot handle.
            if not last_attempt:
                for it in items:
                    if not _looks_translated(by_id[it["id"]], it["text"]):
                        raise CloudflareAIError(
                            f"untranslated cell for id={it['id']} field={it.get('field')}"
                        )
            return by_id
        except CloudflareAuthError:
            raise
        except CloudflareAIError as e:
            last_err = e
            time.sleep(0.5 * attempt)
            continue

    assert last_err is not None
    raise last_err


# ----------------------------- CSV plumbing ---------------------------------


def _read_csv(path: Path) -> tuple[list[str], list[list[str]]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        rows = list(reader)
    if not rows:
        return [], []
    return rows[0], rows[1:]


def _write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def _pack_items_for_batch(
    queue: list[tuple[str, str, str]],
    *,
    short_batch: int = 20,
    long_batch: int = 6,
    long_threshold: int = 180,
) -> list[list[dict[str, str]]]:
    """Group ``(id, field, text)`` queue entries into model-friendly batches.

    Short, single-word cells (category/type/finishes) are packed densely
    (~20 per call); long descriptions go in batches of ~6 so the model has
    room to respond. Each batch keeps a mix of short/long items if available.
    """
    long_items = [q for q in queue if len(q[2]) >= long_threshold]
    short_items = [q for q in queue if len(q[2]) < long_threshold]

    batches: list[list[dict[str, str]]] = []
    for i in range(0, len(long_items), long_batch):
        batches.append(
            [
                {"id": rid, "field": field, "text": text}
                for rid, field, text in long_items[i : i + long_batch]
            ]
        )
    for i in range(0, len(short_items), short_batch):
        batches.append(
            [
                {"id": rid, "field": field, "text": text}
                for rid, field, text in short_items[i : i + short_batch]
            ]
        )
    return batches


def translate_products_csv(
    *,
    account_id: str,
    token: str,
    model: str,
    limit: int | None,
    workers: int,
    force: bool,
    dry_run: bool,
) -> tuple[list[str], list[list[str]]]:
    """Translate ``merged_products.csv`` and return ``(header, rows)`` ready to write.

    Source priority: ``merged_products_original.csv`` if present (the canonical
    English baseline written by ``merge_csvs.py``), otherwise the current
    ``merged_products.csv``. Reading from ``_original`` makes the translator
    idempotent -- you can re-run safely after editing the glossary and the
    inputs are always the English text, never an already-translated copy.
    """
    src_path = PRODUCTS_BACKUP if PRODUCTS_BACKUP.is_file() else PRODUCTS_CSV
    print(f"[plan] source: {src_path.name}", flush=True)
    header, rows = _read_csv(src_path)
    if not header:
        raise SystemExit(f"{src_path} is empty or missing")

    process_n = len(rows) if limit is None else min(max(0, limit), len(rows))

    field_indices = {f: header.index(f) for f in PRODUCT_TRANSLATE_FIELDS if f in header}
    if not field_indices:
        raise SystemExit(
            f"No translatable columns found in {PRODUCTS_CSV.name}; expected one of "
            f"{PRODUCT_TRANSLATE_FIELDS}"
        )

    conn = _open_cache(CACHE_DB)

    queue: list[tuple[str, str, str]] = []
    cache_hits = 0
    cache_translations: dict[str, str] = {}
    cell_lookup: dict[str, tuple[int, int]] = {}

    already_ro = 0
    for r_idx in range(process_n):
        row = rows[r_idx]
        for field, c_idx in field_indices.items():
            if c_idx >= len(row):
                continue
            source = row[c_idx]
            if not source or not source.strip():
                continue
            cell_id = f"r{r_idx}_{field}"
            cell_lookup[cell_id] = (r_idx, c_idx)

            if not force:
                cached = _cache_get(conn, field, source, model)
                if cached is not None:
                    cache_translations[cell_id] = cached
                    cache_hits += 1
                    continue

            if _is_already_romanian(source):
                cache_translations[cell_id] = source
                _cache_put(conn, field, source, source, model)
                already_ro += 1
                continue

            queue.append((cell_id, field, source))

    total = cache_hits + already_ro + len(queue)
    scope = f"first {process_n}" if limit is not None else "all"
    print(
        f"[plan] products: {scope} of {len(rows)} rows / {len(field_indices)} fields "
        f"= {total} cells | cache hits: {cache_hits} | already-RO: {already_ro} "
        f"| to translate: {len(queue)}",
        flush=True,
    )

    if dry_run:
        return header, rows

    if queue:
        batches = _pack_items_for_batch(queue)
        print(f"[plan] dispatching {len(batches)} batch(es) with up to {workers} workers")
        progress_lock = threading.Lock()
        completed = {"n": 0, "cells": 0, "failed": 0}

        auth_error_holder: dict[str, CloudflareAuthError] = {}

        def _run(batch: list[dict[str, str]]) -> dict[str, str]:
            if auth_error_holder:
                return {}
            try:
                return _translate_batch(account_id, token, model, batch)
            except CloudflareAuthError as e:
                auth_error_holder.setdefault("err", e)
                return {}
            except CloudflareAIError as e:
                with progress_lock:
                    completed["failed"] += len(batch)
                print(f"[error] batch failed ({len(batch)} cells): {e}", flush=True)
                return {}

        results: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_to_batch = {pool.submit(_run, b): b for b in batches}
            for fut in as_completed(future_to_batch):
                batch_out = fut.result()
                results.update(batch_out)
                batch_in = future_to_batch[fut]
                for it in batch_in:
                    src = it["text"]
                    tgt = batch_out.get(it["id"])
                    if tgt is not None:
                        _cache_put(conn, it["field"], src, tgt, model)
                with progress_lock:
                    completed["n"] += 1
                    completed["cells"] += len(batch_out)
                    print(
                        f"[batch {completed['n']:>4}/{len(batches)}] "
                        f"cells={completed['cells']:>5}/{len(queue)} "
                        f"failed={completed['failed']}",
                        flush=True,
                    )

        if auth_error_holder:
            raise auth_error_holder["err"]

        cache_translations.update(results)

    out_rows = [list(r) for r in rows]
    for cell_id, target in cache_translations.items():
        r_idx, c_idx = cell_lookup[cell_id]
        while len(out_rows[r_idx]) <= c_idx:
            out_rows[r_idx].append("")
        out_rows[r_idx][c_idx] = target

    return header, out_rows


def snapshot_originals(*, force: bool = False) -> None:
    """Copy current merged files to ``merged_<name>_original.csv``.

    When called standalone, the script preserves an existing ``_original`` so
    re-running the translator keeps the canonical English baseline intact.
    When called from ``merge_csvs.py`` after a fresh merge, pass ``force=True``
    -- the just-merged CSV is itself the new canonical English version and
    should overwrite any stale backup.
    """
    for src, dst in ((PRODUCTS_CSV, PRODUCTS_BACKUP), (VARIANTS_CSV, VARIANTS_BACKUP)):
        if not src.is_file():
            continue
        if dst.is_file() and not force:
            print(f"[snapshot] keeping existing {dst.name}", flush=True)
            continue
        shutil.copy2(src, dst)
        action = "refreshed" if dst.is_file() and force else "snapshot"
        print(f"[snapshot] {action} {src.name} -> {dst.name}", flush=True)


def run(
    *,
    limit: int | None,
    model: str,
    workers: int,
    force: bool,
    dry_run: bool,
    no_snapshot: bool,
    force_snapshot: bool = False,
) -> int:
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
    token = (
        os.environ.get("CLOUDFLARE_WORKERS_AI_TOKEN", "").strip()
        or os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
    )
    if not account_id or not token:
        raise SystemExit(
            "CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN (or "
            "CLOUDFLARE_WORKERS_AI_TOKEN) must be set via environment or .env"
        )

    if not PRODUCTS_CSV.is_file():
        raise SystemExit(f"missing {PRODUCTS_CSV} -- run merge_csvs.py first")

    if not no_snapshot and not dry_run:
        snapshot_originals(force=force_snapshot)

    header, rows = translate_products_csv(
        account_id=account_id,
        token=token,
        model=model,
        limit=limit,
        workers=workers,
        force=force,
        dry_run=dry_run,
    )

    if dry_run:
        print("[dry-run] no files written", flush=True)
        return 0

    _write_csv(PRODUCTS_CSV, header, rows)
    print(f"[ok] wrote translated {PRODUCTS_CSV.name} ({len(rows)} rows)", flush=True)
    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Cloudflare Workers AI model id (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Translate only the first N product rows (sanity-check runs)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Number of concurrent batches (default: 8). Bump to 16+ if the "
        "cheaper 8B model is your default and CF accepts the rate.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore the cache and re-translate every cell (rebuilds the cache)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan only -- count cache hits / cells to translate, no API calls or writes",
    )
    parser.add_argument(
        "--no-snapshot",
        action="store_true",
        help="Skip writing merged_*_original.csv backups (use when you already have them)",
    )
    args = parser.parse_args()

    try:
        return run(
            limit=args.limit,
            model=args.model,
            workers=args.workers,
            force=args.force,
            dry_run=args.dry_run,
            no_snapshot=args.no_snapshot,
        )
    except CloudflareAuthError as e:
        print(f"\n[abort] {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
