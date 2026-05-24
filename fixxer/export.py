"""
export.py — XMP sidecar writer and export utilities.

Writes Lightroom/Capture One compatible XMP sidecar files (.xmp)
adjacent to each RAW file based on the selection decisions in the DB.

Non-destructive: never modifies the original RAW file.
The .xmp sidecar is read by Lightroom Classic when:
  Preferences → Catalog Settings → "Automatically write changes into XMP" is ON
or when the user selects all images and does Metadata → Save Metadata to Files.

Capture One reads XMP sidecars natively for DNG; for other RAW formats,
selections can be exported as a separate CO session XML.

XMP fields written:
  xmp:Rating        — 0–5 star rating
  xmp:Label         — Colour label ('Green' for keep, '' for reject)
  xmpDM:pick        — Lightroom pick flag (1=pick, -1=reject, 0=unflagged)
  dc:description    — Reason string from selection engine (useful for debugging)
"""

import subprocess
import logging
import shutil
from pathlib import Path
from typing import Optional

log = logging.getLogger("fixxer.export")

# Lightroom colour label strings (capital first letter required)
COLOR_MAP = {
    "red":    "Red",
    "yellow": "Yellow",
    "green":  "Green",
    "blue":   "Blue",
    "purple": "Purple",
    "":       "",
}

# Lightroom pick flag values
PICK_MAP = {
    "pick":   "1",
    "reject": "-1",
    "":       "0",
}


def _build_xmp_content(rating: int, label: str, pick: str,
                       reason: str = "", confidence: float = None) -> str:
    """
    Build a syntactically correct XMP sidecar file content string.
    Lightroom is strict about XMP namespace declarations.
    """
    xmp_label = COLOR_MAP.get(label.lower(), "")
    xmp_pick  = PICK_MAP.get(pick.lower(), "0")
    xmp_rating = max(0, min(5, int(rating)))

    confidence_str = ""
    if confidence is not None:
        confidence_str = f'\n   fixxer:AiConfidence="{confidence:.3f}"'

    return f'''<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="Fixxer 0.1.0">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
   xmlns:xmp="http://ns.adobe.com/xap/1.0/"
   xmlns:xmpDM="http://ns.adobe.com/xmp/1.0/DynamicMedia/"
   xmlns:dc="http://purl.org/dc/elements/1.1/"
   xmlns:fixxer="http://fixxer.app/xmp/1.0/"
   xmp:Rating="{xmp_rating}"
   xmp:Label="{xmp_label}"
   xmpDM:pick="{xmp_pick}"{confidence_str}>
   <dc:description>
    <rdf:Alt>
     <rdf:li xml:lang="x-default">{_escape_xml(reason)}</rdf:li>
    </rdf:Alt>
   </dc:description>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>'''


def _escape_xml(s: str) -> str:
    return (s.replace("&", "&amp;")
              .replace("<", "&lt;")
              .replace(">", "&gt;")
              .replace('"', "&quot;"))


def write_xmp_sidecar(image_path: str, rating: int, label: str,
                      pick: str, reason: str = "",
                      confidence: float = None) -> Optional[Path]:
    """
    Write an XMP sidecar file adjacent to the image file.
    Returns the path to the created .xmp file, or None on failure.
    """
    img_path = Path(image_path)
    xmp_path = img_path.with_suffix(".xmp")

    try:
        content = _build_xmp_content(rating, label, pick, reason, confidence)
        xmp_path.write_text(content, encoding="utf-8")
        return xmp_path
    except PermissionError:
        log.error(f"Permission denied writing XMP: {xmp_path}")
        return None
    except Exception as e:
        log.error(f"XMP write failed for {img_path.name}: {e}")
        return None


def write_xmp_via_exiftool(image_path: str, rating: int, label: str,
                            pick: str) -> bool:
    """
    Alternative: write XMP metadata directly into the sidecar via exiftool.
    More robust for edge cases but slower (subprocess per file).
    Used as fallback when direct write fails.
    """
    xmp_label = COLOR_MAP.get(label.lower(), "")
    xmp_pick_flag = {"pick": "1", "reject": "-1", "": "0"}.get(pick.lower(), "0")

    cmd = [
        "exiftool",
        f"-Rating={rating}",
        f"-Label={xmp_label}" if xmp_label else "-Label=",
        f"-PickLabel={xmp_pick_flag}",
        "-overwrite_original",
        "-ext xmp",        # only create/modify sidecar
        str(image_path),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return result.returncode == 0
    except Exception as e:
        log.debug(f"exiftool XMP write failed: {e}")
        return False


def export_selections(db, output_dir: Optional[Path] = None,
                      dry_run: bool = False,
                      progress_callback=None) -> dict:
    """
    Write XMP sidecar files for all selections in the database.

    output_dir: if set, copy XMP files here instead of adjacent to RAW files.
    dry_run:    if True, report what would be written without writing.

    Returns: {written: int, failed: int, skipped: int}
    """
    selections = db.get_selections()
    if not selections:
        log.warning("No selections in DB — run selection engine first")
        return {"written": 0, "failed": 0, "skipped": 0}

    written = failed = skipped = 0

    for sel in selections:
        img_path = Path(sel["path"])

        if not img_path.exists():
            log.warning(f"Source file missing: {img_path}")
            skipped += 1
            continue

        rating     = sel["star"] or 0
        label      = sel["colour_label"] or ""
        pick       = sel["pick_flag"] or ""
        reason     = sel["reason"] or ""
        confidence = sel["ai_confidence"]

        if dry_run:
            action = "KEEP" if sel["selected"] else "REJECT"
            log.info(f"[dry-run] {action} {img_path.name} "
                     f"(⭐{rating}, conf={confidence:.2f}) — {reason}")
            written += 1
            continue

        xmp_path = write_xmp_sidecar(
            str(img_path), rating, label, pick, reason, confidence
        )

        if xmp_path is None:
            failed += 1
            continue

        # Optionally copy to output_dir
        if output_dir:
            out = Path(output_dir)
            out.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(str(xmp_path), out / xmp_path.name)
            except Exception as e:
                log.warning(f"Failed to copy XMP to output dir: {e}")

        written += 1

        if progress_callback:
            progress_callback(written + failed + skipped, len(selections))

    log.info(f"XMP export: {written} written, {failed} failed, {skipped} skipped")
    return {"written": written, "failed": failed, "skipped": skipped}


def export_summary_csv(db, output_path: Path) -> bool:
    """
    Export a CSV summary of all selections — useful for debugging
    and for photographers who want a spreadsheet view.
    """
    import csv

    selections = db.get_selections()
    if not selections:
        return False

    try:
        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "filename", "selected", "star", "colour", "pick",
                "sharpness", "exposure", "blink", "faces", "composite",
                "confidence", "reason"
            ])
            for sel in selections:
                writer.writerow([
                    sel["filename"],
                    "KEEP" if sel["selected"] else "REJECT",
                    sel["star"],
                    sel["colour_label"],
                    sel["pick_flag"],
                    f"{sel['sharpness']:.3f}" if sel["sharpness"] else "",
                    f"{sel['exposure']:.3f}" if sel["exposure"] else "",
                    "YES" if sel["blink_flag"] else "NO",
                    sel["face_count"],
                    f"{sel['composite']:.3f}" if sel["composite"] else "",
                    f"{sel['ai_confidence']:.3f}" if sel["ai_confidence"] else "",
                    sel["reason"],
                ])
        return True
    except Exception as e:
        log.error(f"CSV export failed: {e}")
        return False
