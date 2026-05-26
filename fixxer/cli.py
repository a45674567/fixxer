"""
cli.py — Fixxer command-line interface.

Improvements in this version:
  - Fix #1: DIRECTORY argument now has a full metavar and help string
             so `fixxer cull --help` shows a clear example path.
  - Fix #10: Version stamped into pipeline metadata on every run.
"""

import click
import logging
import sys
import time
from pathlib import Path


def setup_logging(verbose: bool):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        format="%(asctime)s  %(name)-22s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
        level=level,
        stream=sys.stderr,
    )
    for noisy in ["PIL", "mediapipe", "absl", "tensorflow", "cv2"]:
        logging.getLogger(noisy).setLevel(logging.WARNING)


GENRE_CHOICES = [
    "general", "wedding", "portrait", "event",
    "sport", "landscape", "documentary",
]


# ── Root ───────────────────────────────────────────────────────────────────────

@click.group()
@click.version_option("0.1.0", prog_name="fixxer")
def cli():
    """Fixxer — Open-source AI photo culling engine.

    \b
    Typical workflow:
      fixxer cull ~/Shoots/Wedding2024 --genre wedding --target 20
      fixxer review ~/Shoots/Wedding2024
      fixxer export ~/Shoots/Wedding2024

    \b
    Fixxer writes XMP sidecar files (.xmp) next to each RAW file.
    Lightroom and Capture One read these automatically on import.
    """
    pass


# ── cull ───────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument(
    "directory",
    metavar="DIRECTORY",
    type=click.Path(exists=True, file_okay=False, dir_okay=True,
                    readable=True, resolve_path=True),
)  # Fix #1: click.Path with exists=True gives a clear "Path does not exist"
   # error with the actual value shown, far more useful than the old validator.
@click.option("--genre", "-g",
              type=click.Choice(GENRE_CHOICES), default="general", show_default=True,
              help="Shoot genre — adjusts quality scoring weights. "
                   "Use 'wedding' for ceremonies, 'portrait' for studio, "
                   "'landscape' for scenics (disables face scoring).")
@click.option("--target", "-t", type=float, default=None,
              metavar="PCT",
              help="Keep the top PCT% of images. E.g. --target 20 keeps the "
                   "best 20%%. If omitted, uses the genre default.")
@click.option("--count", "-n", type=int, default=None,
              metavar="N",
              help="Keep exactly N images (overrides --target).")
@click.option("--recursive/--no-recursive", default=True, show_default=True,
              help="Scan subdirectories for images.")
@click.option("--workers", "-w", type=int, default=4, show_default=True,
              help="Parallel workers for ingestion (preview extraction).")
@click.option("--score-workers", type=int, default=None,
              help="Parallel workers for scoring. Defaults to CPU count.")
@click.option("--phash-threshold", type=int, default=12, show_default=True,
              help="pHash Hamming distance for duplicate detection (0–64). "
                   "Lower = stricter matching.")
@click.option("--timestamp-window", type=float, default=3.0, show_default=True,
              help="Max seconds between frames to consider as one burst.")
@click.option("--export/--no-export", "do_export", default=True, show_default=True,
              help="Write XMP sidecar files after selection.")
@click.option("--dry-run", is_flag=True, default=False,
              help="Run the full pipeline but write no files to disk. "
                   "Useful for testing settings before committing.")
@click.option("--stages", type=str, default=None,
              metavar="STAGES",
              help="Run only specific pipeline stages, comma-separated. "
                   "E.g. --stages score,select (re-scores and re-selects "
                   "without re-ingesting).")
@click.option("--verbose", "-v", is_flag=True, default=False,
              help="Show debug-level log output.")
