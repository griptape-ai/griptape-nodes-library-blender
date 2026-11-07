"""Trace line art into continuous strokes with a width, instead of outlines around each line.

This runs in the engine, never in Blender: it needs scikit-image, scipy and skan, which Blender's Python lacks.
`trace_centerlines` returns the strokes in pixel coordinates plus a mask of the solid areas (pupils, heavy blobs),
which a skeleton cannot represent. The background Blender traces those into filled shapes, as it does for any ink.

The steps are: separate solid areas from lines, thin the lines to one pixel, walk that skeleton into paths, drop
the spurs and specks thinning leaves, join paths through each junction along the straightest pairs so lines run
on through crossings, then read each line's width from the distance transform, smooth, and simplify.
"""

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import skan
from PIL import Image, ImageDraw
from scipy import ndimage
from skimage import measure, morphology

SPUR_WIDTHS = 1.5  # a spur is also anything shorter than this many half-widths of the line it sits on
SPUR_MIN = 6  # a free-ended branch into a junction shorter than this many pixels is a spur
SPECK_MIN = 6  # an isolated piece shorter than this is noise, unless it is thick
DOT_RATIO = 2.0  # a stub end at least this many times the typical line radius is a dot
THICK_KEEP = 2.0  # a short piece at least this many pixels in radius is kept, as it is a dot rather than noise
SOLID_REACH = 3  # a line ending this close to a solid area meets it
MERGE_JUNCTIONS = 3  # junction pixels this close (in steps) belong to one crossing, such as the two ends of a thick X
INTERNAL_MAX = 10  # a path between two pixels of one junction cluster shorter than this is inside the junction
STRAIGHT_COS = 0.70  # two branches join when their directions are at least about 135 degrees apart
DIRECTION_STEPS = 7  # how far along a branch to look for its direction
SIMPLIFY_PX = 0.6
SMOOTH_WINDOW = 5
RADIUS_SMOOTH_WINDOW = (
    3  # the width is smoothed less than the position, so a short bulge such as a pupil keeps its peak
)
MIN_SOLID_RADIUS = 2
MIN_RESOLUTION = 1500  # images with a shorter long side are scaled up to at least this before tracing
GAP_MIN_AREA = 12  # bare ink smaller than this many pixels is a ragged edge, not a gap at a join
GAP_MIN_DEPTH = 3  # bare ink has to reach this far into the ink, as a wedge at a join does and an edge sliver does not
GAP_OVERLAP = 3  # the gap fill is grown this many pixels into the strokes beside it, so no seam shows between them
HUB_MARGIN = 2  # pixels added to a line's half width when filling the ink around a junction
LINE_RADIUS_PERCENTILE = 90  # the line half width the color fills are grown to cover
SOLID_PERCENTILE = 99  # solid means wider than this share of the drawing's line length
RIDGE_RADIUS = 3  # a dark feature narrower than about twice this many pixels is a line, not a fill
RIDGE_CONTRAST = 0.5  # of the way from the threshold to white that a line must stand out from its surroundings
MIN_WIDTH_PX = 0.8
RADIUS_OFFSET = 0.0  # the distance transform of a line's center pixel reads half a pixel over its half width, which pads the stroke so fills do not show a sliver beside it
PATH_ENDS = (0, 1)
MIN_STROKE_POINTS = 2
NEIGHBOURS = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]


@dataclass
class Centerlines:
    """Result of tracing one image. Strokes are `{"pts": [[row, col], ...], "r": [radius, ...]}` in pixels."""

    size: tuple[int, int]
    strokes: list[dict]
    solid: np.ndarray
    ink: np.ndarray
    line_radius: float = 0.0

    def save(self, json_path: Path, solid_path: Path, ink_path: Path) -> None:
        """Write the strokes to `json_path`. The solid areas and the ink go to PNGs as black on white, or are left out."""
        has_solid = bool(self.solid.any())
        has_ink = bool(self.ink.any())
        for present, mask, path in ((has_solid, self.solid, solid_path), (has_ink, self.ink, ink_path)):
            if present:
                Image.fromarray(np.where(mask, 0, 255).astype(np.uint8)).convert("RGB").save(path)
        content = {
            "size": list(self.size),
            "strokes": self.strokes,
            "solid": str(solid_path) if has_solid else "",
            "ink": str(ink_path) if has_ink else "",
            "line_radius": self.line_radius,
        }
        json_path.write_text(json.dumps(content), encoding="utf-8")


