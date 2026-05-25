"""
ingestion.py — File ingestion pipeline.

Improvements in this version:
  - Fix #3: Filename-sequence fallback timestamps for images missing EXIF.
             IMG_0001.jpg, DSC_0002.ARW etc. are sorted numerically and
             assigned synthetic timestamps so burst clustering still works.
  - Fix #7: Skip exiftool call if EXIF is already in the DB for this file.
             Saves ~15ms per file on re-runs of a large shoot.
"""

import rawpy
import piexif
import subprocess
import json
import hashlib
import re
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from PIL import Image
import io

log = logging.getLogger("fixxer.ingestion")

RAW_EXTENSIONS = {
    ".cr2", ".cr3",         # Canon
    ".nef", ".nrw",         # Nikon
    ".arw", ".srf", ".sr2", # Sony
    ".raf",                 # Fujifilm
    ".orf",                 # Olympus
    ".rw2",                 # Panasonic
    ".pef", ".ptx",         # Pentax
    ".dng",                 # Adobe DNG
    ".3fr",                 # Hasselblad
    ".iiq",                 # Phase One
    ".erf",                 # Epson
    ".mrw",                 # Minolta
    ".x3f",                 # Sigma
}
JPEG_EXTENSIONS     = {".jpg", ".jpeg"}
SUPPORTED_EXTENSIONS = RAW_EXTENSIONS | JPEG_EXTENSIONS
PREVIEW_DIR         = ".fixxer_previews"
EXCLUDED_DIRS       = {PREVIEW_DIR, ".fixxer_cache", "__pycache__", ".git"}

# Regex to extract a sequence number from typical camera filenames:
# IMG_0001, DSC_0001, _MG_1234, P1234567, 20240101_120000, etc.
_SEQ_RE = re.compile(r"(\d{4,})")


def scan_directory(directory: Path, recursive: bool = True) -> List[Path]:
    """Walk directory, skip Fixxer's own working dirs and dot-dirs."""
    directory = Path(directory)
    files     = []

    def _should_skip(rel: Path) -> bool:
        for part in rel.parts:
            if part in EXCLUDED_DIRS or (part.startswith(".") and part != "."):
                return True
        return False

    if recursive:
        for ext in SUPPORTED_EXTENSIONS:
            for f in directory.rglob(f"*{ext}"):
                if not _should_skip(f.relative_to(directory)):
                    files.append(f)
            for f in directory.rglob(f"*{ext.upper()}"):
                if not _should_skip(f.relative_to(directory)):
                    files.append(f)
    else:
        for ext in SUPPORTED_EXTENSIONS:
            files.extend(directory.glob(f"*{ext}"))
            files.extend(directory.glob(f"*{ext.upper()}"))

    return sorted(set(files), key=lambda p: p.name.lower())


def _filename_sequence_ts(path: Path, base_ts: float = 1_700_000_000.0) -> Optional[float]:
    """
    Fix #3: Derive a synthetic timestamp from the numeric sequence in a
    filename when EXIF timestamp is absent.

    IMG_0001.jpg → base_ts + 1
    IMG_0002.jpg → base_ts + 2

    This preserves shot order so burst grouping works even without EXIF.
    base_ts is 2023-11-14 — a safe epoch well before any recent shoot.
    """
    m = _SEQ_RE.search(path.stem)
    if m:
        seq = int(m.group(1))
        return base_ts + seq
    return None


