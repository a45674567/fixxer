# Fixxer

**Open-source AI photo culling engine.**

Fixxer culls your RAW or JPEG shoot — grouping bursts, scoring sharpness, exposure, and blink detection, selecting the best frames, and writing XMP sidecar files that Lightroom and Capture One read automatically.

Runs entirely on your Mac. No subscription. No cloud upload. No black box.

---

## Setup from scratch (nothing installed)

If you have a stock Mac with nothing installed, follow these steps in order. This takes about 10 minutes.

### Step 1 — Install Homebrew

Homebrew is a package manager for macOS. Open **Terminal** (press `Cmd + Space`, type "Terminal", press Enter) and run:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

Follow the prompts. When it finishes, it may ask you to run two more commands to add Homebrew to your PATH — do that if shown.

Close and reopen Terminal when done.

### Step 2 — Install Python

```bash
brew install python
```

This installs Python 3 and pip3. Verify:

```bash
python3 --version
pip3 --version
```

Both should print a version number.

### Step 3 — Install exiftool

Fixxer uses exiftool to read camera metadata from RAW files.

```bash
brew install exiftool
```

### Step 4 — Install Fixxer

```bash
pip3 install git+https://github.com/a45674567/fixxer.git
```

This downloads and installs Fixxer and all its dependencies automatically.

### Step 5 — Add Fixxer to your PATH

After installing, you may need to tell your terminal where to find the `fixxer` command. Run:

```bash
echo 'export PATH="$HOME/Library/Python/3.11/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

> **Note:** If Python installed a different version (e.g. 3.12), replace `3.11` with your version number. Check with `python3 --version`.

### Step 6 — Verify the install

```bash
fixxer --version
```

Should print: `fixxer, version 0.1.1`

---

## Running Fixxer

### Cull a shoot

Point Fixxer at your shoot folder. Drag the folder from Finder into Terminal to paste its path automatically.

```bash
fixxer cull ~/Desktop/MyShoot --genre wedding
```

Available genres: `general` `wedding` `portrait` `event` `sport` `landscape` `documentary`

To keep the top 20% of frames:
```bash
fixxer cull ~/Desktop/MyShoot --genre wedding --target 20
```

To keep exactly 400 images:
```bash
fixxer cull ~/Desktop/MyShoot --genre wedding --count 400
```

### Review selections in your browser

```bash
fixxer review ~/Desktop/MyShoot
```

Opens a local web interface at `http://localhost:7842`. Browse your selections, click any image to see scores, press **K** to keep or **R** to reject. Hit **Export XMP** when done.

### Export XMP sidecar files

XMP files are written automatically after culling. To re-export after making review changes:

```bash
fixxer export ~/Desktop/MyShoot
```

### Check project status

```bash
fixxer status ~/Desktop/MyShoot
```

---

## Importing into Lightroom

1. Run `fixxer cull` on your shoot folder
2. Open Lightroom Classic → **File → Import Photos and Video**
3. Navigate to your shoot folder and import as normal
4. In the Library module, filter by **Flagged** or **★★★+** to see Fixxer's picks

If you've already imported the folder into Lightroom before culling:
- Select all images → **Metadata → Read Metadata from Files**

---

## Importing into Capture One

XMP sidecars are read automatically for DNG files. For manufacturer RAW formats (CR3, ARW, NEF):

- Go to **File → Import XMP** or enable XMP reading in your session settings

---

## Important: files must be on your Mac

Fixxer reads every image file during processing. Files stored in iCloud, OneDrive, or Google Drive must be downloaded locally first.

**In Finder:** select your shoot folder → right-click → **"Keep Downloaded"** or **"Always keep on this device"**. Wait for the download to complete before running Fixxer.

Working from a local drive (e.g. your Desktop or an external SSD) is faster and more reliable than working from cloud storage.

---

## Genres and what they do

Each genre adjusts how Fixxer weights quality factors:

| Genre | Sharpness | Exposure | Blink penalty | Expression |
|---|---|---|---|---|
| `wedding` | medium | medium | high | low |
| `portrait` | high | medium | high | — |
| `event` | medium | medium | medium | medium |
| `sport` | high | medium | low | low |
| `landscape` | high | high | none | none |
| `documentary` | low | medium | medium | high |
| `general` | medium | medium | medium | low |

Sport has a low blink penalty because athletes often have closed eyes mid-action. Landscape ignores faces entirely.

---

## What Fixxer creates in your folder

After running, your shoot folder will contain:

```
MyShoot/
├── IMG_0001.CR3
├── IMG_0001.xmp          ← star rating + pick flag
├── IMG_0002.CR3
├── IMG_0002.xmp
├── ...
├── fixxer_selections.csv ← full scoring report (open in Excel)
└── .fixxer.db            ← project database (safe to delete to reset)
```

To start fresh on a shoot, delete `.fixxer.db` and run `fixxer cull` again.

---

## Troubleshooting

**`zsh: command not found: fixxer`**
Run the PATH setup from Step 5 again, then open a new Terminal window.

**`fixxer cull` says "No supported image files found"**
Check that the folder path is correct and the files are downloaded locally (not cloud stubs).

**Files are on OneDrive/iCloud and showing I/O errors**
The files aren't downloaded. In Finder, right-click the folder → "Keep Downloaded". Wait for sync to complete.

**Review UI shows "Failed to load project"**
The cull may not have completed. Run `fixxer status /path/to/shoot` to check.

**Want to re-cull with different settings**
Delete `.fixxer.db` from the shoot folder and run `fixxer cull` again with new options.

---

## Privacy

Fixxer runs entirely on your machine. No images, previews, or metadata are sent anywhere. The review UI is a local web server — `http://localhost:7842` is only accessible on your own computer.

---

## License

MIT © Andre De Jager  
[github.com/a45674567/fixxer](https://github.com/a45674567/fixxer)
