#!/usr/bin/env python3
"""Compatibility facade for the reorganized demo2 execution system."""

import math
import sys
import types

from executor_system import actions as _actions
from executor_system import config as _config
from executor_system import context as _context
from executor_system import demo_state as _demo_state
from executor_system import dependencies as _dependencies
from executor_system import runtime as _runtime_module

robots = ["robot1", "robot2"]
floor_no = "1"
# ground_truth = [
#     {"name": "Potato", "contains": [], "states": ["SLICED", "COOKED"]},
#     {"name": "Drawer", "contains": [], "states": ["OPENED"]},
#     {"name": "Cabinet", "contains": [], "states": ["OPENED"]},
#     {"name": "Window", "contains": [], "states": ["BROKEN"]},
# ]

# ground_truth = [
#     {"name": "Egg", "contains": [], "states": ["COOKED"]},
# ]

# ground_truth = [
#     {"name": "Mug", "contains": [], "states": ["FILLEDWITHCOFFEE"]},
# ]

# ground_truth = [
#     {"name": "Bottle", "contains": [], "states": ["FILLEDWITHWATER"]},
# ]

# ground_truth = [
#     {"name": "Pan", "contains": [], "states": ["CLEANED"]},
# ]

# ground_truth = [
#     {"name": "Potato", "contains": [], "states": ["COLD"]},
# ]

# ground_truth = [
#     {"name": "Pan", "contains": [], "states": ["Hot"]},
# ]

ground_truth = [
    {"name": "Bread", "contains": [], "states": ["COOKED"]},
]

_demo_state.set_ground_truth(ground_truth)

from executor_system.action_plan import *
from executor_system.actions import *
from executor_system.conflict_resolver import *
from executor_system.config import *
from executor_system.dependencies import *
from executor_system.executor import *
from executor_system.executor_requests import *
from executor_system.goals import *
from executor_system.plan_validator import *
from executor_system.resource_inferencer import *
from executor_system.resource_manager import *
from executor_system.runtime import *
from executor_system.stage_runner import *
from executor_system.synchronous_executor import *
from executor_system.task_plan import *
from executor_system.task_runner import *
from executor_system.thor_adapter import *
from executor_system.utils import *
from executor_system.world_state import *
from executor_system.task_plan import run_action_plan
from executor_system.runtime import ThorRuntime

for _name in dir(_actions):
    if _name.startswith("_") and not _name.startswith("__"):
        globals()[_name] = getattr(_actions, _name)

runtime = None
cv2 = _dependencies.cv2
Controller = _dependencies.Controller
CloudRendering = _dependencies.CloudRendering


def run_subtask_01(robot: RobotRef) -> None:
    # Subtask 1
    # (gotoobject robot1 knife)
    GoToObject(robot, "Knife")
    # (pickupobject robot1 knife)
    PickupObject(robot, "Knife")
    # (gotoobject robot1 potato)
    GoToObject(robot, "Potato")
    # (sliceobject robot1 potato countertop knife)
    SliceObject(robot, "Potato")

def run_subtask_slice_bread(robot: RobotRef) -> None:
    # Subtask 1
    # (gotoobject robot1 knife)
    GoToObject(robot, "Knife")
    # (pickupobject robot1 knife)
    PickupObject(robot, "Knife")
    # (gotoobject robot1 potato)
    GoToObject(robot, "Bread")
    # (sliceobject robot1 potato countertop knife)
    SliceObject(robot, "Bread")

def run_subtask_toast_bread(robot: RobotRef) -> None:
    # (gotoobject robot1 knife)
    GoToObject(robot, "Knife")
    # (openobject robot1 drawer)
    OpenObject(robot, "Drawer")
    # (pickupobject robot1 knife)
    PickupObject(robot, "Knife")
    # (gotoobject robot1 potato)
    GoToObject(robot, "Bread")
    # (sliceobject robot1 potato countertop knife)
    SliceObject(robot, "Bread")
    # (pickupobject robot1 bread)
    PickupObject(robot, "Bread")
    # (gotoobject robot1 toaster)
    GoToObject(robot, "Toaster")
    # (runtoaster robot1 bread)
    RunToaster(robot, "Toaster", "Bread")

