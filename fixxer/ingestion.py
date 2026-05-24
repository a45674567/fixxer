"""
ingestion.py — File ingestion pipeline.

Scans a directory for RAW/JPEG files, extracts EXIF metadata
without full RAW decode, and extracts the embedded JPEG preview
for downstream analysis.

Key design decision: we NEVER do a full RAW decode in the culling
pipeline. Every modern RAW container (CR3, ARW, NEF, RAF, DNG, ORF)
embeds a full-resolution JPEG preview. We extract that and work
exclusively with it. This gives 10–50× throughput vs full decode.
"""

import rawpy
import piexif
import subprocess
import json
import hashlib
import time
import logging
from pathlib import Path
from typing import Iterator, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from PIL import Image
import io

log = logging.getLogger("fixxer.ingestion")

# Supported RAW extensions
RAW_EXTENSIONS = {
    ".cr2", ".cr3",        # Canon
    ".nef", ".nrw",        # Nikon
    ".arw", ".srf", ".sr2",# Sony
    ".raf",                # Fujifilm
    ".orf",                # Olympus
    ".rw2",                # Panasonic
    ".pef", ".ptx",        # Pentax
    ".dng",                # Adobe DNG (universal)
    ".3fr",                # Hasselblad
    ".iiq",                # Phase One
    ".erf",                # Epson
    ".mrw",                # Minolta/Konica
    ".x3f",                # Sigma
}

JPEG_EXTENSIONS = {".jpg", ".jpeg"}
SUPPORTED_EXTENSIONS = RAW_EXTENSIONS | JPEG_EXTENSIONS

# Preview cache subdirectory name
PREVIEW_DIR = ".fixxer_previews"


# Directories that Fixxer itself creates — always excluded from scanning
EXCLUDED_DIRS = {PREVIEW_DIR, ".fixxer_cache", "__pycache__", ".git"}


def scan_directory(directory: Path, recursive: bool = True) -> List[Path]:
    """Walk directory and return all supported image file paths.

    Excludes Fixxer's own working directories (.fixxer_previews etc.)
    and hidden dot-directories to prevent scanning cached previews as
    source files on subsequent runs.
    """
    directory = Path(directory)
    files = []

    def _should_skip(path: Path) -> bool:
        """Return True if this path is inside an excluded directory."""
        for part in path.parts:
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


