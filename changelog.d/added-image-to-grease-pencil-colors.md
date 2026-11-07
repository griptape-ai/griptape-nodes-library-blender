- **Breaking:** `Image to Grease Pencil` now traces in color by default. Its new `Colors` setting
  defaults to 5, so a saved workflow that traced one ink color now gets the line art on `Lines` and
  flat colors on `Fill 1`, `Fill 2`, and so on. Set `Colors` to 0 for the old one-color trace.
  `Colors` traces the line art in `Color`, then splits the rest of the image into that many colors
  and traces each one, leaving out the paper color. `Color Target` chooses where the colors go: a
  layer and material per color, or stamped into the strokes' color attributes on one layer (for
  Vertex Paint). `Min Shape Size` removes the specks and slivers that soft line edges would
  otherwise turn into shapes.
