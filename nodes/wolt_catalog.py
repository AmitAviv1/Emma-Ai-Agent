"""
wolt_catalog.py — Wolt product catalog loader and fuzzy matcher.

Loads your Wolt catalog Excel file and lets the agent match extracted
invoice products to their Wolt listing by barcode, SKU, or name similarity.

SETUP:
  Add to .env:
    WOLT_CATALOG_PATH=/path/to/your-wolt-catalog.xlsx

  Re-upload a new version of the file any time — just run:
    from wolt_catalog import reload_catalog; reload_catalog()

MATCHING LOGIC (in priority order):
  1. Exact barcode (gtin) match
  2. Exact merchant_sku match
  3. Fuzzy name match (similarity ≥ FUZZY_THRESHOLD)
  4. No match → returns None
"""

import os
import re
from difflib import SequenceMatcher
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────
#  CONFIGURATION
# ─────────────────────────────────────────────

WOLT_CATALOG_PATH = os.getenv(
    "WOLT_CATALOG_PATH",
    ""   # Set in .env — e.g. /Users/yourname/Desktop/Projects/AI AGENT/wolt_catalog.xlsx
)

# Minimum similarity score (0-1) to accept a name match
# 0.6 = fairly lenient, 0.75 = strict
FUZZY_THRESHOLD = 0.62


# ─────────────────────────────────────────────
#  CATALOG STRUCTURE
# ─────────────────────────────────────────────

class WoltProduct:
    """One product from the Wolt catalog."""
    __slots__ = ("wolt_id", "barcode", "merchant_sku", "name",
                 "sell_price", "enabled", "category", "image_url")

    def __init__(self, row: tuple):
        self.wolt_id      = row[0] or ""
        self.barcode      = str(row[1]).strip() if row[1] else ""
        self.merchant_sku = str(row[2]).strip() if row[2] else ""
        self.name         = row[3] or ""
        self.sell_price   = float(row[5]) if row[5] else 0.0
        self.enabled      = bool(row[16]) if len(row) > 16 else True
        self.category     = str(row[18]).split("::")[0] if len(row) > 18 and row[18] else ""
        self.image_url    = row[10] or ""

    def to_dict(self) -> dict:
        return {
            "wolt_id":      self.wolt_id,
            "wolt_name":    self.name,
            "wolt_price":   self.sell_price,
            "wolt_enabled": self.enabled,
            "wolt_barcode": self.barcode,
            "wolt_sku":     self.merchant_sku,
            "wolt_category":self.category,
            "wolt_image":   self.image_url,
        }


# ─────────────────────────────────────────────
#  CATALOG LOADER
# ─────────────────────────────────────────────

_catalog: Optional[list] = None           # list of WoltProduct
_barcode_index: dict = {}                  # barcode → WoltProduct
_sku_index: dict = {}                      # merchant_sku → WoltProduct
_name_tokens: list = []                    # list of (normalized_name, WoltProduct)