def _working_size(width: int, height: int, min_resolution: int, max_resolution: int) -> tuple[int, int]:
    """The size to trace at. Small images are scaled up, because lines a couple of pixels wide and pupils a few pixels
    across are too coarse for thinning to follow. Large ones are scaled down to `max_resolution`."""
    longest = max(width, height)
    factor = 1.0
    if min_resolution and longest < min_resolution:
        factor = float(np.ceil(min_resolution / longest))
    if max_resolution and longest * factor > max_resolution:
        factor = max_resolution / longest
    if factor == 1.0:
        return width, height
    return max(1, round(width * factor)), max(1, round(height * factor))


def _load_ink(path: str, threshold: float, invert: bool, min_resolution: int, max_resolution: int) -> np.ndarray:
    """The ink mask, built the way the Blender side builds it: flatten on white, invert, rescale, threshold."""
    with Image.open(path) as opened:
        rgba = opened.convert("RGBA")
    pixels = np.asarray(rgba, dtype=np.float32) / 255.0
    alpha = pixels[:, :, 3:4]
    rgb = pixels[:, :, :3] * alpha + (1.0 - alpha)
    if invert:
        rgb = 1.0 - rgb
    picture = Image.fromarray(np.rint(rgb * 255).astype(np.uint8))
    size = _working_size(*picture.size, min_resolution, max_resolution)
    if size != picture.size:
        upscaling = size[0] > picture.width
        picture = picture.resize(size, Image.Resampling.BICUBIC if upscaling else Image.Resampling.BILINEAR)
    brightness = np.asarray(picture).max(axis=2).astype(np.float32) / 255.0
    # Thin soft lines never get dark enough to pass the threshold, and raising it turns gray fills to ink. A thin dark
    # ridge is much darker than its surroundings within a few pixels, which a wide gray fill is not, so a black
    # top-hat (closing minus the image) finds the lines at any gray level.
    ridge = ndimage.grey_closing(brightness, footprint=morphology.disk(RIDGE_RADIUS)) - brightness
    return (brightness < threshold) | (ridge > (1.0 - threshold) * RIDGE_CONTRAST)


def _degrees(skeleton: np.ndarray) -> np.ndarray:
    padded = np.pad(skeleton.astype(np.int8), 1)
    height, width = skeleton.shape
    degree = np.zeros(skeleton.shape, dtype=np.int8)
    for dy, dx in NEIGHBOURS:
        degree += padded[1 + dy : 1 + dy + height, 1 + dx : 1 + dx + width]
    return degree * skeleton


def _paths(skeleton: np.ndarray) -> list[np.ndarray]:
    # skan raises on a skeleton with no neighbouring pixels at all, so lone pixels are left out first.
    skeleton = skeleton & (_degrees(skeleton) > 0)
    if not skeleton.any():
        return []
    graph = skan.Skeleton(skeleton, keep_images=False)
    return [np.rint(graph.path_coordinates(index)).astype(int) for index in range(graph.n_paths)]


def _redraw(paths: list[np.ndarray], shape: tuple[int, int]) -> np.ndarray:
    drawn = np.zeros(shape, dtype=bool)
    for path in paths:
        drawn[path[:, 0], path[:, 1]] = True
    return drawn


def _solid_areas(ink: np.ndarray, radii: np.ndarray) -> np.ndarray:
    """Pixels where a disk wider than the drawing's lines fits. `radii` are the half widths along the lines."""
    radius = max(MIN_SOLID_RADIUS, int(np.ceil(np.percentile(radii, SOLID_PERCENTILE) + 0.5)))
    return ndimage.binary_opening(ink, structure=morphology.disk(radius))


def _prune(paths: list[np.ndarray], shape: tuple[int, int], distance: np.ndarray, typical: float) -> list[np.ndarray]:
    """Drop short spurs and specks that thinning leaves. Twice, because removing a spur can expose another.

    A short stub whose free end is much thicker than the drawing's lines is a dot, such as a pupil, and stays.
    A fork beside a crossing is only as thick as its line, so it goes.
    """
    for _ in range(2):
        degree = _degrees(_redraw(paths, shape))
        kept = []
        for path in paths:
            free_start = degree[tuple(path[0])] <= 1
            free_end = degree[tuple(path[-1])] <= 1
            radius = float(distance[path[:, 0], path[:, 1]].max()) - 0.5
            if free_start and free_end and len(path) < SPECK_MIN and radius < THICK_KEEP:
                continue
            if free_start != free_end and len(path) < max(SPUR_MIN, SPUR_WIDTHS * radius):
                tip = path[0] if free_start else path[-1]
                if float(distance[tip[0], tip[1]]) - 0.5 < max(THICK_KEEP, DOT_RATIO * typical):
                    continue
            kept.append(path)
        paths = _paths(_redraw(kept, shape))
    return paths


