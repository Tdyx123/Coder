#!/bin/bash
set -eu
cd "$(dirname "$0")/../.."

bash reports/executor_batch_1/verify.sh
bash reports/executor_batch_2/verify.sh

/home/dwb/.pyenv/bin/pyenv exec python -m unittest \
  tests/test_action_registry.py tests/test_plan_contract.py \
  tests/test_runtime_facade.py tests/test_runtime_context_isolation.py \
  tests/test_runtime_metrics.py tests/test_executor_regression_benchmark.py \
  tests/test_reachable_map_cache.py tests/test_execute_plan_compatibility.py \
  tests/test_generation_validation.py tests/test_pddlrun_executor_adapter.py \
  tests/test_movement_coordinator.py tests/test_navigation_execution_scope.py \
  tests/test_runtime_object_aliases.py tests/test_parallel_runner.py
