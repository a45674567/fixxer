"""
cli.py — Fixxer command-line interface.

Commands:
  fixxer cull    — Run full pipeline on a directory
  fixxer export  — Write XMP sidecars from existing project
  fixxer status  — Show project stats
  fixxer review  — Launch local web review UI
"""

import click
import logging
import sys
import time
from pathlib import Path

# ── Logging setup ─────────────────────────────────────────────────────────────
def setup_logging(verbose: bool):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        format="%(asctime)s  %(name)-20s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
        level=level,
        stream=sys.stderr,
    )
    # Quiet noisy third-party loggers
    for noisy in ["PIL", "mediapipe", "absl", "tensorflow"]:
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ── Shared options ─────────────────────────────────────────────────────────────
GENRE_CHOICES = [
    "general", "wedding", "portrait", "event",
    "sport", "landscape", "documentary"
]


def validate_directory(ctx, param, value):
    p = Path(value)
    if not p.exists():
        raise click.BadParameter(f"Directory does not exist: {value}")
    if not p.is_dir():
        raise click.BadParameter(f"Not a directory: {value}")
    return p


# ── CLI root ───────────────────────────────────────────────────────────────────

@click.group()
@click.version_option("0.1.0", prog_name="fixxer")
def cli():
    """Fixxer — Open-source AI photo culling engine.

    \b
    Typical workflow:
      fixxer cull /path/to/shoot --genre wedding --target 20
      fixxer export /path/to/shoot
      fixxer review /path/to/shoot
    """
    pass


# ── cull ───────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("directory", callback=validate_directory)
@click.option("--genre", "-g", type=click.Choice(GENRE_CHOICES),
              default="general", show_default=True,
              help="Shoot genre — adjusts quality scoring weights")
@click.option("--target", "-t", type=float, default=None,
              help="Target keep percentage (e.g. 20 = keep top 20%)")
@click.option("--count", "-n", type=int, default=None,
              help="Target keep count (absolute number of images)")
@click.option("--recursive/--no-recursive", default=True, show_default=True,
              help="Scan subdirectories")
@click.option("--workers", "-w", type=int, default=4, show_default=True,
              help="Parallel workers for ingestion")
@click.option("--phash-threshold", type=int, default=12, show_default=True,
              help="pHash Hamming distance threshold for duplicate detection (0–64)")
@click.option("--timestamp-window", type=float, default=3.0, show_default=True,
              help="Seconds between frames to consider as burst")
@click.option("--export/--no-export", "do_export", default=True, show_default=True,
              help="Write XMP sidecar files after selection")
@click.option("--dry-run", is_flag=True, default=False,
              help="Run pipeline but don't write any files")
@click.option("--stages", type=str, default=None,
              help="Comma-separated stages to run: ingest,cluster,score,select")
@click.option("--verbose", "-v", is_flag=True, default=False)
def cull(directory, genre, target, count, recursive, workers, phash_threshold,
         timestamp_window, do_export, dry_run, stages, verbose):
    """Run the full culling pipeline on a directory of RAW/JPEG files.

    \b
    Example:
      fixxer cull ~/Shoots/Wedding2024 --genre wedding --target 20
    """
    setup_logging(verbose)
    log = logging.getLogger("fixxer.cli")

    from .pipeline import Pipeline, PipelineConfig
    from .db import ProjectDB
    from .export import export_selections, export_summary_csv

    click.echo(f"\n{'─'*50}")
    click.echo(f"  Fixxer 0.1.0 — photo culling engine")
    click.echo(f"{'─'*50}")
    click.echo(f"  Directory : {directory}")
    click.echo(f"  Genre     : {genre}")
    if count:
        click.echo(f"  Target    : {count} images")
    elif target:
        click.echo(f"  Target    : top {target}%")
    else:
        click.echo(f"  Target    : genre default")
    if dry_run:
        click.echo(f"  Mode      : DRY RUN (no files written)")
    click.echo(f"{'─'*50}\n")

    run_stages = stages.split(",") if stages else None

    # Progress display
    current_stage = {"name": "", "bar": None}

    def on_progress(stage, current, total, detail=""):
        if stage != current_stage["name"]:
            if current_stage["bar"]:
                current_stage["bar"].finish()
            current_stage["name"] = stage
            click.echo(f"\n  ── {stage.upper()}")
        pct = int(current / max(total, 1) * 100)
        bar_w = 30
        filled = int(bar_w * current / max(total, 1))
        bar = "█" * filled + "░" * (bar_w - filled)
        detail_str = f" {detail[:30]}" if detail else ""
        click.echo(
            f"\r  [{bar}] {pct:3d}%  {current}/{total}{detail_str}",
            nl=False
        )
        sys.stdout.flush()

    config = PipelineConfig(
        directory=directory,
        genre=genre,
        target_pct=target,
        target_count=count,
        recursive=recursive,
        ingest_workers=workers,
        phash_threshold=phash_threshold,
        timestamp_window=timestamp_window,
    )

    pipeline = Pipeline(config, on_progress=on_progress)
    result = pipeline.run(stages=run_stages)

    click.echo("\n")
    click.echo(result.summary())

    if not result.success:
        click.echo("\n  Pipeline encountered errors. See log above.", err=True)
        sys.exit(1)

    # ── Export XMP ────────────────────────────────────────────────────────
    if do_export and not dry_run:
        click.echo("\n  ── EXPORT (XMP sidecars)")
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
                f"\n  XMP: {export_result['written']} written, "
                f"{export_result['failed']} failed"
            )

            # Always write summary CSV
            csv_path = directory / "fixxer_selections.csv"
            if export_summary_csv(db, csv_path):
                click.echo(f"  CSV: {csv_path}")

    # ── Final stats ───────────────────────────────────────────────────────
    stats = pipeline.get_stats()
    click.echo(f"\n{'─'*50}")
    click.echo(f"  ✓ {stats['kept']} images kept  |  {stats['rejected']} rejected")
    click.echo(
        f"  {stats['total_groups']} groups from {stats['total_images']} images"
    )
    if stats.get("avg_confidence"):
        click.echo(f"  Average AI confidence: {stats['avg_confidence']:.1%}")
    click.echo(f"{'─'*50}\n")


