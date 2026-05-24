"""
tests/test_pipeline.py — MVP test suite for the Fixxer pipeline.

Tests run against synthetic test data (generated JPEG images)
since we don't have real RAW files in the test environment.
The synthetic images test the scoring logic, clustering, and export.
"""

import pytest
import tempfile
import numpy as np
from pathlib import Path
from PIL import Image, ImageDraw


# ── Fixtures ──────────────────────────────────────────────────────────────────

def make_sharp_image(path: Path, size=(640, 480)):
    """Create a synthetic sharp image (high-frequency detail)."""
    img = Image.new("RGB", size, (128, 128, 128))
    draw = ImageDraw.Draw(img)
    # Add grid pattern for high frequency content
    for x in range(0, size[0], 10):
        draw.line([(x, 0), (x, size[1])], fill=(200, 200, 200), width=1)
    for y in range(0, size[1], 10):
        draw.line([(0, y), (size[0], y)], fill=(200, 200, 200), width=1)
    img.save(str(path), "JPEG", quality=95)
    return path


def make_blurry_image(path: Path, size=(640, 480)):
    """Create a synthetic blurry image (uniform, low frequency)."""
    # Solid colour with gentle gradient — low Laplacian variance
    img = Image.new("RGB", size)
    pixels = []
    for y in range(size[1]):
        for x in range(size[0]):
            v = int(128 + 20 * (x / size[0]))
            pixels.append((v, v, v))
    img.putdata(pixels)
    # Apply heavy blur
    from PIL import ImageFilter
    img = img.filter(ImageFilter.GaussianBlur(radius=8))
    img.save(str(path), "JPEG", quality=95)
    return path


def make_dark_image(path: Path, size=(640, 480)):
    """Create an underexposed image."""
    img = Image.new("RGB", size, (15, 15, 15))
    img.save(str(path), "JPEG", quality=95)
    return path


@pytest.fixture
def test_dir():
    """Temporary directory with synthetic test images."""
    with tempfile.TemporaryDirectory() as tmpdir:
        d = Path(tmpdir)
        make_sharp_image(d / "sharp_001.jpg")
        make_sharp_image(d / "sharp_002.jpg")  # Near-duplicate of 001
        make_blurry_image(d / "blurry_001.jpg")
        make_dark_image(d / "dark_001.jpg")
        make_sharp_image(d / "sharp_portrait.jpg", size=(480, 640))
        yield d


@pytest.fixture
def db(test_dir):
    from fixxer.db import ProjectDB
    db = ProjectDB(test_dir)
    return db.connect()


# ── Scoring tests ─────────────────────────────────────────────────────────────

class TestSharpnessScoring:
    def test_sharp_image_scores_higher_than_blurry(self, test_dir):
        from fixxer.scoring import score_sharpness
        import numpy as np

        sharp_img = Image.open(test_dir / "sharp_001.jpg").convert("L")
        blurry_img = Image.open(test_dir / "blurry_001.jpg").convert("L")

        sharp_score, _ = score_sharpness(np.array(sharp_img))
        blurry_score, _ = score_sharpness(np.array(blurry_img))

        assert sharp_score > blurry_score, (
            f"Sharp score {sharp_score:.3f} should exceed blurry score {blurry_score:.3f}"
        )

    def test_sharpness_score_range(self, test_dir):
        from fixxer.scoring import score_sharpness
        import numpy as np

        for name in ["sharp_001.jpg", "blurry_001.jpg"]:
            img = Image.open(test_dir / name).convert("L")
            score, variance = score_sharpness(np.array(img))
            assert 0.0 <= score <= 1.0, f"Score out of range: {score}"
            assert variance >= 0, f"Variance negative: {variance}"


class TestExposureScoring:
    def test_dark_image_scores_lower_than_normal(self, test_dir):
        from fixxer.scoring import score_exposure
        import numpy as np

        normal_img = Image.open(test_dir / "sharp_001.jpg").convert("L")
        dark_img   = Image.open(test_dir / "dark_001.jpg").convert("L")

        normal_score, *_ = score_exposure(np.array(normal_img))
        dark_score, *_   = score_exposure(np.array(dark_img))

        assert normal_score > dark_score, (
            f"Normal exposure {normal_score:.3f} should exceed dark {dark_score:.3f}"
        )

    def test_dark_image_has_low_mean_luminance(self, test_dir):
        from fixxer.scoring import score_exposure
        import numpy as np

        dark_img = Image.open(test_dir / "dark_001.jpg").convert("L")
        _, mean_lum, _, _ = score_exposure(np.array(dark_img))
        assert mean_lum < 30, f"Dark image mean luminance too high: {mean_lum}"


class TestCompositeScoring:
    def test_composite_range(self, test_dir):
        from fixxer.scoring import score_image

        for img_file in test_dir.glob("*.jpg"):
            result = score_image(str(img_file))
            assert result is not None
            assert 0.0 <= result["composite"] <= 1.0
            assert 0.0 <= result["sharpness"] <= 1.0
            assert 0.0 <= result["exposure"] <= 1.0

    def test_sharp_image_composite_higher_than_blurry(self, test_dir):
        from fixxer.scoring import score_image

        sharp  = score_image(str(test_dir / "sharp_001.jpg"))
        blurry = score_image(str(test_dir / "blurry_001.jpg"))

        assert sharp["composite"] > blurry["composite"]

    def test_genre_weights_affect_score(self, test_dir):
        from fixxer.scoring import score_image

        # The genre should change composite but not raw sub-scores
        sharp_general  = score_image(str(test_dir / "sharp_001.jpg"), genre="general")
        sharp_landscape = score_image(str(test_dir / "sharp_001.jpg"), genre="landscape")

        # Raw sharpness score should be the same regardless of genre
        assert sharp_general["sharpness"] == pytest.approx(
            sharp_landscape["sharpness"], abs=0.001
        )


