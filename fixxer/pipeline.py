"""
pipeline.py — Main pipeline orchestrator.

Coordinates the four stages:
  1. Ingestion   — scan files, extract previews + EXIF
  2. Clustering  — group duplicates/bursts by time + pHash
  3. Scoring     — IQA: sharpness, exposure, face/eye state
  4. Selection   — rank and apply keep/reject decisions
  (5. Export)    — write XMP sidecars (called separately)

Each stage is idempotent: re-running picks up where it left off.
The pipeline can be interrupted and resumed at any point.
"""

import time
import logging
from pathlib import Path
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from .db import ProjectDB
from .ingestion import ingest_directory
from .clustering import run_clustering
from .scoring import run_scoring
from .selection import run_selection

log = logging.getLogger("fixxer.pipeline")


@dataclass
class PipelineConfig:
    directory: Path
    genre: str = "general"
    target_pct: Optional[float] = None      # e.g. 20.0 for top 20%
    target_count: Optional[int] = None      # e.g. 400 absolute images
    recursive: bool = True
    ingest_workers: int = 4
    phash_threshold: int = 12
    timestamp_window: float = 3.0


@dataclass
class StageResult:
    name: str
    success: bool
    count: int = 0
    elapsed: float = 0.0
    error: str = ""


@dataclass
class PipelineResult:
    stages: List[StageResult] = field(default_factory=list)
    total_elapsed: float = 0.0

    @property
    def success(self):
        return all(s.success for s in self.stages)

    def summary(self) -> str:
        lines = ["── Fixxer Pipeline Results ──────────────────"]
        for s in self.stages:
            status = "✓" if s.success else "✗"
            lines.append(
                f"  {status} {s.name:<16} {s.count:>5} items  "
                f"({s.elapsed:.1f}s)"
            )
            if s.error:
                lines.append(f"      Error: {s.error}")
        lines.append(f"  Total time: {self.total_elapsed:.1f}s")
        return "\n".join(lines)


class Pipeline:
    """
    Main Fixxer pipeline. Instantiate, configure, run.

    Usage:
        config = PipelineConfig(directory=Path("/Volumes/Photos/Wedding2024"))
        pipeline = Pipeline(config)
        result = pipeline.run()
        print(result.summary())
    """

    def __init__(self, config: PipelineConfig,
                 on_progress: Optional[Callable] = None):
        self.config = config
        self.on_progress = on_progress  # Callback(stage, current, total, detail)
        self.db = ProjectDB(config.directory)

    def _progress(self, stage: str, current: int, total: int, detail: str = ""):
        if self.on_progress:
            self.on_progress(stage, current, total, detail)

    def run(self, stages: List[str] = None) -> PipelineResult:
        """
        Run the full pipeline (or a subset of stages).

        stages: list of stage names to run. If None, runs all.
                Valid values: ["ingest", "cluster", "score", "select"]
        """
        all_stages = ["ingest", "cluster", "score", "select"]
        run_stages = stages or all_stages

        result = PipelineResult()
        t_total_start = time.time()

        with self.db.connect():
            # Persist config to project meta
            self.db.set_meta("genre", self.config.genre)
            self.db.set_meta("directory", str(self.config.directory))
            self.db.set_meta("last_run", time.time())

            for stage_name in run_stages:
                if stage_name not in all_stages:
                    log.warning(f"Unknown stage: {stage_name}, skipping")
                    continue

                t_start = time.time()
                log.info(f"── Stage: {stage_name.upper()} ──")

                try:
                    count = self._run_stage(stage_name)
                    elapsed = time.time() - t_start
                    result.stages.append(StageResult(
                        name=stage_name, success=True,
                        count=count, elapsed=elapsed
                    ))
                    log.info(f"Stage {stage_name} complete: {count} items in {elapsed:.1f}s")

                except Exception as e:
                    elapsed = time.time() - t_start
                    log.error(f"Stage {stage_name} failed: {e}", exc_info=True)
                    result.stages.append(StageResult(
                        name=stage_name, success=False,
                        elapsed=elapsed, error=str(e)
                    ))
                    break  # Don't run downstream stages after failure

        result.total_elapsed = time.time() - t_total_start
        return result

    def _run_stage(self, stage: str) -> int:
        cfg = self.config

        if stage == "ingest":
            return ingest_directory(
                directory=cfg.directory,
                db=self.db,
                recursive=cfg.recursive,
                workers=cfg.ingest_workers,
                progress_callback=lambda c, t, n: self._progress("ingest", c, t, n),
            )

        elif stage == "cluster":
            return run_clustering(
                db=self.db,
                phash_threshold=cfg.phash_threshold,
                timestamp_window=cfg.timestamp_window,
                progress_callback=lambda c, t: self._progress("cluster", c, t),
            )

        elif stage == "score":
            return run_scoring(
                db=self.db,
                genre=cfg.genre,
                progress_callback=lambda c, t, n: self._progress("score", c, t, n),
            )

        elif stage == "select":
            result = run_selection(
                db=self.db,
                genre=cfg.genre,
                target_pct=cfg.target_pct,
                target_count=cfg.target_count,
                progress_callback=lambda c, t: self._progress("select", c, t),
            )
            return result.get("kept", 0)

        return 0

    def get_stats(self) -> dict:
        """Return current project statistics from DB."""
        with self.db.connect():
            img_count = self.db.image_count()
            sel_stats = self.db.selection_stats()
            groups = self.db.get_groups()
            return {
                "total_images": img_count,
                "total_groups": len(groups),
                "kept": sel_stats.get("kept", 0),
                "rejected": sel_stats.get("rejected", 0),
                "avg_confidence": sel_stats.get("avg_conf", 0),
                "genre": self.db.get_meta("genre", "general"),
                "directory": self.db.get_meta("directory", ""),
            }
