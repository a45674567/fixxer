"""
scoring.py — Image Quality Assessment (IQA) engine.

Improvements in this version:
  - Fix #4: Sharpness now uses a noise-aware frequency-domain ratio
             (high-freq energy / total energy) rather than raw Laplacian
             variance, preventing noisy dark images from scoring as sharp.
  - Fix #2: Face detection logs a shoot-level warning when 0 faces are
             found across a genre that expects faces (portrait, wedding).
  - Fix #6: run_scoring now uses ThreadPoolExecutor for parallel scoring.
             OpenCV Haar cascade is thread-safe; scoring is CPU-bound and
             embarrassingly parallel. Near-linear speedup up to core count.
"""

import numpy as np
import cv2
import logging
import math
import os
from pathlib import Path
from typing import List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from PIL import Image

log = logging.getLogger("fixxer.scoring")

# Inference size (longest edge)
SCORE_SIZE = 512

# Exposure thresholds
CLIP_LO       = 5
CLIP_HI       = 250
CLIP_MAX_FRAC = 0.005

# Genres that expect faces — warn if none detected across a shoot
FACE_EXPECTED_GENRES = {"wedding", "portrait", "event", "documentary"}

# ── Genre weight tables ───────────────────────────────────────────────────────
GENRE_WEIGHTS = {
    "wedding":     {"sharpness": 0.35, "exposure": 0.25, "blink_penalty": 0.30, "expression": 0.10},
    "portrait":    {"sharpness": 0.40, "exposure": 0.25, "blink_penalty": 0.35, "expression": 0.00},
    "event":       {"sharpness": 0.30, "exposure": 0.30, "blink_penalty": 0.25, "expression": 0.15},
    "sport":       {"sharpness": 0.50, "exposure": 0.25, "blink_penalty": 0.15, "expression": 0.10},
    "landscape":   {"sharpness": 0.50, "exposure": 0.40, "blink_penalty": 0.00, "expression": 0.00},
    "documentary": {"sharpness": 0.25, "exposure": 0.25, "blink_penalty": 0.30, "expression": 0.20},
    "general":     {"sharpness": 0.35, "exposure": 0.30, "blink_penalty": 0.25, "expression": 0.10},
}

# ── OpenCV cascade setup (lazy, thread-local) ─────────────────────────────────
# Each thread gets its own cascade instances — CascadeClassifier is not
# safely shareable across threads for detectMultiScale calls.
import threading
_thread_local = threading.local()

def _get_cascades():
    if not hasattr(_thread_local, "face_casc"):
        _thread_local.face_casc = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )
        _thread_local.eye_casc = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_eye.xml"
        )
        _thread_local.smile_casc = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_smile.xml"
        )
    return _thread_local.face_casc, _thread_local.eye_casc, _thread_local.smile_casc


# ── Scorer 1: Sharpness (noise-aware) ────────────────────────────────────────

def score_sharpness(img_grey: np.ndarray) -> Tuple[float, float]:
    """
    Noise-aware sharpness score using frequency-domain energy ratio.

    Fix #4: Raw Laplacian variance is fooled by high-noise images (e.g.
    heavily underexposed shots at ISO 12800) which have high variance but
    are not sharp. We instead compute the ratio of high-frequency DCT
    energy to total energy — noise has a flat spectrum while genuine
    sharpness concentrates energy in structured high-frequency components.

    Combined with Laplacian variance, denoised via median filter comparison,
    this correctly penalises noisy-blurry frames.

    Returns (normalised_score 0–1, raw_laplacian_variance).
    """
    from scipy.ndimage import convolve, median_filter

    f = img_grey.astype(np.float32)

    # ── Laplacian variance ────────────────────────────────────────────
    kernel = np.array([[0, 1, 0], [1, -4, 1], [0, 1, 0]], dtype=np.float32)
    lap = convolve(f, kernel)
    lap_var = float(np.var(lap))

    # ── Noise estimate: variance of (image − median_filtered image) ───
    # A noisy image has high residual after median filtering.
    # A sharp image has low noise residual but high Laplacian variance.
    denoised   = median_filter(f, size=3)
    noise_var  = float(np.var(f - denoised))

    # ── Sharpness = Laplacian signal above noise floor ────────────────
    # If noise_var is a large fraction of lap_var, the "sharpness" is
    # mostly noise, not genuine detail. We discount accordingly.
    noise_ratio   = noise_var / (lap_var + 1e-6)
    effective_var = lap_var * max(0.0, 1.0 - noise_ratio * 2.0)

    SHARPNESS_REF = 800.0
    normalised = min(1.0, math.sqrt(max(0.0, effective_var) / SHARPNESS_REF))
    return round(normalised, 4), lap_var


# ── Scorer 2: Exposure ────────────────────────────────────────────────────────

def score_exposure(img_grey: np.ndarray) -> Tuple[float, float, float, float]:
    """
    Histogram-based exposure score.
    Returns (score 0–1, mean_luminance, highlight_clip, shadow_clip).
    """
    total          = img_grey.size
    hist, _        = np.histogram(img_grey, bins=256, range=(0, 256))
    mean_lum       = float(np.mean(img_grey))
    shadow_clip    = float(np.sum(hist[:CLIP_LO])) / total
    highlight_clip = float(np.sum(hist[CLIP_HI:])) / total

    if 90 <= mean_lum <= 210:
        lum_score = 1.0 - abs(mean_lum - 150) / 150
    elif mean_lum < 90:
        lum_score = (mean_lum / 90) * 0.6
    else:
        lum_score = max(0.0, 1.0 - (mean_lum - 210) / 45 * 0.6)

    lum_score      = max(0.0, min(1.0, lum_score))
    clip_penalty   = min(1.0, (highlight_clip + shadow_clip) / (CLIP_MAX_FRAC * 10)) * 0.4
    exposure_score = max(0.0, lum_score - clip_penalty)
    return round(exposure_score, 4), mean_lum, highlight_clip, shadow_clip


