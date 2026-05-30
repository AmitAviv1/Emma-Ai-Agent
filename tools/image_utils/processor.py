import os
import numpy as np
from PIL import Image, ImageOps, ImageFilter, ImageEnhance
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

        # Skip rembg if the image already has a transparent background
        # (re-processing a pre-cut image introduces blur)
        pre = Image.open(io.BytesIO(input_data)).convert("RGBA")
        arr_pre = np.array(pre)
        total_px = arr_pre.shape[0] * arr_pre.shape[1]
        already_transparent = np.sum(arr_pre[:, :, 3] == 0) / total_px > 0.10

        if already_transparent:
            print("🔧 [LOG] Image already has transparent background — skipping rembg...")
            arr = arr_pre
        else:
            print("🔧 [LOG] Removing background...")
            subject = remove(input_data)
            arr = np.array(Image.open(io.BytesIO(subject)).convert("RGBA"))

        # Zero out near-transparent stray pixels so getbbox() stays tight
        arr[arr[:, :, 3] < 20, 3] = 0
        img = Image.fromarray(arr)

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
    target_w, target_h = target_size
    subject_w, subject_h = subject_img.size

    padding_pct = 0.12
    safe_w = int(target_w * (1 - padding_pct * 2))
    safe_h = int(target_h * (1 - padding_pct * 2))

    ratio = min(safe_w / subject_w, safe_h / subject_h)
    new_w = int(subject_w * ratio)
    new_h = int(subject_h * ratio)
    resized = subject_img.resize((new_w, new_h), Image.Resampling.LANCZOS)

    # Sharpness boost — UnsharpMask gives crisper edges than simple Sharpness
    r, g, b, a = resized.split()
    rgb = Image.merge("RGB", (r, g, b))
    rgb = rgb.filter(ImageFilter.UnsharpMask(radius=1.2, percent=140, threshold=2))
    r, g, b = rgb.split()
    resized = Image.merge("RGBA", (r, g, b, a))

    paste_x = (target_w - new_w) // 2
    paste_y = (target_h - new_h) // 2

    # Drop shadow: offset, same shape as product alpha, then blur
    shadow_ox = int(target_w * 0.012)
    shadow_oy = int(target_h * 0.018)
    blur_r    = int(min(target_w, target_h) * 0.022)

    alpha = resized.split()[3]
    shadow_layer = Image.new("RGBA", target_size, (0, 0, 0, 0))
    shadow_fill  = Image.new("RGBA", (new_w, new_h), (20, 20, 20, 150))
    shadow_layer.paste(shadow_fill, (paste_x + shadow_ox, paste_y + shadow_oy), alpha)
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(blur_r))

    # Composite: white → shadow → product
    canvas = Image.new("RGBA", target_size, (255, 255, 255, 255))
    canvas = Image.alpha_composite(canvas, shadow_layer)
    canvas.paste(resized, (paste_x, paste_y), resized)

    # Flatten to RGB (white background, no transparency)
    result = Image.new("RGB", target_size, (255, 255, 255))
    result.paste(canvas, mask=canvas.split()[3])
    return result