import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


from executor_system.process_supervisor import run_owned_process


def process_has_not_exited(pid: int) -> bool:
    """Return whether a PID still has a non-zombie process entry on Linux."""

    stat_path = Path("/proc") / str(pid) / "stat"
    try:
        fields = stat_path.read_text(encoding="utf-8").split()
    except FileNotFoundError:
        return False
    return len(fields) > 2 and fields[2] != "Z"


def wait_for_exit(pid: int, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_has_not_exited(pid):
            return True
        time.sleep(0.02)
    return not process_has_not_exited(pid)


class ProcessSupervisorTest(unittest.TestCase):
    def _start_unrelated_group(self, ready_path: Path) -> subprocess.Popen:
        command = [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; import sys, time; "
                "Path(sys.argv[1]).write_text('ready', encoding='utf-8'); "
                "time.sleep(30)"
            ),
            str(ready_path),
        ]
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.addCleanup(self._stop_test_group, process)
        deadline = time.monotonic() + 2
        while not ready_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(ready_path.exists())
        return process

    @staticmethod
    def _stop_test_group(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=2)

    def test_timeout_terminates_only_owned_group_and_keeps_file_logs(self):
        """Removing A's group must not terminate B's independently-owned group."""

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            unrelated = self._start_unrelated_group(root / "b-ready")
            child_pid_path = root / "a-child.pid"
            stdout_path = root / "a.stdout.log"
            stderr_path = root / "a.stderr.log"
            command = [
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; import signal, subprocess, sys, time; "
                    "child = subprocess.Popen([sys.executable, '-c', "
                    "'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)']); "
                    "Path(sys.argv[1]).write_text(str(child.pid), encoding='utf-8'); "
                    "print('parent-ready', flush=True); time.sleep(30)"
                ),
                str(child_pid_path),
            ]

            outcome = run_owned_process(
                command,
                timeout_seconds=0.15,
                termination_grace_seconds=0.25,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                env=os.environ.copy(),
            )

            child_pid = int(child_pid_path.read_text(encoding="utf-8"))
            self.assertTrue(outcome.timed_out)
            self.assertEqual(outcome.pgid, outcome.pid)
            self.assertTrue(any(event["signal"] == "SIGTERM" for event in outcome.termination_events))
            self.assertTrue(any(event["signal"] == "SIGKILL" for event in outcome.termination_events))
            self.assertTrue(wait_for_exit(child_pid))
            self.assertIsNone(unrelated.poll())
            self.assertEqual((root / "b-ready").read_text(encoding="utf-8"), "ready")
            self.assertIn("parent-ready", stdout_path.read_text(encoding="utf-8"))
            self.assertTrue(stderr_path.is_file())

    def test_parent_exit_does_not_leave_term_ignoring_descendant(self):
        """A completed Popen is insufficient when its process group still has children."""

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            child_pid_path = root / "child.pid"
            command = [
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; import signal, subprocess, sys; "
                    "child = subprocess.Popen([sys.executable, '-c', "
                    "'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)']); "
                    "Path(sys.argv[1]).write_text(str(child.pid), encoding='utf-8')"
                ),
                str(child_pid_path),
            ]

            outcome = run_owned_process(
                command,
                timeout_seconds=2,
                termination_grace_seconds=0.25,
                stdout_path=root / "stdout.log",
                stderr_path=root / "stderr.log",
                env=os.environ.copy(),
            )

            child_pid = int(child_pid_path.read_text(encoding="utf-8"))
            self.assertEqual(outcome.returncode, 0)
            self.assertFalse(outcome.timed_out)
            self.assertTrue(any(event["signal"] == "SIGKILL" for event in outcome.termination_events))
            self.assertTrue(wait_for_exit(child_pid))
