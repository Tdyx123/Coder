# Robot Movement Mode Benchmark

| Case | Mode | Status | Navigation | Teleport | Replans | GCR | TC | SR | Runtime (s) | Failure |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| FloorPlan1_task_2 | teleport | success | 1/1 | 1 | 0 | 0.333 | 0.000 | 0.000 | 3.264 |  |
| FloorPlan1_task_2 | step | success | 1/1 | 0 | 0 | 0.333 | 0.000 | 0.000 | 4.099 |  |
| FloorPlan1_task_13 | teleport | success | 4/4 | 4 | 0 | 1.000 | 1.000 | 0.000 | 3.649 |  |
| FloorPlan1_task_13 | step | success | 4/4 | 0 | 0 | 1.000 | 1.000 | 0.000 | 5.114 |  |
| FloorPlan1_task_0 | teleport | success | 7/7 | 8 | 0 | 0.500 | 0.000 | 0.000 | 5.418 |  |
| FloorPlan1_task_0 | step | success | 7/7 | 0 | 3 | 0.500 | 0.000 | 0.000 | 10.174 |  |
| FloorPlan201_task_16 | teleport | success | 1/1 | 1 | 0 | 0.667 | 0.000 | 0.000 | 3.650 |  |
| FloorPlan201_task_16 | step | success | 1/1 | 0 | 1 | 0.667 | 0.000 | 0.000 | 6.119 |  |
| FloorPlan201_task_13 | teleport | success | 4/4 | 4 | 0 | 1.000 | 1.000 | 0.000 | 3.614 |  |
| FloorPlan201_task_13 | step | success | 4/4 | 0 | 4 | 1.000 | 1.000 | 0.000 | 7.999 |  |
| FloorPlan201_task_1 | teleport | success | 3/3 | 3 | 0 | 0.667 | 0.000 | 0.000 | 4.297 |  |
| FloorPlan201_task_1 | step | success | 3/3 | 0 | 1 | 0.333 | 0.000 | 0.000 | 8.668 |  |
| FloorPlan301_task_12 | teleport | success | 1/1 | 1 | 0 | 0.750 | 0.000 | 0.000 | 3.262 |  |
| FloorPlan301_task_12 | step | success | 1/1 | 0 | 1 | 0.750 | 0.000 | 0.000 | 3.797 |  |
| FloorPlan301_task_10 | teleport | success | 4/4 | 5 | 0 | 0.667 | 0.000 | 0.000 | 3.825 |  |
| FloorPlan301_task_10 | step | success | 4/4 | 0 | 5 | 0.667 | 0.000 | 0.000 | 6.221 |  |
| FloorPlan301_task_0 | teleport | success | 4/4 | 5 | 0 | 0.500 | 0.000 | 0.000 | 3.892 |  |
| FloorPlan301_task_0 | step | success | 4/4 | 0 | 1 | 0.500 | 0.000 | 0.000 | 6.028 |  |
| FloorPlan401_task_22 | teleport | success | 1/1 | 1 | 0 | 0.500 | 0.000 | 0.000 | 3.136 |  |
| FloorPlan401_task_22 | step | success | 1/1 | 0 | 0 | 0.500 | 0.000 | 0.000 | 3.297 |  |
| FloorPlan401_task_1 | teleport | success | 5/5 | 5 | 0 | 1.000 | 1.000 | 0.000 | 3.324 |  |
| FloorPlan401_task_1 | step | success | 5/5 | 0 | 0 | 1.000 | 1.000 | 0.000 | 5.030 |  |
| FloorPlan401_task_10 | teleport | success | 6/6 | 6 | 0 | 0.667 | 0.000 | 0.000 | 3.723 |  |
| FloorPlan401_task_10 | step | success | 6/6 | 0 | 0 | 0.667 | 0.000 | 0.000 | 6.060 |  |

## Aggregates

| Mode | Navigation success | Successful cases | Navigation Teleport | GCR | Planner P95 (s) | Within timeout |
|---|---:|---:|---:|---:|---:|---|
| teleport | 1.000 | 12 | 44 | 0.688 | 0.000 | yes |
| step | 1.000 | 12 | 0 | 0.660 | 0.031 | yes |

## Acceptance

PASS
