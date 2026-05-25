"""
export.py — XMP sidecar writer and export utilities.

Improvements in this version:
  - Fix #5: XMP validation step after every write. Parses the written file
             as XML and confirms required attributes are present before
             reporting success. Reports per-file validation failures clearly.
"""

import subprocess
import logging
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

log = logging.getLogger("fixxer.export")

COLOR_MAP = {
    "red": "Red", "yellow": "Yellow", "green": "Green",
    "blue": "Blue", "purple": "Purple", "": "",
}
PICK_MAP = {"pick": "1", "reject": "-1", "": "0"}


def _build_xmp_content(rating: int, label: str, pick: str,
                        reason: str = "", confidence: float = None) -> str:
    xmp_label  = COLOR_MAP.get(label.lower(), "")
    xmp_pick   = PICK_MAP.get(pick.lower(), "0")
    xmp_rating = max(0, min(5, int(rating)))

    conf_attr = ""
    if confidence is not None:
        conf_attr = f'\n   fixxer:AiConfidence="{confidence:.3f}"'

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
   xmpDM:pick="{xmp_pick}"{conf_attr}>
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
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def _validate_xmp(xmp_path: Path) -> tuple:
    """
    Fix #5: Validate the written XMP file is well-formed XML and contains
    the required rating and pick attributes.

    Returns (is_valid: bool, error_message: str).
    """
    try:
        content = xmp_path.read_text(encoding="utf-8")

        # Strip XMP packet wrapper before parsing — ET doesn't handle
        # the <?xpacket ...?> processing instructions well on all platforms
        inner = content
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("<x:xmpmeta"):
                start = content.index(stripped)
                end   = content.rindex("</x:xmpmeta>") + len("</x:xmpmeta>")
                inner = content[start:end]
                break

        # Register namespaces to avoid ns0: mangling in ET
        ET.register_namespace("x",     "adobe:ns:meta/")
        ET.register_namespace("rdf",   "http://www.w3.org/1999/02/22-rdf-syntax-ns#")
        ET.register_namespace("xmp",   "http://ns.adobe.com/xap/1.0/")
        ET.register_namespace("xmpDM", "http://ns.adobe.com/xmp/1.0/DynamicMedia/")

        root = ET.fromstring(inner)

        # Walk the tree looking for the rdf:Description element
        ns = {
            "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
            "xmp": "http://ns.adobe.com/xap/1.0/",
        }
        desc = root.find(".//rdf:Description", ns)
        if desc is None:
            return False, "Missing rdf:Description element"

        rating_key = "{http://ns.adobe.com/xap/1.0/}Rating"
        pick_key   = "{http://ns.adobe.com/xmp/1.0/DynamicMedia/}pick"

        if rating_key not in desc.attrib:
            return False, "Missing xmp:Rating attribute"
        if pick_key not in desc.attrib:
            return False, "Missing xmpDM:pick attribute"

        return True, ""

    except ET.ParseError as e:
        return False, f"XML parse error: {e}"
    except Exception as e:
        return False, f"Validation error: {e}"


def write_xmp_sidecar(image_path: str, rating: int, label: str,
                       pick: str, reason: str = "",
                       confidence: float = None) -> Optional[Path]:
    """
    Write XMP sidecar adjacent to image file.

    Fix #5: Validates the written file before returning. Returns None
    and logs an error if validation fails.
    """
    img_path = Path(image_path)
    xmp_path = img_path.with_suffix(".xmp")

    try:
        content = _build_xmp_content(rating, label, pick, reason, confidence)
        xmp_path.write_text(content, encoding="utf-8")
    except PermissionError:
        log.error(f"Permission denied writing XMP: {xmp_path}")
        return None
    except Exception as e:
        log.error(f"XMP write failed for {img_path.name}: {e}")
        return None

    # Fix #5: validate immediately after write
    valid, err = _validate_xmp(xmp_path)
    if not valid:
        log.error(
            f"XMP validation failed for {img_path.name}: {err}. "
            f"The sidecar file may not import correctly into Lightroom."
        )
        return None

    return xmp_path


def export_selections(db, output_dir: Optional[Path] = None,
                      dry_run: bool = False,
                      progress_callback=None) -> dict:
    """Write XMP sidecars for all selections. Returns {written, failed, skipped}."""
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
                     f"(★{rating}, conf={confidence:.2f}) — {reason}")
            written += 1
            continue

        xmp_path = write_xmp_sidecar(
            str(img_path), rating, label, pick, reason, confidence
        )

        if xmp_path is None:
            failed += 1
        else:
            written += 1
            if output_dir:
                out = Path(output_dir)
                out.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(str(xmp_path), out / xmp_path.name)
                except Exception as e:
                    log.warning(f"Failed to copy XMP to output dir: {e}")

        if progress_callback:
            progress_callback(written + failed + skipped, len(selections))

    log.info(f"XMP export: {written} written, {failed} failed, {skipped} skipped")
    return {"written": written, "failed": failed, "skipped": skipped}


def export_summary_csv(db, output_path: Path) -> bool:
    """Export a CSV summary of all selections."""
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
                    f"{sel['sharpness']:.3f}"    if sel["sharpness"]    else "",
                    f"{sel['exposure']:.3f}"     if sel["exposure"]     else "",
                    "YES" if sel["blink_flag"] else "NO",
                    sel["face_count"],
                    f"{sel['composite']:.3f}"    if sel["composite"]    else "",
                    f"{sel['ai_confidence']:.3f}" if sel["ai_confidence"] else "",
                    sel["reason"],
                ])
        return True
    except Exception as e:
        log.error(f"CSV export failed: {e}")
        return False
