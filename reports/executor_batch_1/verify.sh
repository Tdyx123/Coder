#!/bin/bash
set -eu
/home/dwb/.pyenv/bin/pyenv exec python -m unittest \
  tests/test_action_plan_pre_task.py tests/test_executor_retry_policy.py \
  tests/test_parallel_runner.py tests/test_movement_config.py \
  tests/test_movement_strategies.py tests/test_movement_coordinator.py \
  tests/test_step_movement.py tests/test_navigation_batch_failures.py \
  tests/test_navigation_execution_scope.py tests/test_runtime_object_aliases.py \
  tests/test_runtime_metadata.py tests/test_runtime_third_party_views.py \
  tests/test_pddlrun_executor_adapter.py tests/test_multi_robot_avoidance.py \
  tests/test_movement_benchmark.py tests/test_evaluation_contract.py \
  tests/test_run_result_contract.py tests/test_execution_shutdown.py \
  tests/test_process_supervisor.py tests/test_run_result_storage.py \
  tests/test_runtime_output_isolation.py tests/test_summarize_run_metrics.py \
  tests/test_generate_single_subtask_code.py tests/test_plantocode_demo_bundle.py tests/test_final_reliability_fixes.py