class _Junctions:
    """Junction pixels and solid areas grouped into clusters, so the branches meeting at one place can be paired up.

    A solid area is a junction too: where thick lines cross, their center is wide enough to count as solid, and the
    arms that end at it have to be joined back through it.
    """

    def __init__(self, paths: list[np.ndarray], shape: tuple[int, int], solid: np.ndarray) -> None:
        degree = _degrees(_redraw(paths, shape))
        self.junction = degree >= 3  # noqa: PLR2004
        grown = ndimage.binary_dilation(self.junction, iterations=MERGE_JUNCTIONS)
        self.labels = np.asarray(measure.label(grown, connectivity=2))
        self.centroid = {
            int(label): np.array(ndimage.center_of_mass(self.labels == label))
            for label in np.unique(self.labels[self.labels > 0])
        }
        near_solid = np.asarray(measure.label(ndimage.binary_dilation(solid, iterations=SOLID_REACH), connectivity=2))
        offset = int(self.labels.max())
        self.solid_labels = np.where(near_solid > 0, near_solid + offset, 0)
        for label in np.unique(self.solid_labels[self.solid_labels > 0]):
            self.centroid[int(label)] = np.array(ndimage.center_of_mass(self.solid_labels == label))
        self.shape = shape

    def cluster_at(self, point: tuple[int, int]) -> int:
        """The cluster a path end sits on or beside, or 0."""
        height, width = self.shape
        row, col = point
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                y, x = row + dy, col + dx
                if 0 <= y < height and 0 <= x < width and self.junction[y, x]:
                    return int(self.labels[y, x])
        return int(self.solid_labels[row, col])


def _direction(path: np.ndarray, end: int) -> np.ndarray:
    """Unit vector pointing from a path's end along the path."""
    ordered = path if end == 0 else path[::-1]
    reach = min(len(ordered) - 1, DIRECTION_STEPS)
    vector = ordered[reach].astype(float) - ordered[0].astype(float)
    length = float(np.linalg.norm(vector))
    return vector / length if length else vector


def _end_point(path: np.ndarray, end: int) -> tuple[int, int]:
    row, col = path[0] if end == 0 else path[-1]
    return int(row), int(col)


def _pair_branches(paths: list[np.ndarray], junctions: _Junctions) -> dict[tuple[int, int], tuple[int, int]]:
    """Link the straightest pairs of branches at each junction. Maps (path, end) to the (path, end) it continues into."""
    at_cluster: dict[int, list[tuple[int, int]]] = {}
    for index, path in enumerate(paths):
        for end in PATH_ENDS:
            cluster = junctions.cluster_at(_end_point(path, end))
            if cluster:
                at_cluster.setdefault(cluster, []).append((index, end))
    link: dict[tuple[int, int], tuple[int, int]] = {}
    for ends in at_cluster.values():
        candidates = []
        for a in range(len(ends)):
            for b in range(a + 1, len(ends)):
                (first, first_end), (second, second_end) = ends[a], ends[b]
                if first == second:
                    continue
                cosine = float(np.dot(_direction(paths[first], first_end), _direction(paths[second], second_end)))
                if -cosine >= STRAIGHT_COS:
                    candidates.append((-cosine, ends[a], ends[b]))
        for _, x, y in sorted(candidates, key=lambda candidate: -candidate[0]):
            if x in link or y in link:
                continue
            link[x] = y
            link[y] = x
    return link


