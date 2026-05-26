"""
tui.py — Interactive terminal UI for Fixxer.

Replaces the raw CLI with a friendly guided flow.
Run with: fixxer

No arguments needed. Walks the user through:
  1. Folder selection
  2. Genre selection
  3. Target selection
  4. Live progress display
  5. Results summary
  6. Optional review UI launch
"""

import sys
import os
import time
import threading
import subprocess
from pathlib import Path


# ── Colours (safe on all modern terminals) ────────────────────────────────────
class C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    DIM    = "\033[2m"
    GREEN  = "\033[32m"
    BLUE   = "\033[34m"
    CYAN   = "\033[36m"
    YELLOW = "\033[33m"
    RED    = "\033[31m"
    WHITE  = "\033[97m"
    BG_DARK = "\033[48;5;234m"

def bold(s):   return f"{C.BOLD}{s}{C.RESET}"
def dim(s):    return f"{C.DIM}{s}{C.RESET}"
def green(s):  return f"{C.GREEN}{s}{C.RESET}"
def blue(s):   return f"{C.BLUE}{s}{C.RESET}"
def cyan(s):   return f"{C.CYAN}{s}{C.RESET}"
def yellow(s): return f"{C.YELLOW}{s}{C.RESET}"
def red(s):    return f"{C.RED}{s}{C.RESET}"
def white(s):  return f"{C.WHITE}{s}{C.RESET}"


# ── Layout helpers ────────────────────────────────────────────────────────────

def clear():
    os.system("clear")

def width():
    try:
        return os.get_terminal_size().columns
    except Exception:
        return 80

def rule(char="─"):
    return dim(char * min(width(), 60))

def header():
    clear()
    w = min(width(), 60)
    print()
    print(f"  {bold(white('fix') + cyan('xer'))}  {dim('photo culling engine')}  {dim('v0.1.1')}")
    print(f"  {rule()}")
    print()

def section(title):
    print(f"\n  {cyan('◆')} {bold(title)}\n")

def success(msg):
    print(f"\n  {green('✓')} {msg}")

def warn(msg):
    print(f"\n  {yellow('⚠')}  {msg}")

def error(msg):
    print(f"\n  {red('✗')} {msg}")

def hint(msg):
    print(f"  {dim(msg)}")


# ── Input helpers ─────────────────────────────────────────────────────────────

def prompt(question, default=None):
    """Simple text prompt."""
    suffix = f" {dim(f'[{default}]')}" if default else ""
    try:
        val = input(f"  {cyan('›')} {question}{suffix}: ").strip()
        return val if val else default
    except (KeyboardInterrupt, EOFError):
        print()
        graceful_exit()


def choose(options, labels=None, default=0):
    """
    Numbered menu. Returns the selected value.
    options: list of return values
    labels: list of display strings (defaults to options)
    """
    labels = labels or [str(o) for o in options]
    for i, label in enumerate(labels):
        marker = cyan("›") if i == default else " "
        num    = dim(f"{i+1}.")
        is_def = dim(" (default)") if i == default else ""
        print(f"  {marker} {num} {label}{is_def}")
    print()

    while True:
        try:
            raw = input(f"  {cyan('›')} Choose [{dim('1')}–{dim(str(len(options)))}] or press Enter for default: ").strip()
        except (KeyboardInterrupt, EOFError):
            print()
            graceful_exit()

        if raw == "":
            return options[default]
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(options):
                return options[idx]
        print(f"  {red('Please enter a number between 1 and ' + str(len(options)))}")


def graceful_exit():
    print(f"\n  {dim('Cancelled. Goodbye.')}\n")
    sys.exit(0)


# ── Folder picker ─────────────────────────────────────────────────────────────

