- **Breaking:** `Image to Grease Pencil` now draws lines as continuous centerline strokes by
  default, with the width of the original line and solid areas such as pupils kept as filled
  shapes, where it used to outline every line and fill it. Set the new `Line Style` to `Outline` for
  the old result. Centerline tracing needs the scikit-image, scipy and skan packages, which the
  library now installs.