def cull(directory, genre, target, count, recursive, workers, score_workers,
         phash_threshold, timestamp_window, do_export, dry_run, stages, verbose):
    """Cull a directory of RAW or JPEG photos.

    \b
    DIRECTORY is the folder containing your shoot files. Examples:
      fixxer cull ~/Desktop/Wedding2024
      fixxer cull /Volumes/SanDisk/Shoot --genre portrait --target 30
      fixxer cull . --genre general --count 100

    \b
    After running, each image will have a .xmp sidecar file containing
    its star rating (2–5 for keeps, 0 for rejects) and pick flag.
    A summary CSV is also written to DIRECTORY/fixxer_selections.csv.
    """
    setup_logging(verbose)

    from .pipeline import Pipeline, PipelineConfig
    from .db import ProjectDB
    from .export import export_selections, export_summary_csv
    from . import __version__

    directory = Path(directory)

    click.echo(f"\n{'─'*52}")
    click.echo(f"  Fixxer {__version__} — photo culling engine")
    click.echo(f"{'─'*52}")
    click.echo(f"  Directory : {directory}")
    click.echo(f"  Genre     : {genre}")
    if count:
        click.echo(f"  Target    : {count} images")
    elif target:
        click.echo(f"  Target    : top {target}%")
    else:
        click.echo(f"  Target    : genre default")
    if dry_run:
        click.echo(f"  ⚠  DRY RUN — no files will be written")
    click.echo(f"{'─'*52}\n")

    run_stages = [s.strip() for s in stages.split(",")] if stages else None

    # ── Progress display ──────────────────────────────────────────────────
    _current_stage = {"name": ""}

    def on_progress(stage, current, total, detail=""):
        if stage != _current_stage["name"]:
            if _current_stage["name"]:
                click.echo()           # newline after previous stage bar
            _current_stage["name"] = stage
            click.echo(f"  ── {stage.upper()}")
        bar_w  = 28
        filled = int(bar_w * current / max(total, 1))
        bar    = "█" * filled + "░" * (bar_w - filled)
        pct    = int(current / max(total, 1) * 100)
        detail_str = f"  {detail[:28]}" if detail else ""
        click.echo(f"\r  [{bar}] {pct:3d}%  {current}/{total}{detail_str}",
                   nl=False)
        sys.stdout.flush()

    config = PipelineConfig(
        directory        = directory,
        genre            = genre,
        target_pct       = target,
        target_count     = count,
        recursive        = recursive,
        ingest_workers   = workers,
        score_workers    = score_workers,
        phash_threshold  = phash_threshold,
        timestamp_window = timestamp_window,
    )

    pipeline = Pipeline(config, on_progress=on_progress)
    result   = pipeline.run(stages=run_stages)
    click.echo("\n")
    click.echo(result.summary())

    if not result.success:
        click.echo("\n  Pipeline encountered errors — see log above.", err=True)
        sys.exit(1)

    # ── XMP export ────────────────────────────────────────────────────────
    if do_export and not dry_run:
        click.echo("\n  ── EXPORT")
        db = ProjectDB(directory)
        with db.connect():
            export_result = export_selections(
                db=db,
                dry_run=dry_run,
                progress_callback=lambda c, t: click.echo(
                    f"\r  Writing XMP: {c}/{t}", nl=False
                ),
            )
            click.echo(
                f"\n  XMP : {export_result['written']} written"
                + (f"  |  {export_result['failed']} failed" if export_result['failed'] else "")
                + (f"  |  {export_result['skipped']} skipped" if export_result['skipped'] else "")
            )
            csv_path = directory / "fixxer_selections.csv"
            if export_summary_csv(db, csv_path):
                click.echo(f"  CSV : {csv_path.name}")

    # ── Final summary ─────────────────────────────────────────────────────
    stats = pipeline.get_stats()
    keep_pct = (stats['kept'] / max(stats['total_images'], 1) * 100)
    click.echo(f"\n{'─'*52}")
    click.echo(f"  ✓  {stats['kept']} kept  ({keep_pct:.0f}%)  |  {stats['rejected']} rejected")
    click.echo(f"     {stats['total_groups']} groups from {stats['total_images']} images")
    if stats.get("avg_confidence"):
        click.echo(f"     Avg AI confidence: {stats['avg_confidence']:.0%}")
    click.echo(f"{'─'*52}\n")


# ── export ─────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument(
    "directory",
    metavar="DIRECTORY",
    type=click.Path(exists=True, file_okay=False, dir_okay=True,
                    readable=True, resolve_path=True),
)
@click.option("--output-dir", type=click.Path(), default=None,
              help="Copy XMP files here instead of adjacent to RAW files.")
@click.option("--dry-run", is_flag=True, default=False,
              help="Show what would be exported without writing.")
