import json

import numpy as np
from centerline import trace_centerlines
from PIL import Image, ImageDraw

SIZE = 200
LINE_WIDTH = 5


def _save(tmp_path, draw, name: str = "drawing.png") -> str:
    picture = Image.new("RGB", (SIZE, SIZE), "white")
    draw(ImageDraw.Draw(picture))
    path = tmp_path / name
    picture.save(path)
    return str(path)


def _extent(stroke: dict) -> tuple[float, float]:
    points = np.array(stroke["pts"])
    return float(np.ptp(points[:, 0])), float(np.ptp(points[:, 1]))


def test_crossing_lines_become_two_continuous_strokes(tmp_path):
    path = _save(
        tmp_path,
        lambda d: (
            d.line([(10, 100), (190, 100)], fill="black", width=LINE_WIDTH),
            d.line([(100, 10), (100, 190)], fill="black", width=LINE_WIDTH),
        ),
    )

    result = trace_centerlines(path, min_resolution=0)

    # The lines run through their crossing, so there is one stroke per line and not four branches.
    assert len(result.strokes) == 2
    spans = sorted(_extent(stroke) for stroke in result.strokes)
    assert spans[0][0] < 5 < 150 < spans[0][1]  # the horizontal line spans columns, barely any rows
    assert spans[1][1] < 5 < 150 < spans[1][0]  # the vertical line spans rows, barely any columns
    assert result.solid.sum() < 100  # only the thick middle of the crossing


def test_stroke_width_follows_the_line(tmp_path):
    path = _save(tmp_path, lambda d: d.line([(10, 100), (190, 100)], fill="black", width=LINE_WIDTH))

    result = trace_centerlines(path, min_resolution=0)

    radius = float(np.median(result.strokes[0]["r"]))
    assert LINE_WIDTH / 2 - 1 <= radius <= LINE_WIDTH / 2 + 1


def test_a_solid_blob_is_left_for_the_outline_trace(tmp_path):
    path = _save(
        tmp_path,
        lambda d: (
            d.line([(10, 40), (190, 40)], fill="black", width=LINE_WIDTH),
            d.ellipse([80, 100, 120, 140], fill="black"),
        ),
    )

    result = trace_centerlines(path, min_resolution=0)

    assert result.solid[120, 100]  # the middle of the blob
    assert not result.solid[40, 100]  # the line is not solid
    assert len(result.strokes) == 1  # only the line is a stroke


def test_a_blank_image_has_nothing_to_trace(tmp_path):
    result = trace_centerlines(_save(tmp_path, lambda _: None), min_resolution=0)

    assert result.strokes == []
    assert not result.solid.any()


def test_invert_traces_light_lines_on_dark(tmp_path):
    path = _save(
        tmp_path,
        lambda d: (
            d.rectangle([0, 0, SIZE, SIZE], fill="black"),
            d.line([(10, 100), (190, 100)], fill="white", width=LINE_WIDTH),
        ),
    )

    assert len(trace_centerlines(path, invert=True, min_resolution=0).strokes) == 1


def test_save_writes_strokes_and_the_solid_mask_only_when_there_is_one(tmp_path):
    drawn = _save(
        tmp_path,
        lambda d: (
            d.line([(10, 40), (190, 40)], fill="black", width=LINE_WIDTH),
            d.ellipse([80, 100, 120, 140], fill="black"),
        ),
    )
    solid = trace_centerlines(drawn, min_resolution=0)
    solid.save(tmp_path / "solid.json", tmp_path / "solid.png", tmp_path / "solid_ink.png")
    saved = json.loads((tmp_path / "solid.json").read_text(encoding="utf-8"))

    assert saved["solid"] == str(tmp_path / "solid.png")
    assert (tmp_path / "solid.png").is_file()

    thin = trace_centerlines(
        _save(tmp_path, lambda d: d.line([(10, 100), (190, 100)], fill="black", width=LINE_WIDTH)), min_resolution=0
    )
    thin.save(tmp_path / "thin.json", tmp_path / "thin.png", tmp_path / "thin_ink.png")
    saved = json.loads((tmp_path / "thin.json").read_text(encoding="utf-8"))

    assert saved["solid"] == ""
    assert not (tmp_path / "thin.png").exists()
    assert saved["size"] == [SIZE, SIZE]


def test_a_thin_soft_line_is_traced_but_a_wide_gray_fill_is_not(tmp_path):
    soft = _save(tmp_path, lambda d: d.line([(10, 100), (190, 100)], fill=(150, 150, 150), width=2), "soft.png")
    fill = _save(tmp_path, lambda d: d.rectangle([40, 40, 160, 160], fill=(150, 150, 150)), "fill.png")

    # Both are lighter than the default threshold of 0.5, so only the line stands out from its surroundings.
    assert len(trace_centerlines(soft, min_resolution=0).strokes) == 1
    assert trace_centerlines(fill, min_resolution=0).strokes == []


def test_small_images_are_traced_at_a_larger_size(tmp_path):
    path = _save(tmp_path, lambda d: d.line([(10, 100), (190, 100)], fill="black", width=LINE_WIDTH))

    result = trace_centerlines(path)

    assert result.size == (SIZE * 8, SIZE * 8)  # 200 px is scaled up to 1600, the next whole step past 1500
    assert result.ink.shape == (SIZE * 8, SIZE * 8)


def test_large_images_are_scaled_down_to_the_maximum(tmp_path):
    path = _save(tmp_path, lambda d: d.line([(10, 100), (190, 100)], fill="black", width=LINE_WIDTH))

    assert trace_centerlines(path, min_resolution=0, max_resolution=100).size == (100, 100)
