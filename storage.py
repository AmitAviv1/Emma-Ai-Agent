"""
storage.py — Google Sheets storage node for the invoice automation pipeline.

SETUP (one-time):
1. pip install gspread google-auth
2. Go to https://console.cloud.google.com → New Project
3. Enable "Google Sheets API" and "Google Drive API"
4. Create a Service Account → download the JSON key
5. Share your target Google Sheet with the service account email (Editor access)
6. Add to .env:
      GOOGLE_SERVICE_ACCOUNT_JSON=/path/to/your-service-account.json
      GOOGLE_SHEET_ID=your_sheet_id_from_the_url

SHEET STRUCTURE (auto-created on first run, per vendor):
  Tab "{Vendor} — Invoice Lines"  — one row per invoice line, full history
  Tab "{Vendor} — Products"       — one row per unique barcode, latest cost + cumulative qty

  Example tabs after processing two suppliers:
    פט פארם — Invoice Lines
    פט פארם — Products
    SuperPharm — Invoice Lines
    SuperPharm — Products
"""

import os
import json
from datetime import datetime
from typing import List, Optional
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────
#  LAZY IMPORT — gspread only required at runtime
# ─────────────────────────────────────────────

def _get_sheet_client():
    """Returns an authenticated gspread client. Fails clearly if credentials missing."""
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        raise ImportError(
            "Missing dependencies. Run: pip install gspread google-auth"
        )

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    info = _load_service_account_info()
    if info is None:
        raise FileNotFoundError(
            "No Google service-account credentials found.\n"
            "  • Local: set GOOGLE_SERVICE_ACCOUNT_JSON in .env to the key file path.\n"
            "  • Streamlit Cloud: add a [gcp_service_account] table in Secrets."
        )

    if isinstance(info, str):        # a file path
        creds = Credentials.from_service_account_file(info, scopes=scopes)
    else:                            # a dict from Secrets / raw JSON
        creds = Credentials.from_service_account_info(info, scopes=scopes)
    return gspread.authorize(creds)


def _load_service_account_info():
    """Resolve service-account credentials from (in priority order):
      1. Streamlit Secrets  →  [gcp_service_account] table (cloud deploy)
      2. GOOGLE_SERVICE_ACCOUNT_JSON holding raw JSON  (env/secret)
      3. GOOGLE_SERVICE_ACCOUNT_JSON holding a file path  (local default)
    Returns a dict, a path string, or None if nothing is configured."""
    # 1) Streamlit Secrets (nested table isn't exported to os.environ)
    try:
        import streamlit as st
        if "gcp_service_account" in st.secrets:
            return dict(st.secrets["gcp_service_account"])
    except Exception:
        pass

    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        return None
    # 2) Raw JSON blob
    if raw.startswith("{"):
        import json
        try:
            return json.loads(raw)
        except Exception:
            return None
    # 3) File path
    if os.path.exists(raw):
        return raw
    return None


def _get_or_create_sheet(client, sheet_id: str, tab_name: str, headers: list):
    """
    Opens a worksheet by name. Creates it with headers if it doesn't exist.
    Returns the worksheet object.
    """
    spreadsheet = client.open_by_key(sheet_id)

    try:
        ws = spreadsheet.worksheet(tab_name)
    except Exception:
        ws = spreadsheet.add_worksheet(title=tab_name, rows=1000, cols=len(headers))
        ws.append_row(headers, value_input_option="USER_ENTERED")
        print(f"📋 [SHEETS] Created new tab: '{tab_name}'")

    return ws


# ─────────────────────────────────────────────
#  COLUMN DEFINITIONS
# ─────────────────────────────────────────────

INVOICE_LINES_HEADERS = [
    "invoice_number",
    "invoice_date",
    "vendor_name",
    "barcode",
    "product_name",
    "quantity",
    "unit_cost",       # cost / quantity
    "line_total",      # cost as extracted
    "status",          # "approved" | "pending_review"
    "processed_at",
]

PRODUCTS_HEADERS = [
    "barcode",
    "product_name",
    "vendor_name",
    "latest_unit_cost",
    "total_qty_ordered",
    "invoice_count",
    "first_seen",
    "last_seen",
]


# ─────────────────────────────────────────────
#  VALIDATION LOGIC
# ─────────────────────────────────────────────

# Import the catalog from extract.py for auto-approval checks
def _load_catalog() -> set:
    try:
        from extract import PRODUCT_CATALOG
        return set(PRODUCT_CATALOG.keys())
    except ImportError:
        return set()


def validate_products(products: list, vendor_name: str) -> dict:
    """
    Splits products into approved (barcode in catalog) and pending (unknown barcode).

    Returns:
        {
            "approved":       [...],   # written to Sheets immediately
            "pending_review": [...],   # held for terminal confirmation
        }
    """
    catalog = _load_catalog()
    approved = []
    pending = []

    for p in products:
        bc = p.get("barcode", "").strip()
        if bc in catalog:
            approved.append({**p, "status": "approved"})
        else:
            pending.append({**p, "status": "pending_review"})

    print(f"✅ [VALIDATION] {len(approved)} products auto-approved (in catalog)")
    if pending:
        print(f"⚠️  [VALIDATION] {len(pending)} unknown products need review:")
        for p in pending:
            print(f"   • [{p.get('barcode','NO BARCODE')}] {p.get('name','')[:60]}"
                  f"  qty={p.get('quantity')}  cost={p.get('cost')} NIS")

    return {"approved": approved, "pending_review": pending}


