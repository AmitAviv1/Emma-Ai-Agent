import os
import base64
import pdfplumber
import fitz  # pymupdf — pip install pymupdf, no system dependencies needed
from typing import List, Optional, Tuple
from pydantic import BaseModel, Field
from langchain_core.messages import HumanMessage, SystemMessage
from dotenv import load_dotenv
from PIL import Image, ImageOps, ImageFilter, ImageEnhance
import re
import sys
import os as _os
sys.path.insert(0, _os.path.dirname(__file__))
from suppliers import (
    identify_supplier_from_text,
    identify_supplier_from_image,
    get_supplier_profile,
    build_vision_prompt,
)

load_dotenv()

# ─────────────────────────────────────────────
#  CONFIGURATION
# ─────────────────────────────────────────────

LLM_PROVIDER    = os.getenv("LLM_PROVIDER", "auto")
OPENAI_MODEL    = "gpt-4o"
ANTHROPIC_MODEL = "claude-sonnet-4-6"   # best Hebrew OCR; swap to claude-opus-4-7 for max quality
GEMINI_MODEL    = "gemini-2.5-flash"    # current stable — fast, cheap, excellent Hebrew OCR


# ─────────────────────────────────────────────
#  SCHEMAS
# ─────────────────────────────────────────────

class Product(BaseModel):
    name: str = Field(description=(
        "COPY the Hebrew product name EXACTLY as printed — every word, brand name, "
        "volume, and English term. Do NOT translate, summarise, or paraphrase. "
        "If you cannot read a character, use '?'."
    ))
    sku: str = Field(
        default="",
        description=(
            "Catalog / item number if one exists (e.g. NT-95, C753/H). "
            "Leave empty if no SKU column exists in this invoice."
        )
    )
    barcode: str = Field(
        default="",
        description=(
            "Copy the EAN/UPC code digit-by-digit from the ברקוד column. "
            "Typically 13 digits. Do NOT add, remove, or change any digit. "
            "Leave empty only if no numeric code exists for this row."
        )
    )
    quantity: float = Field(description="Number from the כמות column. Must be positive.")
    cost: float = Field(description=(
        "Number from the סה\"כ נטו / סכום נטו column (LINE TOTAL — not unit price). "
        "0.0 is valid when a product has 100% discount. Never negative."
    ))


class InvoiceExtraction(BaseModel):
    vendor_name:    str = Field(description="Supplier name exactly as printed on the invoice header.")
    invoice_number: str = Field(default="", description=(
        "Invoice number as printed — e.g. 01/042665, 49416, IN264010392, 103805. "
        "Look for מספר / מס' / חשבונית מס near the top of the invoice. "
        "Leave empty if not found."
    ))
    invoice_date:   str = Field(default="", description=(
        "Invoice date in YYYY-MM-DD format. "
        "Look for תאריך near the top. Convert DD/MM/YYYY or DD/MM/YY to YYYY-MM-DD. "
        "Leave empty if not found."
    ))
    products: List[Product]


# ─────────────────────────────────────────────
#  LLM FACTORY
# ─────────────────────────────────────────────

_base_cache: dict = {}


def _get_base_llm(provider: Optional[str] = None):
    provider = provider or LLM_PROVIDER

    # Auto-detection priority: Anthropic → Gemini → OpenAI
    if provider == "auto":
        if os.getenv("ANTHROPIC_API_KEY"):
            provider = "anthropic"
        elif os.getenv("GOOGLE_API_KEY"):
            provider = "gemini"
        else:
            provider = "openai"

    if provider not in _base_cache:
        if provider == "anthropic":
            from langchain_anthropic import ChatAnthropic
            _base_cache[provider] = ChatAnthropic(
                model=ANTHROPIC_MODEL, temperature=0, max_tokens=8192
            )
        elif provider == "gemini":
            from langchain_google_genai import ChatGoogleGenerativeAI
            _base_cache[provider] = ChatGoogleGenerativeAI(
                model=GEMINI_MODEL,
                temperature=0,
                max_output_tokens=8192,
                google_api_key=os.getenv("GOOGLE_API_KEY"),
            )
        else:
            from langchain_openai import ChatOpenAI
            _base_cache[provider] = ChatOpenAI(
                model=OPENAI_MODEL, temperature=0, max_tokens=8192
            )

    return _base_cache[provider]


