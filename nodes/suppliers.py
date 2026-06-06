"""
suppliers.py — Supplier identification and per-supplier extraction profiles.

HOW IT WORKS:
1. identify_supplier(image_b64) — fast LLM call reads the invoice header,
   returns the supplier key (e.g. "pet_pharm", "beit_erez").
2. get_supplier_profile(key) — returns the profile dict for that supplier,
   containing the exact vision prompt and extraction hints.
3. extract.py calls identify_supplier() before extraction, then uses the
   profile's prompt instead of the generic one.

TO ADD A NEW SUPPLIER:
1. Add an entry to SUPPLIER_PROFILES below.
2. Add identification patterns to SUPPLIER_PATTERNS.
3. Run one invoice through the agent — new products auto-learn into PRODUCT_CATALOG.
"""

import os
import base64
import json
import re
from typing import Optional


# ─────────────────────────────────────────────
#  SUPPLIER PROFILES
#  Each profile contains:
#    display_name   — Hebrew name as it appears in Sheets
#    id_patterns    — strings that identify this supplier (checked in order)
#    has_sku        — whether invoices have a separate SKU/catalog column
#    has_promo_rows — whether free promotional rows (cost=0) appear
#    cost_column    — which column holds the line total
#    column_layout  — human-readable description of column order for the prompt
#    prompt_extra   — additional instructions specific to this supplier
# ─────────────────────────────────────────────

