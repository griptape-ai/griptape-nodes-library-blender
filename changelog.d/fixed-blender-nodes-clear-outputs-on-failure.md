- A Blender node that fails no longer leaves the last run's outputs in place, so a wired `Failed` output does not
  pass on stale values. The errors for a missing image or movie, a missing file, and a failed trace now name the
  parameter to fix and say what to do next. `Blender Camera List` is marked to run again every time, so it reads
  the cameras in Blender each run.