def _get_structured_llm(schema):
    """Always built fresh from cached base — never double-wrapped."""
    return _get_base_llm().with_structured_output(schema)


# ─────────────────────────────────────────────
#  IMAGE PRE-PROCESSING
# ─────────────────────────────────────────────

def encode_image(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def preprocess_image(image_path: str) -> str:
    """Orientation fix + contrast/sharpness boost. Keeps colour (helps column reading)."""
    print(f"🔄 [LOG] Pre-processing: {os.path.basename(image_path)}")
    img = Image.open(image_path)
    img = ImageOps.exif_transpose(img)
    img = ImageEnhance.Contrast(img).enhance(1.3)
    img = img.filter(ImageFilter.SHARPEN)
    out = f"temp_pre_{os.path.basename(image_path)}.jpg"
    img.save(out, "JPEG", quality=95)
    return out


# ─────────────────────────────────────────────
#  DIRECT VISION EXTRACTION  (single stage)
# ─────────────────────────────────────────────

_VISION_SYSTEM = """\
You are a precise OCR engine for Israeli supplier invoices.
Your ONLY job is to read numbers and Hebrew text exactly as printed.

NEVER:
- Invent, guess, or paraphrase product names
- Change any digit in a barcode or price
- Translate Hebrew text
- Skip any product row
- Use the same product name for multiple rows
- Mix the SKU code into the product name — they are SEPARATE fields

ALWAYS:
- Copy each product's Hebrew description verbatim from the שם פריט / תאור פריט column
- Put the item code (NT-95, NT-202 etc.) in the sku field, NOT in the name
- Copy each barcode digit-by-digit from the ברקוד column
- Use סכום נטו / סה"כ נטו (line total) as cost — NOT unit price
- Record cost=0.0 for rows where price shows 0 or is crossed out with 0 written

HEBREW LETTER ACCURACY — these pairs are commonly confused:
- ב (bet) vs כ (kaf) — ב is closed on the right, כ is open
- ד (dalet) vs ר (resh) — ד has a right-angle bottom-right corner, ר is rounded
- ה (he) vs ח (het) — ה has a gap at top-left, ח is fully closed at top
- ו (vav) vs ז (zayin) — ו is a plain vertical stroke, ז has a horizontal foot
- ם (final mem) vs מ (mem) — ם is fully closed, מ is open at bottom-right
- ן (final nun) vs נ (nun) — ן descends below the line, נ stays on the baseline
- ף (final pe) vs פ (pe) — ף has a descender, פ is closed at bottom
- ץ (final tsadi) vs צ (tsadi) — ץ descends, צ stays on baseline
- י (yod) vs ' (apostrophe) — י is a Hebrew letter, never substitute with a punctuation mark
When in doubt about a single character, use your best judgement based on the surrounding word context.
"""

_VISION_PROMPT = """\
This is page {page_num} of an Israeli supplier invoice.

Column layout (the table is RTL — Hebrew reads right to left):
  RIGHT SIDE of page → LEFT SIDE of page
  מס' פריט/SKU | שם פריט (description) | כמות (qty) | מחיר יחידה | %הנחה | נטו ליחידה | סכום נטו | ברקוד

The ברקוד (barcode) column is on the FAR LEFT edge of the table.
These are 13-digit EAN codes. Read every digit carefully.
Some rows may have no barcode — if so, leave barcode empty and fill the sku field with the
item code printed in the rightmost column (e.g. NT-95, NT-202, C753/H).

The סכום נטו / סה"כ נטו column is the line total (cost). Do NOT use unit price as cost.

CRITICAL NAME RULE:
- The product name (שם פריט) is ONLY the Hebrew description column
- Do NOT append the SKU code to the name — they are separate fields
- Example: name="קרניבור עצם קשר טבעית 20 ס\"מ"  sku="NT-95"  (correct)
- Example: name="קרניבור עצם קשר טבעית 20 ס\"מ NT-95"  (WRONG — SKU belongs in sku field)
- Copy the Hebrew name EXACTLY — every word, volume, and brand

Rules:
- Each row (identified by barcode or SKU) = one unique product
- Multi-line descriptions belong to the row with that barcode/SKU
- Rows with cost=0 or crossed-out price → cost=0.0 (free/promo units)
- Extract EVERY row; do not stop early
- If a barcode matches one in the catalog list below, use that catalog name EXACTLY

Return all {expected} products on this page.
"""


def extract_from_image(image_path: str, page_num: int = 1,
                       expected: int = 0, vendor_hint: str = "",
                       supplier_key: Optional[str] = None,
                       total_pages: int = 1) -> dict:
    """Single-stage: image → structured InvoiceExtraction directly."""
    print(f"🔍 [LOG] Direct vision extraction (page {page_num}/{total_pages})...")
    b64 = encode_image(image_path)
    llm_plain = _get_base_llm()
    llm = _get_structured_llm(InvoiceExtraction)

    # Identify supplier if not already known
    if not supplier_key:
        supplier_key = identify_supplier_from_image(b64, llm_plain)

    profile = get_supplier_profile(supplier_key)
    prompt  = build_vision_prompt(
        profile,
        page_num=page_num,
        total_pages=total_pages,
        catalog_block=_catalog_reference_block(supplier_key),
    )

    result: InvoiceExtraction = llm.invoke([
        SystemMessage(content=_VISION_SYSTEM),
        HumanMessage(content=[
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {
                "url": f"data:image/jpeg;base64,{b64}",
                "detail": "high"
            }}
        ])
    ])

    result, warnings = validate_extraction(result)
    corrected, corrections = correct_products(result.products)
    products_dicts = [p.model_dump() for p in corrected]
    merged, merge_logs = merge_promotional_rows(products_dicts)
    return {
        "products":       merged,
        "vendor_name":    result.vendor_name,
        "invoice_number": result.invoice_number,
        "invoice_date":   result.invoice_date,
        "warnings":       warnings + corrections + merge_logs,
    }


