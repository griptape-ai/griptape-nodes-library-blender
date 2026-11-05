"""Save media for the camera plane nodes in the project, so Blender can point at a file that stays put."""

import hashlib
import re
from pathlib import Path

from griptape_nodes.exe_types.param_components.project_file_parameter import ProjectFileParameter
from griptape_nodes.files.file import File
from griptape_nodes.files.project_file import ProjectFileDestination
from griptape_nodes.retained_mode.events.os_events import ExistingFilePolicy


def hashed_filename(stem: str, content: bytes, suffix: str) -> str:
    """`stem` and `suffix` around a short hash of `content`.

    Each distinct image gets its own file, so a plane made earlier keeps its picture when the node runs again with a
    different one. Running again with the same image writes the same file.
    """
    return f"{stem}_{hashlib.sha256(content).hexdigest()[:10]}{suffix}"


def project_path(node_name: str, filename: str) -> Path:
    """Where the project would save `filename` for the node called `node_name`.

    The node name is in the filename because the project's template may leave it out, and two nodes would then share
    a file. `_index` is bound because only CREATE_NEW fills it, and resolve() fails while it's required and empty.
    """
    node = re.sub(r"[^A-Za-z0-9_-]+", "_", node_name).strip("_")
    situated = ProjectFileDestination.from_situation(
        f"{node}_{filename}", ProjectFileParameter.DEFAULT_SITUATION, _index=1
    )
    return Path(situated.resolve())


def save_project_file(node_name: str, filename: str, content: bytes) -> File:
    """Write `content` to the project as `filename` for the node, replacing an earlier run's file."""
    destination = ProjectFileDestination(
        str(project_path(node_name, filename)), existing_file_policy=ExistingFilePolicy.OVERWRITE
    )
    return destination.write_bytes(content)