# ── Clustering tests ─────────────────────────────────────────────────────────

class TestClustering:
    def test_cluster_returns_groups(self, test_dir, db):
        from fixxer.ingestion import ingest_directory
        from fixxer.clustering import run_clustering

        ingest_directory(test_dir, db, workers=1)
        n_groups = run_clustering(db)

        assert n_groups > 0
        assert n_groups <= db.image_count()

    def test_each_group_has_exactly_one_candidate(self, db, test_dir):
        from fixxer.ingestion import ingest_directory
        from fixxer.clustering import run_clustering

        ingest_directory(test_dir, db, workers=1)
        run_clustering(db)

        groups = db.get_groups()
        for gid, members in groups.items():
            candidates = [m for m in members if m["is_candidate"]]
            assert len(candidates) == 1, (
                f"Group {gid} has {len(candidates)} candidates, expected 1"
            )


# ── Pipeline integration test ─────────────────────────────────────────────────

class TestPipeline:
    def test_full_pipeline_runs_end_to_end(self, test_dir):
        from fixxer.pipeline import Pipeline, PipelineConfig

        config = PipelineConfig(
            directory=test_dir,
            genre="general",
            target_pct=50.0,
        )
        pipeline = Pipeline(config)
        result = pipeline.run()

        assert result.success, f"Pipeline failed: {result.summary()}"
        assert len(result.stages) == 4

        stats = pipeline.get_stats()
        assert stats["total_images"] > 0
        assert stats["kept"] > 0
        assert stats["kept"] + stats["rejected"] == stats["total_images"]

    def test_pipeline_is_idempotent(self, test_dir):
        """Running twice should not change selection counts."""
        from fixxer.pipeline import Pipeline, PipelineConfig

        config = PipelineConfig(directory=test_dir, target_pct=50.0)
        p = Pipeline(config)

        r1 = p.run()
        stats1 = p.get_stats()

        r2 = p.run()
        stats2 = p.get_stats()

        assert stats1["kept"] == stats2["kept"]
        assert stats1["rejected"] == stats2["rejected"]


# ── Export tests ──────────────────────────────────────────────────────────────

class TestExport:
    def test_xmp_write_creates_files(self, test_dir):
        from fixxer.pipeline import Pipeline, PipelineConfig
        from fixxer.db import ProjectDB
        from fixxer.export import export_selections

        config = PipelineConfig(directory=test_dir, target_pct=50.0)
        Pipeline(config).run()

        db = ProjectDB(test_dir).connect()
        result = export_selections(db)

        assert result["written"] > 0
        assert result["failed"] == 0

        # Check XMP files exist
        xmp_files = list(test_dir.glob("*.xmp"))
        assert len(xmp_files) > 0

    def test_xmp_content_is_valid(self, test_dir):
        from fixxer.pipeline import Pipeline, PipelineConfig
        from fixxer.db import ProjectDB
        from fixxer.export import export_selections
        import xml.etree.ElementTree as ET

        config = PipelineConfig(directory=test_dir, target_pct=50.0)
        Pipeline(config).run()

        db = ProjectDB(test_dir).connect()
        export_selections(db)

        for xmp_file in test_dir.glob("*.xmp"):
            content = xmp_file.read_text(encoding="utf-8")
            # Should be parseable XML (after stripping packet wrapper)
            # Just check it has the required namespace declarations
            assert 'xmlns:xmp=' in content
            assert 'xmp:Rating=' in content
            assert 'xmpDM:pick=' in content

    def test_csv_export(self, test_dir):
        from fixxer.pipeline import Pipeline, PipelineConfig
        from fixxer.db import ProjectDB
        from fixxer.export import export_summary_csv
        import csv

        config = PipelineConfig(directory=test_dir, target_pct=50.0)
        Pipeline(config).run()

        csv_path = test_dir / "test_export.csv"
        db = ProjectDB(test_dir).connect()
        success = export_summary_csv(db, csv_path)

        assert success
        assert csv_path.exists()

        rows = list(csv.DictReader(open(csv_path)))
        assert len(rows) > 0
        assert "filename" in rows[0]
        assert "selected" in rows[0]
        assert "composite" in rows[0]


# ── DB layer tests ─────────────────────────────────────────────────────────────

class TestDB:
    def test_upsert_is_idempotent(self, test_dir, db):
        data = {
            "path": str(test_dir / "test.jpg"),
            "filename": "test.jpg",
            "ext": ".jpg",
            "size_bytes": 12345,
        }
        id1 = db.upsert_image(data)
        id2 = db.upsert_image(data)
        assert id1 == id2

    def test_meta_roundtrip(self, test_dir, db):
        db.set_meta("genre", "wedding")
        assert db.get_meta("genre") == "wedding"

        db.set_meta("config", {"target": 20.0, "workers": 4})
        cfg = db.get_meta("config")
        assert cfg["target"] == 20.0

    def test_selection_stats(self, test_dir):
        from fixxer.pipeline import Pipeline, PipelineConfig
        from fixxer.db import ProjectDB

        config = PipelineConfig(directory=test_dir, target_pct=40.0)
        Pipeline(config).run()

        db = ProjectDB(test_dir).connect()
        stats = db.selection_stats()

        assert stats["total"] == db.image_count()
        assert stats["kept"] + stats["rejected"] == stats["total"]
