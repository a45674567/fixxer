"""
server.py — Local Flask web server for the review UI.

Serves the review interface at http://localhost:7842
All image data is served from local disk — nothing leaves the machine.
API endpoints:
  GET  /api/project          — project stats
  GET  /api/selections       — all selections with scores
  GET  /api/preview/<id>     — serve preview JPEG
  POST /api/override/<id>    — toggle keep/reject for an image
  POST /api/rerun-selection  — re-run selection engine with new params
"""

import logging
import threading
import webbrowser
import time
import base64
from pathlib import Path
from flask import Flask, jsonify, request, send_file, abort

log = logging.getLogger("fixxer.ui.server")


def create_app(directory: Path):
    """Create and configure the Flask app for a given project directory."""
    from ..db import ProjectDB
    from ..export import export_selections
    from ..selection import run_selection
    from ..scoring import GENRE_WEIGHTS

    app = Flask(__name__, static_folder=None)
    app.config["DIRECTORY"] = directory
    def get_db():
        return ProjectDB(directory)

    # ── API: Project stats ────────────────────────────────────────────────

    @app.route("/api/project")
    def api_project():
        db = get_db()
        with db.connect():
            stats = db.selection_stats()
            groups = db.get_groups()
            genre = db.get_meta("genre", "general")
            return jsonify({
                "directory": str(directory),
                "genre": genre,
                "total_images": db.image_count(),
                "total_groups": len(groups),
                "kept": stats.get("kept") or 0,
                "rejected": stats.get("rejected") or 0,
                "avg_confidence": stats.get("avg_conf") or 0,
                "genres": list(GENRE_WEIGHTS.keys()),
            })

    # ── API: Selections list ──────────────────────────────────────────────

    @app.route("/api/selections")
    def api_selections():
        db = get_db()
        filter_type = request.args.get("filter", "all")
        # all | kept | rejected | uncertain

        with db.connect():
            rows = db.get_selections()
            result = []
            for row in rows:
                sel = dict(row)
                conf = sel.get("ai_confidence") or 0.0
                is_uncertain = conf < 0.65
                if filter_type == "kept" and not sel["selected"]:
                    continue
                if filter_type == "rejected" and sel["selected"]:
                    continue
                if filter_type == "uncertain" and not is_uncertain:
                    continue
                result.append({
                    "id": sel["id"],
                    "filename": sel["filename"],
                    "capture_ts": sel["capture_ts"],
                    "selected": bool(sel["selected"]),
                    "star": sel["star"],
                    "colour_label": sel["colour_label"],
                    "pick_flag": sel["pick_flag"],
                    "reason": sel["reason"],
                    "ai_confidence": conf,
                    "human_override": bool(sel["human_override"]),
                    "sharpness": sel["sharpness"],
                    "exposure": sel["exposure"],
                    "blink_flag": bool(sel["blink_flag"]),
                    "face_count": sel["face_count"],
                    "composite": sel["composite"],
                    "has_preview": bool(sel.get("preview_path")),
                })
        return jsonify(result)

    # ── API: Serve preview image ──────────────────────────────────────────

    @app.route("/api/preview/<int:image_id>")
    def api_preview(image_id):
        db = get_db()
        with db.connect():
            rows = db.get_images(where="id=?", params=(image_id,))
            if not rows:
                abort(404)
            row = rows[0]
            preview_path = row["preview_path"]
            if not preview_path or not Path(preview_path).exists():
                abort(404)
            return send_file(preview_path, mimetype="image/jpeg")

    # ── API: Override decision ────────────────────────────────────────────

    @app.route("/api/override/<int:image_id>", methods=["POST"])
    def api_override(image_id):
        db = get_db()
        data = request.get_json() or {}
        new_decision = data.get("selected")  # bool

        if new_decision is None:
            return jsonify({"error": "missing 'selected' field"}), 400

        with db.connect():
            # Get current state
            rows = db.get_selections()
            current = next((r for r in rows if r["id"] == image_id), None)
            if not current:
                return jsonify({"error": "image not found"}), 404

            ai_decision = current["selected"]
            composite = current["composite"] or 0.0
            ai_conf = current["ai_confidence"] or 0.0

            # Compute new star rating
            if new_decision:
                star = 3 if composite < 0.5 else (4 if composite < 0.75 else 5)
            else:
                star = 0

            db.upsert_selection(image_id, {
                "selected": 1 if new_decision else 0,
                "star": star,
                "colour_label": "green" if new_decision else "",
                "pick_flag": "pick" if new_decision else "reject",
                "human_override": 1,
                "reason": (current["reason"] or "") + " [human-override]",
                "ai_confidence": ai_conf,
            })

            # Log correction for future personalisation
            db.add_correction(
                image_id=image_id,
                ai_decision=1 if ai_decision else 0,
                human_decision=1 if new_decision else 0,
                composite=composite,
            )

        return jsonify({"success": True, "image_id": image_id,
                        "selected": new_decision})

    # ── API: Re-export XMP ────────────────────────────────────────────────

    @app.route("/api/export", methods=["POST"])
    def api_export():
        db = get_db()
        with db.connect():
            result = export_selections(db=db)
        return jsonify(result)

    # ── Serve the review UI ───────────────────────────────────────────────

    @app.route("/")
    @app.route("/<path:path>")
    def serve_ui(path=""):
        return _render_ui()

    return app


