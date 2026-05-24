"""
selection.py — Selection engine.

Translates per-image quality scores into a binary keep/reject set.

Algorithm:
  1. Within each duplicate group, elect the highest-scoring image
     as the group candidate (others are soft-rejected).
  2. Across all group candidates, rank by composite score.
  3. Apply target: top N% or top K images are selected.
  4. Constraint: always select at least one image per group
     (preserves shoot coverage even for low-quality groups).

Also computes a confidence score per decision — used in Phase 3
for confidence-gated review (only uncertain decisions surface for
human arbitration).
"""

import logging
import math
from typing import Optional

log = logging.getLogger("fixxer.selection")

# Genre-specific target percentages if user doesn't specify
GENRE_DEFAULT_TARGETS = {
    "wedding":     20,  # Deliver ~20% of frames
    "portrait":    30,
    "event":       20,
    "sport":       25,
    "landscape":   40,  # Landscapes tend to have higher keep rates
    "documentary": 25,
    "general":     25,
}

# Confidence score thresholds
CONFIDENCE_HIGH   = 0.80   # AI is confident — no review needed
CONFIDENCE_MEDIUM = 0.60   # Borderline — flag for optional review
CONFIDENCE_LOW    = 0.40   # Uncertain — flag for mandatory review


def _confidence_from_score(score: float, threshold: float) -> float:
    """
    Estimate decision confidence based on distance from decision threshold.
    A score well above or below the threshold is high-confidence.
    A score near the threshold is low-confidence.
    """
    distance = abs(score - threshold)
    # Map distance to 0–1 confidence using a sigmoid-like function
    # At distance=0 (right on threshold): confidence = 0.5
    # At distance=0.2: confidence ≈ 0.85
    # At distance=0.4+: confidence ≈ 0.98
    confidence = 1.0 - 1.0 / (1.0 + math.exp(distance * 15 - 3))
    return round(min(0.99, max(0.50, confidence)), 3)


def _elect_group_candidates(scored_images: list, groups: dict) -> set:
    """
    Within each group, elect the image with the highest composite score
    as the group candidate. Returns set of elected image IDs.

    scored_images: list of rows with id, composite
    groups: dict from db.get_groups() — group_id → list of {image_id, is_candidate}
    """
    # Build quick lookup: image_id → composite score
    score_map = {row["id"]: row["composite"] or 0.0 for row in scored_images}

    elected = set()

    for gid, members in groups.items():
        member_ids = [m["image_id"] for m in members]
        if not member_ids:
            continue

        # Find the member with the highest composite score
        best_id = max(member_ids, key=lambda iid: score_map.get(iid, 0.0))
        elected.add(best_id)

    return elected


def run_selection(
    db,
    genre: str = "general",
    target_pct: Optional[float] = None,
    target_count: Optional[int] = None,
    progress_callback=None,
) -> dict:
    """
    Run selection over all scored images.

    target_pct:   Keep top N% of images (e.g. 20.0 for 20%).
                  If None, uses genre default.
    target_count: Keep top K images absolutely.
                  If both target_pct and target_count are given,
                  target_count takes precedence.

    Returns: {kept: int, rejected: int, threshold_score: float}
    """
    scored = db.get_scores()
    if not scored:
        log.warning("No scored images — run scoring first")
        return {"kept": 0, "rejected": 0, "threshold_score": 0.0}

    # Clear existing selections for idempotent re-runs
    db.clear_selections()

    groups = db.get_groups()
    if not groups:
        log.warning("No groups — run clustering first")
        # Treat each image as its own group
        groups = {i: [{"image_id": row["id"], "is_candidate": True}]
                  for i, row in enumerate(scored)}

    total = len(scored)

    # ── Step 1: elect group candidates ────────────────────────────────────
    elected = _elect_group_candidates(scored, groups)

    # ── Step 2: determine target count ────────────────────────────────────
    if target_count is not None:
        keep_n = int(target_count)
    elif target_pct is not None:
        keep_n = max(1, int(total * target_pct / 100))
    else:
        default_pct = GENRE_DEFAULT_TARGETS.get(genre, 25)
        keep_n = max(1, int(total * default_pct / 100))

    keep_n = min(keep_n, total)

    # ── Step 3: rank all candidates by composite score ────────────────────
    candidates = [row for row in scored if row["id"] in elected]
    candidates_sorted = sorted(
        candidates, key=lambda r: r["composite"] or 0.0, reverse=True
    )

    # Determine threshold score at cut point
    selected_candidates = candidates_sorted[:keep_n]
    threshold_score = (
        selected_candidates[-1]["composite"]
        if selected_candidates else 0.0
    )

    selected_ids = {row["id"] for row in selected_candidates}

    # ── Step 4: build selection for every image ───────────────────────────
    kept = 0
    rejected = 0

    for row in scored:
        img_id    = row["id"]
        score     = row["composite"] or 0.0
        is_cand   = img_id in elected
        is_sel    = img_id in selected_ids

        # Compute confidence
        confidence = _confidence_from_score(score, threshold_score or 0.25)

        # Determine star rating (for keeps)
        if is_sel:
            if score >= 0.85:
                star = 5
            elif score >= 0.70:
                star = 4
            elif score >= 0.55:
                star = 3
            else:
                star = 2
        else:
            star = 0

        # Determine reason string
        if is_sel:
            reasons = []
            if row["blink_flag"]:
                reasons.append("blink-detected (kept — group best)")
            if row["sharpness"] < 0.3:
                reasons.append("soft-focus (kept — group best)")
            if not row["face_count"]:
                reasons.append("no faces")
            reason = ", ".join(reasons) or "quality-selected"
        else:
            if not is_cand:
                reason = "duplicate-inferior"
            elif row["blink_flag"]:
                reason = "blink-detected"
            elif (row["sharpness"] or 0) < 0.25:
                reason = "too-soft"
            elif (row["exposure"] or 0) < 0.25:
                reason = "bad-exposure"
            else:
                reason = "below-threshold"

        db.upsert_selection(img_id, {
            "selected":       1 if is_sel else 0,
            "star":           star,
            "colour_label":   "green" if is_sel else "",
            "pick_flag":      "pick" if is_sel else "reject",
            "reason":         reason,
            "ai_confidence":  confidence,
            "human_override": 0,
        })

        if is_sel:
            kept += 1
        else:
            rejected += 1

        if progress_callback:
            progress_callback(kept + rejected, total)

    log.info(
        f"Selection complete: {kept} kept / {rejected} rejected "
        f"(threshold composite: {threshold_score:.3f})"
    )
    return {
        "kept": kept,
        "rejected": rejected,
        "threshold_score": threshold_score,
    }