def _normalize(text: str) -> str:
    """Lowercase, strip punctuation and common noise words for fuzzy matching."""
    text = text.lower()
    # Remove size/weight suffixes that vary between invoice and Wolt name
    text = re.sub(r'\|.*$', '', text)            # strip "| 250 גרם" suffix
    text = re.sub(r'\d+[\.,]\d*\s*(ק"ג|קג|גרם|מ"ל|ליטר|יח)', '', text)
    text = re.sub(r'[^\u0590-\u05FFa-zA-Z0-9\s]', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def _load_from_file(path: str) -> list:
    try:
        from openpyxl import load_workbook
    except ImportError:
        raise ImportError("pip install openpyxl")

    wb = load_workbook(path, read_only=True)
    ws = wb["offers"]
    products = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not any(row):
            continue
        try:
            products.append(WoltProduct(row))
        except Exception:
            pass
    wb.close()
    return products


def _build_indexes(products: list):
    global _barcode_index, _sku_index, _name_tokens
    _barcode_index = {}
    _sku_index     = {}
    _name_tokens   = []

    for p in products:
        if p.barcode:
            _barcode_index[p.barcode] = p
        if p.merchant_sku:
            _sku_index[p.merchant_sku] = p
        _name_tokens.append((_normalize(p.name), p))

    print(f"📦 [WOLT] Catalog loaded: {len(products)} products "
          f"({len(_barcode_index)} with barcode, "
          f"{len(_sku_index)} with SKU)")


def load_catalog(path: str = "") -> bool:
    """
    Load (or reload) the Wolt catalog from an Excel file.
    Returns True on success, False if file not found.
    """
    global _catalog
    path = path or WOLT_CATALOG_PATH
    if not path or not os.path.exists(path):
        print(f"⚠️  [WOLT] Catalog file not found: '{path}'. "
              f"Set WOLT_CATALOG_PATH in .env")
        return False

    print(f"📂 [WOLT] Loading catalog from {os.path.basename(path)}…")
    _catalog = _load_from_file(path)
    _build_indexes(_catalog)
    return True


def reload_catalog():
    """Force reload — call this after uploading a new Wolt export."""
    return load_catalog()


def is_loaded() -> bool:
    return _catalog is not None and len(_catalog) > 0


def _ensure_loaded():
    if not is_loaded():
        load_catalog()


# ─────────────────────────────────────────────
#  MATCHING
# ─────────────────────────────────────────────

def _fuzzy_score(a: str, b: str) -> float:
    """SequenceMatcher similarity ratio between two normalized strings."""
    return SequenceMatcher(None, a, b).ratio()


def match_product(
    barcode: str = "",
    sku: str     = "",
    name: str    = "",
) -> Optional[dict]:
    """
    Find the best Wolt catalog match for an invoice product.

    Priority:
      1. Exact barcode match
      2. Exact SKU match
      3. Best fuzzy name match (if score ≥ FUZZY_THRESHOLD)

    Returns a dict with wolt_* fields, or None if no match found.
    Also includes match_method and match_score for transparency.
    """
    _ensure_loaded()

    if not is_loaded():
        return None

    # 1. Exact barcode
    if barcode and barcode in _barcode_index:
        p = _barcode_index[barcode]
        return {**p.to_dict(), "match_method": "barcode", "match_score": 1.0}

    # 2. Exact SKU (merchant_sku in Wolt = often barcode or internal code)
    if sku and sku in _sku_index:
        p = _sku_index[sku]
        return {**p.to_dict(), "match_method": "sku", "match_score": 1.0}

    # Also try barcode against SKU index (Wolt sometimes stores barcode in merchant_sku)
    if barcode and barcode in _sku_index:
        p = _sku_index[barcode]
        return {**p.to_dict(), "match_method": "barcode_as_sku", "match_score": 1.0}

    # 3. Fuzzy name match
    if name:
        norm_name = _normalize(name)
        best_score = 0.0
        best_match = None

        for norm_wolt, p in _name_tokens:
            score = _fuzzy_score(norm_name, norm_wolt)
            if score > best_score:
                best_score = score
                best_match = p

        if best_score >= FUZZY_THRESHOLD and best_match:
            return {
                **best_match.to_dict(),
                "match_method": "name_fuzzy",
                "match_score": round(best_score, 3),
            }

    return None


def enrich_products(products: list) -> list:
    """
    Add Wolt catalog data to a list of extracted invoice products.
    Each product dict gets wolt_* fields and a margin calculation.

    Returns the enriched list (modifies in place).
    """
    _ensure_loaded()
    matched = 0

    for p in products:
        wolt = match_product(
            barcode=p.get("barcode", ""),
            sku=p.get("sku", ""),
            name=p.get("name", ""),
        )

        if wolt:
            matched += 1
            p.update(wolt)

            # Calculate margin if both prices available
            sell  = wolt.get("wolt_price", 0)
            cost  = p.get("cost", 0)
            qty   = p.get("quantity", 1) or 1
            unit_cost = cost / qty if qty else cost

            if sell > 0 and unit_cost > 0:
                margin_pct = round((sell - unit_cost) / sell * 100, 1)
                p["margin_pct"]  = margin_pct
                p["unit_cost"]   = round(unit_cost, 2)
                p["wolt_price"]  = sell
        else:
            p["match_method"] = "none"
            p["match_score"]  = 0.0

    print(f"🔗 [WOLT] Matched {matched}/{len(products)} products to Wolt catalog")
    return products


# ─────────────────────────────────────────────
#  CATALOG UPLOAD HELPER (for Streamlit UI)
# ─────────────────────────────────────────────

def load_catalog_from_bytes(file_bytes: bytes, filename: str) -> bool:
    """
    Load catalog directly from uploaded file bytes (for Streamlit uploader).
    Saves to a temp path and loads from there.
    """
    import tempfile
    suffix = os.path.splitext(filename)[1]
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name
    result = load_catalog(tmp_path)
    os.unlink(tmp_path)
    return result