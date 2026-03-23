import os
import base64
import pdfplumber
import fitz  # pymupdf — pip install pymupdf, no system dependencies needed
from typing import List, Optional, Tuple
from pydantic import BaseModel, Field
from langchain_openai import ChatOpenAI
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from dotenv import load_dotenv
from PIL import Image, ImageOps, ImageFilter, ImageEnhance
import re

load_dotenv()

# ─────────────────────────────────────────────
#  CONFIGURATION
# ─────────────────────────────────────────────

LLM_PROVIDER    = os.getenv("LLM_PROVIDER", "auto")
OPENAI_MODEL    = "gpt-4o"
ANTHROPIC_MODEL = "claude-sonnet-4-5"   # best Hebrew OCR; swap to claude-opus-4-5 for max quality


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
        description="Leave as empty string. This invoice format has no separate SKU column."
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
        "Number from the סה\"כ נטו column (LINE TOTAL — not unit price). "
        "0.0 is valid when a product has 100% discount. Never negative."
    ))


class InvoiceExtraction(BaseModel):
    vendor_name: str = Field(description="Supplier name exactly as printed on the invoice header.")
    products: List[Product]


# ─────────────────────────────────────────────
#  LLM FACTORY
# ─────────────────────────────────────────────

_base_cache: dict = {}


def _get_base_llm(provider: Optional[str] = None):
    provider = provider or LLM_PROVIDER
    if provider == "auto":
        provider = "anthropic" if os.getenv("ANTHROPIC_API_KEY") else "openai"
    if provider not in _base_cache:
        if provider == "anthropic":
            _base_cache[provider] = ChatAnthropic(
                model=ANTHROPIC_MODEL, temperature=0, max_tokens=8192
            )
        else:
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

ALWAYS:
- Copy each product's Hebrew description verbatim from the תאור פריט column
- Copy each barcode digit-by-digit from the ברקוד / מס' פריט column
- Use סה"כ נטו (line total) as cost — NOT נטו ליחידה (unit price)
- Record cost=0.0 for rows where price shows 0 or is crossed out with 0 written
"""

_VISION_PROMPT = """\
This is page {page_num} of a Pet Pharm (פט פארם) Israeli supplier invoice.

Column layout (the table is RTL — Hebrew reads right to left):
  RIGHT SIDE of page → LEFT SIDE of page
  מס' (#) | תאור פריט (description) | כמות (qty) | ברוטו ליחידה | %הנחה | נטו ליחידה | סה"כ נטו | ברקוד

The ברקוד (barcode) column is on the FAR LEFT edge of the table.
These are 13-digit EAN codes. Read every digit carefully.

The סה"כ נטו column is the SECOND from the left — this is the line total (cost).
Do NOT use ברוטו ליחידה or נטו ליחידה as cost.

Rules:
- Each barcode number = one unique product row
- Multi-line descriptions belong to the row with that barcode
- Rows 7 and 8 on page 1 may show crossed-out prices with 0.00 — cost=0.0 for those
- Extract EVERY row; do not stop early
- If a barcode matches one in the catalog list below, use that catalog name EXACTLY

