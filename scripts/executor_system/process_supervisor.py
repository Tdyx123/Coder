"""Supervise child process groups owned by the parallel plan runner."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence


@dataclass
class ProcessOutcome:
    """The observable lifecycle of one process group started by this module."""

    returncode: Optional[int]
    timed_out: bool
    pid: int
    pgid: int
    termination_events: List[Dict[str, object]] = field(default_factory=list)
    wall_time_seconds: float = 0.0


class ProcessStartCancelled(RuntimeError):
    """Raised when shutdown has closed an owned-process scope."""


class OwnedProcessScope:
    """Own process registration and atomically close it during shutdown."""

    def __init__(self) -> None:
        self._processes: Dict[int, subprocess.Popen] = {}
        self._lock = threading.Lock()
        self._closed = False

    def start(
        self,
        command: Sequence[str],
        *,
        stdout_file: object,
        stderr_file: object,
        env: Optional[Mapping[str, str]],
    ) -> tuple[subprocess.Popen, int]:
        # Keep Popen and registration in this lock so shutdown's snapshot
        # cannot miss a session created concurrently with cancellation.
        with self._lock:
            if self._closed:
                raise ProcessStartCancelled("owned process scope is shutting down")
            process = subprocess.Popen(
                list(command),
                stdout=stdout_file,
                stderr=stderr_file,
                env=dict(env) if env is not None else None,
                start_new_session=True,
            )
            pgid = os.getpgid(process.pid)
            self._processes[pgid] = process
            return process, pgid

    def unregister(self, pgid: int) -> None:
        with self._lock:
            self._processes.pop(pgid, None)

    def cleanup(
        self,
        termination_grace_seconds: float,
        *,
        reason: str = "parent-interrupted",
    ) -> List[Dict[str, object]]:
        # Closing and snapshotting share the registration lock. Workers which
        # have not entered Popen now fail before they can create a session.
        with self._lock:
            self._closed = True
            pgids = list(self._processes)

        events: List[Dict[str, object]] = []
        for pgid in pgids:
            deadline = time.monotonic() + termination_grace_seconds
            events.extend(
                terminate_owned_process_group(
                    pgid,
                    termination_grace_seconds=termination_grace_seconds,
                    reason=reason,
                    deadline=deadline,
                )
            )
            if not _group_has_live_members(pgid):
                self.unregister(pgid)
        return events


_default_process_scope = OwnedProcessScope()


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # The group was created by us but cannot be signalled by this process.
        return True
    return True


def _group_has_live_members(pgid: int) -> bool:
    """Ignore zombie entries while checking our registered process group."""

    proc_root = Path("/proc")
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return _group_exists(pgid)
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8")
        except OSError:
            continue
        closing_paren = stat.rfind(")")
        fields = stat[closing_paren + 2 :].split()
        if len(fields) < 3:
            continue
        state, process_group = fields[0], fields[2]
        if state != "Z" and process_group == str(pgid):
            return True
    return False


def _wait_for_group_exit(pgid: int, deadline: float) -> bool:
    while _group_has_live_members(pgid):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.05, remaining))
    return True


def _signal_group(
    pgid: int,
    sig: signal.Signals,
    *,
    reason: str,
    events: List[Dict[str, object]],
) -> None:
    event: Dict[str, object] = {
        "pgid": pgid,
        "reason": reason,
        "signal": sig.name,
        "error": "",
    }
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        event["error"] = "process group already exited"
    except OSError as exc:
        event["error"] = str(exc)
    events.append(event)


def terminate_owned_process_group(
    pgid: int,
    *,
    termination_grace_seconds: float,
    reason: str,
    deadline: Optional[float] = None,
) -> List[Dict[str, object]]:
    """Stop exactly one previously registered process group within its budget."""

    events: List[Dict[str, object]] = []
    if not _group_has_live_members(pgid):
        return events

    started = time.monotonic()
    cleanup_deadline = deadline if deadline is not None else (
        started + termination_grace_seconds
    )
    term_deadline = min(
        cleanup_deadline,
        started + (termination_grace_seconds * 0.4),
    )
    _signal_group(pgid, signal.SIGTERM, reason=reason, events=events)
    _wait_for_group_exit(pgid, term_deadline)

    if _group_has_live_members(pgid):
        _signal_group(pgid, signal.SIGKILL, reason=reason, events=events)
        if not _wait_for_group_exit(pgid, cleanup_deadline):
            events.append(
                {
                    "pgid": pgid,
                    "reason": reason,
                    "signal": "SIGKILL",
                    "error": "process group remained active after termination budget",
                }
            )
    return events


def cleanup_owned_processes(
    termination_grace_seconds: float,
    *,
    reason: str = "parent-interrupted",
) -> List[Dict[str, object]]:
    """Best-effort cleanup for only process groups registered by this module."""

    return _default_process_scope.cleanup(
        termination_grace_seconds,
        reason=reason,
    )


def run_owned_process(
    command: Sequence[str],
    *,
    timeout_seconds: float,
    termination_grace_seconds: float,
    stdout_path: Path,
    stderr_path: Path,
    env: Optional[Mapping[str, str]],
    process_scope: Optional[OwnedProcessScope] = None,
) -> ProcessOutcome:
    """Run one command in a fresh session and reclaim its entire process group."""

    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    scope = process_scope or _default_process_scope
    with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
        process, pgid = scope.start(
            command,
            stdout_file=stdout_file,
            stderr_file=stderr_file,
            env=env,
        )

        timed_out = False
        events: List[Dict[str, object]] = []
        try:
            cleanup_deadline: Optional[float] = None
            try:
                process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                cleanup_deadline = time.monotonic() + termination_grace_seconds
                events.extend(
                    terminate_owned_process_group(
                        pgid,
                        termination_grace_seconds=termination_grace_seconds,
                        reason="timeout",
                        deadline=cleanup_deadline,
                    )
                )

            # The group can retain descendants even after Popen itself exits.
            if not timed_out and _group_has_live_members(pgid):
                cleanup_deadline = time.monotonic() + termination_grace_seconds
                events.extend(
                    terminate_owned_process_group(
                        pgid,
                        termination_grace_seconds=termination_grace_seconds,
                        reason="process-exited-with-descendants",
                        deadline=cleanup_deadline,
                    )
                )
            if process.poll() is None:
                remaining = max(0.0, (cleanup_deadline or time.monotonic()) - time.monotonic())
                if remaining > 0:
                    try:
                        process.wait(timeout=remaining)
                    except subprocess.TimeoutExpired:
                        pass
                if process.poll() is None:
                    events.append(
                        {
                            "pgid": pgid,
                            "reason": "process-not-reaped",
                            "signal": "SIGKILL",
                            "error": "leader remained active after termination budget",
                        }
                    )
        finally:
            if not _group_has_live_members(pgid):
                scope.unregister(pgid)

    return ProcessOutcome(
        returncode=process.returncode,
        timed_out=timed_out,
        pid=process.pid,
        pgid=pgid,
        termination_events=events,
        wall_time_seconds=time.monotonic() - started,
    )