# ─────────────────────────────────────────────
#  VALIDATION
# ─────────────────────────────────────────────

def validate_extraction(
    extraction: InvoiceExtraction,
) -> Tuple[InvoiceExtraction, List[str]]:
    warnings: List[str] = []
    clean: List[Product] = []

    for i, p in enumerate(extraction.products):
        label = f"Row {i+1} (bc={p.barcode or 'none'})"

        # Clean barcode — strip whitespace, asterisks (tzemach invoices use *barcode*)
        bc = p.barcode.strip().strip("*")
        if bc != p.barcode:
            p = p.model_copy(update={"barcode": bc})

        # If barcode contains non-digits or a decimal point it's not a real barcode
        # (model confused a record number like 5692.3 for a barcode) — clear it
        if p.barcode and not re.match(r"^\d+$", p.barcode):
            warnings.append(f"{label}: '{p.barcode}' is not a valid barcode — cleared.")
            p = p.model_copy(update={"barcode": ""})

        # Use SKU as fallback identifier when barcode is missing
        if not p.barcode.strip() and p.sku.strip():
            p = p.model_copy(update={"barcode": f"SKU-{p.sku}"})

        # Barcode length check (warn but keep — some valid codes are 8 or 12 digits)
        if p.barcode and not p.barcode.startswith("SKU-") and len(p.barcode) not in (8, 12, 13):
            warnings.append(f"{label}: barcode '{p.barcode}' length {len(p.barcode)} unusual — verify.")

        if p.cost < 0:
            warnings.append(f"{label}: negative cost {p.cost} — corrected to 0.")
            p = p.model_copy(update={"cost": 0.0})

        # cost=0 is VALID — 100% discount products are real, never drop them

        if p.quantity <= 0:
            warnings.append(f"{label}: qty {p.quantity} ≤ 0 — skipping (total row?).")
            continue

        if not p.name.strip() and not p.barcode.strip():
            warnings.append(f"{label}: no name and no barcode — skipping (summary row).")
            continue

        clean.append(p)

    for w in warnings:
        print(f"⚠️  [VALIDATION] {w}")

    return InvoiceExtraction(
        vendor_name=extraction.vendor_name,
        invoice_number=extraction.invoice_number,
        invoice_date=extraction.invoice_date,
        products=clean,
    ), warnings


# ─────────────────────────────────────────────
#  PRODUCT CATALOG  (ground truth, grows over time)
# ─────────────────────────────────────────────
# Each entry: barcode → correct product name (Hebrew, exact)
# Add new products after each verified invoice run.
# This serves two purposes:
#   1. Barcode correction — fix OCR digit errors
#   2. Name correction  — override hallucinated names with exact catalog names
#   3. Prompt reference — injected into the vision prompt so the model can anchor on known names