Return all {expected} products on this page.
"""


def extract_from_image(image_path: str, page_num: int = 1,
                       expected: int = 0, vendor_hint: str = "") -> dict:
    """Single-stage: image → structured InvoiceExtraction directly."""
    print(f"🔍 [LOG] Direct vision extraction (page {page_num})...")
    b64 = encode_image(image_path)
    llm = _get_structured_llm(InvoiceExtraction)

    prompt = _VISION_PROMPT.format(
        page_num=page_num,
        expected=f"approximately {expected}" if expected else "all"
    )
    if vendor_hint and vendor_hint != "Unknown":
        prompt += f"\nVendor already identified: {vendor_hint}"

    # Inject known catalog so model can anchor on exact names and barcodes
    prompt += "\n\n" + _catalog_reference_block()

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
    return {
        "products":    [p.model_dump() for p in corrected],
        "vendor_name": result.vendor_name,
        "warnings":    warnings + corrections,
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

        if p.barcode and not re.match(r"^\d+$", p.barcode):
            warnings.append(f"{label}: barcode '{p.barcode}' has non-digit chars — check manually.")

        if p.barcode and len(p.barcode) not in (8, 12, 13):
            warnings.append(f"{label}: barcode length {len(p.barcode)} unexpected (8/12/13).")

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

    return InvoiceExtraction(vendor_name=extraction.vendor_name, products=clean), warnings


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


def _catalog_reference_block() -> str:
    """Formats the product catalog as a reference list for injection into the vision prompt."""
    lines = ["Known products in this vendor's catalog (barcode → exact name):"]
    for bc, name in PRODUCT_CATALOG.items():
        lines.append(f"  {bc} → {name}")
    return "\n".join(lines)



def run_vision_on_image(image_path: str, page_num: int = 1,
                        vendor_hint: str = "") -> dict:
    preprocessed = preprocess_image(image_path)
    try:
        return extract_from_image(
            preprocessed, page_num=page_num, vendor_hint=vendor_hint
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


def get_pdf_type_and_content(file_path: str):
    with pdfplumber.open(file_path) as pdf:
        pages_text = [p.extract_text() or "" for p in pdf.pages]

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


def extract_from_digital_pdf(text: str) -> dict:
    print("⚙️  [LOG] Parsing digital PDF text...")
    llm = _get_structured_llm(InvoiceExtraction)
    result = llm.invoke([
        SystemMessage(content=_DIGITAL_SYSTEM),
        HumanMessage(content=_DIGITAL_PROMPT.format(text=text))
    ])
    result, warnings = validate_extraction(result)
    corrected, corrections = correct_products(result.products)
    return {
        "products":    [p.model_dump() for p in corrected],
        "vendor_name": result.vendor_name,
        "warnings":    warnings + corrections,
    }


# ─────────────────────────────────────────────
#  MAIN NODE  (LangGraph entry point)
# ─────────────────────────────────────────────

def extract_invoice_data(state: dict) -> dict:
    file_path = state.get("file_path")
    print(f"\n🚀 [LOG] Processing: {os.path.basename(file_path)}")

    ext          = os.path.splitext(file_path)[1].lower()
    all_products: list = []
    vendor_name  = "Unknown"
    all_warnings: list = []

    try:
        if ext == ".pdf":
            pdf_type, content = get_pdf_type_and_content(file_path)

            if pdf_type == "digital":
                d            = extract_from_digital_pdf(content)
                all_products = d["products"]
                vendor_name  = d["vendor_name"]
                all_warnings = d["warnings"]
            else:
                for i, img in enumerate(content):
                    print(f"📄 [LOG] Page {i+1}/{len(content)}...")
                    tmp = f"temp_page_{i}.jpg"
                    img.save(tmp, "JPEG")

                    d = run_vision_on_image(tmp, page_num=i + 1, vendor_hint=vendor_name)
                    all_products.extend(d["products"])
                    all_warnings.extend(d.get("warnings", []))
                    if d.get("vendor_name") and vendor_name == "Unknown":
                        vendor_name = d["vendor_name"]

                    if os.path.exists(tmp):
                        os.remove(tmp)

        else:
            d            = run_vision_on_image(file_path, page_num=1)
            all_products = d["products"]
            vendor_name  = d.get("vendor_name", "Unknown")
            all_warnings = d.get("warnings", [])

        print(f"✅ [LOG] Extracted {len(all_products)} products from {vendor_name}")
        if all_warnings:
            print(f"⚠️  [LOG] {len(all_warnings)} validation warning(s).")

        return {
            "products":    all_products,
            "vendor_name": vendor_name,
            "warnings":    all_warnings,
            "status": f"Successfully processed {len(all_products)} items from {vendor_name}",
        }

    except Exception as e:
        print(f"❌ [ERROR] {e}")
        return {
            "products": [], "vendor_name": "Unknown",
            "warnings": [str(e)], "status": "error",
        }