def extract_exif_exiftool(path: Path) -> dict:
    """
    Extract EXIF metadata via exiftool subprocess.
    Fastest and most compatible approach across manufacturers.
    Returns a dict with normalised keys.
    """
    try:
        result = subprocess.run(
            ["exiftool", "-json", "-fast2",
             "-DateTimeOriginal", "-CreateDate",
             "-Make", "-Model", "-LensModel", "-LensInfo",
             "-ISO", "-ExposureTime", "-FNumber", "-FocalLength",
             "-ImageWidth", "-ImageHeight",
             str(path)],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return {}
        data = json.loads(result.stdout)
        if not data:
            return {}
        raw = data[0]

        # Parse capture timestamp
        ts = None
        for key in ["DateTimeOriginal", "CreateDate"]:
            val = raw.get(key, "")
            if val:
                try:
                    from datetime import datetime
                    ts = datetime.strptime(
                        val.split("+")[0].strip(), "%Y:%m:%d %H:%M:%S"
                    ).timestamp()
                    break
                except Exception:
                    pass

        # Parse shutter speed (e.g. "1/250")
        shutter = None
        raw_shutter = raw.get("ExposureTime", "")
        if raw_shutter:
            try:
                if "/" in str(raw_shutter):
                    num, den = str(raw_shutter).split("/")
                    shutter = float(num) / float(den)
                else:
                    shutter = float(raw_shutter)
            except Exception:
                pass

        return {
            "capture_ts": ts,
            "camera": f"{raw.get('Make', '')} {raw.get('Model', '')}".strip() or None,
            "lens": raw.get("LensModel") or raw.get("LensInfo") or None,
            "iso": int(raw.get("ISO", 0)) or None,
            "shutter": shutter,
            "aperture": float(raw.get("FNumber", 0)) or None,
            "focal_mm": float(raw.get("FocalLength", "0").replace(" mm", "") or 0) or None,
        }
    except Exception as e:
        log.debug(f"exiftool failed for {path.name}: {e}")
        return {}


def extract_exif_piexif(path: Path) -> dict:
    """
    Fallback EXIF extraction via piexif (works for JPEG only).
    """
    try:
        exif = piexif.load(str(path))
        exif_data = exif.get("Exif", {})
        ifd0 = exif.get("0th", {})

        # Timestamp
        ts = None
        dt_val = exif_data.get(piexif.ExifIFD.DateTimeOriginal, b"")
        if dt_val:
            try:
                from datetime import datetime
                ts = datetime.strptime(
                    dt_val.decode("ascii"), "%Y:%m:%d %H:%M:%S"
                ).timestamp()
            except Exception:
                pass

        # Shutter
        shutter = None
        exp = exif_data.get(piexif.ExifIFD.ExposureTime)
        if exp and exp[1]:
            shutter = exp[0] / exp[1]

        # Aperture
        aperture = None
        fn = exif_data.get(piexif.ExifIFD.FNumber)
        if fn and fn[1]:
            aperture = fn[0] / fn[1]

        # ISO
        iso = exif_data.get(piexif.ExifIFD.ISOSpeedRatings)

        # Focal length
        focal_mm = None
        fl = exif_data.get(piexif.ExifIFD.FocalLength)
        if fl and fl[1]:
            focal_mm = fl[0] / fl[1]

        make = ifd0.get(piexif.ImageIFD.Make, b"").decode("ascii", errors="ignore").strip("\x00").strip()
        model = ifd0.get(piexif.ImageIFD.Model, b"").decode("ascii", errors="ignore").strip("\x00").strip()

        return {
            "capture_ts": ts,
            "camera": f"{make} {model}".strip() or None,
            "lens": None,
            "iso": int(iso) if iso else None,
            "shutter": shutter,
            "aperture": aperture,
            "focal_mm": focal_mm,
        }
    except Exception as e:
        log.debug(f"piexif failed for {path.name}: {e}")
        return {}


def extract_preview(path: Path, preview_dir: Path,
                    max_dimension: int = 1024) -> Optional[Path]:
    """
    Extract the embedded JPEG preview from a RAW file.
    Falls back to opening as JPEG directly if it's already a JPEG.
    Saves the preview to preview_dir/<stem>.jpg.

    max_dimension: longest edge of the preview (resized if necessary).
    We use 1024px as the default — larger than needed for IQA inference
    but useful for the review UI thumbnails.
    """
    preview_dir.mkdir(parents=True, exist_ok=True)
    preview_path = preview_dir / f"{path.stem}_{hashlib.md5(str(path).encode()).hexdigest()[:8]}.jpg"

    if preview_path.exists():
        return preview_path  # Cache hit

    ext = path.suffix.lower()

    # ── JPEG: just resize and re-save ──────────────────────────────────
    if ext in JPEG_EXTENSIONS:
        try:
            img = Image.open(path)
            img = _resize_to_max(img, max_dimension)
            img = img.convert("RGB")
            img.save(str(preview_path), "JPEG", quality=85)
            return preview_path
        except Exception as e:
            log.warning(f"Failed to process JPEG {path.name}: {e}")
            return None

    # ── RAW: extract embedded preview via rawpy ─────────────────────────
    try:
        with rawpy.imread(str(path)) as raw:
            thumb = raw.extract_thumb()
            if thumb.format == rawpy.ThumbFormat.JPEG:
                # Embedded JPEG — fastest path, no decode needed
                img = Image.open(io.BytesIO(thumb.data))
            elif thumb.format == rawpy.ThumbFormat.BITMAP:
                # Bitmap fallback (uncommon)
                img = Image.fromarray(thumb.data)
            else:
                raise ValueError(f"Unknown thumb format: {thumb.format}")

            img = _resize_to_max(img, max_dimension)
            img = img.convert("RGB")
            img.save(str(preview_path), "JPEG", quality=85)
            return preview_path

    except Exception as e:
        # Last resort: attempt full postprocess at low resolution
        log.debug(f"Preview extract failed for {path.name}, trying postprocess: {e}")
        try:
            with rawpy.imread(str(path)) as raw:
                rgb = raw.postprocess(
                    half_size=True,
                    use_camera_wb=True,
                    output_bps=8,
                    no_auto_bright=True,
                )
                img = Image.fromarray(rgb)
                img = _resize_to_max(img, max_dimension)
                img.save(str(preview_path), "JPEG", quality=85)
                return preview_path
        except Exception as e2:
            log.warning(f"All preview extraction methods failed for {path.name}: {e2}")
            return None


def _resize_to_max(img: Image.Image, max_dim: int) -> Image.Image:
    """Resize image so longest edge ≤ max_dim, preserving aspect ratio."""
    w, h = img.size
    if max(w, h) <= max_dim:
        return img
    if w >= h:
        new_w = max_dim
        new_h = int(h * max_dim / w)
    else:
        new_h = max_dim
        new_w = int(w * max_dim / h)
    return img.resize((new_w, new_h), Image.LANCZOS)


def ingest_file(path: Path, preview_dir: Path) -> dict:
    """
    Full ingestion for a single file.
    Returns dict ready for db.upsert_image().
    """
    path = Path(path)
    ext = path.suffix.lower()

    # EXIF extraction
    exif = extract_exif_exiftool(path)
    if not exif.get("capture_ts") and ext in JPEG_EXTENSIONS:
        exif = extract_exif_piexif(path)

    # Preview extraction
    preview = extract_preview(path, preview_dir)

    return {
        "path": str(path),
        "filename": path.name,
        "ext": ext,
        "size_bytes": path.stat().st_size,
        "capture_ts": exif.get("capture_ts"),
        "camera": exif.get("camera"),
        "lens": exif.get("lens"),
        "iso": exif.get("iso"),
        "shutter": exif.get("shutter"),
        "aperture": exif.get("aperture"),
        "focal_mm": exif.get("focal_mm"),
        "preview_path": str(preview) if preview else None,
    }


def ingest_directory(
    directory: Path,
    db,
    recursive: bool = True,
    workers: int = 4,
    progress_callback=None,
) -> int:
    """
    Scan directory, ingest all files in parallel, persist to DB.
    Returns count of newly ingested files.
    """
    directory = Path(directory)
    preview_dir = directory / PREVIEW_DIR
    files = scan_directory(directory, recursive=recursive)

    if not files:
        log.warning(f"No supported image files found in {directory}")
        return 0

    log.info(f"Found {len(files)} files to ingest")

    # Filter already-ingested files by checking DB
    existing = {row["path"] for row in db.get_images()}
    new_files = [f for f in files if str(f) not in existing]
    log.info(f"{len(new_files)} new files to ingest ({len(files)-len(new_files)} already in DB)")

    if not new_files:
        return 0

    ingested = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(ingest_file, f, preview_dir): f
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
