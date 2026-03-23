import os
from PIL import Image, ImageOps
from rembg import remove
import io
import re

def sanitize_filename(filename):
    """
    Cleans the product name to be a valid filename.
    Removes special characters and replaces spaces with underscores.
    """
    # Keep only Hebrew letters, English letters, numbers and spaces
    clean_name = re.sub(r'[^\u0590-\u05fe\w\s-]', '', filename)
    # Replace spaces with underscores and trim
    return clean_name.strip().replace(" ", "_")

def process_product_images(input_image_path, product_name, output_directory="processed_images"):
    """
    Background removal, centering, and renaming based on product name and size.
    """
    # Clean the product name for the filename
    safe_name = sanitize_filename(product_name)
    print(f"--- [LOG] Processing image for: {product_name} ---")
    
    if not os.path.exists(output_directory):
        os.makedirs(output_directory)
    
    with open(input_image_path, "rb") as input_file:
        input_data = input_file.read()

        print("🔧 [LOG] Removing background...")
        subject = remove(input_data)
        img = Image.open(io.BytesIO(subject)).convert("RGBA")
        
        bbox = img.getbbox()
        if not bbox:
            print("❌ [ERROR] No product detected in image.")
            return None
        
        cropped_subject = img.crop(bbox)
        
        # --- 1000x1000 Format ---
        print("📐 [LOG] Creating 1000x1000 format...")
        square_output = create_formatted_image(cropped_subject, (1000, 1000))
        # New Naming Convention: Name_Size.png
        square_path = os.path.join(output_directory, f"{safe_name}_1000x1000.png")
        square_output.save(square_path, "PNG")
        
        # --- 1000x563 Format ---
        print("📐 [LOG] Creating 1000x563 format...")
        rect_output = create_formatted_image(cropped_subject, (1000, 563))
        rect_path = os.path.join(output_directory, f"{safe_name}_1000x563.png")
        rect_output.save(rect_path, "PNG")
        
        print(f"✅ [LOG] Files saved: \n   1. {square_path}\n   2. {rect_path}")
        return square_path, rect_path

def create_formatted_image(subject_img, target_size):
    """
    פונקציית עזר לשינוי גודל, מירכוז והוספת פאדינג (Padding).
    """
    # יצירת רקע שקוף חדש בגודל המטרה
    canvas = Image.new("RGBA", target_size, (0, 0, 0, 0))
    
    # חישוב הפרופורציות לשינוי גודל
    target_w, target_h = target_size
    subject_w, subject_h = subject_img.size
    
    # השארת מרווח ביטחון (Padding) של 10% מסביב למוצר כדי שייראה טוב
    padding_pct = 0.10
    safe_w = int(target_w * (1 - padding_pct * 2))
    safe_h = int(target_h * (1 - padding_pct * 2))
    
    # שינוי גודל המוצר בצורה פרופורציונלית (Thumbnail)
    ratio = min(safe_w / subject_w, safe_h / subject_h)
    new_w = int(subject_w * ratio)
    new_h = int(subject_h * ratio)
    resized_subject = subject_img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    
    # חישוב המיקום להדבקת המוצר במרכז ה-Canvas
    paste_x = (target_w - new_w) // 2
    paste_y = (target_h - new_h) // 2
    
    # הדבקת המוצר הממורכז על הרקע השקוף
    canvas.paste(resized_subject, (paste_x, paste_y), resized_subject)
    
    return canvas