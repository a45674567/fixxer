"""
scoring.py — Image Quality Assessment (IQA) engine.

Phase 1 MVP: three deterministic scorers + OpenCV face/eye detection.
All operate on the JPEG preview — never the RAW file.

Scorers:
  1. Sharpness   — Laplacian variance on luminance channel
  2. Exposure    — Histogram analysis (mean luminance, clipping)
  3. Face/Eyes   — OpenCV Haar cascades (face + eye presence/absence)
  4. Composite   — Weighted combination, genre-adjusted

MediaPipe FaceMesh (Phase 2) gives EAR-based blink precision.
OpenCV cascades (Phase 1) give reliable binary eye-open/closed detection
with zero model downloads — cascades ship inside the opencv-python package.
"""

import numpy as np
import cv2
import logging
import math
from pathlib import Path
from typing import List, Optional, Tuple
from PIL import Image

log = logging.getLogger("fixxer.scoring")

# ── Laplacian kernel ──────────────────────────────────────────────────────────
LAPLACIAN_KERNEL = np.array([
    [0,  1, 0],
    [1, -4, 1],
    [0,  1, 0],
], dtype=np.float32)

# Normalisation reference: Laplacian variance of a reference-sharp image at 512px
SHARPNESS_REF = 800.0

# Exposure thresholds
CLIP_LO  = 5    # pixel value below which we count as shadow-clipped
CLIP_HI  = 250  # pixel value above which we count as highlight-clipped
CLIP_MAX_FRAC = 0.005  # 0.5% clipping = starting to matter

# Inference size (longest edge)
SCORE_SIZE = 512

# ── Genre weight tables ───────────────────────────────────────────────────────
GENRE_WEIGHTS = {
    "wedding": {
        "sharpness": 0.35, "exposure": 0.25,
        "blink_penalty": 0.30, "expression": 0.10,
    },
    "portrait": {
        "sharpness": 0.40, "exposure": 0.25,
        "blink_penalty": 0.35, "expression": 0.00,
    },
    "event": {
        "sharpness": 0.30, "exposure": 0.30,
        "blink_penalty": 0.25, "expression": 0.15,
    },
    "sport": {
        "sharpness": 0.50, "exposure": 0.25,
        "blink_penalty": 0.15, "expression": 0.10,
    },
    "landscape": {
        "sharpness": 0.50, "exposure": 0.40,
        "blink_penalty": 0.00, "expression": 0.00,
    },
    "documentary": {
        "sharpness": 0.25, "exposure": 0.25,
        "blink_penalty": 0.30, "expression": 0.20,
    },
    "general": {
        "sharpness": 0.35, "exposure": 0.30,
        "blink_penalty": 0.25, "expression": 0.10,
    },
}

# ── OpenCV cascade setup (lazy init) ─────────────────────────────────────────
_face_cascade = None
_eye_cascade  = None
_smile_cascade = None

def _get_cascades():
    global _face_cascade, _eye_cascade, _smile_cascade
    if _face_cascade is None:
        _face_cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + 'haarcascade_frontalface_default.xml'
        )
        _eye_cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + 'haarcascade_eye.xml'
        )
        _smile_cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + 'haarcascade_smile.xml'
        )
    return _face_cascade, _eye_cascade, _smile_cascade


# ── Scorer 1: Sharpness ───────────────────────────────────────────────────────

def score_sharpness(img_grey: np.ndarray) -> Tuple[float, float]:
    """
    Laplacian variance sharpness score.
    img_grey: H×W uint8 array.
    Returns (normalised_score 0–1, raw_variance).
    """
    from scipy.ndimage import convolve
    lap = convolve(img_grey.astype(np.float32), LAPLACIAN_KERNEL)
    variance = float(np.var(lap))
    normalised = min(1.0, math.sqrt(variance / SHARPNESS_REF))
    return round(normalised, 4), variance


# ── Scorer 2: Exposure ────────────────────────────────────────────────────────

def score_exposure(img_grey: np.ndarray) -> Tuple[float, float, float, float]:
    """
    Histogram-based exposure score.
    Returns (score 0–1, mean_luminance, highlight_clip, shadow_clip).
    """
    total = img_grey.size
    hist, _ = np.histogram(img_grey, bins=256, range=(0, 256))

    mean_lum      = float(np.mean(img_grey))
    shadow_clip   = float(np.sum(hist[:CLIP_LO])) / total
    highlight_clip = float(np.sum(hist[CLIP_HI:])) / total

    # Luminance score: ideal range 90–210; decays at extremes
    if 90 <= mean_lum <= 210:
        lum_score = 1.0 - abs(mean_lum - 150) / 150
    elif mean_lum < 90:
        lum_score = (mean_lum / 90) * 0.6
    else:
        lum_score = max(0.0, 1.0 - (mean_lum - 210) / 45 * 0.6)

    lum_score = max(0.0, min(1.0, lum_score))
    clip_penalty = min(1.0, (highlight_clip + shadow_clip) / (CLIP_MAX_FRAC * 10)) * 0.4
    exposure_score = max(0.0, lum_score - clip_penalty)
    return round(exposure_score, 4), mean_lum, highlight_clip, shadow_clip


# ── Scorer 3: Face + Eye state ────────────────────────────────────────────────

