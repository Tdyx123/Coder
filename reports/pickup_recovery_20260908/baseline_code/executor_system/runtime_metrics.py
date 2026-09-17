"""Bounded runtime timing and explicit, secret-free reproducibility metadata.

Timers overlap: reachable_query is a child of controller, and recovery/planning
may contain other work. Never add category totals to derive elapsed wall time.
"""
import copy
import functools
import hashlib
import importlib.metadata
import json
import math
import platform
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path


class RuntimeMetrics:
    def __init__(self, *, clock=None, max_categories=64, max_counters=128):
        self.clock = clock or time.perf_counter
        self._max_categories = max_categories
        self._max_counters = max_counters
        self._timings = {}
        self._counters = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(storage, name, limit):
        # Keep user-supplied labels from creating unbounded cardinality.
        name = str(name)[:128]
        return name if name in storage or len(storage) < limit else '__other__'

    def observe(self, category, seconds):
        seconds = float(seconds)
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError('timing must be finite and nonnegative')
        if category in ('counters', 'timing_semantics'):
            raise ValueError('reserved timing category')
        with self._lock:
            key = self._key(self._timings, category, self._max_categories)
            value = self._timings.setdefault(key, dict(count=0, total_seconds=0.0, max_seconds=0.0))
            value['count'] += 1
            value['total_seconds'] += seconds
            value['max_seconds'] = max(value['max_seconds'], seconds)

    def increment(self, name, amount=1):
        if not isinstance(amount, int):
            raise ValueError('counter amount must be an integer')
        with self._lock:
            key = self._key(self._counters, name, self._max_counters)
            self._counters[key] = self._counters.get(key, 0) + amount

    def snapshot(self):
        with self._lock:
            result = copy.deepcopy(self._timings)
            result['counters'] = dict(self._counters)
        result['timing_semantics'] = 'overlapping; reachable_query is included in controller; do not sum categories'
        return result

    @contextmanager
    def measure(self, category):
        started = self.clock()
        try:
            yield
        finally:
            self.observe(category, max(0.0, self.clock() - started))


_metrics_creation_lock = threading.Lock()


def metrics_for(runtime):
    # Compatibility for __new__-constructed runtimes/fakes. Production creates
    # the instance before launching workers. This lock never stores a runtime.
    metrics = getattr(runtime, 'runtime_metrics', None)
    if metrics is None:
        with _metrics_creation_lock:
            metrics = getattr(runtime, 'runtime_metrics', None)
            if metrics is None:
                metrics = runtime.runtime_metrics = RuntimeMetrics()
    return metrics


def measured(category, counter=None):
    def decorate(function):
        @functools.wraps(function)
        def wrapped(self, *args, **kwargs):
            metrics = metrics_for(self.runtime)
            if counter:
                metrics.increment(counter)
            with metrics.measure(category):
                return function(self, *args, **kwargs)
        return wrapped
    return decorate


def content_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _command_output(command, cwd=None):
    try:
        result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=5, check=False)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def runtime_metadata(repo_root, *, config, plan=None):
    """Read only explicit facts; never serialize os.environ or command stderr."""
    try:
        thor_version = importlib.metadata.version('ai2thor')
    except importlib.metadata.PackageNotFoundError:
        thor_version = None
    git_status = _command_output(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=repo_root)
    return {
        'code_sha': _command_output(['git', 'rev-parse', 'HEAD'], cwd=repo_root),
        'code_dirty': None if git_status is None else bool(git_status),
        'code_root': str(Path(repo_root).resolve()),
        'python_version': platform.python_version(),
        'ai2thor_version': thor_version,
        'platform': platform.platform(),
        'gpu': _command_output(['nvidia-smi', '--query-gpu=uuid,name,driver_version', '--format=csv,noheader']),
        'plan_content_sha256': content_hash(plan) if plan is not None else None,
        'config': copy.deepcopy(config),
    }