# ── export ─────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("directory", callback=validate_directory)
@click.option("--output-dir", type=Path, default=None,
              help="Copy XMP files here instead of adjacent to RAW files")
@click.option("--dry-run", is_flag=True, default=False)
@click.option("--csv/--no-csv", "write_csv", default=True, show_default=True,
              help="Also write a CSV summary")
@click.option("--verbose", "-v", is_flag=True, default=False)
def export(directory, output_dir, dry_run, write_csv, verbose):
    """Write XMP sidecar files from an existing project database.

    Run this after making review adjustments to regenerate XMP files.
    """
    setup_logging(verbose)
    from .db import ProjectDB
    from .export import export_selections, export_summary_csv

    db = ProjectDB(directory)
    with db.connect():
        if db.image_count() == 0:
            click.echo("No project found in this directory. Run `fixxer cull` first.")
            sys.exit(1)

        result = export_selections(db=db, output_dir=output_dir, dry_run=dry_run)
        click.echo(
            f"XMP: {result['written']} written  |  "
            f"{result['failed']} failed  |  {result['skipped']} skipped"
        )

        if write_csv and not dry_run:
            csv_path = (output_dir or directory) / "fixxer_selections.csv"
            if export_summary_csv(db, csv_path):
                click.echo(f"CSV: {csv_path}")


# ── status ─────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("directory", callback=validate_directory)
def status(directory):
    """Show current project statistics."""
    from .db import ProjectDB

    db = ProjectDB(directory)
    with db.connect():
        if db.image_count() == 0:
            click.echo("No Fixxer project found. Run `fixxer cull` first.")
            sys.exit(1)

        stats = db.selection_stats()
        groups = db.get_groups()
        genre = db.get_meta("genre", "general")
        last_run = db.get_meta("last_run")

        import datetime
        lr = datetime.datetime.fromtimestamp(last_run).strftime("%Y-%m-%d %H:%M") if last_run else "never"

        click.echo(f"\n  Project: {directory}")
        click.echo(f"  Genre:   {genre}")
        click.echo(f"  Last run: {lr}\n")
        click.echo(f"  Total images : {db.image_count()}")
        click.echo(f"  Groups       : {len(groups)}")
        click.echo(f"  Kept         : {stats.get('kept', 0)}")
        click.echo(f"  Rejected     : {stats.get('rejected', 0)}")
        if stats.get("avg_conf"):
            click.echo(f"  Avg confidence: {stats['avg_conf']:.1%}")
        click.echo()


# ── review ─────────────────────────────────────────────────────────────────────

@cli.command()
@click.argument("directory", callback=validate_directory)
@click.option("--port", "-p", type=int, default=7842, show_default=True)
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--verbose", "-v", is_flag=True, default=False)
def review(directory, port, host, verbose):
    """Launch the local web review UI for a project.

    Opens a browser-based interface to inspect and override AI selections.
    All processing happens locally — no images leave your machine.
    """
    setup_logging(verbose)
    from .ui.server import start_server
    click.echo(f"\n  Starting Fixxer review UI at http://{host}:{port}")
    click.echo(f"  Project: {directory}")
    click.echo(f"  Press Ctrl+C to stop\n")
    try:
        start_server(directory=directory, host=host, port=port)
    except KeyboardInterrupt:
        click.echo("\n  Server stopped.")


# Entry point
def main():
    cli()


if __name__ == "__main__":
    main()