def prompt_manual_review(pending: list) -> list:
    """
    Interactive terminal prompt for each pending product.
    Returns the subset the user approves.
    """
    if not pending:
        return []

    print("\n" + "="*60)
    print("🔍 MANUAL REVIEW REQUIRED")
    print("="*60)
    approved = []

    for i, p in enumerate(pending):
        print(f"\n[{i+1}/{len(pending)}] Unknown product:")
        print(f"  Barcode : {p.get('barcode', 'N/A')}")
        print(f"  Name    : {p.get('name', 'N/A')}")
        print(f"  Qty     : {p.get('quantity')}    Cost: {p.get('cost')} NIS")

        while True:
            choice = input("  → Approve and save? [y/n/e(dit name)]: ").strip().lower()
            if choice == "y":
                approved.append({**p, "status": "approved"})
                print("  ✅ Approved")
                break
            elif choice == "n":
                print("  ⏭  Skipped (not saved)")
                break
            elif choice == "e":
                new_name = input("  New name: ").strip()
                if new_name:
                    p = {**p, "name": new_name}
                approved.append({**p, "status": "approved"})
                print("  ✅ Approved with edited name")
                break
            else:
                print("  Please enter y, n, or e")

    print("="*60 + "\n")
    return approved


# ─────────────────────────────────────────────
#  GOOGLE SHEETS WRITERS
# ─────────────────────────────────────────────

def _vendor_tab_name(vendor_name: str, suffix: str) -> str:
    """
    Returns a tab name scoped to a vendor.
    Strips characters that Google Sheets rejects in tab names.
    Example: ("פט פארם בע״מ", "Invoice Lines") → "פט פארם בע״מ — Invoice Lines"
    """
    safe = vendor_name.strip()
    for ch in r'[]*?/\\':
        safe = safe.replace(ch, "")
    safe = safe[:60].strip()
    return f"{safe} — {suffix}"


def _write_invoice_lines(ws, products: list, invoice_meta: dict):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = []

    for p in products:
        qty = p.get("quantity", 0) or 1
        cost = p.get("cost", 0.0)
        unit_cost = round(cost / qty, 4) if qty else 0.0

        rows.append([
            invoice_meta.get("invoice_number", ""),
            invoice_meta.get("invoice_date", ""),
            invoice_meta.get("vendor_name", ""),
            p.get("barcode", ""),
            p.get("name", ""),
            qty,
            unit_cost,
            cost,
            p.get("status", "approved"),
            now,
        ])

    if rows:
        ws.append_rows(rows, value_input_option="USER_ENTERED")
        print(f"📝 [SHEETS] Wrote {len(rows)} invoice lines")


def _update_products_tab(ws, products: list, vendor_name: str):
    """
    Upserts the Products tab:
    - New barcode → append row
    - Existing barcode → update latest_unit_cost, total_qty, invoice_count, last_seen
    """
    today = datetime.now().strftime("%Y-%m-%d")

    # Load existing data into a dict keyed by barcode
    existing_rows = ws.get_all_records()
    # Map barcode → {row_index (1-based, after header), data}
    bc_to_row: dict = {}
    for i, row in enumerate(existing_rows, start=2):  # row 1 = header
        bc = str(row.get("barcode", "")).strip()
        if bc:
            bc_to_row[bc] = {"row_index": i, "data": row}

    updates = []   # (cell_range, value) for batch update
    appends = []   # new rows to append

    for p in products:
        bc = p.get("barcode", "").strip()
        if not bc:
            continue

        qty = p.get("quantity", 0) or 0
        cost = p.get("cost", 0.0)
        unit_cost = round(cost / qty, 4) if qty else 0.0
        name = p.get("name", "")

        if bc in bc_to_row:
            # Update existing row
            existing = bc_to_row[bc]["data"]
            ri = bc_to_row[bc]["row_index"]

            new_qty   = (existing.get("total_qty_ordered") or 0) + qty
            new_count = (existing.get("invoice_count") or 0) + 1

            # Columns: barcode(A) name(B) vendor(C) latest_unit_cost(D)
            #          total_qty(E) invoice_count(F) first_seen(G) last_seen(H)
            updates.append((f"B{ri}", name))            # refresh name in case it changed
            updates.append((f"D{ri}", unit_cost))       # latest unit cost
            updates.append((f"E{ri}", new_qty))         # cumulative qty
            updates.append((f"F{ri}", new_count))       # invoice count
            updates.append((f"H{ri}", today))           # last seen
        else:
            # New product
            appends.append([
                bc, name, vendor_name,
                unit_cost, qty, 1,
                today, today,
            ])
            bc_to_row[bc] = {"row_index": None, "data": {}}  # prevent double-insert

    # Batch write updates
    if updates:
        for cell, val in updates:
            ws.update_acell(cell, val)
        print(f"🔄 [SHEETS] Updated {len(updates)//5} existing products in catalog tab")

    # Append new products
    if appends:
        ws.append_rows(appends, value_input_option="USER_ENTERED")
        print(f"➕ [SHEETS] Added {len(appends)} new products to catalog tab")