SUPPLIER_PROFILES: dict = {

    "pet_pharm": {
        "display_name": "פט פארם בע\"מ",
        "website": "https://www.pet-pharm.co.il",
        "brands": [],
        "id_patterns": ["פט פארם", "PET-PHARM", "WWW.PET-PHARM", "514905801", "8122462"],
        "has_sku": False,
        "has_promo_rows": True,
        "cost_column": "סה\"כ נטו",
        "column_layout": """
Table is RTL. Column order left→right on screen:
  סה"כ נטו (LINE TOTAL) | נטו ליחידה (unit price) | %הנחה (discount) |
  ברוטו/לצרכן ליחידה (gross) | כמות (qty) | תאור פריט (description) | מס' פריט (#) | ברקוד (barcode, FAR LEFT)

The BARCODE column is the LEFTMOST on the page.
The LINE TOTAL (סה"כ נטו) is the second column from the left.
""",
        "prompt_extra": """
- This supplier has NO SKU column — only barcodes (13-digit EAN).
- Rows with 100% discount (cost=0) are promotional free units — keep them.
- Some rows show crossed-out prices with 0.00 written — cost is 0.0 for those.
""",
    },

    "beit_erez": {
        "display_name": "בית ארז חוות מילטין בע\"מ",
        "website": "",
        "brands": [],
        "id_patterns": ["בית ארז", "milatin-group", "511088106", "MILATIN", "לתאם אספקה"],
        "has_sku": True,
        "has_promo_rows": True,
        "cost_column": "סה\"כ מחיר",
        "column_layout": """
Table is RTL. Column order left→right on screen:
  סה"כ מחיר (LINE TOTAL) | מחיר לי' אחרי הנחה (net unit) | הנחה% (discount) |
  מחיר ליחידה (unit price) | כמות (qty) | תאור מוצר (description) | ברקוד (barcode) | מק"ט (SKU)

The LINE TOTAL (סה"כ מחיר) is the LEFTMOST column.
The SKU (מק"ט) is the RIGHTMOST column — 8-digit numbers like 64000023.
The barcode is second from right — 13-digit EAN starting with 8682.
""",
        "prompt_extra": """
- Fill the sku field from the מק"ט column (8-digit numbers on the right).
- Rows with 99.90% discount are essentially free — cost will be ~0.06, round to actual value shown.
- The supplier name on these invoices is "בית ארז חוות מילטין בע"מ".
""",
    },

    "fish_and_pets": {
        "display_name": "פיש אנד פטס ישראל 1975 בע\"מ",
        "website": "",
        "brands": [],
        "id_patterns": ["פיש אנד פטס", "Fish & Pets", "fish@milatin", "510704406", "FISH & PETS"],
        "has_sku": True,
        "has_promo_rows": False,
        "cost_column": "סה\"כ מחיר",
        "column_layout": """
Table is RTL. Column order left→right on screen (SAME TEMPLATE AS BEIT AREZ):
  סה"כ מחיר (LINE TOTAL) | מחיר לי' אחרי הנחה (net unit) | הנחה% (discount) |
  מחיר ליחידה (unit price) | כמות (qty) | תאור מוצר (description) | ברקוד (barcode) | מק"ט (SKU)

The LINE TOTAL (סה"כ מחיר) is the LEFTMOST column.
The SKU (מק"ט) is the RIGHTMOST — 9-digit numbers like 392000084.
The barcode is second from right — starts with * symbol, 13 digits inside.
""",
        "prompt_extra": """
- Fill the sku field from the מק"ט column (rightmost, 9-digit numbers).
- Barcodes may appear with asterisks (*6927749810346*) — extract only the digits.
- This invoice has no promotional free rows.
""",
    },

    "osem": {
        "display_name": "אסם נסלה ישראל בע\"מ",
        "website": "https://www.osem.co.il",
        "brands": [],
        "id_patterns": ["אסם נסלה", "אסם", "OSEM", "9001600706", "9001596901", "IL557268760", "IL1326"],
        "has_sku": True,
        "has_promo_rows": True,
        "cost_column": "מחדר נסו לי'",
        "column_layout": """
Table has many columns. Key columns left→right:
  מחדר (net cost per unit, LEFTMOST) | אחוז הנחה מבצע | אחוז הנחה | סה"כ ב"ש"ח ללא פיקדון |
  כמות לפיקדון | כמות יח' | יח' | מידה | כמות | תאור מוצר (description) |
  לשימוש (SKU internal) | מק"ס (catalog number) | ברקוד (barcode)

The BARCODE is on the FAR LEFT.
The catalog number (מק"ס) is an 8-digit number like 12609763.
The LINE TOTAL = מחדר × כמות (multiply net unit price by quantity).
""",
        "prompt_extra": """
- Fill sku from the מק"ס column (8-digit catalog number).
- The cost field = מחדר (net unit cost) × כמות (quantity) — there is no pre-calculated line total.
- Some rows have פיקדון (deposit) charges — include these as separate line items with cost=0.01 if shown.
- Ignore the last row "משמת עץ הפצה כללי" — it is a system/summary row.
- The supplier is "אסם נסלה ישראל בע"מ".
""",
    },

    "pets_pro": {
        "display_name": "פטס פרו רחובות",
        "website": "https://petspro.co.il",
        "brands": [],
        "id_patterns": ["פטס פרו", "petspro.co.il", "PET'S PRO", "021470471", "0524556663"],
        "has_sku": False,
        "has_promo_rows": True,
        "cost_column": "סה\"כ נטו",
        "column_layout": """
Table is RTL. Column order left→right on screen:
  סה"כ נטו (LINE TOTAL) | נטו ליחידה (unit price) | %הנחה (discount) |
  ברוטו/לצרכן ליחידה (gross) | כמות (qty) | תאור פריט (description) | # מס' פריט (row number)

The BARCODE is on the FAR LEFT — 13-digit EAN codes starting with 697.
The LINE TOTAL (סה"כ נטו) is second from left.
Row numbers (#) on the right are NOT SKUs — they are just sequential line numbers.
""",
        "prompt_extra": """
- This invoice has NO catalog SKU — the rightmost column (#) is just a row counter, ignore it.
- Rows with 100% discount (cost=0) are promotional free units — keep them.
- This invoice can be 6+ pages with 100+ products — extract ALL rows on every page.
- Barcodes start with 6976551 or 6971067.
""",
    },

    "biopet": {
        "display_name": "ביופט בע\"מ",
        "website": "",
        "brands": [],
        "id_patterns": ["ביופט", "Biopet", "4023000", "1-800-602-030", "09-8984152", "SELF-84"],
        "has_sku": True,
        "has_promo_rows": False,
        "cost_column": "סה\"כ",
        "column_layout": """
Table is LTR (left to right — unusual for Hebrew invoices!).
Column order left→right:
  # (row) | ברקוד (barcode) | מק"ט (SKU) | תיאור (description) | כמות (qty) |
  מחיר (unit price) | %הנחה מסחרית | %הנחה כמות | %המבצע שלי | מחיר נטו שלי | סה"כ (LINE TOTAL)

The LINE TOTAL (סה"כ) is the RIGHTMOST column.
The SKU (מק"ט) is a 7-digit number like 5554305.
The barcode is second column from left — printed as a barcode image with number below.
""",
        "prompt_extra": """
- This is an LTR invoice — the LINE TOTAL is on the RIGHT, not the left.
- Fill sku from the מק"ט column (7-digit numbers).
- The cost field = סה"כ column (rightmost numeric column).
- No promotional free rows in this invoice type.
""",
    },

    "tzemach": {
        "display_name": "צמח-ישראפט אגודה שיתופית חקלאית בע\"מ",
        "website": "",
        "brands": [],
        "id_patterns": ["צמח-ישראפט", "צמח ישראפט", "570060798", "IN260", "חיפה 3225", "04-6356538"],
        "has_sku": True,
        "has_promo_rows": False,
        "cost_column": "סה\"כ",
        "column_layout": """
Table is RTL. Column order left→right on screen:
  סה"כ (LINE TOTAL) | מחיר לי' אחרי הנחה | הנחה% | מחיר ליחידה | כמות |
  תאור מוצר (description) | ברקוד (barcode) | מק"ט (SKU)

The LINE TOTAL (סה"כ) is the LEFTMOST column.
The SKU (מק"ט) is 5-digit numbers like 30400, 30441.
Barcodes are wrapped in asterisks (*4250231595011*) — extract digits only, no asterisks.
""",
        "prompt_extra": """
- Barcodes appear with asterisk delimiters (*barcode*) — strip the asterisks, keep only digits.
- Fill sku from the מק"ט column (5-digit numbers on the right).
- The last row "מגרדת קרטון אליזקט לחתולים" has barcode *7290119702169* — include it.
""",
    },

    "dudi": {
        "display_name": "דודי סוכנויות",
        "website": "https://dudi-agencies.co.il",
        "brands": [
            {"name": "Gosbi", "website": "https://gosbi.com"},
        ],
        "id_patterns": ["דודי סוכנויות", "512171703", "dudi-agencies.co.il", "103805", "3056197"],
        "has_sku": True,
        "has_promo_rows": True,
        "cost_column": "סה\"כ",
        "column_layout": """
Table is RTL. Column order left→right on screen:
  סה"כ (LINE TOTAL) | הנחה% | מחיר (unit price) | כמות (qty) |
  שם פריט (description) | מ. פריט (SKU) | בר קוד (barcode)

The LINE TOTAL (סה"כ) is the LEFTMOST column.
The SKU (מ. פריט) is 4-digit numbers like 4262, 7233, 7336.
The barcode is on the FAR LEFT of the barcode column — 13-digit EAN.
""",
        "prompt_extra": """
- Fill sku from the מ. פריט column (4-digit numbers).
- Rows with cost=0 are free promotional units — keep them.
- Product names are in Hebrew, may include size (M, L, XL) and weight info.
- Row 3676 "סראנו דגים 100 גרם" has barcode 8430235681255 and cost=0 (free sample).
""",
    },

    "pet_care": {
        "display_name": "א.א פטקאר יבוא והפצה בע\"מ",
        "website": "",
        "brands": [],
        "id_patterns": ["פטקאר", "PET-CARE", "Pet-Care", "515379477", "i8691562"],
        "has_sku": True,
        "has_promo_rows": True,
        "cost_column": "סכום נטו",
        "column_layout": """
Table is RTL. Column order left→right on screen:
  סכום נטו (LINE TOTAL) | % הנחה (discount, handwritten) | מחיר יחידה (unit price) |
  כמות (qty) | ברקוד (barcode) | שם פריט (description) | מס. פריט (SKU, rightmost)

The LINE TOTAL (סכום נטו) is the LEFTMOST column.
The SKU (מס. פריט) is on the FAR RIGHT — format like NT-95, NT-202, C753/H.
The barcode is middle — 13-digit EAN like 0693493258517.
Some rows have no barcode — use the SKU as identifier in those cases.
""",
        "prompt_extra": """
- Fill sku from the מס. פריט column (rightmost, format: NT-95, NT-93, C753/H etc).
- Rows with no barcode are normal — leave barcode empty for those.
- Pairs of rows with same barcode: first row is paid, second is free (promotional).
- The handwritten % discount column can be ignored for cost calculation.
- Cost = סכום נטו column (leftmost).
""",
    },
}