PRODUCT_CATALOG: dict = {
    # barcode: exact Hebrew name as it appears on the invoice
    "8596075006440": "קיווי חטיף פרימיום 100% בשר בקר מיובש בהקפאה פינוק טבעי 45 גרם",
    "742797764092":  "ארם&האמר ערכת דנטלית בטעם וניל ג'ינגר לכל הגילאים 67.5 גרם (3 חלקים)",
    "702160104167":  "שמן סלמון נורווגי 300 מ\"ל טבעי 100% לכלב ולחתול BRILLIANT",
    "4743318143569": "וולדה משחה מרגיעה ומקלה מפני עור יבש או סדוק בכפות הרגליים והאף 40 גרם",
    "4743318102863": "וולדה שמפו לניקוי עמוק ויסודי URBAN-DETOX 400 מ\"ל",
    "4743318102894": "וולדה שמפו לחות HYDRO-BOOST 400 מ\"ל",
    "4743318107011": "וולדה שמפו למניעת קשרים DETANGLING 100 מ\"ל",
    "4743318107004": "וולדה שמפו לפרווה לבנה WHITENING 100 מ\"ל",
    "4743318102733": "וולדה תרסיס אורגני לריכוך ולפתיחה ומניעת קשרים ולהקלת הסירוק 250 מ\"ל",
    "742797802183":  "ו.ו.ל. תרסיס לפתיחת קשרים ולרענון הפרווה בניחוח מישמש עסיסי 355 מ\"ל",
    "742797858784":  "ו.ו.ל. שמפו עם מרכך בניחוח אבטיח 473 מ\"ל",
    "742797802152":  "ו.ו.ל. שמפו שיבולת שועל מרגיע בניחוח וניל 473 מ\"ל",
    "742797912363":  "ארם&האמר מבסם ומנטרל ריח לארגז חול בניחוח נענע ורוזמרין גרגרי קריסטל",
    "7290111773136": "פרו גרום מסרק מתכת להסרת קשרים 20 ס\"מ אורך 5 ס\"מ רוחב אל חלד",
    "5060420393279": "קינג בנגה צעצוע לחתול עם מילוי עלי קטניפ 100% אורגני",
    "5060420393309": "קינג סרדין צעצוע לחתול עם מילוי עלי קטניפ 100% אורגני",
    "5060420393286": "קינג גזר צעצוע לחתול עם מילוי עלי קטניפ 100% אורגני",
    "6932844603076": "מטליות אסוברב בניחוח אלוורה אנטי-בקטריאליות 80 יחידות",
    "6932844603038": "מטליות אסוברב בניחוח פרחוני אנטי-בקטריאליות 80 יחידות",
    "7290111777967": "רולר מקצועי מידה S סופר פטס 60 גליונות 15.5 ס\"מ אורך",
    "7290111779725": "קערת מים ואוכל סיליקון סופר פטס L לכלב לחתול 1 ליטר",
    "8596075006686": "קיווי סט קערות מתקפלות כחולה מסיליקון לטיול 350 מ\"ל מים והאכלה איטית",
    "8596075002336": "קיווי פאוץ' ורוד לחטיפים מסיליקון עם אבזם פלדה לתליה 10*5*13.5 ס\"מ",
    "8596075002343": "קיווי פאוץ' כחול לחטיפים מסיליקון עם אבזם פלדה לתליה 10*5*13.5 ס\"מ",
    "8596075001445": "קיווי צלחת עם ידיות נשיאה מעוצבת שחורה וציפורים כתומות 750 מ\"ל 24 ס\"מ",
    # ── א.א פטקאר / PET-CARE — confirmed from invoice 49416 dated 15/02/2026 ──
    "0693493258517": "קרניבור עצם קשר טבעית 20 ס\"מ NT-95",
    "0693493253598": "קרניבור 1 עצם דחוסה טבעית 30 ס\"מ NT-93",
    "0693493253871": "קרניבור לחי דחוסה וגיד 2 יחידות NT-78",
    "0693493253826": "קרניבור קרקפת 12 ס\"מ מאה גרם NT-72",
    "726529694734":  "קרניבור חטיפי אילוף 400 גרם NT-440",
    "SKU-NT-202":    "קרניבור מיקס חטיפים חצי ק\"ג NT-202",
    "0693493292429": "קרניבור קרקפת גמל 500 גרם NT-160",
    "0693493292436": "קרניבור שלוש לחי (באפלו גמל) NT-155",
    "0788792115293": "קרניבור 3 קרקפות גמל ובאפלו NT-154",
    "0726529694406": "קרניבור סחוס עטוף בברווז NT-113",
    "0726529694383": "קרניבור נתחי ברווז NT-111",
    "0726529694376": "קרניבור נתחי עוף NT-110",
    "0693493292641": "קרניבור לחי מגולגלת לבנה NT-101",
    "0693493292634": "קרניבור לחי גמל מגולגלת NT-100",
    "8019808233819": "תיק חטיפים קמון C753/H",
}


