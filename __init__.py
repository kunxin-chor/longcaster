from pathlib import Path

import folder_paths


# Video Helper Suite discovers extra encoder profiles through this shared path.
# Registering the directory is harmless when VHS is absent; workflows may replace
# its output node with ComfyUI's native Save Video in that case.
folder_paths.add_model_folder_path(
    "VHS_video_formats", str(Path(__file__).resolve().parent / "video_formats")
)

# Register the external VHS format before importing node definitions. VHS caches
# its dynamic format list briefly while ComfyUI builds node schemas at startup.
from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS

WEB_DIRECTORY = "./web"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