# ─────────────────────────────────────────────
#  IDENTIFICATION LOGIC
# ─────────────────────────────────────────────

def identify_supplier_from_text(text: str) -> Optional[str]:
    """
    Fast identification from extracted text — no LLM needed.
    Checks text against each supplier's id_patterns.
    Returns supplier key or None if unrecognized.
    """
    text_lower = text.lower()
    for key, profile in SUPPLIER_PROFILES.items():
        for pattern in profile["id_patterns"]:
            if pattern.lower() in text_lower:
                return key
    return None


def identify_supplier_from_image(image_b64: str, llm) -> Optional[str]:
    """
    LLM-based identification from invoice image header.
    Used when text extraction is unavailable or garbled.
    Returns supplier key or None.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    # Build a compact pattern list for the prompt
    pattern_list = "\n".join(
        f'  "{key}": look for {", ".join(p["id_patterns"][:3])}'
        for key, p in SUPPLIER_PROFILES.items()
    )

    prompt = f"""Look at the header of this invoice image.
Identify the supplier by matching text in the header to these patterns:

{pattern_list}

Return ONLY the supplier key (e.g. "pet_pharm") — no other text.
If you cannot identify the supplier, return "unknown"."""

    try:
        response = llm.invoke([
            SystemMessage(content="You identify Israeli supplier invoices. Return only the supplier key."),
            HumanMessage(content=[
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {
                    "url": f"data:image/jpeg;base64,{image_b64}",
                    "detail": "low"   # low detail — just reading the header
                }}
            ])
        ])
        key = response.content.strip().strip('"').lower()
        if key in SUPPLIER_PROFILES:
            return key
        return None
    except Exception as e:
        print(f"⚠️  [SUPPLIERS] Identification failed: {e}")
        return None


def get_supplier_profile(key: Optional[str]) -> dict:
    """
    Returns the supplier profile for a given key.
    Falls back to a generic profile if key is None or unknown.
    """
    if key and key in SUPPLIER_PROFILES:
        profile = SUPPLIER_PROFILES[key]
        print(f"🏪 [SUPPLIERS] Identified: {profile['display_name']}")
        return profile

    print("⚠️  [SUPPLIERS] Supplier not recognized — using generic extraction profile.")
    return {
        "display_name": "Unknown Supplier",
        "has_sku": True,
        "has_promo_rows": True,
        "cost_column": "LINE TOTAL",
        "column_layout": """