def extract_exif_exiftool(path: Path) -> dict:
    """Extract EXIF via exiftool. Returns normalised dict."""
    try:
        result = subprocess.run(
            ["exiftool", "-json", "-fast2",
             "-DateTimeOriginal", "-CreateDate",
             "-Make", "-Model", "-LensModel", "-LensInfo",
             "-ISO", "-ExposureTime", "-FNumber", "-FocalLength",
             str(path)],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return {}
        data = json.loads(result.stdout)
        if not data:
            return {}
        raw = data[0]

        ts = None
        for key in ["DateTimeOriginal", "CreateDate"]:
            val = raw.get(key, "")
            if val:
                try:
                    ts = datetime.strptime(
                        val.split("+")[0].strip(), "%Y:%m:%d %H:%M:%S"
                    ).timestamp()
                    break
                except Exception:
                    pass

        shutter = None
        raw_sh  = raw.get("ExposureTime", "")
        if raw_sh:
            try:
                if "/" in str(raw_sh):
                    n, d = str(raw_sh).split("/")
                    shutter = float(n) / float(d)
                else:
                    shutter = float(raw_sh)
            except Exception:
                pass

        return {
            "capture_ts": ts,
            "camera":     f"{raw.get('Make', '')} {raw.get('Model', '')}".strip() or None,
            "lens":       raw.get("LensModel") or raw.get("LensInfo") or None,
            "iso":        int(raw.get("ISO", 0)) or None,
            "shutter":    shutter,
            "aperture":   float(raw.get("FNumber", 0)) or None,
            "focal_mm":   float(str(raw.get("FocalLength", "0")).replace(" mm", "") or 0) or None,
        }
    except Exception as e:
        log.debug(f"exiftool failed for {path.name}: {e}")
        return {}


def extract_exif_piexif(path: Path) -> dict:
    """Fallback EXIF extraction for JPEG via piexif."""
    try:
        exif      = piexif.load(str(path))
        exif_data = exif.get("Exif", {})
        ifd0      = exif.get("0th", {})

        ts = None
        dv = exif_data.get(piexif.ExifIFD.DateTimeOriginal, b"")
        if dv:
            try:
                ts = datetime.strptime(dv.decode("ascii"), "%Y:%m:%d %H:%M:%S").timestamp()
            except Exception:
                pass

        shutter = None
        exp     = exif_data.get(piexif.ExifIFD.ExposureTime)
        if exp and exp[1]:
            shutter = exp[0] / exp[1]

        aperture = None
        fn       = exif_data.get(piexif.ExifIFD.FNumber)
        if fn and fn[1]:
            aperture = fn[0] / fn[1]

        iso      = exif_data.get(piexif.ExifIFD.ISOSpeedRatings)
        focal_mm = None
        fl       = exif_data.get(piexif.ExifIFD.FocalLength)
        if fl and fl[1]:
            focal_mm = fl[0] / fl[1]

        make  = ifd0.get(piexif.ImageIFD.Make,  b"").decode("ascii", errors="ignore").strip("\x00").strip()
        model = ifd0.get(piexif.ImageIFD.Model, b"").decode("ascii", errors="ignore").strip("\x00").strip()

        return {
            "capture_ts": ts,
            "camera":     f"{make} {model}".strip() or None,
            "lens":       None,
            "iso":        int(iso) if iso else None,
            "shutter":    shutter,
            "aperture":   aperture,
            "focal_mm":   focal_mm,
        }
    except Exception as e:
        log.debug(f"piexif failed for {path.name}: {e}")
        return {}


def extract_preview(path: Path, preview_dir: Path,
                    max_dimension: int = 1024) -> Optional[Path]:
    """Extract or cache the JPEG preview for a RAW/JPEG file."""
    preview_dir.mkdir(parents=True, exist_ok=True)
    preview_path = preview_dir / f"{path.stem}_{hashlib.md5(str(path).encode()).hexdigest()[:8]}.jpg"

    if preview_path.exists():
        return preview_path

    ext = path.suffix.lower()

    if ext in JPEG_EXTENSIONS:
        try:
            img = Image.open(path)
            img = _resize_to_max(img, max_dimension).convert("RGB")
            img.save(str(preview_path), "JPEG", quality=85)
            return preview_path
        except Exception as e:
            log.warning(f"JPEG preview failed for {path.name}: {e}")
            return None

    try:
        with rawpy.imread(str(path)) as raw:
            thumb = raw.extract_thumb()
            if thumb.format == rawpy.ThumbFormat.JPEG:
                img = Image.open(io.BytesIO(thumb.data))
            elif thumb.format == rawpy.ThumbFormat.BITMAP:
                img = Image.fromarray(thumb.data)
            else:
                raise ValueError(f"Unknown thumb format: {thumb.format}")
            img = _resize_to_max(img, max_dimension).convert("RGB")
            img.save(str(preview_path), "JPEG", quality=85)
            return preview_path
    except Exception as e:
        log.debug(f"Preview extract failed for {path.name}, trying postprocess: {e}")
        try:
            with rawpy.imread(str(path)) as raw:
                rgb = raw.postprocess(half_size=True, use_camera_wb=True,
                                      output_bps=8, no_auto_bright=True)
                img = _resize_to_max(Image.fromarray(rgb), max_dimension)
                img.save(str(preview_path), "JPEG", quality=85)
                return preview_path
        except Exception as e2:
            log.warning(f"All preview methods failed for {path.name}: {e2}")
            return None


def _resize_to_max(img: Image.Image, max_dim: int) -> Image.Image:
    w, h = img.size
    if max(w, h) <= max_dim:
        return img
    scale = max_dim / max(w, h)
    return img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)


