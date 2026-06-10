"""Optional runtime dependency loading for AI2-THOR execution."""

_MISSING_DEPENDENCIES = []

try:
    import cv2
except ImportError:
    cv2 = None
    _MISSING_DEPENDENCIES.append("opencv-python (cv2)")

try:
    from ai2thor.controller import Controller
    from ai2thor.platform import CloudRendering
except ImportError:
    Controller = None
    CloudRendering = None
    _MISSING_DEPENDENCIES.append("ai2thor")

def require_dependencies() -> None:
    if not _MISSING_DEPENDENCIES:
        return
    deps = ", ".join(sorted(set(_MISSING_DEPENDENCIES)))
    raise RuntimeError(
        "Missing required runtime dependencies: "
        f"{deps}. Install them before running the AI2-THOR simulation."
    )
