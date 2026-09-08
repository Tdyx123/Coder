# Development evidence

This directory preserves the ignored implementation workspace before cleanup.

- `task-1-report.md` through `task-6-report.md` contain implementation and RED/GREEN
  records. Their adjacent `review` and `rereview` files preserve every review round.
- `final-review.md`, `final-fix-report.md`, and `final-rereview.md` preserve the final
  whole-branch review and scoped fix review.
- `progress.md` is the complete progress ledger, including every `Ruling:` line. The
  retained cancellation ruling requires checks before the controller lock and again
  immediately before submission inside the lock.
- `final-case06-diagnosis.md` is the bounded read-only analysis of the final paired GCR
  loss. It records what is proven and leaves the first trajectory divergence unresolved.
- `focused-test-logs.tar.xz` contains 96 small task-focused `.log` files under their
  original relative paths. It excludes raw simulation attempts and duplicated report
  trees. Tracked-file SHA-256:
  `c59db13e65f2b450442ff62d84a3d5dcffdcd09d20f6ad1d9b6bdea1ef73a0e9`.
- `verification-939be0ba.log.xz` is the exact output from the final clean-code
  `bash reports/executor_batch_3/verify.sh` run: overlapping groups 462, 588, and 320,
  each `OK`. Group counts must not be summed as unique tests because the scripts share
  test modules. Tracked-file SHA-256:
  `2bac5fb33dc9e0d599156693d6be575896f95025a2f6af82a650ea723d891b23`;
  decompressed payload SHA-256:
  `de1f79347463f4d7d1568b761f34eae065286f329c528491c7f741f32f4222b5`.

The final code under test was
`939be0ba48b9b9d058cc64eb036f9639ddda3ff0`. The documentation/evidence commit that
contains this directory is necessarily later and was not the code used for simulation.