def _build_prefix_map() -> dict:
    """
    Build a barcode→correct lookup that uses the LONGEST prefix
    that uniquely identifies each barcode in the catalog.
    Avoids collisions where multiple barcodes share the same 7-digit prefix.
    Returns: {prefix_string: correct_full_barcode}
    """
    barcodes = list(PRODUCT_CATALOG.keys())
    prefix_map: dict = {}

    for bc in barcodes:
        for length in range(7, len(bc) + 1):
            prefix = bc[:length]
            collides = any(
                other[:length] == prefix
                for other in barcodes
                if other != bc
            )
            if not collides:
                prefix_map[prefix] = bc
                break

    return prefix_map


# Build once at import time
_PREFIX_MAP: dict = _build_prefix_map()


def learn_from_approved(new_products: list) -> int:
    """
    Permanently adds newly-approved unknown products into PRODUCT_CATALOG
    by rewriting this source file. Also rebuilds the prefix map in memory.
    Returns the number of products added.
    """
    if not new_products:
        return 0

    to_add = [
        p for p in new_products
        if p.get("barcode", "").strip()
        and p["barcode"].strip() not in PRODUCT_CATALOG
    ]
    if not to_add:
        return 0

    new_lines = []
    for p in to_add:
        bc   = p["barcode"].strip()
        name = p.get("name", "").replace('"', '\\"')
        new_lines.append(f'    "{bc}": "{name}",')

    extract_path = os.path.abspath(__file__)
    with open(extract_path, "r", encoding="utf-8") as f:
        source = f.read()

    marker = "}\n\n\ndef _build_prefix_map"
    if marker not in source:
        print("⚠️  [LEARN] Could not locate PRODUCT_CATALOG closing brace — skipping auto-learn.")
        return 0

    insertion = "\n".join(new_lines) + "\n"
    updated = source.replace(marker, insertion + marker, 1)

    with open(extract_path, "w", encoding="utf-8") as f:
        f.write(updated)

    # Update in-memory catalog and rebuild prefix map immediately
    for p in to_add:
        PRODUCT_CATALOG[p["barcode"].strip()] = p.get("name", "")
    global _PREFIX_MAP
    _PREFIX_MAP = _build_prefix_map()

    print(f"🧠 [LEARN] Added {len(to_add)} new product(s) to PRODUCT_CATALOG:")
    for p in to_add:
        print(f"   + [{p['barcode']}] {p.get('name','')[:60]}")

    return len(to_add)


def correct_products(products: List[Product]) -> Tuple[List[Product], List[str]]:
    """
    Post-processing pass using the product catalog:

    1. Exact barcode match → replace name with catalog name (fixes hallucinated names)
    2. Unique-prefix barcode match → correct the barcode, then replace name
    3. No match → keep as-is, log for manual review

    Grows automatically as you add entries to PRODUCT_CATALOG.
    """
    corrections: List[str] = []
    fixed: List[Product] = []

    for p in products:
        bc = p.barcode.strip()
        updates: dict = {}

        if bc in PRODUCT_CATALOG:
            # Exact match — correct the name if the model hallucinated it
            correct_name = PRODUCT_CATALOG[bc]
            if p.name.strip() != correct_name:
                corrections.append(f"Name corrected for {bc}: '{p.name[:35]}...' → catalog name")
                updates["name"] = correct_name

        else:
            # Try prefix matching with collision-safe map
            matched_bc = None
            for length in range(7, len(bc) + 1):
                prefix = bc[:length]
                if prefix in _PREFIX_MAP:
                    matched_bc = _PREFIX_MAP[prefix]
                    break

            if matched_bc and matched_bc != bc:
                corrections.append(
                    f"Barcode corrected: '{bc}' → '{matched_bc}' "
                    f"for '{p.name[:35]}'"
                )
                updates["barcode"] = matched_bc
                updates["name"] = PRODUCT_CATALOG[matched_bc]

        if updates:
            p = p.model_copy(update=updates)
        fixed.append(p)

    for c in corrections:
        print(f"🔧 [CORRECTION] {c}")

    return fixed, corrections