def _render_ui() -> str:
    """Return the single-page review UI as an HTML string."""
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Fixxer — Review</title>
<style>
  :root {
    --bg: #0f0f0e; --surface: #1a1a18; --surface2: #242422;
    --border: rgba(255,255,255,0.07); --border2: rgba(255,255,255,0.13);
    --text: #e8e8e2; --text2: #8a8a80; --text3: #5a5a55;
    --green: #2ecc71; --red: #e74c3c; --amber: #f39c12;
    --blue: #3498db; --accent: #4a90d9;
    --font: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    --mono: 'Courier New', monospace;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: var(--font);
         font-size: 14px; min-height: 100vh; }

  /* ── Header ── */
  .header { background: var(--surface); border-bottom: 1px solid var(--border);
            padding: 0 1.5rem; height: 54px; display: flex; align-items: center;
            gap: 1.5rem; position: sticky; top: 0; z-index: 100; }
  .logo { font-weight: 700; font-size: 16px; color: var(--text);
          letter-spacing: -0.03em; }
  .logo span { color: var(--accent); }
  .stats { display: flex; gap: 1.25rem; margin-left: auto; }
  .stat { text-align: center; }
  .stat .val { font-size: 18px; font-weight: 600; line-height: 1; }
  .stat .lbl { font-size: 10px; color: var(--text3); letter-spacing: 0.06em;
               text-transform: uppercase; margin-top: 2px; }
  .stat.kept .val { color: var(--green); }
  .stat.rejected .val { color: var(--red); }

  /* ── Toolbar ── */
  .toolbar { background: var(--surface); border-bottom: 1px solid var(--border);
             padding: 0.6rem 1.5rem; display: flex; gap: 0.75rem;
             align-items: center; flex-wrap: wrap; }
  .filter-btn { background: var(--surface2); border: 1px solid var(--border2);
                color: var(--text2); padding: 5px 14px; border-radius: 6px;
                font-size: 12px; cursor: pointer; transition: all 0.15s; }
  .filter-btn:hover { border-color: var(--accent); color: var(--text); }
  .filter-btn.active { background: var(--accent); border-color: var(--accent);
                        color: #fff; }
  .sort-select { background: var(--surface2); border: 1px solid var(--border2);
                 color: var(--text2); padding: 5px 10px; border-radius: 6px;
                 font-size: 12px; }
  .toolbar-right { margin-left: auto; display: flex; gap: 0.5rem; }
  .btn { padding: 6px 14px; border-radius: 6px; font-size: 12px; cursor: pointer;
         border: none; font-family: var(--font); transition: all 0.15s; }
  .btn-export { background: var(--accent); color: #fff; }
  .btn-export:hover { opacity: 0.85; }

  /* ── Grid ── */
  .grid-wrap { padding: 1.25rem 1.5rem; }
  .grid { display: grid;
          grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
          gap: 8px; }
  .card { background: var(--surface); border: 1px solid var(--border);
          border-radius: 8px; overflow: hidden; cursor: pointer;
          transition: border-color 0.15s, transform 0.1s; position: relative; }
  .card:hover { border-color: var(--border2); transform: translateY(-1px); }
  .card.kept { border-color: rgba(46,204,113,0.35); }
  .card.rejected { border-color: rgba(231,76,60,0.15); }
  .card.selected-card { outline: 2px solid var(--accent); }

  .card-img-wrap { position: relative; aspect-ratio: 3/2; background: #111;
                   overflow: hidden; }
  .card-img { width: 100%; height: 100%; object-fit: cover;
              transition: opacity 0.2s; }
  .card-img.loading { opacity: 0; }
  .card-overlay { position: absolute; top: 0; left: 0; right: 0; bottom: 0;
                  display: flex; align-items: flex-start;
                  justify-content: space-between; padding: 6px; opacity: 0;
                  transition: opacity 0.15s; background: rgba(0,0,0,0.4); }
  .card:hover .card-overlay { opacity: 1; }
  .keep-btn, .reject-btn { width: 30px; height: 30px; border-radius: 50%;
                            border: none; cursor: pointer; font-size: 14px;
                            display: flex; align-items: center; justify-content: center;
                            transition: transform 0.1s; }
  .keep-btn { background: var(--green); }
  .keep-btn:hover { transform: scale(1.15); }
  .reject-btn { background: var(--red); }
  .reject-btn:hover { transform: scale(1.15); }

  .card-badge { position: absolute; top: 6px; right: 6px; }
  .badge { display: inline-block; font-size: 10px; font-weight: 700;
           padding: 2px 7px; border-radius: 10px; text-transform: uppercase;
           letter-spacing: 0.05em; }
  .badge-keep { background: rgba(46,204,113,0.9); color: #fff; }
  .badge-reject { background: rgba(231,76,60,0.7); color: #fff; }
  .badge-override { background: rgba(243,156,18,0.9); color: #fff; }

  .card-blink { position: absolute; bottom: 6px; left: 6px;
                background: rgba(231,76,60,0.85); color: #fff;
                font-size: 10px; padding: 2px 6px; border-radius: 4px; }

  .card-info { padding: 8px 10px; }
  .card-name { font-size: 11px; color: var(--text2); white-space: nowrap;
               overflow: hidden; text-overflow: ellipsis; margin-bottom: 4px; }
  .card-scores { display: flex; gap: 6px; flex-wrap: wrap; }
  .score-chip { font-family: var(--mono); font-size: 10px; color: var(--text3);
                background: var(--surface2); padding: 1px 5px; border-radius: 3px; }
  .score-chip.good { color: var(--green); }
  .score-chip.bad  { color: var(--red); }
  .score-chip.mid  { color: var(--amber); }
  .stars { color: #f1c40f; font-size: 11px; }

  /* ── Lightbox ── */
  .lightbox { position: fixed; inset: 0; background: rgba(0,0,0,0.93);
              display: flex; flex-direction: column; align-items: center;
              justify-content: center; z-index: 200; display: none; }
  .lightbox.open { display: flex; }
  .lb-img-wrap { flex: 1; display: flex; align-items: center;
                 justify-content: center; padding: 1rem; width: 100%; }
  .lb-img { max-width: 100%; max-height: 70vh; object-fit: contain;
            border-radius: 4px; }
  .lb-info { background: var(--surface); border-top: 1px solid var(--border);
             width: 100%; padding: 1rem 2rem; display: flex;
             align-items: center; gap: 2rem; }
  .lb-name { font-size: 15px; font-weight: 500; }
  .lb-scores { display: flex; gap: 1rem; }
  .lb-score { text-align: center; }
  .lb-score .v { font-size: 16px; font-weight: 600; }
  .lb-score .l { font-size: 10px; color: var(--text3); text-transform: uppercase;
                 letter-spacing: 0.06em; }
  .lb-actions { margin-left: auto; display: flex; gap: 0.75rem; }
  .lb-keep { background: var(--green); color: #fff; }
  .lb-reject { background: var(--red); color: #fff; }
  .lb-close { background: var(--surface2); color: var(--text2); }
  .lb-nav { position: absolute; top: 50%; transform: translateY(-50%);
            background: rgba(255,255,255,0.08); border: none; color: #fff;
            width: 44px; height: 44px; border-radius: 50%; font-size: 20px;
            cursor: pointer; display: flex; align-items: center;
            justify-content: center; }
  .lb-prev { left: 1rem; }
  .lb-next { right: 1rem; }
  .lb-nav:hover { background: rgba(255,255,255,0.18); }

  /* ── Empty state ── */
  .empty { text-align: center; padding: 4rem 2rem; color: var(--text3); }
  .empty h3 { font-size: 1.2rem; margin-bottom: 0.5rem; color: var(--text2); }

  /* ── Toast ── */
  /* Fix #9: keyboard shortcut guide */
  .kbd-guide { display: flex; align-items: center; gap: 6px; font-size: 11px;
               color: var(--text3); margin-left: 0.5rem; }
  .kbd { background: var(--surface2); border: 1px solid var(--border2);
         border-radius: 4px; padding: 1px 6px; font-family: var(--mono);
         font-size: 10px; color: var(--text2); }
  .toast { position: fixed; bottom: 1.5rem; right: 1.5rem; z-index: 300;
           background: var(--surface); border: 1px solid var(--border2);
           border-radius: 8px; padding: 0.75rem 1.25rem; font-size: 13px;
           opacity: 0; transform: translateY(8px); transition: all 0.25s;
           pointer-events: none; }
  .toast.show { opacity: 1; transform: translateY(0); }
  .toast.success { border-color: var(--green); color: var(--green); }
  .toast.error { border-color: var(--red); color: var(--red); }

  /* ── Loading ── */
  .loading-screen { display: flex; align-items: center; justify-content: center;
                    height: 60vh; flex-direction: column; gap: 1rem;
                    color: var(--text3); }
  .spinner { width: 32px; height: 32px; border: 2px solid var(--border2);
             border-top-color: var(--accent); border-radius: 50%;
             animation: spin 0.8s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>

<div class="header">
  <div class="logo">fix<span>xer</span></div>
  <div id="project-path" style="color:var(--text3);font-size:12px;"></div>
  <div class="stats">
    <div class="stat kept"><div class="val" id="stat-kept">—</div><div class="lbl">Kept</div></div>
    <div class="stat rejected"><div class="val" id="stat-rejected">—</div><div class="lbl">Rejected</div></div>
    <div class="stat"><div class="val" id="stat-total">—</div><div class="lbl">Total</div></div>
    <div class="stat"><div class="val" id="stat-groups">—</div><div class="lbl">Groups</div></div>
  </div>
</div>

<div class="toolbar">
  <button class="filter-btn active" onclick="setFilter('all')">All</button>
  <button class="filter-btn" onclick="setFilter('kept')">Kept</button>
  <button class="filter-btn" onclick="setFilter('rejected')">Rejected</button>
  <button class="filter-btn" onclick="setFilter('uncertain')">Uncertain</button>
  <select class="sort-select" onchange="setSort(this.value)">
    <option value="composite-desc">Quality ↓</option>
    <option value="composite-asc">Quality ↑</option>
    <option value="ts-asc">Time ↑</option>
    <option value="ts-desc">Time ↓</option>
    <option value="sharpness-desc">Sharpness ↓</option>
  </select>
  <div class="kbd-guide" title="Keyboard shortcuts">
    <span class="kbd">K</span> Keep
    <span class="kbd">R</span> Reject
    <span class="kbd">←→</span> Navigate
    <span class="kbd">Esc</span> Close
  </div>
  <div class="toolbar-right">
    <button class="btn btn-export" onclick="exportXMP()">Export XMP</button>
  </div>
</div>

<div class="grid-wrap">
  <div class="grid" id="grid">
    <div class="loading-screen">
      <div class="spinner"></div>
      <div>Loading project…</div>
    </div>
  </div>
</div>

<!-- Lightbox -->
<div class="lightbox" id="lightbox">
  <button class="lb-nav lb-prev" onclick="lbNav(-1)">‹</button>
  <button class="lb-nav lb-next" onclick="lbNav(1)">›</button>
  <div class="lb-img-wrap">
    <img class="lb-img" id="lb-img" src="" alt="">
  </div>
  <div class="lb-info">
    <div>
      <div class="lb-name" id="lb-name"></div>
      <div id="lb-reason" style="font-size:11px;color:var(--text3);margin-top:2px;"></div>
    </div>
    <div class="lb-scores" id="lb-scores"></div>
    <div class="lb-actions">
      <button class="btn lb-keep" onclick="lbOverride(true)">✓ Keep</button>
      <button class="btn lb-reject" onclick="lbOverride(false)">✕ Reject</button>
      <button class="btn lb-close" onclick="closeLightbox()">Close</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
let allImages = [];
let filteredImages = [];
let currentFilter = 'all';
let currentSort = 'composite-desc';
let lbIndex = -1;

// ── Load project ──────────────────────────────────────────────────────────────
async function loadProject() {
  try {
    const [proj, sels] = await Promise.all([
      fetch('/api/project').then(r => r.json()),
      fetch('/api/selections').then(r => r.json()),
    ]);
    document.getElementById('stat-kept').textContent = proj.kept;
    document.getElementById('stat-rejected').textContent = proj.rejected;
    document.getElementById('stat-total').textContent = proj.total_images;
    document.getElementById('stat-groups').textContent = proj.total_groups;
    document.getElementById('project-path').textContent =
      proj.directory.split('/').slice(-2).join('/');

    allImages = sels;
    applyFilterAndSort();
  } catch(e) {
    document.getElementById('grid').innerHTML =
      '<div class="empty"><h3>Failed to load project</h3><p>' + e.message + '</p></div>';
  }
}

// ── Filter + Sort ─────────────────────────────────────────────────────────────
function setFilter(f) {
  currentFilter = f;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  event.target.classList.add('active');
  applyFilterAndSort();
}

function setSort(s) {
  currentSort = s;
  applyFilterAndSort();
}

function applyFilterAndSort() {
  let imgs = [...allImages];

  // Filter
  if (currentFilter === 'kept')     imgs = imgs.filter(i => i.selected);
  if (currentFilter === 'rejected') imgs = imgs.filter(i => !i.selected);
  if (currentFilter === 'uncertain') imgs = imgs.filter(i => (i.ai_confidence || 0) < 0.65);

  // Sort
  const [key, dir] = currentSort.split('-');
  const mult = dir === 'asc' ? 1 : -1;
  const getter = { composite: i => i.composite || 0,
                   ts: i => i.capture_ts || 0,
                   sharpness: i => i.sharpness || 0 };
  imgs.sort((a,b) => mult * ((getter[key]?.(a)||0) - (getter[key]?.(b)||0)));

  filteredImages = imgs;
  renderGrid(imgs);
}

// ── Render grid ───────────────────────────────────────────────────────────────
function renderGrid(imgs) {
  const grid = document.getElementById('grid');
  if (!imgs.length) {
    grid.innerHTML = '<div class="empty"><h3>No images match filter</h3></div>';
    return;
  }

  grid.innerHTML = imgs.map((img, idx) => {
    const q = img.composite || 0;
    const qClass = q >= 0.7 ? 'good' : q >= 0.45 ? 'mid' : 'bad';
    const cardClass = img.selected ? 'kept' : 'rejected';
    const badgeClass = img.human_override ? 'badge-override' : (img.selected ? 'badge-keep' : 'badge-reject');
    const badgeText  = img.human_override ? 'Override' : (img.selected ? 'Keep' : 'Reject');
    const stars = img.star ? '★'.repeat(img.star) : '';

    const expQ = (img.exposure||0) >= 0.65 ? 'good' : (img.exposure||0) >= 0.4 ? 'mid' : 'bad';
    const shpQ = (img.sharpness||0) >= 0.6 ? 'good' : (img.sharpness||0) >= 0.35 ? 'mid' : 'bad';

    return `
    <div class="card ${cardClass}" data-idx="${idx}" onclick="openLightbox(${idx})">
      <div class="card-img-wrap">
        <img class="card-img loading" src="/api/preview/${img.id}"
          onload="this.classList.remove('loading')"
          onerror="this.src=''" alt="${img.filename}">
        <div class="card-overlay">
          <button class="keep-btn" onclick="quickOverride(event,${img.id},true)">✓</button>
          <button class="reject-btn" onclick="quickOverride(event,${img.id},false)">✕</button>
        </div>
        <div class="card-badge"><span class="badge ${badgeClass}">${badgeText}</span></div>
        ${img.blink_flag ? '<div class="card-blink">👁 Blink</div>' : ''}
      </div>
      <div class="card-info">
        <div class="card-name">${img.filename}</div>
        <div class="card-scores">
          <span class="score-chip ${qClass}">Q ${(q*100).toFixed(0)}%</span>
          <span class="score-chip ${shpQ}">Shp ${((img.sharpness||0)*100).toFixed(0)}%</span>
          <span class="score-chip ${expQ}">Exp ${((img.exposure||0)*100).toFixed(0)}%</span>
          ${img.face_count ? `<span class="score-chip">👤${img.face_count}</span>` : ''}
        </div>
        ${stars ? `<div class="stars">${stars}</div>` : ''}
      </div>
    </div>`;
  }).join('');
}

// ── Quick override (card hover buttons) ───────────────────────────────────────
async function quickOverride(e, imageId, selected) {
  e.stopPropagation();
  await doOverride(imageId, selected);
}

// ── Lightbox ──────────────────────────────────────────────────────────────────
function openLightbox(idx) {
  lbIndex = idx;
  renderLightbox();
  document.getElementById('lightbox').classList.add('open');
}

function closeLightbox() {
  document.getElementById('lightbox').classList.remove('open');
}

function lbNav(dir) {
  lbIndex = Math.max(0, Math.min(filteredImages.length - 1, lbIndex + dir));
  renderLightbox();
}

function renderLightbox() {
  const img = filteredImages[lbIndex];
  if (!img) return;
  document.getElementById('lb-img').src = '/api/preview/' + img.id;
  document.getElementById('lb-name').textContent = img.filename;
  document.getElementById('lb-reason').textContent =
    `${img.reason || ''} · Confidence: ${((img.ai_confidence||0)*100).toFixed(0)}%`;
  document.getElementById('lb-scores').innerHTML = `
    <div class="lb-score"><div class="v">${((img.composite||0)*100).toFixed(0)}%</div><div class="l">Quality</div></div>
    <div class="lb-score"><div class="v">${((img.sharpness||0)*100).toFixed(0)}%</div><div class="l">Sharp</div></div>
    <div class="lb-score"><div class="v">${((img.exposure||0)*100).toFixed(0)}%</div><div class="l">Exposure</div></div>
    <div class="lb-score"><div class="v">${img.face_count||0}</div><div class="l">Faces</div></div>
    <div class="lb-score"><div class="v" style="color:${img.blink_flag?'var(--red)':'var(--green)'}">
      ${img.blink_flag?'YES':'NO'}</div><div class="l">Blink</div></div>
  `;
}

async function lbOverride(selected) {
  const img = filteredImages[lbIndex];
  if (!img) return;
  await doOverride(img.id, selected);
  closeLightbox();
}

// ── Override API ──────────────────────────────────────────────────────────────
async function doOverride(imageId, selected) {
  try {
    const r = await fetch(`/api/override/${imageId}`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({selected}),
    });
    if (!r.ok) throw new Error('Override failed');

    // Update local state
    const img = allImages.find(i => i.id === imageId);
    if (img) {
      img.selected = selected;
      img.human_override = true;
      img.pick_flag = selected ? 'pick' : 'reject';
    }

    // Update stats
    const kept = allImages.filter(i => i.selected).length;
    document.getElementById('stat-kept').textContent = kept;
    document.getElementById('stat-rejected').textContent = allImages.length - kept;

    applyFilterAndSort();
    showToast(selected ? '✓ Marked as keep' : '✕ Marked as reject', 'success');
  } catch(e) {
    showToast('Override failed', 'error');
  }
}

// ── Export ────────────────────────────────────────────────────────────────────
async function exportXMP() {
  showToast('Exporting XMP sidecars…', '');
  try {
    const r = await fetch('/api/export', {method: 'POST'});
    const data = await r.json();
    showToast(`✓ ${data.written} XMP files written`, 'success');
  } catch(e) {
    showToast('Export failed', 'error');
  }
}

// ── Toast ─────────────────────────────────────────────────────────────────────
let toastTimer;
function showToast(msg, type='') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = `toast show ${type}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 2500);
}

// ── Keyboard ──────────────────────────────────────────────────────────────────
document.addEventListener('keydown', e => {
  const lb = document.getElementById('lightbox');
  if (lb.classList.contains('open')) {
    if (e.key === 'Escape') closeLightbox();
    if (e.key === 'ArrowLeft')  lbNav(-1);
    if (e.key === 'ArrowRight') lbNav(1);
    if (e.key === 'k' || e.key === 'K') lbOverride(true);
    if (e.key === 'r' || e.key === 'R') lbOverride(false);
  }
});

// ── Boot ──────────────────────────────────────────────────────────────────────
loadProject();
</script>
</body>
</html>"""


def start_server(directory: Path, host: str = "127.0.0.1", port: int = 7842):
    """Start the Flask review server."""
    app = create_app(Path(directory))
    app.run(host=host, port=port, debug=False, use_reloader=False)
