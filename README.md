# Fixxer

**Open-source AI photo culling engine — rebuilt from first principles.**

Fixxer automates the triage step between capture and delivery. Given a directory of RAW or JPEG files, it produces XMP sidecar annotations (star ratings, colour labels, pick flags) that Lightroom and Capture One read natively — with no subscription, no cloud upload, and no black box.

---

## What it does

1. **Scans** your shoot directory, extracting embedded JPEG previews from RAW files without a full decode (~10–50× faster than postprocessing)
2. **Groups** burst sequences using timestamp + perceptual hash clustering — compressing the decision space from N images to N/3–5 groups
3. **Scores** each image across sharpness (Laplacian variance), exposure (histogram analysis), and eye-state / face presence (OpenCV Haar cascades)
4. **Selects** the top N% using genre-calibrated composite scoring with within-group winner election
5. **Exports** XMP sidecar files adjacent to each RAW — ready to import into Lightroom or Capture One

Everything runs locally. No images leave your machine.

---

## Install

```bash
pip install fixxer
```

**Dependencies installed automatically:** `rawpy`, `Pillow`, `imagehash`, `numpy`, `scipy`, `opencv-python-headless`, `click`, `flask`

**System requirement:** `exiftool` for EXIF extraction  
macOS: `brew install exiftool`  
Ubuntu/Debian: `apt install libimage-exiftool-perl`

---

## Usage

### CLI — full pipeline

```bash
# Cull a wedding shoot, keep top 20%
fixxer cull /Volumes/Photos/Wedding_2024 --genre wedding --target 20

# Portrait session — keep top 30%, absolute count
fixxer cull ~/Shoots/Portraits_June --genre portrait --count 80

# Preview what would be selected without writing anything
fixxer cull ~/Shoots/Event --dry-run

# Check project status after a run
fixxer status /Volumes/Photos/Wedding_2024

# Re-export XMP after reviewing in the web UI
fixxer export /Volumes/Photos/Wedding_2024
```

### Web review UI

```bash
fixxer review /Volumes/Photos/Wedding_2024
# Opens at http://localhost:7842
```

The review UI shows all AI selections in a grid. Click any image to open the lightbox. Press **K** to keep, **R** to reject. Every override is logged as a correction event for future personalisation. Hit **Export XMP** when done.

### Python API

```python
from pathlib import Path
from fixxer.pipeline import Pipeline, PipelineConfig

config = PipelineConfig(
    directory=Path("/Volumes/Photos/Wedding_2024"),
    genre="wedding",
    target_pct=20.0,
)

pipeline = Pipeline(config)
result = pipeline.run()
print(result.summary())
# ── Fixxer Pipeline Results ──
#   ✓ ingest         2347 items  (18.2s)
#   ✓ cluster        2347 items  (4.1s)
#   ✓ score          2347 items  (94.3s)
#   ✓ select          469 items  (0.2s)
#   Total time: 116.8s
```

Run specific stages only:

```python
result = pipeline.run(stages=["score", "select"])  # Re-score and re-select only
```

---

## Genres

Genre selection adjusts quality score weights — what counts as a disqualifying defect varies by context.

| Genre | Sharpness | Exposure | Blink penalty | Expression |
|---|---|---|---|---|
| `wedding` | 0.35 | 0.25 | 0.30 | 0.10 |
| `portrait` | 0.40 | 0.25 | 0.35 | — |
| `event` | 0.30 | 0.30 | 0.25 | 0.15 |
| `sport` | 0.50 | 0.25 | 0.15 | 0.10 |
| `landscape` | 0.50 | 0.40 | — | — |
| `documentary` | 0.25 | 0.25 | 0.30 | 0.20 |
| `general` | 0.35 | 0.30 | 0.25 | 0.10 |

Sport has a lower blink penalty because athletes commonly have closed eyes mid-action. Landscape ignores blink entirely (no faces expected). You can customise genre weights in `fixxer/scoring.py`.

---

## Output files

After running, your shoot directory will contain:

