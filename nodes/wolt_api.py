def upload_to_wolt(state: dict):
    """
    Temporary node for uploading to Wolt.
    Currently just prints a confirmation that we reached this stage.
    """
    print("--- [NODE] Upload to Wolt (Simulation) ---")
    
    products = state.get("products", [])
    
    # In the future, the API or Playwright logic will go here
    if products:
        print(f"✅ [LOG] Agent is ready to upload {len(products)} products.")
    else:
        print("⚠️ [LOG] No products found to upload.")
    
    return {
        "status": "completed"
    }