Identify the column layout from the header row.
The LINE TOTAL column is usually the leftmost numeric column for RTL invoices,
or the rightmost for LTR invoices.
The barcode column contains 8-13 digit EAN/UPC numbers.
The SKU column contains shorter alphanumeric catalog codes.
""",
        "prompt_extra": """
- Extract all product rows completely.
- Use the line total (not unit price) as cost.
- Keep rows with cost=0 if they have a valid product name.
""",
    }


def build_vision_prompt(profile: dict, page_num: int = 1,
                        total_pages: int = 1, catalog_block: str = "") -> str:
    """
    Builds the supplier-specific vision prompt from a profile.
    This replaces the generic _VISION_PROMPT in extract.py.
    """
    has_sku_instruction = (
        "Fill the sku field from the catalog/SKU column."
        if profile.get("has_sku")
        else "Leave sku as empty string — this invoice has no separate SKU column."
    )

    promo_instruction = (
        "Rows with cost=0 or 100% discount are real products given free — KEEP them."
        if profile.get("has_promo_rows")
        else "All rows should have a positive cost."
    )

    prompt = f"""This is page {page_num} of {total_pages} from a supplier invoice.
Supplier: {profile['display_name']}

COLUMN LAYOUT:
{profile['column_layout'].strip()}

EXTRACTION RULES:
- Copy Hebrew product names EXACTLY as printed — every word, brand, size, English term.
- Copy barcodes digit-by-digit — do NOT add, remove, or change any digit.
- Cost field = {profile['cost_column']} column value.
- {has_sku_instruction}
- {promo_instruction}
- Extract EVERY product row — do not stop early.
- Do NOT invent data that is not visible in the image.

SUPPLIER-SPECIFIC NOTES:
{profile['prompt_extra'].strip()}
"""

    if catalog_block:
        prompt += f"\n\nKNOWN PRODUCTS (if barcode matches, use this exact name):\n{catalog_block}"

    return prompt