```
Wedding_2024/
├── IMG_0001.CR3
├── IMG_0001.xmp          ← Star rating + pick flag + colour label
├── IMG_0002.CR3
├── IMG_0002.xmp
├── ...
├── fixxer_selections.csv ← Full scoring report (open in Excel)
└── .fixxer.db            ← Project state (SQLite — safe to delete to reset)
```

### Lightroom import

1. Run `fixxer cull` on your shoot directory
2. In Lightroom: **File → Import Photos and Video**
3. Import as normal — star ratings and pick flags will be applied automatically
4. Filter by ★★★+ or Flagged to see Fixxer's selections

Alternatively: import first, then in Library module select all → **Metadata → Read Metadata from Files** to pull in XMP from existing sidecars.

### Capture One import

XMP sidecars are read automatically for DNG files. For manufacturer RAW formats (CR3, ARW, NEF), use **File → Import XMP** or import via a session that has XMP reading enabled.

---

## Architecture

```
RAW files on disk
      │
      ▼
 [Ingestion]  ── rawpy preview extract (no full decode) + exiftool EXIF
      │
      ▼
 [Clustering]  ── timestamp window + pHash Hamming distance grouping
      │
      ▼
 [Scoring]  ── Laplacian sharpness + histogram exposure + OpenCV face/eye
      │
      ▼
 [Selection]  ── group-level winner election + percentile threshold
      │
      ▼
 [Export]  ── XMP sidecar write per image + CSV summary
```

All state persists to a SQLite database (`.fixxer.db`) in the project directory. Every stage is idempotent — re-running picks up where it left off and only reprocesses new files.

---

## Performance targets (Phase 1)

| Metric | Target | 
|---|---|
| Throughput | ≥ 600 RAW files/min on M2 MacBook Air |
| First result | ≤ 60s from project open |
| Blink recall | ≥ 88% (Phase 1 Haar cascade) |
| Blur detection | ≥ 82% agreement with manual review |
| XMP round-trip | 100% Lightroom compatible |

Phase 2 adds a trained EfficientNet-B0 multi-head backbone and MediaPipe FaceMesh for EAR-based blink detection (target: ≥ 95% recall).

---

## Roadmap

**Phase 1 (current) — Functional replica**
- [x] RAW preview extraction via rawpy
- [x] pHash duplicate grouping
- [x] Laplacian sharpness scoring
- [x] OpenCV Haar cascade eye detection
- [x] Genre-calibrated composite scoring
- [x] Group-level winner election
- [x] XMP sidecar export
- [x] CLI + local web review UI
- [x] Fully idempotent pipeline with SQLite state

**Phase 2 — Performance parity**
- [ ] Multi-head EfficientNet-B0 IQA backbone (ONNX + CoreML)
- [ ] MediaPipe FaceMesh EAR-based blink detection
- [ ] Photographer preference feedback loop (sklearn SGD)
- [ ] Onboarding preference seeding (drag keeper examples)
- [ ] Calibrated confidence scores per decision
- [ ] Parallel scoring pipeline

**Phase 3 — Superior version**
- [ ] DINOv2 moment rarity scoring (protect unique frames)
- [ ] Confidence-gated review (only uncertain decisions surface)
- [ ] Narrative arc coverage (wedding ceremony beat detection)
- [ ] Opt-in federated personalisation prior
- [ ] `pip install fixxer` Python library API

---

## Privacy

Fixxer is fully local. No images, previews, or metadata are transmitted anywhere. The only network activity is the optional federated personalisation feature (Phase 3), which is opt-in and transmits only embedding vectors — never images.

---

## Contributing

Issues and PRs welcome. The highest-value contributions in Phase 1:

1. **Genre weight calibration** — if you have a dataset of manually-culled shoots for a specific genre, use it to tune the weight vectors in `fixxer/scoring.py`
2. **Manufacturer compatibility testing** — test preview extraction quality across Fujifilm, Sony, Nikon, Canon on your hardware
3. **XMP compatibility** — test the output against Capture One, Lightroom Mobile, ON1, and report any import failures

---

## License

MIT © Andre De Jager