INVOICES_SUMMARY_HEADERS = [
    "invoice_number",
    "invoice_date",
    "vendor_name",
    "total_products",
    "invoice_total",   # sum of all line totals
    "processed_at",
]


def _write_invoice_summary(client, sheet_id: str, approved: list, invoice_meta: dict):
    """
    Appends one row to the 'Invoices' summary tab — one row per invoice run.
    This tab is shared across all vendors (the full invoice index).
    """
    ws = _get_or_create_sheet(
        client, sheet_id,
        tab_name="Invoices",
        headers=INVOICES_SUMMARY_HEADERS,
    )

    invoice_total = round(sum(p.get("cost", 0.0) for p in approved), 2)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    ws.append_row([
        invoice_meta.get("invoice_number", ""),
        invoice_meta.get("invoice_date", ""),
        invoice_meta.get("vendor_name", ""),
        len(approved),
        invoice_total,
        now,
    ], value_input_option="USER_ENTERED")

    print(f"🧾 [SHEETS] Invoice summary written — total: ₪{invoice_total}")


# ─────────────────────────────────────────────
#  MAIN NODE  (LangGraph entry point)
# ─────────────────────────────────────────────

def store_invoice_data(state: dict) -> dict:
    """
    LangGraph node. Receives state from the extraction node and:
    1. Validates products (auto-approve catalog items, flag unknowns)
    2. Prompts user for manual review of unknown items
    3. Writes approved items to Google Sheets
    4. Returns updated state with storage_status

    Wire into graph.py:
        from storage import store_invoice_data
        graph.add_node("store", store_invoice_data)
        graph.add_edge("extract", "store")      # or wherever fits in your flow
    """
    products     = state.get("products", [])
    vendor_name  = state.get("vendor_name", "Unknown")
    warnings     = state.get("warnings", [])

    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    if not sheet_id:
        msg = "GOOGLE_SHEET_ID not set in .env — skipping storage."
        print(f"⚠️  [STORAGE] {msg}")
        return {**state, "storage_status": msg}

    if not products:
        msg = "No products to store."
        print(f"⚠️  [STORAGE] {msg}")
        return {**state, "storage_status": msg}

    # ── Extract invoice metadata from state (populate these in your extract node) ──
    invoice_meta = {
        "invoice_number": state.get("invoice_number", ""),
        "invoice_date":   state.get("invoice_date",   datetime.now().strftime("%Y-%m-%d")),
        "vendor_name":    vendor_name,
    }

    print(f"\n📊 [STORAGE] Preparing to store {len(products)} products for {vendor_name}")

    # Step 1 — Validate
    validation  = validate_products(products, vendor_name)
    approved    = validation["approved"]
    pending     = validation["pending_review"]

    # Step 2 — Manual review for unknowns
    if pending:
        manually_approved = prompt_manual_review(pending)
        approved.extend(manually_approved)
        skipped = len(pending) - len(manually_approved)
        if skipped:
            print(f"⏭  [STORAGE] {skipped} product(s) skipped by user")

        # Auto-learn: write newly approved products into PRODUCT_CATALOG
        if manually_approved:
            try:
                from nodes.extract import learn_from_approved
                learn_from_approved(manually_approved)
            except Exception as e:
                print(f"⚠️  [LEARN] Could not update catalog: {e}")

    if not approved:
        msg = "No products approved for storage."
        print(f"⚠️  [STORAGE] {msg}")
        return {**state, "storage_status": msg}

    # Step 3 — Write to Google Sheets (vendor-separated tabs)
    try:
        client = _get_sheet_client()

        lines_tab    = _vendor_tab_name(vendor_name, "Invoice Lines")
        products_tab = _vendor_tab_name(vendor_name, "Products")

        print(f"📂 [SHEETS] Writing to tabs: '{lines_tab}' / '{products_tab}'")

        ws_lines = _get_or_create_sheet(
            client, sheet_id,
            tab_name=lines_tab,
            headers=INVOICE_LINES_HEADERS,
        )
        ws_products = _get_or_create_sheet(
            client, sheet_id,
            tab_name=products_tab,
            headers=PRODUCTS_HEADERS,
        )

        _write_invoice_lines(ws_lines, approved, invoice_meta)
        _update_products_tab(ws_products, approved, vendor_name)
        _write_invoice_summary(client, sheet_id, approved, invoice_meta)

        msg = (f"Stored {len(approved)} products to Google Sheets "
               f"({len(pending) - (len(approved) - len(validation['approved']))} skipped)")
        print(f"✅ [STORAGE] {msg}")
        return {**state, "storage_status": msg}

    except Exception as e:
        msg = f"Google Sheets write failed: {e}"
        print(f"❌ [STORAGE] {msg}")
        return {**state, "storage_status": msg}