def run_subtask_cook_egg(robot: RobotRef) -> None:
    # (gotoobject robot1 fridge)
    GoToObject(robot, "Fridge")
    # (gotoobject robot1 fridge)
    GoToObject(robot, "Fridge")
    # (openobject robot1 fridge)
    OpenObject(robot, "Fridge")
    # (pickupobject robot1 egg)
    PickupObject(robot, "Egg")
    # (gotoobject robot1 pan)
    GoToObject(robot, "Pan")
    # (putobject robot1 egg pan)
    PutObject(robot, "Egg", "Pan")
    # (break robot1 egg)
    PrepareEgg(robot, "Egg")
    # (pickupobject robot1 pan)
    PickupObject(robot, "Pan")
    # (cookbystoveburner robot1 stoveburner pan food)
    CookByStoveBurner(robot, "StoveBurner", "Pan", "Egg")

def run_subtask_fill_coffee(robot: RobotRef) -> None:
    # (gotoobject robot1 mug)
    GoToObject(robot, "Mug")
    # (pickupobject robot1 mug)
    PickupObject(robot, "Mug")
    # (gotoobject robot1 coffeemachine)
    GoToObject(robot, "CoffeeMachine")
    # (putobject robot1 mug coffeemachine)
    PutObject(robot, "Mug", "CoffeeMachine")
    # (putobject robot1 coffeemachine mug)
    RunCoffeeMachine(robot, "CoffeeMachine", "Mug")

def run_subtask_fill_water(robot: RobotRef) -> None:
    # (gotoobject robot1 bottle)
    GoToObject(robot, "Bottle")
    # (pickupobject robot1 bottle)
    PickupObject(robot, "Bottle")
    # (gotoobject robot1 sink)
    GoToObject(robot, "Sink")
    # (putobject robot1 sink bottle)
    FillWater(robot, "Sink", "Bottle")

def run_subtask_clean(robot: RobotRef) -> None: 
    # (gotoobject robot1 bottle)
    GoToObject(robot, "Pan")
    # (pickupobject robot1 bottle)
    PickupObject(robot, "Pan")
    # (gotoobject robot1 sink)
    GoToObject(robot, "Sink")
    # (putobject robot1 sink bottle)
    CleanObject(robot, "Pan", "Sink")

def run_subtask_cold(robot: RobotRef) -> None: 
    # (gotoobject robot1 potato)
    GoToObject(robot, "Potato")
    # (pickupobject robot1 bottle)
    PickupObject(robot, "Potato")
    # (gotoobject robot1 fridge)
    GoToObject(robot, "Fridge")
    # (openobject robot1 fridge)
    OpenObject(robot, "Fridge")
    # (putobject robot1 potato fridge)
    PutObject(robot, "Potato", "Fridge")
    # (closeobject robot1 fridge)
    CloseObject(robot, "Fridge")
    # (coldobject robot1 fridge potato)
    ColdObject(robot, "Fridge", "Potato")

def run_subtask_heat(robot: RobotRef) -> None:
    # (gotoobject robot1 pan)
    GoToObject(robot, "Pan")
    # (pickupobject robot1 egg)
    PickupObject(robot, "Pan")
    # (cookbystoveburner robot1 stoveburner pan food)
    HeatByStoveBurner(robot, "StoveBurner", "Pan")

def run_subtask_02(robot: RobotRef) -> None:
    # Subtask 2
    # (gotoobject robot1 drawer)
    GoToObject(robot, "Drawer")
    # (openobject robot1 drawer)
    OpenObject(robot, "Drawer")


def run_subtask_03(robot: RobotRef) -> None:
    # Subtask 3
    # (gotoobject robot1 cabinet)
    GoToObject(robot, "Cabinet")
    # (openobject robot1 cabinet)
    OpenObject(robot, "Cabinet")


