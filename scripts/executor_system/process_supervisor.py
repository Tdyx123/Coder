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


_owned_processes: Dict[int, subprocess.Popen] = {}
_owned_processes_lock = threading.Lock()


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
) -> List[Dict[str, object]]:
    """Stop exactly one previously registered process group within its budget."""

    events: List[Dict[str, object]] = []
    if not _group_has_live_members(pgid):
        return events

    started = time.monotonic()
    term_deadline = started + (termination_grace_seconds * 0.4)
    _signal_group(pgid, signal.SIGTERM, reason=reason, events=events)
    _wait_for_group_exit(pgid, term_deadline)

    if _group_has_live_members(pgid):
        kill_deadline = started + termination_grace_seconds
        _signal_group(pgid, signal.SIGKILL, reason=reason, events=events)
        if not _wait_for_group_exit(pgid, kill_deadline):
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

    with _owned_processes_lock:
        pgids = list(_owned_processes)

    events: List[Dict[str, object]] = []
    for pgid in pgids:
        events.extend(
            terminate_owned_process_group(
                pgid,
                termination_grace_seconds=termination_grace_seconds,
                reason=reason,
            )
        )
        if not _group_has_live_members(pgid):
            with _owned_processes_lock:
                _owned_processes.pop(pgid, None)
    return events


def run_owned_process(
    command: Sequence[str],
    *,
    timeout_seconds: float,
    termination_grace_seconds: float,
    stdout_path: Path,
    stderr_path: Path,
    env: Optional[Mapping[str, str]],
) -> ProcessOutcome:
    """Run one command in a fresh session and reclaim its entire process group."""

    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
        process = subprocess.Popen(
            list(command),
            stdout=stdout_file,
            stderr=stderr_file,
            env=dict(env) if env is not None else None,
            start_new_session=True,
        )
        pgid = os.getpgid(process.pid)
        with _owned_processes_lock:
            _owned_processes[pgid] = process

        timed_out = False
        events: List[Dict[str, object]] = []
        try:
            try:
                process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                events.extend(
                    terminate_owned_process_group(
                        pgid,
                        termination_grace_seconds=termination_grace_seconds,
                        reason="timeout",
                    )
                )

            # The group can retain descendants even after Popen itself exits.
            if _group_has_live_members(pgid):
                events.extend(
                    terminate_owned_process_group(
                        pgid,
                        termination_grace_seconds=termination_grace_seconds,
                        reason="process-exited-with-descendants",
                    )
                )
            if process.poll() is None:
                try:
                    process.wait(timeout=termination_grace_seconds)
                except subprocess.TimeoutExpired:
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
                with _owned_processes_lock:
                    _owned_processes.pop(pgid, None)

    return ProcessOutcome(
        returncode=process.returncode,
        timed_out=timed_out,
        pid=process.pid,
        pgid=pgid,
        termination_events=events,
        wall_time_seconds=time.monotonic() - started,
    )