def pick_folder():
    section("Select your shoot folder")
    hint("Tip: drag the folder from Finder straight into this window, then press Enter.")
    hint("Or type the path manually.")
    print()

    while True:
        raw = prompt("Folder path")
        if not raw:
            error("No folder entered. Try again.")
            continue

        # Strip quotes that drag-and-drop sometimes adds
        raw = raw.strip("'\"").strip()
        path = Path(raw).expanduser().resolve()

        if not path.exists():
            error(f"Folder not found: {path}")
            hint("Make sure the files are downloaded locally (not cloud-only).")
            print()
            continue

        if not path.is_dir():
            error("That's a file, not a folder. Try again.")
            print()
            continue

        # Count supported files
        exts = {
            ".cr2",".cr3",".nef",".nrw",".arw",".raf",".orf",
            ".rw2",".dng",".jpg",".jpeg",".pef",".x3f",".mrw"
        }
        files = [f for f in path.rglob("*")
                 if f.suffix.lower() in exts
                 and ".fixxer_previews" not in str(f)]

        if not files:
            error(f"No RAW or JPEG files found in: {path.name}")
            hint("Check the folder and make sure files are downloaded from iCloud/OneDrive.")
            print()
            continue

        success(f"{len(files)} images found in  {bold(path.name)}")
        return path, len(files)


# ── Genre picker ──────────────────────────────────────────────────────────────

GENRES = [
    ("wedding",     "Wedding",     "Prioritises open eyes, emotion, expression"),
    ("portrait",    "Portrait",    "Maximum sharpness and eye-state accuracy"),
    ("event",       "Event",       "Balanced — good for parties, corporate, sport"),
    ("sport",       "Sport",       "High sharpness, tolerates motion blur"),
    ("landscape",   "Landscape",   "Sharpness and exposure — ignores faces"),
    ("documentary", "Documentary", "Tolerates motion blur, values emotion"),
    ("general",     "General",     "Balanced defaults — good starting point"),
]

def pick_genre():
    section("Select shoot genre")
    hint("This adjusts how Fixxer weights sharpness, blink detection, and expression.")
    print()

    labels = [f"{bold(g[1]):<22} {dim(g[2])}" for g in GENRES]
    choice = choose(
        options=[g[0] for g in GENRES],
        labels=labels,
        default=6,  # general
    )
    selected = next(g for g in GENRES if g[0] == choice)
    success(f"Genre set to  {bold(selected[1])}")
    return choice


# ── Target picker ─────────────────────────────────────────────────────────────

TARGETS = [
    (10,  "10%  — very selective  (e.g. 200 from 2,000)"),
    (15,  "15%  — selective"),
    (20,  "20%  — recommended for most shoots"),
    (25,  "25%  — generous"),
    (30,  "30%  — keep more, review less"),
    (40,  "40%  — light cull only"),
    (None,"Custom number of images"),
]

def pick_target(total_images):
    section("How many photos to keep?")
    hint(f"Your shoot has {bold(str(total_images))} images.")
    print()

    labels = []
    for pct, label in TARGETS:
        if pct is not None:
            count = int(total_images * pct / 100)
            labels.append(f"{label}  {dim('≈ ' + str(count) + ' images')}")
        else:
            labels.append(label)

    choice = choose(
        options=[t[0] for t in TARGETS],
        labels=labels,
        default=2,  # 20%
    )

    if choice is None:
        while True:
            raw = prompt(f"How many images to keep (1–{total_images})")
            if raw and raw.isdigit() and 1 <= int(raw) <= total_images:
                count = int(raw)
                success(f"Will keep  {bold(str(count))}  images")
                return None, count
            error("Please enter a valid number.")
    else:
        count = int(total_images * choice / 100)
        success(f"Will keep top  {bold(str(choice) + '%')}  ≈ {bold(str(count))}  images")
        return choice, None


# ── Confirmation screen ───────────────────────────────────────────────────────

