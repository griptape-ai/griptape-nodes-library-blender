import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "blender"))

from media_files import hashed_filename


def test_same_content_gets_the_same_name():
    assert hashed_filename("camera_plane", b"abc", ".png") == hashed_filename("camera_plane", b"abc", ".png")


def test_different_content_gets_a_different_name():
    assert hashed_filename("camera_plane", b"abc", ".png") != hashed_filename("camera_plane", b"abd", ".png")


def test_name_keeps_stem_and_suffix():
    name = hashed_filename("camera_plane", b"abc", ".mp4")
    assert name.startswith("camera_plane_")
    assert name.endswith(".mp4")
