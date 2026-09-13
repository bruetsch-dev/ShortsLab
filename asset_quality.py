import os
from pathlib import Path
from PIL import Image

try:
    import cv2
    import numpy as np
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False

try:
    import imagehash
    HAS_IMAGEHASH = True
except ImportError:
    HAS_IMAGEHASH = False

def calculate_phash(img):
    if HAS_IMAGEHASH:
        return str(imagehash.phash(img))
    
    # Fallback to simple dhash (difference hash)
    img = img.convert('L').resize((9, 8), Image.Resampling.LANCZOS)
    pixels = list(img.getdata())
    difference = []
    for row in range(8):
        for col in range(8):
            pixel_left = img.getpixel((col, row))
            pixel_right = img.getpixel((col + 1, row))
            difference.append(pixel_left > pixel_right)
            
    decimal_value = 0
    hex_string = []
    for index, value in enumerate(difference):
        if value:
            decimal_value += 2**(index % 8)
        if (index % 8) == 7:
            hex_string.append(hex(decimal_value)[2:].rjust(2, '0'))
            decimal_value = 0
            
    return ''.join(hex_string)

def calculate_blur_score(path):
    if not HAS_CV2:
        return 100.0 # Fallback assuming sharp if no cv2

    try:
        # Load in grayscale
        image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if image is None:
            return 0.0
        # Compute Laplacian variance
        return cv2.Laplacian(image, cv2.CV_64F).var()
    except Exception:
        return 100.0

def assess_text_risk(path):
    if not HAS_CV2:
        return "unknown"
        
    try:
        image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if image is None:
            return "unknown"
        
        edges = cv2.Canny(image, 100, 200)
        edge_density = np.sum(edges > 0) / (image.shape[0] * image.shape[1])
        
        # Simple heuristic: lots of high-frequency edges often mean text or noisy background
        if edge_density > 0.08:
            return "high"
        elif edge_density > 0.04:
            return "medium"
        return "low"
    except Exception:
        return "unknown"

def validate_and_analyze_image(path, existing_hashes=None):
    """
    Runs local heuristics on an image.
    Returns dict with pass/fail and metrics.
    """
    path = Path(path)
    result = {
        "local_quality_pass": False,
        "local_reject_reason": "",
        "width": 0,
        "height": 0,
        "megapixels": 0,
        "blur_score": 0.0,
        "phash": "",
        "crop_feasibility": "unknown",
        "text_risk": "unknown"
    }
    
    if not path.exists() or path.stat().st_size < 5000:
        result["local_reject_reason"] = "file_too_small_or_missing"
        return result

    try:
        with Image.open(path) as img:
            img.verify()
    except Exception:
        result["local_reject_reason"] = "corrupt_or_invalid_image"
        return result

    try:
        with Image.open(path) as img:
            width, height = img.size
            result["width"] = width
            result["height"] = height
            result["megapixels"] = round((width * height) / 1000000.0, 2)
            
            if width < 300 or height < 300:
                result["local_reject_reason"] = "resolution_too_low"
                return result
                
            if result["megapixels"] < 0.1:
                result["local_reject_reason"] = "megapixels_too_low"
                return result
            
            # Crop feasibility for 9:16 Shorts
            aspect_ratio = width / float(height)
            if aspect_ratio > 2.5:
                result["crop_feasibility"] = "bad"
                result["local_reject_reason"] = "too_wide_panorama"
                return result
            elif aspect_ratio > 1.8:
                result["crop_feasibility"] = "risky"
            elif aspect_ratio < 0.4:
                result["crop_feasibility"] = "bad"
                result["local_reject_reason"] = "too_tall_strip"
                return result
            else:
                result["crop_feasibility"] = "good"
            
            phash = calculate_phash(img)
            result["phash"] = phash
            
            if existing_hashes and phash in existing_hashes:
                result["local_reject_reason"] = "duplicate_image"
                return result
                
    except Exception as e:
        result["local_reject_reason"] = f"read_error_{e}"
        return result

    blur_score = calculate_blur_score(path)
    result["blur_score"] = round(float(blur_score), 2)
    
    if blur_score < 15.0:
        result["local_reject_reason"] = "too_blurry"
        return result

    text_risk = assess_text_risk(path)
    result["text_risk"] = text_risk

    result["local_quality_pass"] = True
    return result
