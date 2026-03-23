from graph import app
import os

def run_automation():
    # Path to your invoice
    image_path = "/Users/amitaviv/Desktop/Projects/AI AGENT/invoices/Pet Pharm invoice 1.pdf"
    
    print(f"--- [LOG] Running automation for: {image_path} ---")
    
    if not os.path.exists(image_path):
        print(f"❌ [ERROR] File not found at: {image_path}")
        return

    # Invoke the Graph (Agent)
    final_output = app.invoke({
        "products": [],
        "status": "starting",
        "errors": [],
        "file_path": image_path
    })

    # --- Final Results Output ---
    print("\n" + "="*70)
    print(f"{'Product':<25} | {'SKU':<15} | {'Qty':<6} | {'Net Cost':<10}")
    print("-" * 70)

    products = final_output.get('products', [])
    vendor_status = final_output.get('status', 'Unknown')
    
    print(f"\n✅ {vendor_status}")
    print("-" * 50)
    
    if products:
        for i, p in enumerate(products, 1):
            print(f"{i}. {p['name']}")
            # Added Barcode to the log
            print(f"   SKU: {p['sku']} | Barcode: {p.get('barcode', 'N/A')} | Qty: {p['quantity']} | Cost: {p['cost']} NIS")
            print("-" * 30)
    else:
        print("❌ [LOG] No products were extracted.")

    print("="*70)
    print(f"Final Status: {final_output.get('status', 'unknown')}")

if __name__ == "__main__":
    run_automation()