def run_subtask_04(robot: RobotRef) -> None:
    # Subtask 4
    # (gotoobject robot1 window)
    GoToObject(robot, "Window")
    # (breakobject robot1 window)
    BreakObject(robot, "Window")


def run_subtask_05(robot: RobotRef) -> None:
    # Subtask 5
    # (gotoobject robot1 potato)
    GoToObject(robot, "Potato")
    # (pickupobject robot1 potato)
    PickupObject(robot, "Potato")
    # (gotoobject robot1 microwave)
    GoToObject(robot, "Microwave")
    # (openobject robot1 microwave)
    OpenObject(robot, "Microwave")
    # (putobject robot1 potato microwave)
    PutObject(robot, "Potato", "Microwave")
    # (closeobject robot1 microwave)
    CloseObject(robot, "Microwave")
    # (runmicrowave robot1 microwave potato)
    RunMicrowave(robot, "Microwave", "Potato")


def run_subtask_06(robot: RobotRef) -> None:
    # Subtask 6
    # (gotoobject robot1 potato)
    GoToObject(robot, "Potato")
    # (pickupobject robot1 potato)
    PickupObject(robot, "Potato")
    # (gotoobject robot1 fridge)
    GoToObject(robot, "Fridge")
    # (openobject robot1 fridge)
    OpenObject(robot, "Fridge")
    # (putobject robot1 potato fridge)
    PutObject(robot, "Potato", "Fridge")
    # (closeobject robot1 fridge)
    CloseObject(robot, "Fridge")

def run_subtask_07(robot: RobotRef) -> None:
    # Subtask 7
    # (gotoobject robot1 bread)
    GoToObject(robot, "Bread")
    # PickupObject(robot, "bread")
    PickupObject(robot, "Bread")
    # (gotoobject robot1 pan)
    GoToObject(robot, "Toaster")
    # (putobject robot1 bread pan)
    PutObject(robot, "Bread", "Toaster")


def main() -> int:
    global runtime
    runtime = ThorRuntime(robots, floor_no, CLOUD_RENDERING, RENDER_IMAGE)
    _context.runtime = runtime
    try:
        run_action_plan(
            TaskPlanParser("tmp0").parse(
                [
                    ("Phase 1", [(robots[0], [run_subtask_toast_bread])]),
                ]
            )
        )
        runtime.step({"action": "Done"}, check_success=False)
        metrics = runtime.evaluate(ground_truth)
        print(
            "SR:{sr}, TC:{tc}, GCR:{gcr}, Exec:{exec_rate}, RU:{ru}".format(
                sr=int(metrics["sr"]),
                tc=int(metrics["tc"]),
                gcr=metrics["gcr"],
                exec_rate=metrics["exec_rate"],
                ru=metrics["ru"],
            )
        )
        runtime.log_unmet_goals(ground_truth)
        runtime.generate_video()
        runtime.write_final_metadata()
        return 0
    finally:
        runtime.stop()
        runtime = None
        _context.runtime = None


class _Demo2Facade(types.ModuleType):
    def __getattribute__(self, name):
        if name == "runtime":
            return _context.runtime
        if name == "cv2":
            return _dependencies.cv2
        return super().__getattribute__(name)

    def __setattr__(self, name, value):
        if name == "runtime":
            _context.runtime = value
        elif name == "cv2":
            _dependencies.cv2 = value
            _runtime_module.cv2 = value
        elif name == "ground_truth":
            _demo_state.set_ground_truth(value)
        elif hasattr(_demo_state, name):
            setattr(_demo_state, name, value)
        elif hasattr(_config, name):
            setattr(_config, name, value)
            if hasattr(_actions, name):
                setattr(_actions, name, value)
            if hasattr(_runtime_module, name):
                setattr(_runtime_module, name, value)
        elif hasattr(_dependencies, name):
            setattr(_dependencies, name, value)
            if hasattr(_runtime_module, name):
                setattr(_runtime_module, name, value)
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _Demo2Facade


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(1)
