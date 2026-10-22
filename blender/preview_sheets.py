"""Combine per-image Grease Pencil previews into a contact sheet and an animated GIF.

Pillow only, no engine imports, so this can be tested without a running engine.
"""

import io
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

SHEET_CELL = 320
SHEET_GAP = 8
SHEET_LABEL_HEIGHT = 18
SHEET_BACKGROUND = (230, 230, 230)
GIF_MAX_SIZE = 512
BLENDER_FPS = 24


def contact_sheet(previews: list[Path]) -> bytes:
    """A PNG grid of `previews` in order, each scaled to fit one cell and labelled with its number."""
    if not previews:
        msg = "There are no previews to put on a contact sheet."
        raise ValueError(msg)
    columns = math.ceil(math.sqrt(len(previews)))
    rows = math.ceil(len(previews) / columns)
    cell_height = SHEET_CELL + SHEET_LABEL_HEIGHT
    sheet = Image.new(
        "RGB",
        (columns * SHEET_CELL + (columns + 1) * SHEET_GAP, rows * cell_height + (rows + 1) * SHEET_GAP),
        SHEET_BACKGROUND,
    )
    draw = ImageDraw.Draw(sheet)
    for index, path in enumerate(previews):
        column, row = index % columns, index // columns
        left = SHEET_GAP + column * (SHEET_CELL + SHEET_GAP)
        top = SHEET_GAP + row * (cell_height + SHEET_GAP)
        draw.text((left, top + 2), str(index + 1), fill=(60, 60, 60))
        with Image.open(path) as image:
            thumbnail = ImageOps.contain(image.convert("RGB"), (SHEET_CELL, SHEET_CELL))
        offset_x = left + (SHEET_CELL - thumbnail.width) // 2
        offset_y = top + SHEET_LABEL_HEIGHT + (SHEET_CELL - thumbnail.height) // 2
        sheet.paste(thumbnail, (offset_x, offset_y))
    buffer = io.BytesIO()
    sheet.save(buffer, format="PNG")
    return buffer.getvalue()


def animated_gif(previews: list[Path], frame_step: int = 1) -> bytes:
    """A looping GIF of `previews`, each held for `frame_step` frames at Blender's default 24 fps."""
    if not previews:
        msg = "There are no previews to animate."
        raise ValueError(msg)
    with Image.open(previews[0]) as first:
        size = ImageOps.contain(first, (GIF_MAX_SIZE, GIF_MAX_SIZE)).size
    frames = []
    for path in previews:
        with Image.open(path) as image:
            frame = ImageOps.pad(image.convert("RGB"), size, color=(255, 255, 255))
        frames.append(frame.quantize(colors=256, method=Image.Quantize.MEDIANCUT))
    buffer = io.BytesIO()
    frames[0].save(
        buffer,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=round(1000 * max(1, frame_step) / BLENDER_FPS),
        loop=0,
        disposal=2,
    )
    return buffer.getvalue()
