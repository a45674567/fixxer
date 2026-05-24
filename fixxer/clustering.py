"""
clustering.py — Duplicate detection and burst grouping.

Two-pass algorithm:
  Pass 1 (temporal): Images within TIMESTAMP_WINDOW seconds of each other
                     are candidate duplicates. O(N log N) after sort.
  Pass 2 (perceptual): pHash Hamming distance confirms visual similarity.
                       Only runs within candidate windows — effectively O(N).

Result: a group graph where each "group" is a burst or duplicate set,
with one elected candidate (highest priority for IQA scoring) per group.
"""

import imagehash
import logging
from pathlib import Path
from typing import Optional
from PIL import Image

log = logging.getLogger("fixxer.clustering")

# Images within this many seconds are burst candidates
TIMESTAMP_WINDOW = 3.0

# Max Hamming distance (out of 64 bits) to consider perceptually similar
# Lower = stricter matching. 10 is a good default; tune per manufacturer.
PHASH_THRESHOLD = 12

# Fallback group ID when image has no timestamp (singletons with no TS)
_SINGLETON_COUNTER = 0


def compute_phash(preview_path: str) -> Optional[str]:
    """Compute 64-bit perceptual hash from the JPEG preview."""
    if not preview_path:
        return None
    try:
        img = Image.open(preview_path).convert("L")  # greyscale for hash
        return str(imagehash.phash(img, hash_size=8))
    except Exception as e:
        log.debug(f"pHash failed for {preview_path}: {e}")
        return None


def hamming_distance(h1: str, h2: str) -> int:
    """Compute Hamming distance between two hex pHash strings."""
    try:
        a = imagehash.hex_to_hash(h1)
        b = imagehash.hex_to_hash(h2)
        return a - b
    except Exception:
        return 999  # Treat as dissimilar on error


def cluster_images(images: list, phash_threshold: int = PHASH_THRESHOLD,
                   timestamp_window: float = TIMESTAMP_WINDOW) -> dict:
    """
    Cluster images into duplicate/burst groups.

    images: list of sqlite3.Row with fields: id, capture_ts, preview_path
    Returns: dict mapping group_id → list of {image_id, phash, is_candidate}
    """
    global _SINGLETON_COUNTER

    if not images:
        return {}

    # ── Compute pHash for all images ──────────────────────────────────────
    hashes = {}
    for img in images:
        h = compute_phash(img["preview_path"])
        hashes[img["id"]] = h
        log.debug(f"pHash {img['id']}: {h}")

    # ── Pass 1: temporal windowing ─────────────────────────────────────────
    # Sort by capture timestamp; group consecutive images within window
    with_ts = [i for i in images if i["capture_ts"] is not None]
    without_ts = [i for i in images if i["capture_ts"] is None]

    with_ts_sorted = sorted(with_ts, key=lambda i: i["capture_ts"])

    # Build temporal candidate windows (union-find via simple window scan)
    temporal_groups = []
    current_group = []

    for img in with_ts_sorted:
        if not current_group:
            current_group = [img]
        elif img["capture_ts"] - current_group[-1]["capture_ts"] <= timestamp_window:
            current_group.append(img)
        else:
            temporal_groups.append(current_group)
            current_group = [img]
    if current_group:
        temporal_groups.append(current_group)

    # ── Pass 2: pHash verification within temporal groups ─────────────────
    # Split temporal groups into final groups based on visual similarity
    final_groups = []

    for temp_group in temporal_groups:
        if len(temp_group) == 1:
            final_groups.append(temp_group)
            continue

        # Within the temporal group, split by pHash similarity
        # Simple greedy: first image starts a sub-group; subsequent images
        # join if their hash is within threshold of ANY member of sub-group.
        subgroups = []
        for img in temp_group:
            h = hashes.get(img["id"])
            placed = False
            for sg in subgroups:
                for member in sg:
                    mh = hashes.get(member["id"])
                    if h and mh and hamming_distance(h, mh) <= phash_threshold:
                        sg.append(img)
                        placed = True
                        break
                if placed:
                    break
            if not placed:
                subgroups.append([img])

        final_groups.extend(subgroups)

    # Images without timestamps become singletons
    for img in without_ts:
        final_groups.append([img])

    # ── Assign group IDs and elect candidates ─────────────────────────────
    result = {}
    for gid, group in enumerate(final_groups):
        members = []
        for img in group:
            members.append({
                "image_id": img["id"],
                "phash": hashes.get(img["id"]),
                "is_candidate": False,
            })
        # Elect first member as candidate (scoring will reorder within group)
        # The selection engine later picks the highest-scoring candidate.
        if members:
            members[0]["is_candidate"] = True
        result[gid] = members

    log.info(
        f"Clustering: {len(images)} images → {len(result)} groups "
        f"(avg {len(images)/max(len(result),1):.1f} per group)"
    )
    return result


def run_clustering(db, phash_threshold: int = PHASH_THRESHOLD,
                   timestamp_window: float = TIMESTAMP_WINDOW,
                   progress_callback=None) -> int:
    """
    Run clustering on all images in db and persist group assignments.
    Returns total number of groups created.
    """
    images = db.get_images()
    if not images:
        log.warning("No images in DB to cluster")
        return 0

    groups = cluster_images(
        images,
        phash_threshold=phash_threshold,
        timestamp_window=timestamp_window,
    )

    # Clear existing groups — clustering is always a full rebuild for idempotency
    db.clear_groups()
    log.info(f"Clustering: {len(images)} images -> {len(groups)} groups")

    total = 0
    for gid, members in groups.items():
        for m in members:
            db.upsert_group_membership(
                group_id=gid,
                image_id=m["image_id"],
                phash=m["phash"] or "",
                is_candidate=m["is_candidate"],
            )
        total += 1
        if progress_callback:
            progress_callback(total, len(groups))

    return total