@click.option("--csv/--no-csv", "write_csv", default=True, show_default=True)
@click.option("--verbose", "-v", is_flag=True, default=False)
def export(directory, output_dir, dry_run, write_csv, verbose):
    """Write XMP sidecar files from an existing project database.

    \b
    Run this after making review adjustments in `fixxer review` to
    regenerate XMP files with your override decisions applied.

    \b
    Example:
      fixxer export ~/Shoots/Wedding2024
    """
    setup_logging(verbose)
    from .db import ProjectDB
    from .export import export_selections, export_summary_csv

    directory = Path(directory)
    db        = ProjectDB(directory)

    with db.connect():
        if db.image_count() == 0:
            click.echo("No Fixxer project found. Run `fixxer cull` first.")
            sys.exit(1)

        out_dir = Path(output_dir) if output_dir else None
        result  = export_selections(db=db, output_dir=out_dir, dry_run=dry_run)
        click.echo(
            f"XMP: {result['written']} written"
            + (f"  |  {result['failed']} failed"   if result['failed']  else "")
            + (f"  |  {result['skipped']} skipped"  if result['skipped'] else "")
        )

        if write_csv and not dry_run:
            csv_path = (out_dir or directory) / "fixxer_selections.csv"
            if export_summary_csv(db, csv_path):
                click.echo(f"CSV: {csv_path}")


# ── status ─────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument(
    "directory",
    metavar="DIRECTORY",
    type=click.Path(exists=True, file_okay=False, dir_okay=True,
                    readable=True, resolve_path=True),
)
def status(directory):
    """Show current project statistics for a culled shoot.

    \b
    Example:
      fixxer status ~/Shoots/Wedding2024
    """
    from .db import ProjectDB
    import datetime

    directory = Path(directory)
    db        = ProjectDB(directory)

    with db.connect():
        if db.image_count() == 0:
            click.echo("No Fixxer project found. Run `fixxer cull` first.")
            sys.exit(1)

        stats     = db.selection_stats()
        groups    = db.get_groups()
        genre     = db.get_meta("genre", "general")
        version   = db.get_meta("fixxer_version", "unknown")
        last_run  = db.get_meta("last_run")
        lr        = (datetime.datetime.fromtimestamp(last_run).strftime("%Y-%m-%d %H:%M")
                     if last_run else "never")
        total     = db.image_count()
        kept      = stats.get("kept") or 0
        rejected  = stats.get("rejected") or 0
        keep_pct  = kept / max(total, 1) * 100

        click.echo(f"\n  Project  : {directory}")
        click.echo(f"  Genre    : {genre}")
        click.echo(f"  Version  : Fixxer {version}")
        click.echo(f"  Last run : {lr}\n")
        click.echo(f"  Total images : {total}")
        click.echo(f"  Groups       : {len(groups)}")
        click.echo(f"  Kept         : {kept}  ({keep_pct:.0f}%)")
        click.echo(f"  Rejected     : {rejected}")
        if stats.get("avg_conf"):
            click.echo(f"  Avg confidence : {stats['avg_conf']:.0%}")
        click.echo()


# ── review ─────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument(
    "directory",
    metavar="DIRECTORY",
    type=click.Path(exists=True, file_okay=False, dir_okay=True,
                    readable=True, resolve_path=True),
)
@click.option("--port", "-p", type=int, default=7842, show_default=True,
              help="Port for the local review server.")
@click.option("--host", default="127.0.0.1", show_default=True,
              help="Host to bind the server to.")
@click.option("--no-browser", is_flag=True, default=False,
              help="Don't automatically open the browser.")
@click.option("--verbose", "-v", is_flag=True, default=False)
def review(directory, port, host, no_browser, verbose):
    """Launch the local web review UI for a culled shoot.

    \b
    Opens a browser-based interface to inspect, compare, and override
    AI selections. All processing is local — no images leave your machine.

    \b
    Keyboard shortcuts in the review UI:
      K           Keep selected image
      R           Reject selected image
      ← →         Navigate between images in lightbox
      Escape      Close lightbox

    \b
    Example:
      fixxer review ~/Shoots/Wedding2024
      fixxer review ~/Shoots/Wedding2024 --port 8080
    """
    setup_logging(verbose)
    from .ui.server import start_server
    import webbrowser
    import threading

    directory = Path(directory)
    url       = f"http://{host}:{port}"

    click.echo(f"\n  Fixxer Review UI")
    click.echo(f"  Project : {directory}")
    click.echo(f"  URL     : {url}")
    click.echo(f"  Press Ctrl+C to stop\n")

    if not no_browser:
        # Open browser after a short delay to let the server start
        def _open():
            time.sleep(1.2)
            webbrowser.open(url)
        threading.Thread(target=_open, daemon=True).start()

    try:
        start_server(directory=directory, host=host, port=port)
    except KeyboardInterrupt:
        click.echo("\n  Server stopped.")


def main():
    # If called with no arguments, launch the interactive TUI
    if len(sys.argv) == 1:
        from .ui.tui import run_tui
        run_tui()
    else:
        cli()


if __name__ == "__main__":
    main()
