from image_agent import image_app

def run_test():
    print("🚀 [TEST] Starting Image Agent Test...")
    
    # Define a test product
    # You can change the name to any product from your invoice
    test_input = {
        "product_name": "פרו פלאן עוף ואורז לכלב בוגר 14 קג",
        "product_sku": "12345678",
        "brand_sites": ["purina.co.il", "petpoint.co.il"], # Optional: priority sites
        "image_url": None,
        "local_path": None,
        "processed_files": [],
        "status": "starting"
    }

    # Run the graph
    print(f"--- [LOG] Processing product: {test_input['product_name']} ---")
    
    final_state = image_app.invoke(test_input)

    # Check results
    print("\n" + "="*50)
    print("📊 [TEST RESULTS]")
    print(f"Status: {final_state['status']}")
    
    if final_state['processed_files']:
        print("✅ Success! Created files:")
        for file_path in final_state['processed_files']:
            print(f"   - {file_path}")
    else:
        if final_state['status'] == "request_manual_upload":
            print("❌ Image not found. Agent is waiting for manual photo.")
        else:
            print(f"❌ Test ended with status: {final_state['status']}")
    print("="*50)

if __name__ == "__main__":
    run_test()