def confirm(folder, genre, target_pct, target_count, total_images):
    section("Ready to cull")

    keep = target_count if target_count else int(total_images * target_pct / 100)

    print(f"  {'Folder':<12} {bold(folder.name)}")
    print(f"  {'Path':<12} {dim(str(folder))}")
    print(f"  {'Images':<12} {bold(str(total_images))}")
    print(f"  {'Genre':<12} {bold(genre.title())}")
    print(f"  {'Keep':<12} {bold(str(keep))} images ({bold(str(target_pct) + '%') if target_pct else 'custom'})")
    print()

    hint("Press Enter to start, or Ctrl+C to cancel.")
    try:
        input(f"  {cyan('›')} Start culling? ")
    except (KeyboardInterrupt, EOFError):
        print()
        graceful_exit()


# ── Live progress display ─────────────────────────────────────────────────────

_stage_labels = {
    "ingest":  "Extracting previews",
    "cluster": "Grouping bursts",
    "score":   "Scoring images",
    "select":  "Selecting keepers",
}

_stage_order = ["ingest", "cluster", "score", "select"]

def run_with_progress(folder, genre, target_pct, target_count):
    """Run the pipeline with a live progress display."""
    from fixxer.pipeline import Pipeline, PipelineConfig
    from fixxer.db import ProjectDB
    from fixxer.export import export_selections, export_summary_csv

    section("Culling in progress")

    stage_times = {}
    current = {"stage": None, "current": 0, "total": 0, "detail": ""}
    done    = {"flag": False}

    def on_progress(stage, c, t, detail=""):
        current["stage"]   = stage
        current["current"] = c
        current["total"]   = t
        current["detail"]  = detail or ""

    def render_loop():
        """Redraws progress line at ~10fps while pipeline runs."""
        completed_stages = []
        last_stage = None

        while not done["flag"]:
            stage   = current["stage"]
            c       = current["current"]
            t       = max(current["total"], 1)
            detail  = current["detail"]

            if stage and stage != last_stage:
                if last_stage and last_stage not in completed_stages:
                    elapsed = time.time() - stage_times.get(last_stage, time.time())
                    sys.stdout.write(f"\r  {green('✓')} {_stage_labels.get(last_stage, last_stage):<24} {dim('done')}\n")
                    sys.stdout.flush()
                    completed_stages.append(last_stage)
                stage_times[stage] = time.time()
                last_stage = stage

            if stage and stage not in completed_stages:
                pct     = int(c / t * 100)
                bar_w   = 20
                filled  = int(bar_w * c / t)
                bar     = green("█" * filled) + dim("░" * (bar_w - filled))
                label   = _stage_labels.get(stage, stage)
                det     = f"  {dim(detail[:20])}" if detail else ""
                line    = f"\r  {cyan('◌')} {label:<24} [{bar}] {pct:>3}%{det}"
                sys.stdout.write(line)
                sys.stdout.flush()

            time.sleep(0.1)

        # Mark final stage done
        if last_stage and last_stage not in completed_stages:
            sys.stdout.write(f"\r  {green('✓')} {_stage_labels.get(last_stage, last_stage):<24} {dim('done')}\n")
            sys.stdout.flush()

    # Start render thread
    render_thread = threading.Thread(target=render_loop, daemon=True)
    render_thread.start()

    t_start = time.time()

    config = PipelineConfig(
        directory        = folder,
        genre            = genre,
        target_pct       = float(target_pct) if target_pct else None,
        target_count     = int(target_count) if target_count else None,
        score_workers    = None,  # auto
    )

    pipeline = Pipeline(config, on_progress=on_progress)
    result   = pipeline.run()

    done["flag"] = True
    render_thread.join(timeout=0.5)

    elapsed = time.time() - t_start

    if not result.success:
        failed = [s for s in result.stages if not s.success]
        error(f"Pipeline failed at stage: {failed[0].name}")
        hint(failed[0].error)
        return None, None

    # Export XMP
    sys.stdout.write(f"  {cyan('◌')} Writing XMP sidecars       ")
    sys.stdout.flush()
    db = ProjectDB(folder)
    with db.connect():
        export_result = export_selections(db=db)
        csv_path = folder / "fixxer_selections.csv"
        export_summary_csv(db, csv_path)
    sys.stdout.write(f"\r  {green('✓')} Writing XMP sidecars       {dim('done')}\n")
    sys.stdout.flush()

    return pipeline.get_stats(), elapsed


