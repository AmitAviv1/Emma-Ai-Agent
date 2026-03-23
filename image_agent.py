import os
import requests
from typing import TypedDict, List, Optional
from langgraph.graph import StateGraph, END
from tools.image_utils.search import search_product_image
from tools.image_utils.processor import process_product_images

class ImageState(TypedDict):
    product_name: str
    product_sku: str
    brand_sites: Optional[List[str]]
    image_url: Optional[str]
    local_path: Optional[str]
    processed_files: List[str]
    status: str

# --- NODES ---

def search_node(state: ImageState):
    img_url = search_product_image(state['product_name'], state.get('brand_sites'))
    
    if img_url:
        # Ask for confirmation in terminal
        print("\n" + "!"*30)
        user_choice = input(f"Review the opened image. Use it? (y/n): ").lower()
        print("!"*30 + "\n")
        
        if user_choice == 'y':
            return {"image_url": img_url, "status": "confirmed"}
        
    return {"status": "request_manual_upload"}

def manual_upload_node(state: ImageState):
    print("--- [NODE] Manual Upload ---")
    path = input("Please drag & drop the image file here or enter its full path: ").strip().replace("'", "").replace('"', '')
    if os.path.exists(path):
        return {"local_path": path, "status": "confirmed"}
    print("❌ File not found. Retrying...")
    return manual_upload_node(state)

def download_node(state: ImageState):
    if state.get('local_path'): return {} # Already have a local file
    
    print("--- [NODE] Downloading Image ---")
    local_path = f"temp_{state['product_sku']}.png"
    response = requests.get(state['image_url'], stream=True)
    if response.status_code == 200:
        with open(local_path, 'wb') as f:
            f.write(response.content)
        return {"local_path": local_path}
    return {"status": "error"}

def process_node(state: ImageState):
    print("--- [NODE] Processing Image ---")
    # Pass the product name from the state to the processor
    paths = process_product_images(
        input_image_path=state['local_path'], 
        product_name=state['product_name']
    )
    return {"processed_files": list(paths), "status": "completed"}

# --- BUILD GRAPH ---

workflow = StateGraph(ImageState)

workflow.add_node("search", search_node)
workflow.add_node("manual_upload", manual_upload_node)
workflow.add_node("download", download_node)
workflow.add_node("process", process_node)

workflow.set_entry_point("search")

# Conditional: If search fails or 'n' is pressed -> manual_upload
def check_confirmation(state: ImageState):
    return "download" if state['status'] == "confirmed" else "manual"

workflow.add_conditional_edges("search", check_confirmation, {
    "download": "download",
    "manual": "manual_upload"
})

workflow.add_edge("manual_upload", "process")
workflow.add_edge("download", "process")
workflow.add_edge("process", END)

image_app = workflow.compile()