# Map each supplier key to the barcode prefixes that belong to their products.
# Used to filter the catalog block so only relevant entries are injected.
_SUPPLIER_BARCODE_PREFIXES: dict = {
    "pet_pharm":    ["8596075", "742797", "702160", "4743318", "7290111", "5060420", "6932844"],
    "pets_pro":     ["6976551", "6971067"],
    "pet_care":     ["0693493", "7265296", "0726529", "0788792", "8019808", "SKU-NT"],
    "beit_erez":    ["8682"],
    "fish_and_pets": ["8682"],
    "tzemach":      ["4250231"],
    "dudi":         ["8430235"],
}


def _catalog_reference_block(supplier_key: Optional[str] = None) -> str:
    """
    Formats the product catalog as a reference list for injection into the vision prompt.
    When supplier_key is given, only entries relevant to that supplier are included,
    keeping the prompt concise and reducing hallucination from unrelated products.
    """
    prefixes = _SUPPLIER_BARCODE_PREFIXES.get(supplier_key or "", []) if supplier_key else []

    def _relevant(bc: str) -> bool:
        if not prefixes:
            return True
        return any(bc.startswith(p) for p in prefixes)

    entries = [(bc, name) for bc, name in PRODUCT_CATALOG.items() if _relevant(bc)]
    if not entries:
        # Fall back to full catalog if nothing matched (e.g. new supplier)
        entries = list(PRODUCT_CATALOG.items())

    lines = ["Known products in this vendor's catalog (barcode → exact name):"]
    for bc, name in entries:
        lines.append(f"  {bc} → {name}")
    return "\n".join(lines)


def merge_promotional_rows(products: List[dict]) -> Tuple[List[dict], List[str]]:
    """
    Merges "buy X get Y free" invoice rows into a single product with:
      - combined quantity (paid + free)
      - original total cost (unchanged)
      - recalculated effective unit cost = cost / total_qty

    Detection rule: two consecutive rows share the same barcode where
    one row has cost > 0 (paid) and the next has cost == 0 (free gift).

    Example:
      NT-95 | qty 16 | cost 160.00  →  merged:
      NT-95 | qty  4 | cost   0.00       NT-95 | qty 20 | cost 160.00 | unit 8.00
    """
    if not products:
        return products, []

    merged   = []
    logs     = []
    skip_next = False

    for i, p in enumerate(products):
        if skip_next:
            skip_next = False
            continue

        # Look ahead: next row same barcode and cost=0?
        if i + 1 < len(products):
            nxt = products[i + 1]
            same_bc   = p.get("barcode") and p["barcode"] == nxt.get("barcode")
            paid_free = p.get("cost", 0) > 0 and nxt.get("cost", 0) == 0

            if same_bc and paid_free:
                total_qty  = p["quantity"] + nxt["quantity"]
                total_cost = p["cost"]
                unit_cost  = round(total_cost / total_qty, 4) if total_qty else 0

                merged_product = {
                    **p,
                    "quantity": total_qty,
                    "cost":     total_cost,
                    "unit_cost_effective": unit_cost,
                }
                merged.append(merged_product)
                logs.append(
                    f"Merged promo rows for {p['barcode']}: "
                    f"{p['quantity']} paid + {nxt['quantity']} free = "
                    f"{total_qty} units @ ₪{unit_cost:.4f} effective unit cost"
                )
                skip_next = True
                continue

        merged.append(p)

    for log in logs:
        print(f"🔀 [MERGE] {log}")

    return merged, logs



def run_vision_on_image(image_path: str, page_num: int = 1,
                        vendor_hint: str = "", supplier_key: Optional[str] = None,
                        total_pages: int = 1) -> dict:
    preprocessed = preprocess_image(image_path)
    try:
        return extract_from_image(
            preprocessed, page_num=page_num, vendor_hint=vendor_hint,
            supplier_key=supplier_key, total_pages=total_pages,
        )
    except Exception as e:
        print(f"❌ [ERROR] Vision failed on {image_path}: {e}")
        return {"products": [], "vendor_name": vendor_hint or "Unknown", "warnings": [str(e)]}
    finally:
        if preprocessed and os.path.exists(preprocessed):
            os.remove(preprocessed)


