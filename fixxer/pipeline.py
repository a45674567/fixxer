"""
pipeline.py — Main pipeline orchestrator.

Improvements in this version:
  - Fix #8: Proper resume — each stage checks for existing work before
             processing. Scoring skips already-scored images. Clustering
             only rebuilds if image count has changed. Version is stamped
             into project metadata on every run.
  - Fix #10: Fixxer version is written to .fixxer.db metadata on every run,
              so you always know which version processed a given shoot.
"""

import time
import logging
import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional, Callable

from . import __version__
from .db import ProjectDB
from .ingestion import ingest_directory
from .clustering import run_clustering
from .scoring import run_scoring
from .selection import run_selection

log = logging.getLogger("fixxer.pipeline")


@dataclass
class PipelineConfig:
    directory:        Path
    genre:            str   = "general"
    target_pct:       Optional[float] = None
    target_count:     Optional[int]   = None
    recursive:        bool  = True
    ingest_workers:   int   = 4
    score_workers:    Optional[int] = None   # None = auto (CPU count)
    phash_threshold:  int   = 12
    timestamp_window: float = 3.0


@dataclass
class StageResult:
    name:    str
    success: bool
    count:   int   = 0
    elapsed: float = 0.0
    skipped: int   = 0        # items skipped because already done
    error:   str   = ""


@dataclass
class PipelineResult:
    stages:        List[StageResult] = field(default_factory=list)
    total_elapsed: float = 0.0

    @property
    def success(self):
        return all(s.success for s in self.stages)

    def summary(self) -> str:
        lines = [f"── Fixxer {__version__} Pipeline Results ───────────────────"]
        for s in self.stages:
            status  = "✓" if s.success else "✗"
            skipped = f"  ({s.skipped} already done)" if s.skipped else ""
            lines.append(
                f"  {status} {s.name:<16} {s.count:>5} items  "
                f"({s.elapsed:.1f}s){skipped}"
            )
            if s.error:
                lines.append(f"      Error: {s.error}")
        lines.append(f"  Total time: {self.total_elapsed:.1f}s")
        return "\n".join(lines)


class Pipeline:
    """
    Main Fixxer pipeline.

    Usage:
        config = PipelineConfig(directory=Path("/Volumes/Photos/Wedding2024"))
        result = Pipeline(config).run()
        print(result.summary())
    """

    def __init__(self, config: PipelineConfig,
                 on_progress: Optional[Callable] = None):
        self.config      = config
        self.on_progress = on_progress
        self.db          = ProjectDB(config.directory)

    def _progress(self, stage: str, current: int, total: int, detail: str = ""):
        if self.on_progress:
            self.on_progress(stage, current, total, detail)

    def run(self, stages: List[str] = None) -> PipelineResult:
        """Run the full pipeline or a named subset of stages."""
        all_stages = ["ingest", "cluster", "score", "select"]
        run_stages = stages or all_stages

        result        = PipelineResult()
        t_total_start = time.time()

        with self.db.connect():
            # Fix #10: stamp version + run time into project metadata
            self.db.set_meta("fixxer_version", __version__)
            self.db.set_meta("genre",          self.config.genre)
            self.db.set_meta("directory",      str(self.config.directory))
            self.db.set_meta("last_run",       time.time())

            for stage_name in run_stages:
                if stage_name not in all_stages:
                    log.warning(f"Unknown stage: {stage_name}, skipping")
                    continue

                t_start = time.time()
                log.info(f"── Stage: {stage_name.upper()} ──")

                try:
                    count, skipped = self._run_stage(stage_name)
                    elapsed        = time.time() - t_start
                    result.stages.append(StageResult(
                        name=stage_name, success=True,
                        count=count, elapsed=elapsed, skipped=skipped,
                    ))
                    log.info(
                        f"Stage {stage_name}: {count} processed, "
                        f"{skipped} skipped, {elapsed:.1f}s"
                    )
                except Exception as e:
                    elapsed = time.time() - t_start
                    log.error(f"Stage {stage_name} failed: {e}", exc_info=True)
                    result.stages.append(StageResult(
                        name=stage_name, success=False,
                        elapsed=elapsed, error=str(e),
                    ))
                    break

        result.total_elapsed = time.time() - t_total_start
        return result

    def _run_stage(self, stage: str):
        """
        Run one stage. Returns (processed_count, skipped_count).

        Fix #8: Each stage checks existing DB state and skips already-
        processed work, enabling true resume after interruption.
        """
        cfg = self.config

        if stage == "ingest":
            count = ingest_directory(
                directory=cfg.directory,
                db=self.db,
                recursive=cfg.recursive,
                workers=cfg.ingest_workers,
                progress_callback=lambda c, t, n: self._progress("ingest", c, t, n),
            )
            total   = self.db.image_count()
            skipped = total - count
            return count, skipped

        elif stage == "cluster":
            # Fix #8: Only re-cluster if image count changed since last cluster
            last_clustered = self.db.get_meta("last_clustered_image_count", 0)
            current_count  = self.db.image_count()
            if last_clustered == current_count and self.db.get_groups():
                log.info(f"Cluster: image count unchanged ({current_count}), skipping rebuild")
                return 0, current_count
            count = run_clustering(
                db=self.db,
                phash_threshold=cfg.phash_threshold,
                timestamp_window=cfg.timestamp_window,
                progress_callback=lambda c, t: self._progress("cluster", c, t),
            )
            self.db.set_meta("last_clustered_image_count", current_count)
            return count, 0

        elif stage == "score":
            # unscored_images() already filters — this is inherently resumable
            unscored_before = len(self.db.unscored_images())
            count = run_scoring(
                db=self.db,
                genre=cfg.genre,
                workers=cfg.score_workers,
                progress_callback=lambda c, t, n: self._progress("score", c, t, n),
            )
            skipped = unscored_before - count
            return count, max(0, self.db.image_count() - unscored_before)

        elif stage == "select":
            result  = run_selection(
                db=self.db,
                genre=cfg.genre,
                target_pct=cfg.target_pct,
                target_count=cfg.target_count,
                progress_callback=lambda c, t: self._progress("select", c, t),
            )
            return result.get("kept", 0), 0

        return 0, 0

    def get_stats(self) -> dict:
        """Return current project statistics."""
        with self.db.connect():
            img_count = self.db.image_count()
            sel_stats = self.db.selection_stats()
            groups    = self.db.get_groups()
            return {
                "total_images":    img_count,
                "total_groups":    len(groups),
                "kept":            sel_stats.get("kept", 0),
                "rejected":        sel_stats.get("rejected", 0),
                "avg_confidence":  sel_stats.get("avg_conf", 0),
                "genre":           self.db.get_meta("genre", "general"),
                "directory":       self.db.get_meta("directory", ""),
                "fixxer_version":  self.db.get_meta("fixxer_version", "unknown"),
            }
