"""
db.py — SQLite project state store.

Stores everything: file manifest, EXIF metadata, quality scores,
duplicate groups, personalisation corrections, and final selections.
One .fixxer.db file per project directory.
"""

import sqlite3
import json
import time
from pathlib import Path
from typing import Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    path        TEXT UNIQUE NOT NULL,
    filename    TEXT NOT NULL,
    ext         TEXT NOT NULL,
    size_bytes  INTEGER,
    capture_ts  REAL,           -- Unix timestamp from EXIF
    camera      TEXT,
    lens        TEXT,
    iso         INTEGER,
    shutter     REAL,
    aperture    REAL,
    focal_mm    REAL,
    preview_path TEXT,          -- Path to extracted JPEG preview
    ingested_at REAL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS scores (
    image_id        INTEGER PRIMARY KEY REFERENCES images(id),
    sharpness       REAL,       -- 0.0–1.0 (Laplacian variance, normalised)
    lap_variance    REAL,       -- Raw Laplacian variance
    exposure        REAL,       -- 0.0–1.0
    mean_luminance  REAL,
    highlight_clip  REAL,       -- Fraction of pixels clipped at highlight
    shadow_clip     REAL,       -- Fraction of pixels clipped at shadow
    face_count      INTEGER DEFAULT 0,
    blink_flag      INTEGER DEFAULT 0,  -- 1 if any face has closed eyes
    min_ear         REAL,       -- Minimum Eye Aspect Ratio across faces
    expression      REAL,       -- 0.0–1.0 smile confidence
    composite       REAL,       -- Weighted composite quality score
    scored_at       REAL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS groups (
    group_id        INTEGER,
    image_id        INTEGER REFERENCES images(id),
    is_candidate    INTEGER DEFAULT 0,  -- 1 = elected winner of this group
    phash           TEXT,
    PRIMARY KEY (group_id, image_id)
);

CREATE TABLE IF NOT EXISTS selections (
    image_id        INTEGER PRIMARY KEY REFERENCES images(id),
    selected        INTEGER NOT NULL,   -- 1 = keep, 0 = reject
    star            INTEGER DEFAULT 0,  -- 0–5
    colour_label    TEXT DEFAULT '',    -- 'red','yellow','green','blue','purple',''
    pick_flag       TEXT DEFAULT '',    -- 'pick','reject',''
    reason          TEXT DEFAULT '',
    ai_confidence   REAL,
    human_override  INTEGER DEFAULT 0,  -- 1 if photographer changed AI decision
    selected_at     REAL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS corrections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    image_id        INTEGER REFERENCES images(id),
    ai_decision     INTEGER,    -- What AI decided (1=keep, 0=reject)
    human_decision  INTEGER,    -- What photographer decided
    composite_score REAL,
    embedding_json  TEXT,       -- JSON array of backbone embedding (for personalisation)
    corrected_at    REAL DEFAULT (unixepoch())
);

CREATE TABLE IF NOT EXISTS project_meta (
    key     TEXT PRIMARY KEY,
    value   TEXT
);

CREATE INDEX IF NOT EXISTS idx_images_ts ON images(capture_ts);
CREATE INDEX IF NOT EXISTS idx_groups_gid ON groups(group_id);
CREATE INDEX IF NOT EXISTS idx_selections_sel ON selections(selected);
"""


class ProjectDB:
    """Single-project SQLite database wrapper."""

    def __init__(self, project_dir: Path):
        self.project_dir = Path(project_dir)
        self.db_path = self.project_dir / ".fixxer.db"
        self._conn: Optional[sqlite3.Connection] = None

    def connect(self) -> "ProjectDB":
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        return self

    def __enter__(self):
        return self.connect()

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if not self._conn:
            raise RuntimeError("DB not connected. Use as context manager or call .connect()")
        return self._conn

    # ── Meta ──────────────────────────────────────────────────────────────

    def set_meta(self, key: str, value):
        self.conn.execute(
            "INSERT OR REPLACE INTO project_meta(key, value) VALUES (?, ?)",
            (key, json.dumps(value))
        )
        self.conn.commit()

    def get_meta(self, key: str, default=None):
        row = self.conn.execute(
            "SELECT value FROM project_meta WHERE key=?", (key,)
        ).fetchone()
        return json.loads(row["value"]) if row else default

    # ── Images ────────────────────────────────────────────────────────────

    def upsert_image(self, data: dict) -> int:
        cols = list(data.keys())
        vals = list(data.values())
        placeholders = ", ".join("?" * len(vals))
        col_str = ", ".join(cols)
        update_str = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "path")
        self.conn.execute(
            f"""INSERT INTO images ({col_str}) VALUES ({placeholders})
                ON CONFLICT(path) DO UPDATE SET {update_str}""",
            vals
        )
        self.conn.commit()
        return self.conn.execute(
            "SELECT id FROM images WHERE path=?", (data["path"],)
        ).fetchone()["id"]

    def get_images(self, where: str = "", params=()):
        q = "SELECT * FROM images"
        if where:
            q += f" WHERE {where}"
        q += " ORDER BY capture_ts ASC, id ASC"
        return self.conn.execute(q, params).fetchall()

    def image_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]

    def unscored_images(self):
        return self.conn.execute("""
            SELECT i.* FROM images i
            LEFT JOIN scores s ON s.image_id = i.id
            WHERE s.image_id IS NULL
        """).fetchall()

    # ── Scores ────────────────────────────────────────────────────────────

    def upsert_score(self, image_id: int, data: dict):
        data["image_id"] = image_id
        cols = list(data.keys())
        vals = list(data.values())
        placeholders = ", ".join("?" * len(vals))
        col_str = ", ".join(cols)
        update_str = ", ".join(
            f"{c}=excluded.{c}" for c in cols if c != "image_id"
        )
        self.conn.execute(
            f"""INSERT INTO scores ({col_str}) VALUES ({placeholders})
                ON CONFLICT(image_id) DO UPDATE SET {update_str}""",
            vals
        )
        self.conn.commit()

    def get_scores(self):
        return self.conn.execute("""
            SELECT i.id, i.path, i.filename, i.capture_ts,
                   s.sharpness, s.exposure, s.face_count, s.blink_flag,
                   s.min_ear, s.expression, s.composite,
                   s.mean_luminance, s.highlight_clip, s.lap_variance
            FROM images i
            JOIN scores s ON s.image_id = i.id
            ORDER BY i.capture_ts ASC, i.id ASC
        """).fetchall()

    # ── Groups ────────────────────────────────────────────────────────────

    def clear_groups(self):
        """Delete all group assignments — called before re-clustering."""
        self.conn.execute("DELETE FROM groups")
        self.conn.commit()

    def upsert_group_membership(self, group_id: int, image_id: int,
                                 phash: str, is_candidate: bool = False):
        self.conn.execute("""
            INSERT OR REPLACE INTO groups(group_id, image_id, phash, is_candidate)
            VALUES (?, ?, ?, ?)
        """, (group_id, image_id, phash, 1 if is_candidate else 0))
        self.conn.commit()

    def get_groups(self):
        """Returns dict: group_id → list of (image_id, is_candidate)."""
        rows = self.conn.execute(
            "SELECT group_id, image_id, is_candidate FROM groups ORDER BY group_id"
        ).fetchall()
        groups = {}
        for r in rows:
            groups.setdefault(r["group_id"], []).append(
                {"image_id": r["image_id"], "is_candidate": bool(r["is_candidate"])}
            )
        return groups

    def get_group_for_image(self, image_id: int):
        return self.conn.execute(
            "SELECT group_id, is_candidate FROM groups WHERE image_id=?",
            (image_id,)
        ).fetchone()

    # ── Selections ────────────────────────────────────────────────────────

    def upsert_selection(self, image_id: int, data: dict):
        data["image_id"] = image_id
        cols = list(data.keys())
        vals = list(data.values())
        placeholders = ", ".join("?" * len(vals))
        col_str = ", ".join(cols)
        update_str = ", ".join(
            f"{c}=excluded.{c}" for c in cols if c != "image_id"
        )
        self.conn.execute(
            f"""INSERT INTO selections ({col_str}) VALUES ({placeholders})
                ON CONFLICT(image_id) DO UPDATE SET {update_str}""",
            vals
        )
        self.conn.commit()

    def get_selections(self):
        return self.conn.execute("""
            SELECT i.id, i.path, i.filename, i.capture_ts, i.preview_path,
                   sel.selected, sel.star, sel.colour_label, sel.pick_flag,
                   sel.reason, sel.ai_confidence, sel.human_override,
                   s.sharpness, s.exposure, s.blink_flag, s.face_count,
                   s.composite, s.expression
            FROM images i
            JOIN selections sel ON sel.image_id = i.id
            JOIN scores s ON s.image_id = i.id
            ORDER BY sel.selected DESC, s.composite DESC
        """).fetchall()

    def selection_stats(self) -> dict:
        row = self.conn.execute("""
            SELECT
                COUNT(*) as total,
                SUM(selected) as kept,
                COUNT(*) - SUM(selected) as rejected,
                ROUND(AVG(CASE WHEN selected=1 THEN ai_confidence END), 3) as avg_conf
            FROM selections
        """).fetchone()
        return dict(row) if row else {}

    # ── Corrections (personalisation) ─────────────────────────────────────

    def add_correction(self, image_id: int, ai_decision: int,
                       human_decision: int, composite: float,
                       embedding: Optional[list] = None):
        self.conn.execute("""
            INSERT INTO corrections
                (image_id, ai_decision, human_decision, composite_score, embedding_json)
            VALUES (?, ?, ?, ?, ?)
        """, (image_id, ai_decision, human_decision, composite,
              json.dumps(embedding) if embedding else None))
        self.conn.commit()

    def get_corrections(self):
        return self.conn.execute(
            "SELECT * FROM corrections ORDER BY corrected_at ASC"
        ).fetchall()


    def clear_selections(self):
        """Delete all selections — called before re-running selection engine."""
        self.conn.execute("DELETE FROM selections")
        self.conn.commit()

    def clear_scores(self):
        """Delete all scores — called before re-scoring."""
        self.conn.execute("DELETE FROM scores")
        self.conn.commit()
