- **Breaking:** `Blender Camera Capture` and `Blender Camera List` fail the node when Blender cannot be reached,
  reports an error, or sends back an unusable image. The editor shows the reason, and Blender's reply under
  More info. Before, they ended normally and put an error on the output, so a workflow that read the error from
  `image_output` or `cameras_output` no longer sees it. Every Blender node now shows the host and port, and a link
  to the Blender Lab MCP add-on, when it cannot reach Blender. Needs Griptape Nodes engine 0.105.0 or later.
  https://github.com/griptape-ai/griptape-nodes-library-blender/issues/7
