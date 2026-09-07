"""Owned run directories, frame/video output and final scene metadata.

Dependencies for frame conversion, OpenCV and metadata configuration are
explicit providers so runtime compatibility patch points remain live after
construction. Mutable counters and controller events stay on the runtime.
"""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence

from .config import DIRECTIONAL_VIEW_NAMES, THIRD_PARTY_VIEW_NAMES, TOP_VIEW_NAME


class RuntimeArtifacts:
    def __init__(
        self, runtime, *, cv2_provider: Callable, frame_converter: Callable,
        metadata_enabled: Callable, logger: Callable,
    ):
        self.runtime = runtime
        self._cv2_provider = cv2_provider
        self._frame_converter = frame_converter
        self._metadata_enabled = metadata_enabled
        self._log = logger

    @staticmethod
    def resolve_output_root(output_root: Optional[Path]) -> Path:
        """Return an owned, absolute media directory for one runtime instance."""

        module_source_root = Path(__file__).resolve().parent
        if output_root is None:
            return Path(tempfile.mkdtemp(prefix="lammap-thor-")).resolve()

        container = Path(output_root).expanduser().resolve()
        # ``prepare_output_dirs`` removes media-shaped children.  Refuse the
        # module directory and every ancestor of it so a caller cannot turn
        # that targeted cleanup into source-tree cleanup.
        if module_source_root == container or module_source_root.is_relative_to(container):
            raise ValueError(
                "output_root must be a run-output container, not the runtime "
                "source directory or one of its ancestors."
            )
        if container.exists() and not container.is_dir():
            raise ValueError("output_root must be a directory")
        container.mkdir(parents=True, exist_ok=True)
        # An explicit root is a caller-owned container, which can hold metrics,
        # prior attempts, or concurrent runtimes.  Only a freshly allocated
        # child belongs to this runtime and may be cleaned by prepare_output_dirs.
        return Path(tempfile.mkdtemp(prefix="lammap-runtime-", dir=str(container))).resolve()

    def prepare(self) -> None:
        runtime = self.runtime
        if not runtime.render_image:
            return

        for path in runtime.output_root.glob("agent_*"):
            if path.is_dir():
                shutil.rmtree(path)
        for view_name in THIRD_PARTY_VIEW_NAMES + DIRECTIONAL_VIEW_NAMES:
            view_path = runtime.output_root / view_name
            if view_path.is_dir():
                shutil.rmtree(view_path)
        for video_path in runtime.output_root.glob("video_*.mp4"):
            if video_path.is_file():
                video_path.unlink()

        for i in range(runtime.physical_agent_count):
            (runtime.output_root / f"agent_{i + 1}").mkdir(parents=True, exist_ok=True)
        for view_name in THIRD_PARTY_VIEW_NAMES:
            (runtime.output_root / view_name).mkdir(parents=True, exist_ok=True)

    def write_final_metadata(self) -> Optional[Path]:
        runtime = self.runtime
        if not self._metadata_enabled():
            return None
        with runtime.controller_lock:
            last_event = (
                None
                if runtime.controller is None
                else getattr(runtime.controller, "last_event", None)
            )
            metadata = getattr(last_event, "metadata", {}) or {}
        metadata_path = runtime.output_root / "metadata.txt"
        metadata_path.write_text(
            json.dumps(metadata, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        return metadata_path

    def save_frames(self, event) -> None:
        runtime = self.runtime
        if not runtime.render_image:
            return

        cv2 = self._cv2_provider()
        events = list(getattr(event, "events", None) or [event])
        wrote_frame = False
        for i, agent_event in enumerate(events[: runtime.physical_agent_count]):
            frame = self._frame_converter(agent_event)
            if frame is None:
                continue
            frame_path = runtime.output_root / f"agent_{i + 1}" / f"img_{runtime.frame_counter:05d}.png"
            if not cv2.imwrite(str(frame_path), frame):
                self._log(f"Warning: failed to write frame {frame_path}")
            else:
                wrote_frame = True
            if runtime.show_windows:
                cv2.imshow(f"agent{i}", frame)

        third_party_frames = runtime.event_third_party_camera_frames(event, events)
        for view_name, view_frame in zip(runtime.active_third_party_view_names(), third_party_frames):
            view_bgr = cv2.cvtColor(view_frame, cv2.COLOR_RGB2BGR)
            frame_path = runtime.output_root / view_name / f"img_{runtime.frame_counter:05d}.png"
            if not cv2.imwrite(str(frame_path), view_bgr):
                self._log(f"Warning: failed to write frame {frame_path}")
            else:
                wrote_frame = True
            if runtime.show_windows:
                cv2.imshow(view_name.replace("_", " ").title(), view_bgr)

        if runtime.render_image and not wrote_frame and not runtime.missing_frame_warning_emitted:
            metadata = getattr(event, "metadata", {}) or {}
            action = metadata.get("lastAction") or "<unknown>"
            self._log(
                "Warning: renderImage=1 but AI2-THOR returned no frame "
                f"for action {action}; no image was saved."
            )
            runtime.missing_frame_warning_emitted = True

        if runtime.show_windows:
            cv2.waitKey(25)
        runtime.frame_counter += 1

    def active_third_party_view_names(self) -> List[str]:
        runtime = self.runtime
        view_names = getattr(runtime, "third_party_view_names", None)
        if view_names:
            return list(view_names)
        if getattr(runtime, "top_view_enabled", False):
            return [TOP_VIEW_NAME]
        return []

    def event_third_party_camera_frames(
        self,
        event: Any,
        events: Sequence[Any],
    ) -> List[Any]:
        frame_sources = [event]
        if events:
            frame_sources.append(events[0])
        for frame_source in frame_sources:
            third_party_frames = getattr(frame_source, "third_party_camera_frames", None)
            if third_party_frames:
                return list(third_party_frames)
        return []

    def generate_video(self) -> None:
        runtime = self.runtime
        if not runtime.render_image:
            return

        if shutil.which("ffmpeg") is None:
            self._log("ffmpeg not found; skipping video generation.")
            return

        frame_rate = 5
        view_folders = [runtime.output_root / view_name for view_name in THIRD_PARTY_VIEW_NAMES]
        for imgs_folder in sorted(runtime.output_root.glob("agent_*")) + view_folders:
            if not imgs_folder.is_dir() or not any(imgs_folder.glob("img_*.png")):
                continue
            view = imgs_folder.name
            command_set = [
                "ffmpeg",
                "-y",
                "-framerate",
                str(frame_rate),
                "-i",
                str(imgs_folder / "img_%05d.png"),
                "-pix_fmt",
                "yuv420p",
                str(runtime.output_root / f"video_{view}.mp4"),
            ]
            result = subprocess.run(
                command_set,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                self._log(f"Warning: ffmpeg failed for {view}: {result.stderr.strip()}")

    def close_windows(self) -> None:
        cv2 = self._cv2_provider()
        if self.runtime.show_windows and cv2 is not None:
            cv2.destroyAllWindows()