# ─────────────────────────────────────────────
#  PDF ENGINE
# ─────────────────────────────────────────────

def _is_hebrew_garbled(text: str) -> bool:
    if not text or len(text.strip()) < 100:
        return False
    heb  = sum(1 for c in text if "\u0590" <= c <= "\u05FF")
    garb = sum(1 for c in text if c in r"""!@#$%^&*~`|<>\/'""")
    h = heb  / max(len(text), 1)
    g = garb / max(len(text), 1)
    bad = h < 0.05 and g > 0.03
    if bad:
        print(f"⚠️  [LOG] Garbled font encoding (hebrew={h:.3f}, garbage={g:.3f}). Vision path.")
    return bad


def _pdf_to_pil_images(file_path: str, dpi: int = 300) -> List[Image.Image]:
    doc = fitz.open(file_path)
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    imgs = []
    for page in doc:
        pix = page.get_pixmap(matrix=mat, alpha=False)
        imgs.append(Image.frombytes("RGB", [pix.width, pix.height], pix.samples))
    doc.close()
    return imgs


def _extract_rtl_page_text(page) -> str:
    """
    Extract text from a single pdfplumber page in correct RTL reading order.
    Groups words into lines by vertical position, then sorts each line right→left.
    Falls back to extract_text() if word extraction yields nothing.
    """
    words = page.extract_words(x_tolerance=3, y_tolerance=3, keep_blank_chars=False)
    if not words:
        return page.extract_text() or ""

    # Round top coordinate to 5-pixel bands so words on the same visual line cluster
    for w in words:
        w["_line"] = round(w["top"] / 5) * 5

    lines: list[str] = []
    current_band = None
    current_words: list[dict] = []

    for w in sorted(words, key=lambda x: (x["_line"], -x["x0"])):
        if w["_line"] != current_band:
            if current_words:
                lines.append(" ".join(cw["text"] for cw in current_words))
            current_band = w["_line"]
            current_words = [w]
        else:
            current_words.append(w)

    if current_words:
        lines.append(" ".join(cw["text"] for cw in current_words))

    return "\n".join(lines)


def get_pdf_type_and_content(file_path: str):
    with pdfplumber.open(file_path) as pdf:
        pages_text = [_extract_rtl_page_text(p) for p in pdf.pages]

    avg = sum(len(t.strip()) for t in pages_text) / max(len(pages_text), 1)
    full = "\n--- PAGE BREAK ---\n".join(pages_text)

    if avg <= 200 or _is_hebrew_garbled(full):
        label = "Scanned PDF" if avg <= 200 else "Broken font encoding"
        print(f"📸 [LOG] {label} — rendering via pymupdf...")
        return "scanned", _pdf_to_pil_images(file_path)

    print("💎 [LOG] Digital PDF — extracting text...")
    return "digital", full


# ─────────────────────────────────────────────
#  DIGITAL PDF
# ─────────────────────────────────────────────

_DIGITAL_SYSTEM = """\
Invoice parsing engine for Hebrew supplier invoices.
Extract every product line exactly. Never hallucinate. Never skip rows.\
"""

_DIGITAL_PROMPT = """\
Extract all products from this invoice text.

- Copy Hebrew product names exactly as they appear.
- barcode = 8-13 digit code. Copy every digit exactly.
- cost = line total (סה"כ נטו). Multiply unit × qty only if no line total.
- cost=0.0 is valid (100% discount).
- Skip only VAT/subtotal/grand-total rows.

Invoice text:
{text}
"""