def _chain(
    paths: list[np.ndarray], link: dict[tuple[int, int], tuple[int, int]], junctions: _Junctions
) -> list[tuple[np.ndarray, int, int]]:
    """Follow the links into strokes. Each is (points, junction cluster at its start, junction cluster at its end)."""
    visited: set[int] = set()

    def walk(first: int, enter_end: int) -> tuple[np.ndarray, int, int]:
        start_cluster = 0 if (first, enter_end) in link else junctions.cluster_at(_end_point(paths[first], enter_end))
        pieces = []
        current, end = first, enter_end
        while True:
            visited.add(current)
            piece = paths[current] if end == 0 else paths[current][::-1]
            pieces.append(piece)
            following = link.get((current, 1 - end))
            if following is None or following[0] in visited:
                end_cluster = junctions.cluster_at((int(piece[-1][0]), int(piece[-1][1]))) if following is None else 0
                return np.concatenate(pieces).astype(float), start_cluster, end_cluster
            current, end = following

    strokes = []
    for index in range(len(paths)):
        if index in visited:
            continue
        for end in PATH_ENDS:
            if (index, end) not in link:
                strokes.append(walk(index, end))
                break
    strokes.extend(walk(index, 0) for index in range(len(paths)) if index not in visited)  # closed loops
    return strokes


def _smooth(values: np.ndarray, window: int) -> np.ndarray:
    """Moving average that keeps the ends where they are."""
    pad = window // 2
    if len(values) < window + 1 or pad == 0:
        return values
    if values.ndim == 2:  # noqa: PLR2004
        head = values[0] * 2 - values[pad:0:-1]
        tail = values[-1] * 2 - values[-2 : -pad - 2 : -1]
    else:
        head = np.full(pad, values[0])
        tail = np.full(pad, values[-1])
    padded = np.concatenate([head, values, tail])
    kernel = np.ones(window) / window
    if values.ndim == 2:  # noqa: PLR2004
        return np.stack([np.convolve(padded[:, axis], kernel, mode="valid") for axis in range(2)], axis=1)
    return np.convolve(padded, kernel, mode="valid")


def _simplify(points: np.ndarray, radii: np.ndarray, tolerance: float) -> np.ndarray:
    """Indices of the points Douglas-Peucker keeps, judging the width as well as the position.

    Without the width, a straight run with a bulge in it, such as a pupil on a line, would lose its middle points and
    the bulge with them.
    """
    samples = np.column_stack([points, radii])
    keep = np.zeros(len(samples), dtype=bool)
    keep[[0, -1]] = True
    pending = [(0, len(samples) - 1)]
    while pending:
        low, high = pending.pop()
        if high <= low + 1:
            continue
        origin, target = samples[low], samples[high]
        span = target - origin
        length = float(np.linalg.norm(span))
        between = samples[low + 1 : high]
        if length == 0:
            offsets = np.linalg.norm(between - origin, axis=1)
        else:
            offsets = np.linalg.norm(np.cross(between - origin, span), axis=1) / length
        worst = int(np.argmax(offsets))
        if offsets[worst] > tolerance:
            index = low + 1 + worst
            keep[index] = True
            pending.extend([(low, index), (index, high)])
    return np.flatnonzero(keep)


def _finish(
    points: np.ndarray, start_cluster: int, end_cluster: int, distance: np.ndarray, junctions: _Junctions
) -> dict:
    """Give a stroke its widths, smooth it, close it up to its junctions or extend its free ends, and simplify it."""
    radii = np.maximum(
        ndimage.map_coordinates(distance, [points[:, 0], points[:, 1]], order=1) - RADIUS_OFFSET, MIN_WIDTH_PX
    )
    points, radii = _smooth(points, SMOOTH_WINDOW), _smooth(radii, RADIUS_SMOOTH_WINDOW)
    # A skeleton stops about half a width short of a line's tip, so free ends are extended by that much. An end that
    # stops at a junction goes on to the junction's center, so it touches the line it meets.
    heads, tails = [], []
    for at_start, cluster in ((True, start_cluster), (False, end_cluster)):
        ordered = points if at_start else points[::-1]
        if cluster:
            extra = junctions.centroid.get(cluster)
            added = [] if extra is None else [extra]
        else:
            reach = min(len(ordered) - 1, 6)
            vector = ordered[0] - ordered[reach]
            length = float(np.linalg.norm(vector))
            added = [ordered[0] + vector / length * radii[0 if at_start else -1]] if length else []
        (heads if at_start else tails).extend(added)
    if heads:
        points = np.vstack([np.array(heads), points])
        radii = np.concatenate([[radii[0]] * len(heads), radii])
    if tails:
        points = np.vstack([points, np.array(tails)])
        radii = np.concatenate([radii, [radii[-1]] * len(tails)])
    keep = _simplify(points, radii, SIMPLIFY_PX)
    return {"pts": points[keep].tolist(), "r": radii[keep].tolist()}


