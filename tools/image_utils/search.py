import os
import requests
import webbrowser
from dotenv import load_dotenv

load_dotenv()

# Default trusted sites
DEFAULT_SITES = [
    "biopet.co.il",
    "my-pet.co.il",
    "dudi-agencies.co.il",
    "pet-pharm.co.il",
    "milatin-group.co.il"
]

def search_product_image(product_name, brand_sites=None):
    print(f"🔍 [LOG] Searching image for: {product_name}")
    
    sites_to_search = brand_sites if brand_sites else DEFAULT_SITES
    # Building a better search query
    query = f"{product_name} packshot"
    if sites_to_search:
        query += f" (site:{' OR site:'.join(sites_to_search)})"

    url = "https://google.serper.dev/images"
    payload = {"q": query, "num": 5}
    headers = {
        'X-API-KEY': os.getenv("SERPER_API_KEY"),
        'Content-Type': 'application/json'
    }

    try:
        response = requests.post(url, headers=headers, json=payload)
        results = response.json()
        
        if "images" in results and len(results["images"]) > 0:
            img_url = results["images"][0]["imageUrl"]
            print(f"✅ [LOG] Found image candidate: {img_url}")
            
            # Open the image in browser for human review
            print("🌐 [LOG] Opening image in browser for your review...")
            webbrowser.open(img_url)
            
            return img_url
        
        return None
    except Exception as e:
        print(f"⚠️ [ERROR] Search failed: {e}")
        return None