def ingest_file(path: Path, preview_dir: Path,
                existing_exif: Optional[dict] = None) -> dict:
    """
    Ingest a single file.

    Fix #7: If EXIF data already exists in the DB (passed as existing_exif),
    skip the exiftool subprocess call entirely. Only extract the preview
    if it isn't cached yet.
    """
    path = Path(path)
    ext  = path.suffix.lower()

    if existing_exif is not None:
        # Fix #7: reuse DB EXIF — only extract preview if missing
        exif = existing_exif
    else:
        exif = extract_exif_exiftool(path)
        if not exif.get("capture_ts") and ext in JPEG_EXTENSIONS:
            exif = extract_exif_piexif(path)

    # Fix #3: Fall back to filename-sequence timestamp if EXIF has none
    if not exif.get("capture_ts"):
        seq_ts = _filename_sequence_ts(path)
        if seq_ts is not None:
            exif["capture_ts"] = seq_ts
            log.debug(f"Using filename-sequence timestamp for {path.name}: {seq_ts}")

    preview = extract_preview(path, preview_dir)

    return {
        "path":         str(path),
        "filename":     path.name,
        "ext":          ext,
        "size_bytes":   path.stat().st_size,
        "capture_ts":   exif.get("capture_ts"),
        "camera":       exif.get("camera"),
        "lens":         exif.get("lens"),
        "iso":          exif.get("iso"),
        "shutter":      exif.get("shutter"),
        "aperture":     exif.get("aperture"),
        "focal_mm":     exif.get("focal_mm"),
        "preview_path": str(preview) if preview else None,
    }


def ingest_directory(directory: Path, db, recursive: bool = True,
                     workers: int = 4, progress_callback=None) -> int:
    """
    Scan directory, ingest new files in parallel, persist to DB.

    Fix #7: Builds a set of already-ingested paths with their stored EXIF
    so returning files skip the exiftool call.
    """
    directory   = Path(directory)
    preview_dir = directory / PREVIEW_DIR
    files       = scan_directory(directory, recursive=recursive)

    if not files:
        log.warning(f"No supported image files found in {directory}")
        return 0

    log.info(f"Found {len(files)} files")

    # Fix #7: build existing map: path → stored EXIF dict (to skip re-extraction)
    existing_rows = db.get_images()
    existing_exif = {
        row["path"]: {
            "capture_ts": row["capture_ts"],
            "camera":     row["camera"],
            "lens":       row["lens"],
            "iso":        row["iso"],
            "shutter":    row["shutter"],
            "aperture":   row["aperture"],
            "focal_mm":   row["focal_mm"],
        }
        for row in existing_rows
    }

    new_files = [f for f in files if str(f) not in existing_exif]
    log.info(f"{len(new_files)} new / {len(files) - len(new_files)} cached")

    if not new_files:
        return 0

    ingested = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(ingest_file, f, preview_dir, None): f
            for f in new_files
        }
        for future in as_completed(futures):
            path = futures[future]
            try:
                data = future.result()
                db.upsert_image(data)
                ingested += 1
                if progress_callback:
                    progress_callback(ingested, len(new_files), path.name)
            except Exception as e:
                log.error(f"Ingestion failed for {path.name}: {e}")

    return ingested
