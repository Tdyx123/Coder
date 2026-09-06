import ast
import importlib.util
import io
import json
import py_compile
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from executor_system.generated_plan_runtime import build_hardcoded_bundle
from executor_system.parallel_runner import is_runner_compatible_executable
from plantocode import main as plantocode_main


def load_script_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


lammap_baseline = load_script_module(
    "plantocode_demo_lammap_baseline",
    ROOT / "scripts" / "baselines" / "LaMMA-P.py",
)
smart_llm_baseline = load_script_module(
    "plantocode_demo_smart_llm_baseline",
    ROOT / "scripts" / "baselines" / "SMART-LLM.py",
)
scale_plan_baseline = load_script_module(
    "plantocode_demo_scale_plan_baseline",
    ROOT / "scripts" / "baselines" / "Scale-Plan.py",
)


def write_json(path: Path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")


def load_bundle_data_from_executable(path: Path):
    executable_text = path.read_text(encoding="utf-8")
    parsed = ast.parse(executable_text)
    for node in parsed.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(isinstance(target, ast.Name) and target.id == "BUNDLE_DATA" for target in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError("BUNDLE_DATA assignment not found")


def write_parallel_run_fixture_task(
    root: Path,
    floor_plan: str,
    task_index: int,
    log_parts=("logs", "intermediate_runs"),
) -> Path:
    task_run_dir = root
    for part in log_parts:
        task_run_dir = task_run_dir / part
    task_run_dir = (
        task_run_dir
        / f"sample___{floor_plan}"
        / "task"
        / f"20260526_{task_index:03d}"
    )
    write_json(
        task_run_dir / "inputs" / "task_context.json",
        {
            "task": f"open the cabinet on floor {floor_plan}.",
            "robots": [{"name": "robot1", "skills": ["GoToObject", "OpenObject"]}],
            "objects_ai": "objects = [{'name': 'Cabinet'}]",
        },
    )
    (task_run_dir / "02_allocate").mkdir(parents=True)
    (task_run_dir / "02_allocate" / "02_allocate_output.txt").write_text(
        "# Sequence of Operations:\nSubtask 1: Robot 1;\n",
        encoding="utf-8",
    )

    outputs_dir = task_run_dir / "08_planner" / "outputs"
    outputs_dir.mkdir(parents=True)
    plan_path = outputs_dir / "subtask_01_problem_validated_plan.txt"
    plan_path.write_text(
        "(gotoobject robot1 cabinet)\n(openobject robot1 cabinet)\n",
        encoding="utf-8",
    )
    write_json(
        task_run_dir / "08_planner" / "planner_manifest.json",
        [
            {
                "problem_file": "subtask_01_problem_validated.pddl",
                "return_code": 0,
                "compatibility_output": str(plan_path),
            }
        ],
    )
    write_json(
        task_run_dir / "run_manifest.json",
        {
            "repo_root": str(root),
            "task": f"open the cabinet on floor {floor_plan}.",
            "test_set": "sample",
            "floor_plan": floor_plan,
            "task_index": task_index,
            "task_run_dir": str(task_run_dir),
        },
    )

    dataset_dir = root / "data" / "sample"
    dataset_dir.mkdir(parents=True, exist_ok=True)
    lines = ["" for _ in range(task_index + 1)]
    lines[task_index] = json.dumps(
        {
            "task": f"open the cabinet on floor {floor_plan}.",
            "robot list": [1],
            "object_states": [{"name": "Cabinet", "contains": [], "states": ["OPENED"]}],
            "trans": 1,
            "min_trans": 2,
        },
        ensure_ascii=False,
    )
    (dataset_dir / f"FloorPlan{floor_plan}.jsonl").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )
    return task_run_dir


def write_parallel_run_summary(root: Path, parallel_run: Path, task_run_dirs):
    summaries = []
    for task_run_dir in task_run_dirs:
        manifest = json.loads((task_run_dir / "run_manifest.json").read_text(encoding="utf-8"))
        summaries.append(
            {
                "floor_plan": manifest["floor_plan"],
                "results": [
                    {
                        "floor_plan": manifest["floor_plan"],
                        "task_index": manifest["task_index"],
                        "task": manifest["task"],
                        "status": "success",
                        "task_run_dir": str(task_run_dir),
                    }
                ],
            }
        )
    write_json(
        parallel_run / "summary.json",
        {
            "repo_root": str(root),
            "test_set": "sample",
            "summaries": summaries,
        },
    )


def write_lammap_native_fixture(root: Path) -> Path:
    baseline_root = root / "baselines" / "LaMMA-P"
    task_run_dir = (
        baseline_root
        / "logs"
        / "intermediate_runs"
        / "unit_set"
        / "open_the_drawer"
        / "20260621_001"
    )
    write_json(
        task_run_dir / "inputs" / "task_context.json",
        {
            "task": "open the drawer.",
            "robots": [{"name": "robot1", "skills": ["GoToObject", "OpenObject"]}],
            "objects_ai": "objects = [{'name': 'Drawer'}]",
        },
    )
    write_json(
        task_run_dir / "run_manifest.json",
        {
            "repo_root": "/home/dwb/thor/LaMMA-P",
            "task": "open the drawer.",
            "test_set": "manifest_decoy",
            "floor_plan": "2",
            "task_index": 0,
        },
    )
    (task_run_dir / "08_final_match").mkdir(parents=True)
    (task_run_dir / "08_final_match" / "02_final_plan.txt").write_text(
        "```pddl\n"
        "0.000: (gotoobject robot drawer) [1.000]\n"
        "1.000: (openobject robot drawer) [1.000]\n"
        "```",
        encoding="utf-8",
    )
    dataset_dir = root / "data" / "unit_set"
    dataset_dir.mkdir(parents=True, exist_ok=True)
    (dataset_dir / "FloorPlan2.jsonl").write_text(
        json.dumps(
            {
                "task": "open the drawer.",
                "robot list": [1],
                "object_states": [{"name": "Drawer", "contains": [], "states": ["OPENED"]}],
                "trans": 1,
                "min_trans": 2,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    external_task_run_dir = (
        Path("/home/dwb/thor/LaMMA-P/logs/intermediate_runs")
        / task_run_dir.relative_to(baseline_root / "logs" / "intermediate_runs")
    )
    write_json(
        baseline_root / "parallel_runs" / "pddlrun_lammap_fixture" / "summary.json",
        {
            "repo_root": "/home/dwb/thor/LaMMA-P",
            "test_set": "unit_set",
            "summaries": [
                {
                    "floor_plan": "2",
                    "test_set": "floor_summary_decoy",
                    "results": [
                        {
                            "floor_plan": "2",
                            "task_index": 0,
                            "task": "open the drawer.",
                            "test_set": "result_decoy",
                            "status": "success",
                            "task_run_dir": str(external_task_run_dir),
                        }
                    ],
                }
            ],
        },
    )
    return task_run_dir


def write_smart_native_fixture(root: Path, source_name: str = "code_plan.py") -> Path:
    baseline_root = root / "baselines" / "SMART-LLM"
    task_run_dir = baseline_root / "logs" / "2" / f"native_{source_name.replace('.', '_')}"
    task_run_dir.mkdir(parents=True, exist_ok=True)
    source_text = (
        "```python\n"
        if source_name == "decomposed_plan.py"
        else ""
    )
    source_text += (
        "def open_drawer(robot):\n"
        "    GoToObject(robot, 'Drawer')\n"
        "    OpenObject(robot, 'Drawer')\n"
        "open_drawer(robots[0])\n"
    )
    if source_name == "decomposed_plan.py":
        source_text += "```\n"
    (task_run_dir / source_name).write_text(source_text, encoding="utf-8")
    (task_run_dir / "log.txt").write_text(
        "\n".join(
            [
                "open the drawer.",
                "",
                "Floor Plan: 2",
                "",
                "objects = [{'name': 'Drawer'}]",
                "robots = [{'name': 'robot1'}]",
                "trans = 0",
                "max_trans = 0",
                "test-set: unit_set",
            ]
        ),
        encoding="utf-8",
    )
    dataset_dir = root / "data" / "unit_set"
    dataset_dir.mkdir(parents=True, exist_ok=True)
    (dataset_dir / "FloorPlan2.jsonl").write_text(
        json.dumps(
            {
                "task": "open the drawer.",
                "robot list": [1],
                "object_states": [{"name": "Drawer", "contains": [], "states": ["OPENED"]}],
                "trans": 1,
                "min_trans": 2,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return task_run_dir


def write_scale_plan_fixture(root: Path) -> Path:
    baseline_root = root / "baselines" / "Scale-Plan"
    task = "open the drawer and the cabinet."
    task_run_dir = (
        baseline_root
        / "logs"
        / "intermediate_runs"
        / "unit_set"
        / "open_drawer_and_cabinet"
        / "20260701_001"
    )
    dataset_dir = root / "data" / "unit_set"
    dataset_file = dataset_dir / "FloorPlan6.jsonl"
    dataset_dir.mkdir(parents=True, exist_ok=True)
    dataset_file.write_text(
        json.dumps(
            {
                "task": task,
                "robot list": [18, 1],
                "object_states": [
                    {"name": "Drawer", "contains": [], "states": ["OPENED"]},
                    {"name": "Cabinet", "contains": [], "states": ["OPENED"]},
                ],
                "trans": 2,
                "min_trans": 4,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    decoy_dataset_dir = root / "data" / "decoy_set"
    decoy_dataset_file = decoy_dataset_dir / "FloorPlan6.jsonl"
    decoy_dataset_dir.mkdir(parents=True, exist_ok=True)
    decoy_dataset_file.write_text(
        json.dumps(
            {
                "task": "decoy task",
                "robot list": [1],
                "object_states": [
                    {"name": "DecoyObject", "contains": [], "states": ["OPENED"]},
                ],
                "trans": 1,
                "min_trans": 1,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    write_json(
        task_run_dir / "inputs" / "task_context.json",
        {
            "task": task,
            "task_index": 0,
            "test_set": "task_context_decoy",
            "test_set_path": str(decoy_dataset_dir),
            "dataset_file": str(decoy_dataset_file),
            "floor_plan": "6",
            "robots": [
                {"name": "robot1", "skills": ["GoToObject", "OpenObject"]},
                {"name": "robot2", "skills": ["GoToObject", "OpenObject"]},
                {"name": "robot18", "skills": ["GoToObject", "OpenObject"]},
            ],
            "objects_ai": "objects = [{'name': 'Drawer'}, {'name': 'Cabinet'}]",
        },
    )
    write_json(
        task_run_dir / "run_manifest.json",
        {
            "repo_root": str(root),
            "task": task,
            "test_set": "manifest_decoy",
            "test_set_path": str(decoy_dataset_dir),
            "dataset_file": str(decoy_dataset_file),
            "floor_plan": "6",
            "task_index": 0,
        },
    )
    write_json(
        task_run_dir / "05_plan" / "03_final_plan.json",
        {
            "stages": [
                {
                    "stage_id": "parallel-1",
                    "parallel_group_id": "parallel-1",
                    "subtask_ids": ["subtask-1", "subtask-2"],
                    "plans": [
                        {
                            "subtask_id": "subtask-1",
                            "robot": "robot18",
                            "plan": "(GoToObject robot18 Drawer)\n(OpenObject robot18 Drawer)",
                        },
                        {
                            "subtask_id": "subtask-2",
                            "robot": "robot1",
                            "plan": "(GoToObject robot1 Cabinet)\n(OpenObject robot1 Cabinet)",
                        },
                    ],
                },
                {
                    "stage_id": "sequential-2",
                    "parallel_group_id": "sequential-2",
                    "subtask_ids": ["subtask-3"],
                    "plans": [
                        {
                            "subtask_id": "subtask-3",
                            "robot": "robot18",
                            "plan": "(GoToObject robot18 Drawer)",
                        },
                    ],
                },
            ]
        },
    )

    relative_task_run = task_run_dir.relative_to(baseline_root / "logs" / "intermediate_runs")
    external_task_run_dir = (
        Path("/home/dwb/thor/Scale-Plan/logs/intermediate_runs")
        / relative_task_run
    )
    write_json(
        baseline_root / "logs" / "scale_plan_parallel" / "pddlrun_scale_plan_fixture" / "summary.json",
        {
            "repo_root": "/home/dwb/thor/Scale-Plan",
            "test_set": "unit_set",
            "test_set_path": str(decoy_dataset_dir),
            "dataset_file": str(decoy_dataset_file),
            "summaries": [
                {
                    "floor_plan": "6",
                    "test_set": "floor_summary_decoy",
                    "test_set_path": str(decoy_dataset_dir),
                    "dataset_file": str(decoy_dataset_file),
                    "task_count": 1,
                    "success_count": 1,
                    "failure_count": 0,
                    "results": [
                        {
                            "floor_plan": "6",
                            "test_set": "result_decoy",
                            "test_set_path": str(decoy_dataset_dir),
                            "dataset_file": str(decoy_dataset_file),
                            "task_index": 0,
                            "task": task,
                            "status": "success",
                            "task_run_dir": str(external_task_run_dir),
                        }
                    ],
                }
            ],
        },
    )
    return task_run_dir


class PlanToCodeDemoBundleTest(unittest.TestCase):
    def test_plantocode_generates_loadable_bundles_without_plan_validation(self):
        cases = [
            ("empty", "; cost = 0\n", None, 0),
            ("stale_audit", "; cost = 0\n", '[{"subtask_id": 99, "verified": true}]', 0),
            ("duplicate_audit", "; empty\n", '[{"subtask_id": 1}, {"subtask_id": 1}]', 0),
            ("malformed_audit", "; empty\n", "{invalid json", 0),
            ("wrong_audit_shape", "; empty\n", "{}", 0),
            ("mixed", "; empty\n", None, 1),
            ("duplicate_plan", "(openobject robot1 cabinet)\n", None, 1),
        ]
        for case, plan_text, audit_text, action_count in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp_dir:
                root = Path(tmp_dir)
                task_run_dir = write_parallel_run_fixture_task(root, "6", 0)
                planner_dir = task_run_dir / "08_planner"
                plan = planner_dir / "outputs/subtask_01_problem_validated_plan.txt"
                plan.write_text(plan_text, encoding="utf-8")
                if audit_text is not None:
                    (planner_dir / "noop_subtasks.json").write_text(audit_text, encoding="utf-8")
                # Generation must not depend on the expected-subtask audit artifact.
                expected_path = task_run_dir / "04_problem_files/03_subtasks.json"
                expected_path.parent.mkdir(parents=True)
                expected_path.write_text("{invalid json", encoding="utf-8")
                if case in {"mixed", "duplicate_plan"}:
                    (task_run_dir / "02_allocate/02_allocate_output.txt").write_text(
                        "Subtask 1: Robot 1;\nSubtask 1: Robot 1;\nSubtask 2: Robot 1;\n",
                        encoding="utf-8",
                    )
                    extra = planner_dir / (
                        "outputs/subtask_03_plan.txt" if case == "mixed"
                        else "outputs/subtask_01_duplicate_plan.txt"
                    )
                    extra.write_text(
                        "(openobject robot1 cabinet)\n" if case == "mixed"
                        else "(closeobject robot1 cabinet)\n", encoding="utf-8",
                    )
                    write_json(planner_dir / "planner_manifest.json", [
                        {"compatibility_output": str(plan)},
                        {"compatibility_output": str(extra)},
                    ])

                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    result_code = plantocode_main([
                        "--logs-dir", str(root / "logs"),
                        "--output-dir", str(root / "summary"),
                    ])
                self.assertEqual(result_code, 0, stdout.getvalue())
                executable = task_run_dir / "plan_to_code/executable_plan.py"
                bundle = build_hardcoded_bundle(load_bundle_data_from_executable(executable))
                self.assertEqual(bundle.noop_subtasks, [])
                self.assertEqual(bundle.no_trans, action_count)
                self.assertEqual(len(bundle.task_plan.stages), action_count)
                if action_count:
                    actions = bundle.task_plan.stages[0].robot_action_queues["robot1"]
                    self.assertEqual([action.action_type for action in actions], ["OpenObject"])
                    self.assertEqual(actions[0].args(), ("Cabinet",))

    def test_generated_runtime_preserves_optional_noop_subtasks(self):
        proof = {
            "subtask_id": 1,
            "goal_literals": ["(ready drawer)"],
            "verified": True,
        }
        bundle_data = {
            "task": "unit task",
            "task_plan": {"task_id": "unit", "stages": []},
            "gcr": [],
            "no_trans": 0,
            "phases": [],
            "object_mappings": {},
            "object_mapping_warnings": [],
            "noop_subtasks": [proof],
        }

        bundle = build_hardcoded_bundle(bundle_data)

        self.assertEqual(bundle.noop_subtasks, [proof])

    def test_generated_runtime_requires_bundle_gcr(self):
        bundle_data = {
            "task": "unit task",
            "task_plan": {"task_id": "unit", "stages": []},
            "no_trans": 0,
            "phases": [],
            "object_mappings": {},
            "object_mapping_warnings": [],
        }

        with self.assertRaisesRegex(RuntimeError, "missing required 'gcr'"):
            build_hardcoded_bundle(bundle_data)

    def test_generated_runtime_rejects_non_list_bundle_gcr(self):
        bundle_data = {
            "task": "unit task",
            "task_plan": {"task_id": "unit", "stages": []},
            "gcr": {"name": "Drawer"},
            "no_trans": 0,
            "phases": [],
            "object_mappings": {},
            "object_mapping_warnings": [],
        }

        with self.assertRaisesRegex(RuntimeError, r"BUNDLE_DATA\['gcr'\] must be a list"):
            build_hardcoded_bundle(bundle_data)

    def test_plantocode_rejects_baseline_options(self):
        for argv in (
            ["--base-line", "LaMMA-P"],
            ["--root", "./baselines/LaMMA-P"],
        ):
            with self.subTest(argv=argv):
                with redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        plantocode_main(argv)
                self.assertEqual(raised.exception.code, 2)

    def test_pddlrun_fixture_generates_demo_style_script_with_hardcoded_bundle(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            task_run_dir = root / "logs" / "intermediate_runs" / "sample___6" / "task" / "20260526_001"

            write_json(
                task_run_dir / "inputs" / "task_context.json",
                {
                    "task": "break the window, then open the cabinet and the drawer.",
                    "robots": [
                        {"name": "robot1", "skills": ["GoToObject", "OpenObject"]},
                        {"name": "robot2", "skills": ["GoToObject", "BreakObject"]},
                    ],
                    "objects_ai": "\n\nobjects = [{'name': 'Window'}, {'name': 'Cabinet'}, {'name': 'Drawer'}]",
                },
            )
            (task_run_dir / "02_allocate").mkdir(parents=True)
            (task_run_dir / "02_allocate" / "02_allocate_output.txt").write_text(
                "# Sequence of Operations:\n"
                "Subtask 1: Robot 2;\n"
                "Subtask 2: Robot 1;Subtask 3: Robot 1;\n",
                encoding="utf-8",
            )

            outputs_dir = task_run_dir / "08_planner" / "outputs"
            outputs_dir.mkdir(parents=True)
            (outputs_dir / "subtask_01_problem_validated_plan.txt").write_text(
                "(gotoobject robot1 window1)\n(breakobject robot1 window1)\n",
                encoding="utf-8",
            )
            (outputs_dir / "subtask_02_problem_validated_plan.txt").write_text(
                "(gotoobject robot1 cabinet)\n(openobject robot1 cabinet)\n",
                encoding="utf-8",
            )
            (outputs_dir / "subtask_03_problem_validated_plan.txt").write_text(
                "(gotoobject robot1 drawer)\n(openobject robot1 drawer)\n",
                encoding="utf-8",
            )
            write_json(
                task_run_dir / "08_planner" / "planner_manifest.json",
                [
                    {
                        "problem_file": "subtask_01_problem_validated.pddl",
                        "return_code": 0,
                        "compatibility_output": str(outputs_dir / "subtask_01_problem_validated_plan.txt"),
                    },
                    {
                        "problem_file": "subtask_02_problem_validated.pddl",
                        "return_code": 0,
                        "compatibility_output": str(outputs_dir / "subtask_02_problem_validated_plan.txt"),
                    },
                    {
                        "problem_file": "subtask_03_problem_validated.pddl",
                        "return_code": 0,
                        "compatibility_output": str(outputs_dir / "subtask_03_problem_validated_plan.txt"),
                    },
                ],
            )
            write_json(
                task_run_dir / "run_manifest.json",
                {
                    "repo_root": str(root),
                    "task": "break the window, then open the cabinet and the drawer.",
                    "test_set": "sample",
                    "floor_plan": "6",
                    "task_index": 0,
                    "task_run_dir": str(task_run_dir),
                    "gpu_device": 0,
                },
            )

            dataset_dir = root / "data" / "sample"
            dataset_dir.mkdir(parents=True)
            (dataset_dir / "FloorPlan6.jsonl").write_text(
                json.dumps(
                    {
                        "task": "break the window, then open the cabinet and the drawer.",
                        "robot list": [1, 2],
                        "object_states": [
                            {"name": "Window", "contains": [], "states": ["BROKEN"]},
                            {"name": "Cabinet", "contains": [], "states": ["OPENED"]},
                            {"name": "Drawer", "contains": [], "states": ["OPENED"]},
                        ],
                        "trans": 3,
                        "min_trans": 6,
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            result_code = plantocode_main(
                [
                    "--logs-dir",
                    str(root / "logs"),
                    "--output-dir",
                    str(root / "summary"),
                ]
            )

            self.assertEqual(result_code, 0)
            executable_plan = task_run_dir / "plan_to_code" / "executable_plan.py"
            parallel_executable_plan = task_run_dir / "plan_to_code" / "parallel_executable_plan.py"
            summary = json.loads((root / "summary" / "plan_to_code_summary.json").read_text(encoding="utf-8"))
            details = json.loads((root / "summary" / "plan_to_code_results.json").read_text(encoding="utf-8"))

            self.assertTrue(executable_plan.exists())
            self.assertFalse(parallel_executable_plan.exists())
            self.assertEqual(summary["successful_generations"], 1)
            self.assertEqual(details[0]["status"], "success")
            py_compile.compile(str(executable_plan), doraise=True)

            executable_text = executable_plan.read_text(encoding="utf-8")
            self.assertIn("BUNDLE_DATA =", executable_text)
            self.assertNotIn("TaskPlan.from_dict(BUNDLE_DATA[\"task_plan\"])", executable_text)
            self.assertNotIn("build_task_plan_from_pddlrun_paths(", executable_text)
            self.assertIn("--runner-mode", executable_text)
            self.assertIn("os.environ[\"renderImage\"] = \"0\"", executable_text)
            self.assertIn("run_generated_plan(BUNDLE_DATA, TASK_FILE, TASK_INDEX, __file__)", executable_text)
            self.assertNotIn("DEFAULT_RUNNER_TIMEOUT_SECONDS = 100.0", executable_text)
            self.assertNotIn("run_action_plan_tolerant(", executable_text)
            self.assertNotIn("_Demo2Facade", executable_text)

            parsed = ast.parse(executable_text)
            bundle_data = None
            for node in parsed.body:
                if not isinstance(node, ast.Assign):
                    continue
                if any(isinstance(target, ast.Name) and target.id == "BUNDLE_DATA" for target in node.targets):
                    bundle_data = ast.literal_eval(node.value)
                    break

            self.assertIsNotNone(bundle_data)
            self.assertEqual(bundle_data["no_trans"], 6)
            self.assertEqual(bundle_data["noop_subtasks"], [])
            self.assertEqual(
                bundle_data["gcr"],
                [
                    {"name": "Window", "contains": [], "states": ["BROKEN"]},
                    {"name": "Cabinet", "contains": [], "states": ["OPENED"]},
                    {"name": "Drawer", "contains": [], "states": ["OPENED"]},
                ],
            )
            self.assertNotIn("gpu_device", bundle_data)
            self.assertNotIn("gpu_device", details[0])
            self.assertEqual(len(bundle_data["task_plan"]["stages"]), 2)
            first_stage = bundle_data["task_plan"]["stages"][0]
            second_stage = bundle_data["task_plan"]["stages"][1]
            self.assertEqual(list(first_stage["robot_action_queues"]), ["robot2"])
            self.assertEqual(
                [action["action_type"] for action in first_stage["robot_action_queues"]["robot2"]],
                ["GoToObject", "BreakObject"],
            )
            self.assertEqual(list(second_stage["robot_action_queues"]), ["robot1"])
            self.assertEqual(
                [action["action_type"] for action in second_stage["robot_action_queues"]["robot1"]],
                ["GoToObject", "OpenObject", "GoToObject", "OpenObject"],
            )
            self.assertEqual(bundle_data["object_mapping_warnings"], [])

    def test_plantocode_gcr_uses_plan_multi_instance_tokens_and_defaults_first(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            task_run_dir = root / "logs" / "intermediate_runs" / "sample___6" / "task" / "20260526_002"
            task = "open the selected drawer and put the selected credit card inside."

            write_json(
                task_run_dir / "inputs" / "task_context.json",
                {
                    "task": task,
                    "robots": [{"name": "robot1", "skills": ["GoToObject", "OpenObject", "PickupObject", "PutObject"]}],
                    "objects_ai": (
                        "objects = [{'name': 'Drawer'}, {'name': 'CreditCard', 'mass': 0.01}, "
                        "{'name': 'Box'}, {'name': 'Watch'}]"
                    ),
                },
            )
            (task_run_dir / "02_allocate").mkdir(parents=True)
            (task_run_dir / "02_allocate" / "02_allocate_output.txt").write_text(
                "# Sequence of Operations:\nSubtask 1: Robot 1;\n",
                encoding="utf-8",
            )

            outputs_dir = task_run_dir / "08_planner" / "outputs"
            outputs_dir.mkdir(parents=True)
            plan_path = outputs_dir / "subtask_01_problem_validated_plan.txt"
            plan_path.write_text(
                "(gotoobject robot1 Drawer_2)\n"
                "(openobject robot1 Drawer_2)\n"
                "(gotoobject robot1 CreditCard_2)\n"
                "(pickupobject robot1 CreditCard_2)\n"
                "(gotoobject robot1 Drawer_2)\n"
                "(putobject robot1 CreditCard_2 Drawer_2)\n",
                encoding="utf-8",
            )
            write_json(
                task_run_dir / "08_planner" / "planner_manifest.json",
                [
                    {
                        "problem_file": "subtask_01_problem_validated.pddl",
                        "return_code": 0,
                        "compatibility_output": str(plan_path),
                    }
                ],
            )

            bindings = [
                {
                    "object": "Drawer_1",
                    "object_type": "Drawer",
                    "object_id": "Drawer|+00.00|+00.50|+00.00",
                    "number": 1,
                    "count": 2,
                    "multiple": True,
                },
                {
                    "object": "Drawer_2",
                    "object_type": "Drawer",
                    "object_id": "Drawer|+01.00|+00.50|+00.00",
                    "number": 2,
                    "count": 2,
                    "multiple": True,
                },
                {
                    "object": "CreditCard_1",
                    "object_type": "CreditCard",
                    "object_id": "CreditCard|+00.00|+00.90|+00.00",
                    "number": 1,
                    "count": 2,
                    "multiple": True,
                },
                {
                    "object": "CreditCard_2",
                    "object_type": "CreditCard",
                    "object_id": "CreditCard|+01.00|+00.90|+00.00",
                    "number": 2,
                    "count": 2,
                    "multiple": True,
                },
                {
                    "object": "Box_1",
                    "object_type": "Box",
                    "object_id": "Box|+00.00|+00.80|+00.00",
                    "number": 1,
                    "count": 2,
                    "multiple": True,
                },
                {
                    "object": "Box_2",
                    "object_type": "Box",
                    "object_id": "Box|+01.00|+00.80|+00.00",
                    "number": 2,
                    "count": 2,
                    "multiple": True,
                },
                {
                    "object": "Watch_1",
                    "object_type": "Watch",
                    "object_id": "Watch|+00.00|+00.95|+00.00",
                    "number": 1,
                    "count": 2,
                    "multiple": True,
                },
                {
                    "object": "Watch_2",
                    "object_type": "Watch",
                    "object_id": "Watch|+01.00|+00.95|+00.00",
                    "number": 2,
                    "count": 2,
                    "multiple": True,
                },
            ]
            write_json(
                task_run_dir / "05_problem_generation" / "key_object_id_bindings.json",
                bindings,
            )
            write_json(
                task_run_dir / "run_manifest.json",
                {
                    "repo_root": str(root),
                    "task": task,
                    "test_set": "sample",
                    "floor_plan": "6",
                    "task_index": 0,
                    "task_run_dir": str(task_run_dir),
                },
            )

            dataset_dir = root / "data" / "sample"
            dataset_dir.mkdir(parents=True)
            (dataset_dir / "FloorPlan6.jsonl").write_text(
                json.dumps(
                    {
                        "task": task,
                        "robot list": [1],
                        "object_states": [
                            {"name": "Drawer", "contains": ["CreditCard"], "states": ["OPENED"]},
                            {"name": "Box", "contains": ["Watch"], "states": []},
                        ],
                        "trans": 1,
                        "min_trans": 6,
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            result_code = plantocode_main(
                [
                    "--logs-dir",
                    str(root / "logs"),
                    "--output-dir",
                    str(root / "summary"),
                ]
            )

            self.assertEqual(result_code, 0)
            bundle_data = load_bundle_data_from_executable(
                task_run_dir / "plan_to_code" / "executable_plan.py"
            )

            self.assertEqual(
                bundle_data["gcr"],
                [
                    {"name": "Drawer_2", "contains": ["CreditCard_2"], "states": ["OPENED"]},
                    {"name": "Box_1", "contains": ["Watch_1"], "states": []},
                ],
            )

    def test_plantocode_falls_back_to_robot1_when_allocation_has_no_assignments(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            task_run_dir = write_parallel_run_fixture_task(root, "6", 0)
            task = "open the cabinet and drawer with incomplete allocation output."

            write_json(
                task_run_dir / "inputs" / "task_context.json",
                {
                    "task": task,
                    "robots": [
                        {"name": "robot1", "skills": ["GoToObject", "OpenObject"]},
                        {"name": "robot2", "skills": ["GoToObject", "OpenObject"]},
                    ],
                    "objects_ai": "objects = [{'name': 'Cabinet'}, {'name': 'Drawer'}]",
                },
            )
            (task_run_dir / "02_allocate" / "02_allocate_output.txt").write_text(
                "# Sequence of Operations:\nNo executable allocation was produced.\n",
                encoding="utf-8",
            )

            outputs_dir = task_run_dir / "08_planner" / "outputs"
            plan_1 = outputs_dir / "subtask_01_problem_validated_plan.txt"
            plan_2 = outputs_dir / "subtask_02_problem_validated_plan.txt"
            plan_2.write_text(
                "(gotoobject robot2 drawer)\n(openobject robot2 drawer)\n",
                encoding="utf-8",
            )
            write_json(
                task_run_dir / "08_planner" / "planner_manifest.json",
                [
                    {
                        "problem_file": "subtask_01_problem_validated.pddl",
                        "return_code": 0,
                        "compatibility_output": str(plan_1),
                    },
                    {
                        "problem_file": "subtask_02_problem_validated.pddl",
                        "return_code": 0,
                        "compatibility_output": str(plan_2),
                    },
                ],
            )
            write_json(
                task_run_dir / "run_manifest.json",
                {
                    "repo_root": str(root),
                    "task": task,
                    "test_set": "sample",
                    "floor_plan": "6",
                    "task_index": 0,
                    "task_run_dir": str(task_run_dir),
                },
            )
            (root / "data" / "sample" / "FloorPlan6.jsonl").write_text(
                json.dumps(
                    {
                        "task": task,
                        "robot list": [1, 2],
                        "object_states": [
                            {"name": "Cabinet", "contains": [], "states": ["OPENED"]},
                            {"name": "Drawer", "contains": [], "states": ["OPENED"]},
                        ],
                        "trans": 2,
                        "min_trans": 4,
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            result_code = plantocode_main(
                [
                    "--logs-dir",
                    str(root / "logs"),
                    "--output-dir",
                    str(root / "summary"),
                ]
            )

            self.assertEqual(result_code, 0)
            executable_plan = task_run_dir / "plan_to_code" / "executable_plan.py"
            summary = json.loads((root / "summary" / "plan_to_code_summary.json").read_text(encoding="utf-8"))
            details = json.loads((root / "summary" / "plan_to_code_results.json").read_text(encoding="utf-8"))
            bundle_data = load_bundle_data_from_executable(executable_plan)

            self.assertTrue(executable_plan.exists())
            py_compile.compile(str(executable_plan), doraise=True)
            self.assertEqual(summary["successful_generations"], 1)
            self.assertEqual(summary["failed_generations"], 0)
            self.assertEqual(details[0]["status"], "success")
            self.assertEqual(details[0]["phase_count"], 1)
            self.assertEqual(details[0]["no_trans"], 4)
            self.assertEqual(
                bundle_data["phases"],
                [[{"subtask_id": 1, "robot_number": 1}, {"subtask_id": 2, "robot_number": 1}]],
            )
            stage = bundle_data["task_plan"]["stages"][0]
            self.assertEqual(list(stage["robot_action_queues"]), ["robot1"])
            self.assertEqual(
                [action["action_type"] for action in stage["robot_action_queues"]["robot1"]],
                ["GoToObject", "OpenObject", "GoToObject", "OpenObject"],
            )
            self.assertEqual(
                [action["parameters"]["args"] for action in stage["robot_action_queues"]["robot1"]],
                [("Cabinet",), ("Cabinet",), ("Drawer",), ("Drawer",)],
            )

    def test_plantocode_falls_back_to_robot1_when_allocation_references_unknown_robot(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            task_run_dir = write_parallel_run_fixture_task(root, "6", 0)
            task = "open the cabinet with an out of range allocation robot."

            write_json(
                task_run_dir / "inputs" / "task_context.json",
                {
                    "task": task,
                    "robots": [
                        {"name": "robot1", "skills": ["GoToObject", "OpenObject"]},
                        {"name": "robot2", "skills": ["GoToObject", "OpenObject"]},
                        {"name": "robot3", "skills": ["GoToObject", "OpenObject"]},
                        {"name": "robot4", "skills": ["GoToObject", "OpenObject"]},
                    ],
                    "objects_ai": "objects = [{'name': 'Cabinet'}]",
                },
            )
            (task_run_dir / "02_allocate" / "02_allocate_output.txt").write_text(
                "# Sequence of Operations:\nSubtask 1: Robot 18;\n",
                encoding="utf-8",
            )
            write_json(
                task_run_dir / "run_manifest.json",
                {
                    "repo_root": str(root),
                    "task": task,
                    "test_set": "sample",
                    "floor_plan": "6",
                    "task_index": 0,
                    "task_run_dir": str(task_run_dir),
                },
            )
            (root / "data" / "sample" / "FloorPlan6.jsonl").write_text(
                json.dumps(
                    {
                        "task": task,
                        "robot list": [1, 2, 3, 4],
                        "object_states": [
                            {"name": "Cabinet", "contains": [], "states": ["OPENED"]},
                        ],
                        "trans": 1,
                        "min_trans": 2,
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            result_code = plantocode_main(
                [
                    "--logs-dir",
                    str(root / "logs"),
                    "--output-dir",
                    str(root / "summary"),
                ]
            )

            self.assertEqual(result_code, 0)
            executable_plan = task_run_dir / "plan_to_code" / "executable_plan.py"
            summary = json.loads((root / "summary" / "plan_to_code_summary.json").read_text(encoding="utf-8"))
            details = json.loads((root / "summary" / "plan_to_code_results.json").read_text(encoding="utf-8"))
            bundle_data = load_bundle_data_from_executable(executable_plan)

            self.assertTrue(executable_plan.exists())
            py_compile.compile(str(executable_plan), doraise=True)
            self.assertEqual(summary["successful_generations"], 1)
            self.assertEqual(summary["failed_generations"], 0)
            self.assertEqual(details[0]["status"], "success")
            self.assertEqual(details[0]["phase_count"], 1)
            self.assertEqual(details[0]["no_trans"], 2)
            self.assertEqual(
                bundle_data["phases"],
                [[{"subtask_id": 1, "robot_number": 1}]],
            )
            stage = bundle_data["task_plan"]["stages"][0]
            self.assertEqual(list(stage["robot_action_queues"]), ["robot1"])
            self.assertEqual(
                [action["action_type"] for action in stage["robot_action_queues"]["robot1"]],
                ["GoToObject", "OpenObject"],
            )

    def test_plantocode_skips_allocated_subtask_without_plan(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            task_run_dir = write_parallel_run_fixture_task(root, "6", 0)
            task = "open the cabinet, skip an unplanned subtask, then open the drawer."

            write_json(
                task_run_dir / "inputs" / "task_context.json",
                {
                    "task": task,
                    "robots": [{"name": "robot1", "skills": ["GoToObject", "OpenObject"]}],
                    "objects_ai": "objects = [{'name': 'Cabinet'}, {'name': 'Drawer'}]",
                },
            )
            (task_run_dir / "02_allocate" / "02_allocate_output.txt").write_text(
                "# Sequence of Operations:\n"
                "Subtask 1: Robot 1;\n"
                "Subtask 2: Robot 1;\n"
                "Subtask 3: Robot 1;\n",
                encoding="utf-8",
            )
            outputs_dir = task_run_dir / "08_planner" / "outputs"
            plan_1 = outputs_dir / "subtask_01_problem_validated_plan.txt"
            plan_3 = outputs_dir / "subtask_03_problem_validated_plan.txt"
            plan_3.write_text(
                "(gotoobject robot1 drawer)\n(openobject robot1 drawer)\n",
                encoding="utf-8",
            )
            write_json(
                task_run_dir / "08_planner" / "planner_manifest.json",
                [
                    {
                        "problem_file": "subtask_01_problem_validated.pddl",
                        "return_code": 0,
                        "compatibility_output": str(plan_1),
                    },
                    {
                        "problem_file": "subtask_03_problem_validated.pddl",
                        "return_code": 0,
                        "compatibility_output": str(plan_3),
                    },
                ],
            )
            write_json(
                task_run_dir / "run_manifest.json",
                {
                    "repo_root": str(root),
                    "task": task,
                    "test_set": "sample",
                    "floor_plan": "6",
                    "task_index": 0,
                    "task_run_dir": str(task_run_dir),
                },
            )
            (root / "data" / "sample" / "FloorPlan6.jsonl").write_text(
                json.dumps(
                    {
                        "task": task,
                        "robot list": [1],
                        "object_states": [
                            {"name": "Cabinet", "contains": [], "states": ["OPENED"]},
                            {"name": "Drawer", "contains": [], "states": ["OPENED"]},
                        ],
                        "trans": 2,
                        "min_trans": 4,
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            result_code = plantocode_main(
                [
                    "--logs-dir",
                    str(root / "logs"),
                    "--output-dir",
                    str(root / "summary"),
                ]
            )

            self.assertEqual(result_code, 0)
            executable_plan = task_run_dir / "plan_to_code" / "executable_plan.py"
            details = json.loads((root / "summary" / "plan_to_code_results.json").read_text(encoding="utf-8"))

            self.assertEqual(details[0]["status"], "success")
            bundle = build_hardcoded_bundle(load_bundle_data_from_executable(executable_plan))
            self.assertEqual(bundle.no_trans, 4)
            self.assertEqual(len(bundle.task_plan.stages), 2)

    def test_plantocode_appends_unassigned_planner_subtasks_in_final_phase(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            task_run_dir = write_parallel_run_fixture_task(root, "6", 0)
            task = "open the cabinet, then run the extra drawer plan last."

            write_json(
                task_run_dir / "inputs" / "task_context.json",
                {
                    "task": task,
                    "robots": [
                        {"name": "robot1", "skills": ["GoToObject", "OpenObject"]},
                        {"name": "robot2", "skills": ["GoToObject", "OpenObject"]},
                    ],
                    "objects_ai": "objects = [{'name': 'Cabinet'}, {'name': 'Drawer'}]",
                },
            )
            (task_run_dir / "02_allocate" / "02_allocate_output.txt").write_text(
                "# Sequence of Operations:\n"
                "Subtask 1: Robot 1;\n",
                encoding="utf-8",
            )
            outputs_dir = task_run_dir / "08_planner" / "outputs"
            plan_1 = outputs_dir / "subtask_01_problem_validated_plan.txt"
            plan_3 = outputs_dir / "subtask_03_problem_validated_plan.txt"
            plan_3.write_text(
                "(gotoobject robot2 drawer)\n(openobject robot2 drawer)\n",
                encoding="utf-8",
            )
            write_json(
                task_run_dir / "08_planner" / "planner_manifest.json",
                [
                    {
                        "problem_file": "subtask_01_problem_validated.pddl",
                        "return_code": 0,
                        "compatibility_output": str(plan_1),
                    },
                    {
                        "problem_file": "subtask_03_problem_validated.pddl",
                        "return_code": 0,
                        "compatibility_output": str(plan_3),
                    },
                ],
            )
            write_json(
                task_run_dir / "run_manifest.json",
                {
                    "repo_root": str(root),
                    "task": task,
                    "test_set": "sample",
                    "floor_plan": "6",
                    "task_index": 0,
                    "task_run_dir": str(task_run_dir),
                },
            )
            (root / "data" / "sample" / "FloorPlan6.jsonl").write_text(
                json.dumps(
                    {
                        "task": task,
                        "robot list": [1, 2],
                        "object_states": [
                            {"name": "Cabinet", "contains": [], "states": ["OPENED"]},
                            {"name": "Drawer", "contains": [], "states": ["OPENED"]},
                        ],
                        "trans": 2,
                        "min_trans": 4,
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            result_code = plantocode_main(
                [
                    "--logs-dir",
                    str(root / "logs"),
                    "--output-dir",
                    str(root / "summary"),
                ]
            )

            self.assertEqual(result_code, 0)
            executable_plan = task_run_dir / "plan_to_code" / "executable_plan.py"
            details = json.loads((root / "summary" / "plan_to_code_results.json").read_text(encoding="utf-8"))
            bundle_data = load_bundle_data_from_executable(executable_plan)

            self.assertEqual(details[0]["status"], "success")
            self.assertEqual(details[0]["phase_count"], 2)
            self.assertEqual(
                bundle_data["phases"],
                [
                    [{"subtask_id": 1, "robot_number": 1}],
                    [{"subtask_id": 3, "robot_number": 2}],
                ],
            )
            second_stage = bundle_data["task_plan"]["stages"][1]
            self.assertEqual(list(second_stage["robot_action_queues"]), ["robot2"])
            self.assertEqual(
                [action["parameters"]["args"] for action in second_stage["robot_action_queues"]["robot2"]],
                [("Drawer",), ("Drawer",)],
            )

    def test_plantocode_skips_missing_manifest_plan_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            task_run_dir = write_parallel_run_fixture_task(root, "6", 0)
            task = "open the cabinet, skip a failed subtask, then open the drawer."

            write_json(
                task_run_dir / "inputs" / "task_context.json",
                {
                    "task": task,
                    "robots": [{"name": "robot1", "skills": ["GoToObject", "OpenObject"]}],
                    "objects_ai": "objects = [{'name': 'Cabinet'}, {'name': 'Drawer'}]",
                },
            )
            (task_run_dir / "02_allocate" / "02_allocate_output.txt").write_text(
                "# Sequence of Operations:\n"
                "Subtask 1: Robot 1;\n"
                "Subtask 2: Robot 1;\n"
                "Subtask 3: Robot 1;\n",
                encoding="utf-8",
            )
            outputs_dir = task_run_dir / "08_planner" / "outputs"
            plan_1 = outputs_dir / "subtask_01_problem_validated_plan.txt"
            missing_plan_2 = outputs_dir / "subtask_02_problem_validated_plan.txt"
            plan_3 = outputs_dir / "subtask_03_problem_validated_plan.txt"
            plan_3.write_text(
                "(gotoobject robot1 drawer)\n(openobject robot1 drawer)\n",
                encoding="utf-8",
            )
            write_json(
                task_run_dir / "08_planner" / "planner_manifest.json",
                [
                    {
                        "problem_file": "subtask_01_problem_validated.pddl",
                        "return_code": 0,
                        "compatibility_output": str(plan_1),
                    },
                    {
                        "problem_file": "subtask_02_problem_validated.pddl",
                        "return_code": 0,
                        "compatibility_output": str(missing_plan_2),
                    },
                    {
                        "problem_file": "subtask_03_problem_validated.pddl",
                        "return_code": 0,
                        "compatibility_output": str(plan_3),
                    },
                ],
            )
            write_json(
                task_run_dir / "run_manifest.json",
                {
                    "repo_root": str(root),
                    "task": task,
                    "test_set": "sample",
                    "floor_plan": "6",
                    "task_index": 0,
                    "task_run_dir": str(task_run_dir),
                },
            )
            (root / "data" / "sample" / "FloorPlan6.jsonl").write_text(
                json.dumps(
                    {
                        "task": task,
                        "robot list": [1],
                        "object_states": [
                            {"name": "Cabinet", "contains": [], "states": ["OPENED"]},
                            {"name": "Drawer", "contains": [], "states": ["OPENED"]},
                        ],
                        "trans": 2,
                        "min_trans": 4,
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result_code = plantocode_main(
                    [
                        "--logs-dir",
                        str(root / "logs"),
                        "--output-dir",
                        str(root / "summary"),
                    ]
                )

            output = stdout.getvalue()
            self.assertEqual(result_code, 0)
            self.assertIn("Skipping missing planner output listed in manifest", output)
            self.assertIn(str(missing_plan_2), output)
            self.assertNotIn("Planner output file(s) not found", output)

            executable_plan = task_run_dir / "plan_to_code" / "executable_plan.py"
            details = json.loads((root / "summary" / "plan_to_code_results.json").read_text(encoding="utf-8"))

            self.assertEqual(details[0]["status"], "success")
            bundle = build_hardcoded_bundle(load_bundle_data_from_executable(executable_plan))
            self.assertEqual(bundle.no_trans, 4)
            self.assertEqual(len(bundle.task_plan.stages), 2)

    def test_lammap_baseline_mode_writes_parallel_runner_summary_path(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            baseline_root = Path(tmp_dir) / "baselines" / "LaMMA-P"
            task_run_dir = write_parallel_run_fixture_task(baseline_root, "6", 0)

            result_code = lammap_baseline.main(
                [
                    "--root",
                    str(baseline_root),
                ]
            )

            self.assertEqual(result_code, 0)
            executable_plan = task_run_dir / "plan_to_code" / "executable_plan.py"
            output_dir = baseline_root / "plan_to_code_results"
            details = json.loads((output_dir / "plan_to_code_results.json").read_text(encoding="utf-8"))
            summary = json.loads((output_dir / "plan_to_code_summary.json").read_text(encoding="utf-8"))

            self.assertEqual(summary["successful_generations"], 1)
            self.assertEqual(details[0]["status"], "success")
            self.assertTrue(details[0]["success"])
            self.assertEqual(details[0]["generated"]["executable_plan"], str(executable_plan))
            self.assertTrue(is_runner_compatible_executable(executable_plan))

    def test_lammap_baseline_mode_uses_native_final_plan_artifact(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            baseline_root = root / "baselines" / "LaMMA-P"
            task_run_dir = write_lammap_native_fixture(root)

            result_code = lammap_baseline.main(
                [
                    "--root",
                    str(baseline_root),
                ]
            )

            self.assertEqual(result_code, 0)
            executable_plan = task_run_dir / "plan_to_code" / "executable_plan.py"
            output_dir = baseline_root / "plan_to_code_results"
            details = json.loads((output_dir / "plan_to_code_results.json").read_text(encoding="utf-8"))
            bundle_data = load_bundle_data_from_executable(executable_plan)

            self.assertTrue(executable_plan.exists())
            self.assertEqual(details[0]["status"], "success")
            self.assertEqual(details[0]["category"], "timed_direct_actions")
            self.assertEqual(details[0]["test_set"], "unit_set")
            self.assertEqual(details[0]["generated"]["executable_plan"], str(executable_plan))
            self.assertTrue(is_runner_compatible_executable(executable_plan))
            self.assertEqual([item["name"] for item in bundle_data["gcr"]], ["Drawer"])

    def test_lammap_baseline_requires_top_level_summary_test_set(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            baseline_root = root / "baselines" / "LaMMA-P"
            task_run_dir = write_lammap_native_fixture(root)
            source_summary = (
                baseline_root
                / "parallel_runs"
                / "pddlrun_lammap_fixture"
                / "summary.json"
            )
            summary_data = json.loads(source_summary.read_text(encoding="utf-8"))
            summary_data.pop("test_set")
            write_json(source_summary, summary_data)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result_code = lammap_baseline.main(
                    [
                        "--root",
                        str(baseline_root),
                    ]
                )

            self.assertEqual(result_code, 1)
            self.assertIn(
                "missing required non-empty top-level 'test_set'",
                stdout.getvalue(),
            )
            self.assertFalse((task_run_dir / "plan_to_code" / "executable_plan.py").exists())

    def test_smart_llm_baseline_mode_writes_parallel_runner_summary_path(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            baseline_root = Path(tmp_dir) / "baselines" / "SMART-LLM"
            task_run_dir = write_parallel_run_fixture_task(
                baseline_root,
                "6",
                0,
                log_parts=("logs", "2"),
            )

            result_code = smart_llm_baseline.main(
                [
                    "--root",
                    str(baseline_root),
                ]
            )

            self.assertEqual(result_code, 0)
            executable_plan = task_run_dir / "plan_to_code" / "executable_plan.py"
            details = json.loads((baseline_root / "plan_to_code_results.json").read_text(encoding="utf-8"))
            summary = json.loads((baseline_root / "plan_to_code_summary.json").read_text(encoding="utf-8"))

            self.assertEqual(summary["successful_generations"], 1)
            self.assertEqual(details[0]["status"], "success")
            self.assertTrue(details[0]["success"])
            self.assertEqual(details[0]["generated"]["executable_plan"], str(executable_plan))
            self.assertTrue(is_runner_compatible_executable(executable_plan))

    def test_smart_llm_baseline_mode_uses_native_code_plan_artifact(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            baseline_root = root / "baselines" / "SMART-LLM"
            task_run_dir = write_smart_native_fixture(root, "code_plan.py")

            result_code = smart_llm_baseline.main(
                [
                    "--root",
                    str(baseline_root),
                ]
            )

            self.assertEqual(result_code, 0)
            executable_plan = task_run_dir / "plan_to_code" / "executable_plan.py"
            details = json.loads((baseline_root / "plan_to_code_results.json").read_text(encoding="utf-8"))

            self.assertTrue(executable_plan.exists())
            self.assertEqual(details[0]["status"], "success")
            self.assertEqual(details[0]["source_path"], str(task_run_dir / "code_plan.py"))
            self.assertEqual(details[0]["generated"]["executable_plan"], str(executable_plan))
            self.assertTrue(is_runner_compatible_executable(executable_plan))

    def test_smart_llm_baseline_mode_uses_native_decomposed_plan_artifact(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            baseline_root = root / "baselines" / "SMART-LLM"
            task_run_dir = write_smart_native_fixture(root, "decomposed_plan.py")

            result_code = smart_llm_baseline.main(
                [
                    "--root",
                    str(baseline_root),
                ]
            )

            self.assertEqual(result_code, 0)
            executable_plan = task_run_dir / "plan_to_code" / "executable_plan.py"
            details = json.loads((baseline_root / "plan_to_code_results.json").read_text(encoding="utf-8"))

            self.assertTrue(executable_plan.exists())
            self.assertEqual(details[0]["status"], "success")
            self.assertEqual(details[0]["source_path"], str(task_run_dir / "decomposed_plan.py"))
            self.assertEqual(details[0]["generated"]["executable_plan"], str(executable_plan))
            self.assertTrue(is_runner_compatible_executable(executable_plan))

    def test_scale_plan_baseline_mode_uses_summary_index_and_final_plan_json(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            baseline_root = root / "baselines" / "Scale-Plan"
            task_run_dir = write_scale_plan_fixture(root)

            result_code = scale_plan_baseline.main(
                [
                    "--root",
                    str(baseline_root),
                ]
            )

            self.assertEqual(result_code, 0)
            executable_plan = task_run_dir / "plan_to_code" / "executable_plan.py"
            output_dir = baseline_root / "plan_to_code_results"
            details = json.loads((output_dir / "plan_to_code_results.json").read_text(encoding="utf-8"))
            summary = json.loads((output_dir / "plan_to_code_summary.json").read_text(encoding="utf-8"))
            bundle_data = load_bundle_data_from_executable(executable_plan)

            self.assertEqual(summary["successful_generations"], 1)
            self.assertEqual(details[0]["status"], "success")
            self.assertEqual(details[0]["test_set"], "unit_set")
            self.assertNotIn("source_task_run_dir", details[0])
            self.assertEqual(details[0]["generated"]["executable_plan"], str(executable_plan))
            self.assertTrue(is_runner_compatible_executable(executable_plan))
            self.assertEqual(
                [item["name"] for item in bundle_data["gcr"]],
                ["Drawer", "Cabinet"],
            )

            stages = bundle_data["task_plan"]["stages"]
            self.assertEqual(len(stages), 2)
            queues = stages[0]["robot_action_queues"]
            self.assertEqual(set(queues), {"robot1", "robot2"})
            self.assertEqual([action["action_type"] for action in queues["robot1"]], [
                "GoToObject",
                "OpenObject",
            ])
            self.assertEqual([action["action_type"] for action in queues["robot2"]], [
                "GoToObject",
                "OpenObject",
            ])
            self.assertEqual(
                [action["parameters"]["args"] for action in queues["robot1"]],
                [["Drawer"], ["Drawer"]],
            )
            self.assertEqual(
                [action["parameters"]["args"] for action in queues["robot2"]],
                [["Cabinet"], ["Cabinet"]],
            )
            self.assertTrue(
                all(action["robot_id"] == "robot1" for action in queues["robot1"])
            )
            self.assertTrue(
                all(action["robot_id"] == "robot2" for action in queues["robot2"])
            )

            repeated_robot_queue = stages[1]["robot_action_queues"]
            self.assertEqual(set(repeated_robot_queue), {"robot1"})
            self.assertEqual(repeated_robot_queue["robot1"][0]["robot_id"], "robot1")

    def test_scale_plan_baseline_rejects_robot_outside_dataset_team(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            baseline_root = root / "baselines" / "Scale-Plan"
            task_run_dir = write_scale_plan_fixture(root)
            final_plan_path = task_run_dir / "05_plan" / "03_final_plan.json"
            final_plan = json.loads(final_plan_path.read_text(encoding="utf-8"))
            outside_plan = final_plan["stages"][0]["plans"][1]
            outside_plan["robot"] = "robot2"
            outside_plan["plan"] = (
                "(GoToObject robot2 Cabinet)\n(OpenObject robot2 Cabinet)"
            )
            write_json(final_plan_path, final_plan)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result_code = scale_plan_baseline.main(
                    [
                        "--root",
                        str(baseline_root),
                    ]
                )

            self.assertEqual(result_code, 1)
            details = json.loads(
                (baseline_root / "plan_to_code_results" / "plan_to_code_results.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertIn("robot2", details[0]["error"])
            self.assertIn("'robot list'", details[0]["error"])
            self.assertFalse((task_run_dir / "plan_to_code" / "executable_plan.py").exists())

    def test_scale_plan_robot_id_map_rejects_invalid_dataset_teams(self):
        cases = (
            (None, "missing a non-empty list"),
            ([], "missing a non-empty list"),
            ([18, 18], "Duplicate robot id"),
            (["not-a-robot"], "Invalid robot id"),
            ([0], "Invalid robot id"),
        )
        for robot_ids, expected_error in cases:
            with self.subTest(robot_ids=robot_ids):
                with self.assertRaisesRegex(
                    scale_plan_baseline.ScalePlanConversionError,
                    expected_error,
                ):
                    scale_plan_baseline.build_robot_id_map(robot_ids)

    def test_scale_plan_baseline_requires_top_level_summary_test_set(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            baseline_root = root / "baselines" / "Scale-Plan"
            task_run_dir = write_scale_plan_fixture(root)
            source_summary = (
                baseline_root
                / "logs"
                / "scale_plan_parallel"
                / "pddlrun_scale_plan_fixture"
                / "summary.json"
            )
            summary_data = json.loads(source_summary.read_text(encoding="utf-8"))
            summary_data.pop("test_set")
            write_json(source_summary, summary_data)

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result_code = scale_plan_baseline.main(
                    [
                        "--root",
                        str(baseline_root),
                    ]
                )

            self.assertEqual(result_code, 1)
            self.assertIn(
                "missing required non-empty top-level 'test_set'",
                stdout.getvalue(),
            )
            self.assertFalse((task_run_dir / "plan_to_code" / "executable_plan.py").exists())

    def test_baseline_mode_explicit_logs_and_output_dirs_override_defaults(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            baseline_root = root / "baselines" / "LaMMA-P"
            source_root = root / "custom_source"
            output_dir = root / "custom_summary"
            task_run_dir = write_parallel_run_fixture_task(source_root, "6", 0)

            result_code = lammap_baseline.main(
                [
                    "--root",
                    str(baseline_root),
                    "--logs-dir",
                    str(source_root / "logs"),
                    "--output-dir",
                    str(output_dir),
                ]
            )

            self.assertEqual(result_code, 0)
            executable_plan = task_run_dir / "plan_to_code" / "executable_plan.py"
            details = json.loads((output_dir / "plan_to_code_results.json").read_text(encoding="utf-8"))

            self.assertTrue(executable_plan.exists())
            self.assertEqual(details[0]["generated"]["executable_plan"], str(executable_plan))
            self.assertFalse((baseline_root / "plan_to_code_results" / "plan_to_code_results.json").exists())
            self.assertTrue(is_runner_compatible_executable(executable_plan))

    def test_parallel_run_converts_all_summary_tasks(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            floor6_task = write_parallel_run_fixture_task(root, "6", 0)
            floor7_task = write_parallel_run_fixture_task(root, "7", 0)
            parallel_run = root / "parallel_runs" / "pddlrun_fixture"
            write_parallel_run_summary(root, parallel_run, [floor6_task, floor7_task])

            result_code = plantocode_main(
                [
                    "--parallel-run",
                    str(parallel_run),
                    "--output-dir",
                    str(root / "summary"),
                ]
            )

            self.assertEqual(result_code, 0)
            self.assertTrue((floor6_task / "plan_to_code" / "executable_plan.py").exists())
            self.assertTrue((floor7_task / "plan_to_code" / "executable_plan.py").exists())
            summary = json.loads((root / "summary" / "plan_to_code_summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["total_results"], 2)
            self.assertEqual(summary["successful_generations"], 2)

    def test_parallel_run_floor_plan_filter_converts_only_matching_tasks(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            floor6_task = write_parallel_run_fixture_task(root, "6", 0)
            floor7_task = write_parallel_run_fixture_task(root, "7", 0)
            parallel_run = root / "parallel_runs" / "pddlrun_fixture"
            write_parallel_run_summary(root, parallel_run, [floor6_task, floor7_task])

            result_code = plantocode_main(
                [
                    "--parallel-run",
                    str(parallel_run),
                    "--floor-plan",
                    "FloorPlan6",
                    "--output-dir",
                    str(root / "summary"),
                ]
            )

            self.assertEqual(result_code, 0)
            self.assertTrue((floor6_task / "plan_to_code" / "executable_plan.py").exists())
            self.assertFalse((floor7_task / "plan_to_code" / "executable_plan.py").exists())
            summary = json.loads((root / "summary" / "plan_to_code_summary.json").read_text(encoding="utf-8"))
            details = json.loads((root / "summary" / "plan_to_code_results.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["total_results"], 1)
            self.assertEqual(summary["successful_generations"], 1)
            self.assertEqual(details[0]["floor_plan"], "6")


if __name__ == "__main__":
    unittest.main()
