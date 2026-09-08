# Whole-branch final fix report

Base: `46342de74d67a1b9373e6ed25fcda8d8d96d662e`.

Scope: the P2 generated-entrypoint finding in `final-review.md`. No delegation,
live simulation, or full-suite execution. The deferred WorldState annotation P3
was not changed.

## Root cause and template evidence

The verifier checked the shared import-to-main-guard interval, leaving the module
prefix unrestricted. A prefix could exit successfully, disable the guard, or
overwrite the exit/error symbols while still being selected as a verified script.

Read the actual `scripts/baseline_converters/common.py:render_executable_plan`
template and its historical implementations at `a8764a45`, `da1866e4`, and
`3a22a657`, plus a repository historical generated script. Their imports,
REPO_ROOT/scripts path bootstrap, runner-mode environment setup, and diagnostic
exit wrapper have the same structure.

## RED

Working directory for all commands: `/tmp/coder-executor-batch-3`.

`/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_execute_plan_compatibility.py tests/test_executor_regression_benchmark.py`

Exit 1; 34 tests ran with nine failing subtests. The two reported prefix variants
and seven related unsupported template changes incorrectly selected the preferred
candidate instead of the valid generated fallback. The existing interstitial
statement rejection and new actual-renderer positive test passed.

## Change

The verifier now checks the complete module structure through AST comparison:

- Optional module docstring and future-annotations import, followed by the known
  generated bootstrap with a literal repository-root string; minimal wrappers
  may omit that bootstrap or contain only `import sys`.
- One absolute shared-runtime import using `main` or `run_generated_plan`, then
  exactly the three plain literal generated assignments, and a final main guard.
- The complete supported direct SystemExit guard or known diagnostic try wrapper,
  including its handler, with no extra control paths or statements.

This deliberately rejects customized aliases, annotated assignments, other exit
spellings, and modified bootstrap/handler forms rather than guessing their
behavior. Supported historical/current generator output is preserved. This is a
bounded generated-template compatibility contract, not a Python security sandbox.

Behavioral regression coverage skips each noncanonical preferred candidate,
executes a valid fallback through the compatibility CLI, observes the fallback
shared-runtime task marker, and verifies exit-code 23 forwarding. Positive tests
execute both current `render_executable_plan` output (with `--runner-mode`) and an
independent frozen historical bootstrap/exit fixture from `a8764a45`.

## GREEN

- `/home/dwb/.pyenv/bin/pyenv exec python -m unittest tests/test_execute_plan_compatibility.py tests/test_executor_regression_benchmark.py`:
  exit 0; 35 tests ran, all OK.
- `/home/dwb/.pyenv/bin/pyenv exec python -m py_compile scripts/execute_plan.py tests/test_execute_plan_compatibility.py`:
  exit 0.
- `git diff --check`: exit 0.

Real acceptance remains unchanged: the required final 240 step and 24 teleport
runs are pending; previous baseline and event performance failures retain their
recorded meaning. This focused fix does not claim whole-branch real acceptance.