def score_faces(img_grey: np.ndarray, img_rgb: np.ndarray) -> dict:
    """
    OpenCV Haar cascade face + eye detection.

    Strategy:
    - Detect frontal faces
    - Within each face ROI, detect eyes
    - If a face has 0 eyes detected → likely blink or heavily closed eyes
    - Smile detection for basic expression score

    Returns dict: face_count, blink_flag, min_ear (None; EAR in Phase 2),
                  expression (0–1 smile proxy).
    """
    result = {
        "face_count": 0,
        "blink_flag": 0,
        "min_ear": None,
        "expression": 0.0,
    }

    face_casc, eye_casc, smile_casc = _get_cascades()

    # Work on a contrast-enhanced grey image for better detection
    eq = cv2.equalizeHist(img_grey)
    h, w = eq.shape

    faces = face_casc.detectMultiScale(
        eq,
        scaleFactor=1.1,
        minNeighbors=5,
        minSize=(max(20, w // 20), max(20, h // 20)),
    )

    if len(faces) == 0:
        return result

    result["face_count"] = len(faces)
    any_blink = False
    smile_scores = []

    for (fx, fy, fw, fh) in faces:
        # Eye detection in upper 60% of face ROI (avoids mouth region)
        eye_roi_h = int(fh * 0.6)
        eye_roi = eq[fy:fy + eye_roi_h, fx:fx + fw]

        eyes = eye_casc.detectMultiScale(
            eye_roi,
            scaleFactor=1.1,
            minNeighbors=3,
            minSize=(max(10, fw // 10), max(10, fh // 10)),
        )

        # If fewer than 2 eyes detected in the face ROI, flag as blink
        # Threshold: 0 eyes = definite blink; 1 eye = likely blink/partial
        if len(eyes) < 2:
            any_blink = True

        # Smile detection in lower 50% of face
        smile_roi = eq[fy + fh // 2: fy + fh, fx:fx + fw]
        smiles = smile_casc.detectMultiScale(
            smile_roi,
            scaleFactor=1.7,
            minNeighbors=20,
            minSize=(max(15, fw // 5), max(10, fh // 10)),
        )
        smile_scores.append(1.0 if len(smiles) > 0 else 0.0)

    result["blink_flag"] = 1 if any_blink else 0
    result["expression"] = float(np.mean(smile_scores)) if smile_scores else 0.0
    return result


# ── Composite scorer ──────────────────────────────────────────────────────────

def compute_composite(sharpness: float, exposure: float,
                       blink_flag: int, expression: float,
                       genre: str = "general") -> float:
    """Weighted composite quality score [0–1] with genre calibration."""
    w = GENRE_WEIGHTS.get(genre, GENRE_WEIGHTS["general"])

    base_weight = w["sharpness"] + w["exposure"] + w["expression"]
    raw = (sharpness * w["sharpness"] +
           exposure  * w["exposure"]  +
           expression * w["expression"])

    score = raw / base_weight if base_weight > 0 else 0.0
    if blink_flag:
        score = max(0.0, score - w["blink_penalty"])

    return round(min(1.0, max(0.0, score)), 4)


# ── Main entry ────────────────────────────────────────────────────────────────

def score_image(preview_path: str, genre: str = "general") -> Optional[dict]:
    """Score a single image from its preview JPEG path."""
    if not preview_path:
        return None
    path = Path(preview_path)
    if not path.exists():
        log.warning(f"Preview not found: {preview_path}")
        return None

    try:
        img_pil = Image.open(str(path)).convert("RGB")

        # Resize to scoring resolution
        w, h = img_pil.size
        if max(w, h) > SCORE_SIZE:
            scale = SCORE_SIZE / max(w, h)
            img_pil = img_pil.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

        img_rgb  = np.array(img_pil, dtype=np.uint8)
        img_grey = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2GRAY)

        sharpness, lap_var            = score_sharpness(img_grey)
        exposure, mean_lum, h_clip, s_clip = score_exposure(img_grey)
        face_data                     = score_faces(img_grey, img_rgb)

        composite = compute_composite(
            sharpness=sharpness,
            exposure=exposure,
            blink_flag=face_data["blink_flag"],
            expression=face_data["expression"],
            genre=genre,
        )

        return {
            "sharpness":      sharpness,
            "lap_variance":   lap_var,
            "exposure":       exposure,
            "mean_luminance": mean_lum,
            "highlight_clip": h_clip,
            "shadow_clip":    s_clip,
            "face_count":     face_data["face_count"],
            "blink_flag":     face_data["blink_flag"],
            "min_ear":        face_data["min_ear"],
            "expression":     face_data["expression"],
            "composite":      composite,
        }

    except Exception as e:
        log.error(f"Scoring failed for {preview_path}: {e}", exc_info=True)
        return None


def run_scoring(db, genre: str = "general", workers: int = 1,
                progress_callback=None) -> int:
    """Score all unscored images in db. Returns count scored."""
    unscored = db.unscored_images()
    if not unscored:
        log.info("All images already scored")
        return 0

    log.info(f"Scoring {len(unscored)} images (genre={genre})")
    scored = 0

    for img in unscored:
        try:
            result = score_image(img["preview_path"], genre=genre)
            if result:
                db.upsert_score(img["id"], result)
                scored += 1
            if progress_callback:
                progress_callback(scored, len(unscored), img["filename"])
        except Exception as e:
            log.error(f"Score failed for {img['filename']}: {e}")

    return scored