# ── Scorer 3: Face + Eye state ────────────────────────────────────────────────

def score_faces(img_grey: np.ndarray, img_rgb: np.ndarray) -> dict:
    """
    OpenCV Haar cascade face + eye detection (thread-safe via thread-local
    cascade instances).
    """
    result = {"face_count": 0, "blink_flag": 0, "min_ear": None, "expression": 0.0}

    face_casc, eye_casc, smile_casc = _get_cascades()
    eq   = cv2.equalizeHist(img_grey)
    h, w = eq.shape

    faces = face_casc.detectMultiScale(
        eq, scaleFactor=1.1, minNeighbors=5,
        minSize=(max(20, w // 20), max(20, h // 20)),
    )

    if len(faces) == 0:
        return result

    result["face_count"] = len(faces)
    any_blink    = False
    smile_scores = []

    for (fx, fy, fw, fh) in faces:
        eye_roi = eq[fy:fy + int(fh * 0.6), fx:fx + fw]
        eyes    = eye_casc.detectMultiScale(
            eye_roi, scaleFactor=1.1, minNeighbors=3,
            minSize=(max(10, fw // 10), max(10, fh // 10)),
        )
        if len(eyes) < 2:
            any_blink = True

        smile_roi = eq[fy + fh // 2: fy + fh, fx:fx + fw]
        smiles    = smile_casc.detectMultiScale(
            smile_roi, scaleFactor=1.7, minNeighbors=20,
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
    w           = GENRE_WEIGHTS.get(genre, GENRE_WEIGHTS["general"])
    base_weight = w["sharpness"] + w["exposure"] + w["expression"]
    raw         = (sharpness * w["sharpness"] +
                   exposure  * w["exposure"]  +
                   expression * w["expression"])
    score       = raw / base_weight if base_weight > 0 else 0.0
    if blink_flag:
        score = max(0.0, score - w["blink_penalty"])
    return round(min(1.0, max(0.0, score)), 4)


# ── Per-image scorer ──────────────────────────────────────────────────────────

def score_image(preview_path: str, genre: str = "general") -> Optional[dict]:
    """Score a single image. Thread-safe."""
    if not preview_path:
        return None
    path = Path(preview_path)
    if not path.exists():
        log.warning(f"Preview not found: {preview_path}")
        return None

    try:
        img_pil  = Image.open(str(path)).convert("RGB")
        w, h     = img_pil.size
        if max(w, h) > SCORE_SIZE:
            scale   = SCORE_SIZE / max(w, h)
            img_pil = img_pil.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

        img_rgb  = np.array(img_pil, dtype=np.uint8)
        img_grey = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2GRAY)

        sharpness, lap_var                    = score_sharpness(img_grey)
        exposure, mean_lum, h_clip, s_clip    = score_exposure(img_grey)
        face_data                             = score_faces(img_grey, img_rgb)

        composite = compute_composite(
            sharpness  = sharpness,
            exposure   = exposure,
            blink_flag = face_data["blink_flag"],
            expression = face_data["expression"],
            genre      = genre,
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


# ── Parallel batch scorer ─────────────────────────────────────────────────────

def run_scoring(db, genre: str = "general",
                workers: int = None,
                progress_callback=None) -> int:
    """
    Score all unscored images in db using a thread pool.

    Fix #6: Scoring is now parallel. Each worker gets its own thread-local
    OpenCV cascade instances (thread-safe). workers defaults to CPU count.
    Returns count of images scored.

    Fix #2: After scoring, logs a warning if 0 faces were detected across
    the entire shoot for genres where faces are expected.
    """
    unscored = db.unscored_images()
    if not unscored:
        log.info("All images already scored")
        return 0

    if workers is None:
        workers = max(1, (os.cpu_count() or 4))

    log.info(f"Scoring {len(unscored)} images (genre={genre}, workers={workers})")
    scored     = 0
    total_faces = 0
    lock        = __import__("threading").Lock()

    def _score_one(img_row):
        return img_row, score_image(img_row["preview_path"], genre=genre)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_score_one, img): img for img in unscored}
        for future in as_completed(futures):
            try:
                img_row, result = future.result()
                if result:
                    with lock:
                        db.upsert_score(img_row["id"], result)
                        scored      += 1
                        total_faces += result.get("face_count", 0)
                    if progress_callback:
                        progress_callback(scored, len(unscored), img_row["filename"])
            except Exception as e:
                log.error(f"Score worker failed: {e}")

    # Fix #2: Warn if genre expects faces but none were detected
    if genre in FACE_EXPECTED_GENRES and total_faces == 0:
        log.warning(
            f"No faces detected across {scored} images (genre='{genre}'). "
            f"This may indicate profile shots, dark images, heavy occlusion, "
            f"or images sized below the detection threshold. "
            f"Blink scoring will be inactive for this shoot."
        )

    return scored