def extract_from_digital_pdf(text: str, supplier_key: Optional[str] = None) -> dict:
    print("⚙️  [LOG] Parsing digital PDF text...")
    profile = get_supplier_profile(supplier_key)

    # Build a supplier-aware text prompt
    supplier_hint = f"""
Supplier: {profile['display_name']}
Cost column: {profile['cost_column']}
Has SKU column: {profile.get('has_sku', False)}
{profile['prompt_extra'].strip()}
"""
    full_prompt = _DIGITAL_PROMPT.format(text=text) + f"\n\nSUPPLIER CONTEXT:\n{supplier_hint}"

    llm = _get_structured_llm(InvoiceExtraction)
    result = llm.invoke([
        SystemMessage(content=_DIGITAL_SYSTEM),
        HumanMessage(content=full_prompt)
    ])
    result, warnings = validate_extraction(result)
    corrected, corrections = correct_products(result.products)
    products_dicts = [p.model_dump() for p in corrected]
    merged, merge_logs = merge_promotional_rows(products_dicts)
    return {
        "products":       merged,
        "vendor_name":    result.vendor_name or profile["display_name"],
        "invoice_number": result.invoice_number,
        "invoice_date":   result.invoice_date,
        "warnings":       warnings + corrections + merge_logs,
    }


# ─────────────────────────────────────────────
#  MAIN NODE  (LangGraph entry point)
# ─────────────────────────────────────────────

def extract_invoice_data(state: dict) -> dict:
    file_path = state.get("file_path")
    print(f"\n🚀 [LOG] Processing: {os.path.basename(file_path)}")

    ext          = os.path.splitext(file_path)[1].lower()
    all_products: list = []
    vendor_name    = "Unknown"
    invoice_number = ""
    invoice_date   = ""
    all_warnings: list = []
    supplier_key = None   # identified once, reused across all pages

    try:
        if ext == ".pdf":
            pdf_type, content = get_pdf_type_and_content(file_path)

            if pdf_type == "digital":
                # Try to identify supplier from text first (fast, no LLM)
                supplier_key = identify_supplier_from_text(content)
                if supplier_key:
                    profile = get_supplier_profile(supplier_key)
                    vendor_name = profile["display_name"]
                d              = extract_from_digital_pdf(content, supplier_key=supplier_key)
                all_products   = d["products"]
                vendor_name    = d["vendor_name"] or vendor_name
                invoice_number = d.get("invoice_number", "")
                invoice_date   = d.get("invoice_date", "")
                all_warnings   = d["warnings"]
            else:
                total_pages = len(content)
                for i, img in enumerate(content):
                    print(f"📄 [LOG] Page {i+1}/{total_pages}...")
                    tmp = f"temp_page_{i}.jpg"
                    img.save(tmp, "JPEG")

                    d = run_vision_on_image(
                        tmp, page_num=i + 1,
                        vendor_hint=vendor_name,
                        supplier_key=supplier_key,
                        total_pages=total_pages,
                    )
                    all_products.extend(d["products"])
                    all_warnings.extend(d.get("warnings", []))

                    if d.get("vendor_name") and vendor_name == "Unknown":
                        vendor_name = d["vendor_name"]
                    # Capture invoice number/date from first page that has them
                    if not invoice_number:
                        invoice_number = d.get("invoice_number", "")
                    if not invoice_date:
                        invoice_date = d.get("invoice_date", "")
                    if not supplier_key and d.get("supplier_key"):
                        supplier_key = d["supplier_key"]

                    if os.path.exists(tmp):
                        os.remove(tmp)

        else:
            d              = run_vision_on_image(file_path, page_num=1)
            all_products   = d["products"]
            vendor_name    = d.get("vendor_name", "Unknown")
            invoice_number = d.get("invoice_number", "")
            invoice_date   = d.get("invoice_date", "")
            all_warnings   = d.get("warnings", [])

        print(f"✅ [LOG] Extracted {len(all_products)} products from {vendor_name}")
        if invoice_number:
            print(f"📄 [LOG] Invoice: {invoice_number}  Date: {invoice_date}")
        if all_warnings:
            print(f"⚠️  [LOG] {len(all_warnings)} validation warning(s).")

        # Enrich with Wolt catalog data (sell price, margin, match info)
        try:
            from wolt_catalog import enrich_products, is_loaded
            if is_loaded():
                all_products = enrich_products(all_products)
        except ImportError:
            pass

        return {
            "products":       all_products,
            "vendor_name":    vendor_name,
            "invoice_number": invoice_number,
            "invoice_date":   invoice_date,
            "warnings":       all_warnings,
            "status": f"Successfully processed {len(all_products)} items from {vendor_name}",
        }

    except Exception as e:
        print(f"❌ [ERROR] {e}")
        return {
            "products": [], "vendor_name": "Unknown",
            "warnings": [str(e)], "status": "error",
        }