def _covered(strokes: list[dict], shape: tuple[int, int]) -> np.ndarray:
    """The pixels the strokes paint: a round brush at each point, joined by its widest neighbor's width."""
    canvas = Image.new("L", (shape[1], shape[0]), 0)
    draw = ImageDraw.Draw(canvas)
    for stroke in strokes:
        points, radii = stroke["pts"], stroke["r"]
        for (row, col), radius in zip(points, radii, strict=True):
            draw.ellipse([col - radius, row - radius, col + radius, row + radius], fill=255)
        for (row0, col0), (row1, col1), radius0, radius1 in zip(points, points[1:], radii, radii[1:], strict=False):
            draw.line([(col0, row0), (col1, row1)], fill=255, width=max(1, round(2 * max(radius0, radius1))))
    return np.asarray(canvas) > 0


def _junction_gaps(strokes: list[dict], ink: np.ndarray, solid: np.ndarray, distance: np.ndarray) -> np.ndarray:
    """Ink the strokes leave bare where lines meet, as a mask to fill in.

    Strokes are round, but the ink at a T or a corner has a pointed wedge beside the join, and the color behind shows
    through it. Those wedges reach deep into the ink, while the slivers along a stroke's edge do not.
    """
    bare = ink & ~_covered(strokes, ink.shape) & ~solid
    labels = np.asarray(measure.label(bare, connectivity=2))
    keep = np.zeros_like(bare)
    for region in measure.regionprops(labels):
        rows, cols = region.coords[:, 0], region.coords[:, 1]
        if region.area >= GAP_MIN_AREA and float(distance[rows, cols].max()) >= GAP_MIN_DEPTH:
            keep[rows, cols] = True
    return ndimage.binary_dilation(keep, iterations=GAP_OVERLAP) & ink


def _junction_hubs(junctions: "_Junctions", ink: np.ndarray, typical: float) -> np.ndarray:
    """The ink around each junction, out to about one line width. Overlapping strokes leave small seams where they
    meet, so the join is filled solid instead of left to the strokes."""
    reach = int(np.ceil(typical)) + HUB_MARGIN
    return ndimage.binary_dilation((junctions.labels > 0) | (junctions.solid_labels > 0), iterations=reach) & ink


def trace_centerlines(
    path: str,
    threshold: float = 0.5,
    invert: bool = False,
    max_resolution: int = 2048,
    min_resolution: int = MIN_RESOLUTION,
) -> Centerlines:
    """Trace the dark parts of the image at `path` into continuous strokes and a mask of solid areas.

    The result is at the size the image was traced at, which is `Centerlines.size`. Pass `min_resolution=0` to keep
    small images at their own size.
    """
    ink = _load_ink(path, threshold, invert, min_resolution, max_resolution)
    height, width = ink.shape
    if not ink.any():
        return Centerlines((width, height), [], np.zeros_like(ink), ink)
    distance = np.asarray(ndimage.distance_transform_edt(ink), dtype=np.float32)
    radii = distance[morphology.skeletonize(ink)] - 0.5
    solid = _solid_areas(ink, radii)
    lines = ink & ~ndimage.binary_dilation(solid, iterations=1)
    typical = float(np.median(radii))
    paths = _prune(_paths(morphology.skeletonize(lines)), ink.shape, distance, typical)
    junctions = _Junctions(paths, ink.shape, solid)
    paths = [
        path
        for path in paths
        if not (
            junctions.cluster_at(_end_point(path, 0))
            and junctions.cluster_at(_end_point(path, 0)) == junctions.cluster_at(_end_point(path, 1))
            and len(path) < INTERNAL_MAX
        )
    ]
    strokes = [
        _finish(points, start, end, distance, junctions)
        for points, start, end in _chain(paths, _pair_branches(paths, junctions), junctions)
    ]
    strokes = [s for s in strokes if len(s["pts"]) >= MIN_STROKE_POINTS]
    return Centerlines(
        (width, height),
        strokes,
        solid | _junction_gaps(strokes, ink, solid, distance) | _junction_hubs(junctions, ink, typical),
        ink,
        float(np.percentile(radii, LINE_RADIUS_PERCENTILE)),
    )
