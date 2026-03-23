import operator
from typing import Annotated, TypedDict, List
from langgraph.graph import StateGraph, END
from nodes.extract import extract_invoice_data
from nodes.inventory import check_inventory_status
from nodes.wolt_api import upload_to_wolt
from nodes.storage import store_invoice_data

# 1. Define the data structure that flows through the graph (State)
class AgentState(TypedDict):
    # List of products extracted from the invoice
    products: List[dict]
    # Process status (e.g. "extracted", "verified", "uploaded")
    status: str
    # Error messages if any occurred
    errors: List[str]
    file_path: str
    # Invoice metadata (populated by the extract node)
    vendor_name: str
    invoice_number: str
    invoice_date: str
    warnings: List[str]
    # Storage status from Google Sheets node
    storage_status: str

# 2. Create the graph
workflow = StateGraph(AgentState)

# 3. Add nodes — each node is a function defined in the nodes/ directory
workflow.add_node("extract_data",      extract_invoice_data)
workflow.add_node("store_data",        store_invoice_data)
workflow.add_node("process_inventory", check_inventory_status)
workflow.add_node("upload_wolt",       upload_to_wolt)

# 4. Define edges — the order of operations
workflow.set_entry_point("extract_data")  # starting point

# Extract → save to Google Sheets (with validation)
workflow.add_edge("extract_data",      "store_data")

# Save → check inventory
workflow.add_edge("store_data",        "process_inventory")

# Check inventory → upload to Wolt
workflow.add_edge("process_inventory", "upload_wolt")

# End the workflow
workflow.add_edge("upload_wolt", END)

# 5. Compile the graph so it can be executed
app = workflow.compile()

print("Graph defined successfully!")