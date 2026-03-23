"""
app.py — Streamlit UI for the Invoice Automation Agent

Run:
    streamlit run app.py

Install:
    pip install streamlit
"""

import os
import sys
import tempfile
import streamlit as st
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ── add nodes/ to path so imports work from project root ──────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "nodes"))

# ─────────────────────────────────────────────
#  PAGE CONFIG
# ─────────────────────────────────────────────

st.set_page_config(
    page_title="Invoice Agent",
    page_icon="🧾",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
    [data-testid="stSidebar"] { min-width: 220px; max-width: 220px; }
    .block-container { padding-top: 2rem; }
    div[data-testid="metric-container"] { background: #f8f8f8; border-radius: 8px; padding: 1rem; }
    .product-row { padding: 10px 0; border-bottom: 0.5px solid #eee; }
    .badge-approved { background:#eaf3de; color:#27500a; padding:2px 10px;
                      border-radius:12px; font-size:12px; font-weight:500; }
    .badge-pending  { background:#faeeda; color:#633806; padding:2px 10px;
                      border-radius:12px; font-size:12px; font-weight:500; }
    .badge-skipped  { background:#f1efe8; color:#5f5e5a; padding:2px 10px;
                      border-radius:12px; font-size:12px; font-weight:500; }
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────
#  SESSION STATE DEFAULTS
# ─────────────────────────────────────────────

for key, default in {
    "page":            "upload",
    "products":        [],
    "vendor_name":     "",
    "invoice_number":  "",
    "invoice_date":    "",
    "approved":        [],
    "pending":         [],
    "decisions":       {},   # barcode → "approve" | "skip", name edits
    "saved":           False,
    "save_result":     "",
    "log_lines":       [],
}.items():
    if key not in st.session_state:
        st.session_state[key] = default


# ─────────────────────────────────────────────
#  SIDEBAR NAVIGATION
# ─────────────────────────────────────────────

def nav_button(label, page, badge=None):
    is_active = st.session_state.page == page
    cols = st.sidebar.columns([4, 1]) if badge else [st.sidebar]
    with cols[0]:
        if st.button(
            label,
            key=f"nav_{page}",
            use_container_width=True,
            type="primary" if is_active else "secondary",
        ):
            st.session_state.page = page
            st.rerun()
    if badge:
        with cols[1]:
            st.markdown(
                f'<div style="background:#e24b4a;color:#fff;border-radius:10px;'
                f'text-align:center;font-size:11px;font-weight:600;padding:2px 6px;'
                f'margin-top:6px">{badge}</div>',
                unsafe_allow_html=True,
            )

with st.sidebar:
    st.markdown("### 🧾 Invoice Agent")
    st.markdown("---")
    nav_button("📤  Upload", "upload")
    pending_count = len([
        b for b, d in st.session_state.decisions.items()
        if d.get("decision") == "pending"
    ]) or len(st.session_state.pending)
    nav_button(
        "🔍  Review",
        "review",
        badge=pending_count if pending_count > 0 and not st.session_state.saved else None,
    )
    nav_button("📋  Results", "results")
    nav_button("📚  Catalog", "catalog")
    st.markdown("---")
    st.caption("Logs")
    if st.session_state.log_lines:
        st.code("\n".join(st.session_state.log_lines[-20:]), language=None)


# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────

def add_log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    st.session_state.log_lines.append(f"{ts}  {msg}")


def run_extraction(pdf_path: str):
    """Runs the extraction pipeline and populates session state."""
    from extract import extract_invoice_data
    from storage import validate_products

    add_log(f"Starting extraction: {os.path.basename(pdf_path)}")

    with st.spinner("Extracting invoice data…"):
        result = extract_invoice_data({"file_path": pdf_path})

    products    = result.get("products", [])
    vendor_name = result.get("vendor_name", "Unknown")
    warnings    = result.get("warnings", [])

    add_log(f"Extracted {len(products)} products from {vendor_name}")
    for w in warnings:
        add_log(f"⚠ {w}")

    validation = validate_products(products, vendor_name)

    st.session_state.products       = products
    st.session_state.vendor_name    = vendor_name
    st.session_state.invoice_number = result.get("invoice_number", "")
    st.session_state.invoice_date   = result.get("invoice_date", "")
    st.session_state.approved       = validation["approved"]
    st.session_state.pending        = validation["pending_review"]
    st.session_state.saved          = False
    st.session_state.save_result    = ""

    # Initialise decisions: catalog items auto-approved, unknowns pending
    decisions = {}
    for p in validation["approved"]:
        decisions[p["barcode"]] = {"decision": "approve", "name": p["name"]}
    for p in validation["pending_review"]:
        decisions[p["barcode"]] = {"decision": "pending", "name": p["name"]}
    st.session_state.decisions = decisions

    add_log(f"Auto-approved {len(validation['approved'])}, pending review: {len(validation['pending_review'])}")


def save_to_sheets():
    """Writes approved products to Google Sheets."""
    from storage import (
        _get_sheet_client, _get_or_create_sheet,
        _write_invoice_lines, _update_products_tab,
        _write_invoice_summary, _vendor_tab_name,
        INVOICE_LINES_HEADERS, PRODUCTS_HEADERS,
    )
    from extract import learn_from_approved

    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    if not sheet_id:
        return "❌ GOOGLE_SHEET_ID not set in .env"

    decisions   = st.session_state.decisions
    vendor_name = st.session_state.vendor_name
    all_products = st.session_state.products

    # Build final approved list respecting UI decisions + name edits
    approved      = []
    newly_learned = []

    for p in all_products:
        bc  = p.get("barcode", "")
        dec = decisions.get(bc, {})
        if dec.get("decision") == "approve":
            edited_name = dec.get("name", p["name"])
            approved.append({**p, "name": edited_name, "status": "approved"})
            # Track products that were unknown (pending) and manually approved
            if p in st.session_state.pending:
                newly_learned.append({**p, "name": edited_name})

    if not approved:
        return "⚠️ No products approved — nothing to save."

    invoice_meta = {
        "invoice_number": st.session_state.invoice_number,
        "invoice_date":   st.session_state.invoice_date or datetime.now().strftime("%Y-%m-%d"),
        "vendor_name":    vendor_name,
    }

    try:
        client       = _get_sheet_client()
        lines_tab    = _vendor_tab_name(vendor_name, "Invoice Lines")
        products_tab = _vendor_tab_name(vendor_name, "Products")

        ws_lines    = _get_or_create_sheet(client, sheet_id, lines_tab,    INVOICE_LINES_HEADERS)
        ws_products = _get_or_create_sheet(client, sheet_id, products_tab, PRODUCTS_HEADERS)

        _write_invoice_lines(ws_lines, approved, invoice_meta)
        _update_products_tab(ws_products, approved, vendor_name)
        _write_invoice_summary(client, sheet_id, approved, invoice_meta)

        # Auto-learn newly approved unknown products
        if newly_learned:
            learned = learn_from_approved(newly_learned)
            add_log(f"🧠 Learned {learned} new product(s) into catalog")

        add_log(f"✅ Saved {len(approved)} products to Google Sheets")
        return f"✅ Saved {len(approved)} products to Google Sheets."

    except Exception as e:
        add_log(f"❌ Save failed: {e}")
        return f"❌ Save failed: {e}"


# ─────────────────────────────────────────────
#  PAGE: UPLOAD
# ─────────────────────────────────────────────

def page_upload():
    st.title("Upload invoice")

    uploaded = st.file_uploader(
        "Drop a PDF invoice or image here",
        type=["pdf", "jpg", "jpeg", "png"],
        label_visibility="collapsed",
    )

    if uploaded:
        ext = uploaded.name.rsplit(".", 1)[-1].lower()
        suffix = f".{ext}"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(uploaded.read())
            tmp_path = tmp.name

        col1, col2 = st.columns([3, 1])
        with col1:
            st.info(f"**{uploaded.name}** — {uploaded.size // 1024} KB")
            if ext in ("jpg", "jpeg", "png"):
                st.image(uploaded, width=400)
        with col2:
            if st.button("Process invoice", type="primary", use_container_width=True):
                run_extraction(tmp_path)
                os.unlink(tmp_path)
                if st.session_state.pending:
                    st.session_state.page = "review"
                else:
                    st.session_state.page = "results"
                st.rerun()

    # Show last extraction summary if available
    if st.session_state.products:
        st.markdown("---")
        st.subheader("Last extraction")
        c1, c2, c3, c4 = st.columns(4)
        total_cost = sum(p.get("cost", 0) for p in st.session_state.approved)
        c1.metric("Supplier",  st.session_state.vendor_name)
        c2.metric("Products",  len(st.session_state.products))
        c3.metric("Pending review", len(st.session_state.pending))
        c4.metric("Total (net)", f"₪{total_cost:,.2f}")


# ─────────────────────────────────────────────
#  PAGE: REVIEW
# ─────────────────────────────────────────────

def page_review():
    st.title("Review unknown products")

    pending = st.session_state.pending
    if not pending:
        st.success("No unknown products — all items were auto-approved from the catalog.")
        if st.button("Go to results →"):
            st.session_state.page = "results"
            st.rerun()
        return

    if st.session_state.saved:
        st.success(st.session_state.save_result)
        if st.button("Process another invoice"):
            st.session_state.page = "upload"
            st.rerun()
        return

    st.caption(
        f"{len(pending)} product(s) not found in catalog. "
        "Approve to save, skip to discard, or edit the name before approving."
    )
    st.markdown("---")

    decisions = st.session_state.decisions

    for p in pending:
        bc  = p["barcode"]
        dec = decisions.get(bc, {"decision": "pending", "name": p["name"]})

        with st.container():
            col_info, col_name, col_action = st.columns([2, 3, 2])

            with col_info:
                st.markdown(f"**Barcode**")
                st.code(bc, language=None)
                st.caption(f"Qty: {p['quantity']}  |  Cost: ₪{p['cost']:.2f}")

            with col_name:
                st.markdown("**Product name**")
                edited = st.text_input(
                    "name",
                    value=dec.get("name", p["name"]),
                    key=f"name_{bc}",
                    label_visibility="collapsed",
                )
                decisions[bc] = {**dec, "name": edited}

            with col_action:
                st.markdown("**Decision**")
                current = dec.get("decision", "pending")
                choice = st.radio(
                    "decision",
                    ["approve", "skip"],
                    index=0 if current == "approve" else 1,
                    key=f"dec_{bc}",
                    horizontal=True,
                    label_visibility="collapsed",
                )
                decisions[bc] = {**decisions[bc], "decision": choice}

        st.markdown('<hr style="margin:4px 0;opacity:0.2">', unsafe_allow_html=True)

    st.session_state.decisions = decisions
    st.markdown("---")

    approved_count = sum(1 for d in decisions.values() if d.get("decision") == "approve")
    skipped_count  = sum(1 for d in decisions.values() if d.get("decision") == "skip")
    pending_count  = sum(1 for d in decisions.values() if d.get("decision") == "pending")

    st.caption(f"Approved: {approved_count + len(st.session_state.approved)}  |  "
               f"Skipped: {skipped_count}  |  Still pending: {pending_count}")

    col_save, col_skip = st.columns([2, 1])
    with col_save:
        if st.button("Save all to Google Sheets", type="primary", use_container_width=True,
                     disabled=pending_count > 0):
            with st.spinner("Saving…"):
                result = save_to_sheets()
            st.session_state.save_result = result
            st.session_state.saved = True
            st.rerun()
    with col_skip:
        if st.button("Save only auto-approved", use_container_width=True):
            # Mark all pending as skip before saving
            for bc, d in decisions.items():
                if d.get("decision") == "pending":
                    decisions[bc] = {**d, "decision": "skip"}
            st.session_state.decisions = decisions
            with st.spinner("Saving…"):
                result = save_to_sheets()
            st.session_state.save_result = result
            st.session_state.saved = True
            st.rerun()

    if pending_count > 0:
        st.caption("⬆ Resolve all pending items before saving, or use 'Save only auto-approved'.")


# ─────────────────────────────────────────────
#  PAGE: RESULTS
# ─────────────────────────────────────────────

def page_results():
    st.title("Extraction results")

    products = st.session_state.products
    if not products:
        st.info("No results yet — upload an invoice first.")
        return

    decisions   = st.session_state.decisions
    vendor_name = st.session_state.vendor_name
    total_cost  = sum(p.get("cost", 0) for p in products)

    # Summary metrics
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Supplier",     vendor_name)
    c2.metric("Products",     len(products))
    c3.metric("Invoice total", f"₪{total_cost:,.2f}")
    c4.metric("Saved",        "Yes ✓" if st.session_state.saved else "Not yet")

    if st.session_state.save_result:
        if "✅" in st.session_state.save_result:
            st.success(st.session_state.save_result)
        else:
            st.error(st.session_state.save_result)

    st.markdown("---")

    # Filter bar
    col_search, col_filter = st.columns([3, 1])
    with col_search:
        search = st.text_input("Search", placeholder="Product name or barcode…",
                               label_visibility="collapsed")
    with col_filter:
        show = st.selectbox("Show", ["All", "Approved", "Pending", "Skipped"],
                            label_visibility="collapsed")

    # Table header
    hcols = st.columns([3, 2, 1, 1, 1])
    for col, label in zip(hcols, ["Product", "Barcode", "Qty", "Cost", "Status"]):
        col.markdown(f"**{label}**")
    st.markdown('<hr style="margin:4px 0">', unsafe_allow_html=True)

    shown = 0
    for p in products:
        bc     = p.get("barcode", "")
        name   = p.get("name", "")
        dec    = decisions.get(bc, {}).get("decision", "approve")
        status = dec

        if search and search.lower() not in name.lower() and search not in bc:
            continue
        if show == "Approved" and status != "approve":
            continue
        if show == "Pending"  and status != "pending":
            continue
        if show == "Skipped"  and status != "skip":
            continue

        badge_html = {
            "approve": '<span class="badge-approved">Approved</span>',
            "pending": '<span class="badge-pending">Pending</span>',
            "skip":    '<span class="badge-skipped">Skipped</span>',
        }.get(status, "")

        row = st.columns([3, 2, 1, 1, 1])
        row[0].write(name[:60] + ("…" if len(name) > 60 else ""))
        row[1].code(bc, language=None)
        row[2].write(p.get("quantity", ""))
        row[3].write(f"₪{p.get('cost', 0):.2f}")
        row[4].markdown(badge_html, unsafe_allow_html=True)
        shown += 1

    if shown == 0:
        st.caption("No products match the current filter.")

    st.markdown("---")
    if not st.session_state.saved:
        if st.button("Save to Google Sheets →", type="primary"):
            if st.session_state.pending:
                st.session_state.page = "review"
            else:
                with st.spinner("Saving…"):
                    result = save_to_sheets()
                st.session_state.save_result = result
                st.session_state.saved = True
            st.rerun()


# ─────────────────────────────────────────────
#  PAGE: CATALOG
# ─────────────────────────────────────────────

def page_catalog():
    st.title("Product catalog")
    st.caption("All known products locked into the agent. New approvals are added here automatically.")

    try:
        from extract import PRODUCT_CATALOG
    except ImportError:
        st.error("Could not load PRODUCT_CATALOG from extract.py")
        return

    search = st.text_input("Search catalog", placeholder="Name or barcode…",
                           label_visibility="collapsed")

    st.markdown(f"**{len(PRODUCT_CATALOG)} products** in catalog")
    st.markdown("---")

    hcols = st.columns([2, 5])
    hcols[0].markdown("**Barcode**")
    hcols[1].markdown("**Product name**")
    st.markdown('<hr style="margin:4px 0">', unsafe_allow_html=True)

    shown = 0
    for bc, name in PRODUCT_CATALOG.items():
        if search and search.lower() not in name.lower() and search not in bc:
            continue
        row = st.columns([2, 5])
        row[0].code(bc, language=None)
        row[1].write(name)
        shown += 1

    if shown == 0:
        st.caption("No products match your search.")

    st.markdown("---")
    st.info(
        "To manually add a product, open `nodes/extract.py` and add an entry to `PRODUCT_CATALOG`. "
        "Products approved through the Review page are added automatically."
    )


# ─────────────────────────────────────────────
#  ROUTER
# ─────────────────────────────────────────────

pages = {
    "upload":  page_upload,
    "review":  page_review,
    "results": page_results,
    "catalog": page_catalog,
}

pages[st.session_state.page]()