# ── Results screen ────────────────────────────────────────────────────────────

def show_results(stats, elapsed, folder):
    section("Results")

    kept      = stats.get("kept", 0)
    rejected  = stats.get("rejected", 0)
    total     = stats.get("total_images", 0)
    groups    = stats.get("total_groups", 0)
    conf      = stats.get("avg_confidence", 0)
    keep_pct  = int(kept / max(total, 1) * 100)

    mins = int(elapsed // 60)
    secs = int(elapsed % 60)
    time_str = f"{mins}m {secs}s" if mins else f"{secs}s"

    print(f"  {'Kept':<16} {green(bold(str(kept)))}  {dim(f'({keep_pct}%)')}")
    print(f"  {'Rejected':<16} {dim(str(rejected))}")
    print(f"  {'Burst groups':<16} {dim(str(groups))}")
    print(f"  {'AI confidence':<16} {dim(f'{conf:.0%}')}")
    print(f"  {'Time taken':<16} {dim(time_str)}")
    print()
    print(f"  {green('✓')} XMP files written next to each image")
    print(f"  {green('✓')} CSV report saved to  {dim('fixxer_selections.csv')}")
    print()
    hint("In Lightroom: import the folder, then filter by ★★★+ or Flagged.")
    hint("In Capture One: the ratings will load automatically on import.")


# ── Review UI prompt ──────────────────────────────────────────────────────────

def offer_review(folder):
    print()
    print(f"  {rule()}")
    print()
    print(f"  {bold('Want to review selections in your browser?')}")
    print()
    print(f"  {dim('1.')} {bold('Yes')}  — open the review UI now")
    print(f"  {dim('2.')} {bold('No')}   — I'll import directly into Lightroom/Capture One")
    print()

    try:
        raw = input(f"  {cyan('›')} Choice [1/2]: ").strip()
    except (KeyboardInterrupt, EOFError):
        print()
        return

    if raw == "1" or raw == "":
        launch_review(folder)


def launch_review(folder):
    import webbrowser

    print()
    print(f"  {cyan('◌')} Starting review server...")

    proc = subprocess.Popen(
        [sys.executable, "-m", "fixxer.cli", "review", str(folder),
         "--no-browser"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    time.sleep(1.5)
    webbrowser.open("http://127.0.0.1:7842")

    print(f"  {green('✓')} Review UI open at  {bold('http://127.0.0.1:7842')}")
    print()
    hint("Keyboard shortcuts: K = keep   R = reject   ← → = navigate   Esc = close")
    hint("Click 'Export XMP' in the browser when you're done reviewing.")
    print()
    print(f"  {rule()}")
    print()
    hint("Press Ctrl+C here to stop the review server when finished.")
    print()

    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        print(f"\n  {dim('Review server stopped.')}")

    print()


# ── Main entry point ──────────────────────────────────────────────────────────

def run_tui():
    """Full interactive guided flow."""
    try:
        header()

        # Step 1: folder
        folder, total = pick_folder()

        # Step 2: genre
        header()
        genre = pick_genre()

        # Step 3: target
        header()
        target_pct, target_count = pick_target(total)

        # Step 4: confirm
        header()
        confirm(folder, genre, target_pct, target_count, total)

        # Step 5: run
        header()
        stats, elapsed = run_with_progress(folder, genre, target_pct, target_count)

        if stats is None:
            sys.exit(1)

        # Step 6: results
        show_results(stats, elapsed, folder)

        # Step 7: offer review
        offer_review(folder)

        print(f"  {dim('Done. Have a great edit.')}\n")

    except KeyboardInterrupt:
        print(f"\n\n  {dim('Cancelled.')}\n")
        sys.exit(0)
