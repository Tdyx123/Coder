import json
import random
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent
for path in (_SCRIPT_DIR, _REPO_ROOT):
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.append(path_str)

from ai2thor_object_cache import get_ai2_thor_objects_cached
from file_processor import PDDLError
from llm_handler import LLMHandler
from resources.robots import robots
from run_config import load_run_config


def _repo_root() -> Path:
    return _REPO_ROOT


AI2THOR_OBJECT_PROPERTIES_PATH = _REPO_ROOT / "data" / "all_ai2thor_objects.json"
AI2THOR_OBJECT_PROPERTY_FIELDS = (
    "breakable",
    "pickupable",
    "sliceable",
    "openable",
    "receptacle",
    "toggleable",
    "dirtyable",
)


class ObjectPropertiesError(ValueError):
    """Raised when floor-plan-specific AI2-THOR object properties are invalid."""


def _normalize_scene_name(floor_plan: Union[int, str]) -> str:
    text = str(floor_plan).strip()
    if not text:
        raise ObjectPropertiesError("floor_plan is required")

    if text.startswith("FloorPlan"):
        text = text[len("FloorPlan"):]

    if not text.isdigit():
        raise ObjectPropertiesError(f"Invalid floor_plan: {floor_plan}")

    scene_id = int(text)
    if scene_id < 1:
        raise ObjectPropertiesError(f"Invalid floor_plan: {floor_plan}")

    return f"FloorPlan{scene_id}"


def _load_ai2thor_object_type_properties(
    floor_plan: Union[int, str],
    path: Path = AI2THOR_OBJECT_PROPERTIES_PATH,
) -> Dict[str, Dict[str, bool]]:
    scene_name = _normalize_scene_name(floor_plan)

    if not path.exists():
        raise FileNotFoundError(f"AI2-THOR object properties file not found: {path}")

    try:
        with open(path, "r", encoding="utf-8") as f:
            objects = json.load(f)
    except json.JSONDecodeError as exc:
        raise ObjectPropertiesError(f"Invalid AI2-THOR object properties JSON: {path}") from exc

    if not isinstance(objects, list):
        raise ObjectPropertiesError(f"AI2-THOR object properties must be a list: {path}")

    object_type_properties: Dict[str, Dict[str, bool]] = {}
    for index, item in enumerate(objects):
        if not isinstance(item, dict):
            raise ObjectPropertiesError(f"AI2-THOR object entry #{index} must be an object")

        scene = item.get("scene")
        if not isinstance(scene, str) or not scene:
            raise ObjectPropertiesError(f"AI2-THOR object entry #{index} is missing scene")
        if scene != scene_name:
            continue

        object_type = item.get("objectType")
        if not isinstance(object_type, str) or not object_type:
            raise ObjectPropertiesError(f"AI2-THOR object entry #{index} is missing objectType")

        properties = object_type_properties.setdefault(
            object_type,
            {field: False for field in AI2THOR_OBJECT_PROPERTY_FIELDS},
        )
        for field in AI2THOR_OBJECT_PROPERTY_FIELDS:
            properties[field] = properties[field] or bool(item.get(field, False))

    if not object_type_properties:
        raise ObjectPropertiesError(
            f"No AI2-THOR object properties found for scene {scene_name} in {path}"
        )

    return object_type_properties


def _objects_with_property(
    object_type_properties: Dict[str, Dict[str, bool]],
    property_name: str,
) -> List[str]:
    return sorted(
        object_type
        for object_type, properties in object_type_properties.items()
        if properties.get(property_name, False)
    )


def _openable_receptacles(
    object_type_properties: Dict[str, Dict[str, bool]],
) -> List[str]:
    return sorted(
        object_type
        for object_type, properties in object_type_properties.items()
        if properties.get("openable", False) and properties.get("receptacle", False)
    )


def _non_openable_receptacles(
    object_type_properties: Dict[str, Dict[str, bool]],
) -> List[str]:
    return sorted(
        object_type
        for object_type, properties in object_type_properties.items()
        if properties.get("receptacle", False) and not properties.get("openable", False)
    )


def _build_object_skill_sets(
    floor_plan: Union[int, str],
    path: Path = AI2THOR_OBJECT_PROPERTIES_PATH,
) -> Dict[str, List[str]]:
    object_type_properties = _load_ai2thor_object_type_properties(floor_plan, path)
    return {
        "breakable_objects": _objects_with_property(object_type_properties, "breakable"),
        "pickupable_objects": _objects_with_property(object_type_properties, "pickupable"),
        "sliceable_objects": _objects_with_property(object_type_properties, "sliceable"),
        "washable_objects": _objects_with_property(object_type_properties, "dirtyable"),
        "openable_objects": _objects_with_property(object_type_properties, "openable"),
        "openable_containers": _openable_receptacles(object_type_properties),
        "has_placing_surface_objects": _non_openable_receptacles(object_type_properties),
        "switchable_objects": _objects_with_property(object_type_properties, "toggleable"),
    }

food = ['Apple', 'Bread', 'Egg', 'Lettuce', 'Potato', 'Tomato']
food_containers = ['Pot', 'Bowl', 'Plate', 'Pan']

SKILL_TO_ROBOT_SKILLS = {
    'Open': ['GoToObject', 'OpenObject'],
    'SwitchOn': ['GoToObject', 'SwitchOn'],
    'Wash': ['GoToObject', 'PickupObject', 'CleanObject'],
    'Break': ['GoToObject', 'BreakObject'],
    'Slice': ['GoToObject', 'PickupObject', 'SliceObject'],
    'PutOn': ['GoToObject', 'PickupObject', 'PutObject'],
    'PutIn': ['GoToObject', 'OpenObject', 'CloseObject', 'PickupObject', 'PutObject'],
}

MASS_OBJECTS = ['Knife']

class DataEngine:

    def __init__(self, model: str = "deepseek-chat"):
        self.config = load_run_config(_repo_root(), error_cls=PDDLError)
        self.llm = LLMHandler(self.config)
        self.model = model
        seed = int(time.time())
        random.seed(seed)

    def extract_subtask_skill(self, folder: str) -> List[dict]:
        folder_path = Path(folder)
        if not folder_path.exists():
            raise FileNotFoundError(f"Folder not found: {folder}")

        results = []
        txt_files = list(folder_path.glob("*.txt"))

        for file_path in txt_files:
            try:
                content = file_path.read_text(encoding="utf-8")
            except Exception:
                continue

            subtask_match = re.search(r"#Subtask\s+\d+(?:[:.]\s*)?(.+)", content)
            if not subtask_match:
                continue

            subtask_name = subtask_match.group(1).strip()
            skill_matches = re.findall(r"^([A-Za-z]+Object):", content, re.MULTILINE)
            skills = list(dict.fromkeys(skill_matches))

            results.append({"subtask": subtask_name, "skills": skills})

        return results
    
    def get_robot(self, robot_idx: int) -> List[str]:
        if 1 <= robot_idx <= len(robots):
            return robots[robot_idx - 1]
        return None

    def get_objects(self, floor_plan: int) -> List[str]:
        def convert_to_name_list(objs: List[str], obj_mass: List[float]) -> List[str]:
            return objs
        return get_ai2_thor_objects_cached(floor_plan, convert_to_name_list)

    def get_objects_with_mass(self, floor_plan: int) -> List[Dict[str, Any]]:
        def convert_to_dict(objs: List[str], obj_mass: List[float]) -> List[Dict[str, Any]]:
            return [{'name': obj, 'mass': mass} for obj, mass in zip(objs, obj_mass)]
        return get_ai2_thor_objects_cached(floor_plan, convert_to_dict)

    def _get_object_mass(self, obj_name: str, obj_mass_map: Dict[str, float]) -> float:
        return obj_mass_map.get(obj_name, 0.0)

    def subtask_to_str(self, subtask: Dict) -> str:
        skill = subtask['skill']
        objs = subtask['objects']
        obj_strs = [o.lower() for o in objs]

        if skill == 'Open':
            return f"open the {obj_strs[0]}"
        elif skill == 'SwitchOn':
            return f"switch on the {obj_strs[0]}"
        elif skill == 'Wash':
            return f"wash the {obj_strs[0]}"
        elif skill == 'Break':
            return f"break the {obj_strs[0]}"
        elif skill == 'Slice':
            return f"slice the {obj_strs[0]}"
        elif skill == 'PutOn':
            return f"put {obj_strs[0]} on {obj_strs[1]}"
        elif skill == 'PutIn':
            return f"put {obj_strs[0]} in {obj_strs[1]}"
        else:
            return f"unknown skill: {skill}"

    def get_subtask_final_state(self, subtask: Dict) -> List[Dict]:
        skill = subtask['skill']
        objs = subtask['objects']
        results = []

        if skill == 'Open':
            results.append({"name": objs[0], "contains": [], "state": "OPENED"})
        elif skill == 'Close':
            results.append({"name": objs[0], "contains": [], "state": "CLOSED"})
        elif skill == 'SwitchOn':
            results.append({"name": objs[0], "contains": [], "state": "ON"})
        elif skill == 'SwitchOff':
            results.append({"name": objs[0], "contains": [], "state": "OFF"})
        elif skill == 'Wash':
            results.append({"name": objs[0], "contains": [], "state": "CLEANED"})
        elif skill == 'Break':
            results.append({"name": objs[0], "contains": [], "state": "BROKEN"})
        elif skill == 'Slice':
            results.append({"name": objs[0], "contains": [], "state": "SLICED"})
        elif skill == 'PutOn':
            results.append({"name": objs[1], "contains": [objs[0]], "state": None})
        elif skill == 'PutIn':
            results.append({"name": objs[1], "contains": [objs[0]], "state": "CLOSED"})

        return results

    def get_task_final_state(self, subtasks: List[Dict]) -> List[Dict]:
        name_to_obj = {}

        for subtask in subtasks:
            partial_states = self.get_subtask_final_state(subtask)
            for ps in partial_states:
                name = ps["name"]
                if name not in name_to_obj:
                    name_to_obj[name] = {"contains": [], "state": None}

                if ps["contains"]:
                    name_to_obj[name]["contains"].extend(ps["contains"])

                if ps["state"] is not None:
                    name_to_obj[name]["state"] = ps["state"]

        return [{"name": name, **data} for name, data in name_to_obj.items()]

    def _robot_can_complete_subtask(self, robot: Dict, subtask: Dict, obj_mass_map: Dict[str, float]) -> bool:
        required_skills = SKILL_TO_ROBOT_SKILLS.get(subtask['skill'], [])
        if not all(s in robot['skills'] for s in required_skills):
            return False

        if 'PickupObject' in required_skills:
            if subtask['skill'] == 'Slice':
                obj_name = 'Knife'
            else:
                obj_name = subtask['objects'][0]
            obj_mass = self._get_object_mass(obj_name, obj_mass_map)
            if obj_mass > robot['mass_capacity']:
                return False

        return True

    def get_all_objects_from_cache(self) -> List[str]:
        cache_dir = self.config.ai2thor_objects_cache_dir
        if not cache_dir.exists():
            return []

        results = []
        for cache_file in cache_dir.glob("FloorPlan*.json"):
            with open(cache_file, "r", encoding="utf-8") as f:
                objects = json.load(f)
                if isinstance(objects, list):
                    results.extend(objects)
        
        return list(set([result["name"] for result in results]))

    def create_singe_task_back(self, floor_plan: int, robot_idxs: List[int], complexity: int = 0) -> Union[str, List[any]]:
        task_prompt = ""
        with open("resources/prompt_generate_task.txt", 'r', encoding='utf-8') as f:
            task_prompt = f.read()
        
        final_state_prompt = ""
        with open("resources/prompt_generate_final_state.txt", 'r', encoding='utf-8') as f:
            final_state_prompt = f.read()
        
        objects = self.get_objects(floor_plan)
        robots = []
        for idx, robot_idx in enumerate(robot_idxs):
            robot = self.get_robot(robot_idx)
            robot["name"] = f"robot{idx + 1}"
            robots.append(robot)
       
        task_prompt = task_prompt.replace("CurrentObjects", f"{objects}")
        task_prompt = task_prompt.replace("CurrentRobots", f"{robots}")

        _, task = self.llm.query_model(task_prompt, self.model)

        final_state_prompt = final_state_prompt.replace("CurrentObjects", f"{objects}")
        final_state_prompt = final_state_prompt.replace("CurrentTask", "task")
        _, final_state = self.llm.query_model(final_state_prompt, self.model)

        final_state_lines = final_state.strip().split('\n')
        parsed_final_state = []
        for line in final_state_lines:
            line = line.strip()
            if not line:
                continue
            parsed_final_state.append(json.loads(line))

        return task, parsed_final_state

    def create_singe_task(self, floor_plan: int, created_set: set, complexity: int = 0) -> Tuple[List[Dict], List[str]]:
        MAX_TASK_ATTEMPTS = 3
        MAX_ROBOTS_ATTEMPTS = 3
        skill_sets = _build_object_skill_sets(floor_plan)

        for _ in range(MAX_TASK_ATTEMPTS):
            objects = self.get_objects_with_mass(floor_plan)
            obj_mass_map = {o['name']: o['mass'] for o in objects}

            num = 1
            if complexity == 0:
                num = random.choices([1, 2], weights=[1, 2], k=1)[0]
            elif complexity == 1:
                num = random.choices([3, 4, 5], weights=[3, 3, 1], k=1)[0]

            subtasks = []
            while json.dumps(subtasks, sort_keys=True) in created_set:
                subtasks = self.generate_task([o['name'] for o in objects], num, skill_sets)

            for _ in range(MAX_ROBOTS_ATTEMPTS):
                num_robots = random.randint(2, 4)
                robot_indices = random.sample(range(1, len(robots) + 1), num_robots)
                selected_robots = [self.get_robot(idx) for idx in robot_indices]

                assigned_robots = []
                valid = True
                for subtask in subtasks:
                    assigned = None
                    for robot in selected_robots:
                        if self._robot_can_complete_subtask(robot, subtask, obj_mass_map):
                            assigned = robot['name']
                            break
                    if assigned is None:
                        valid = False
                        break
                    assigned_robots.append(assigned)

                if valid:
                    return subtasks, assigned_robots, selected_robots

        raise RuntimeError("Could not generate task with assignable robots")

    def check_and_tran2nl(self, subtasks: List[any]):
        prompt = """# Determine if the following subtasks are logically valid. Only output "No" if they are extremely unreasonable or contradictory.
- A single subtask is considered logically valid unless it is physically impossible or nonsensical.
- When multiple subtasks are present, they are logically valid as long as they can be executed in some sensible order. Minor inefficiencies or non-ideal sequencing do not make them invalid.

# If they are logically valid, describe them together in a complete natural language sentence. If they are not logically valid (i.e., very unreasonable), output "No".

# Example1

# Subtasks
wash the mug
put mug in cabinet

# Output
wash the mug, then put it in the cabinet.

# Example2

# Subtasks
        
put vase on countertop
break vase

# Output
No

# Example3

# Subtasks
        
break the plate
wash the fork
wash the butterknife

# Output
break the plate, then wash the fork and the butterknife.

# Output
wash the mug, then put it in the cabinet.

# Example4

# Subtasks
        
put sink on saltshaker
put ladle on sinkbasin 

# Output
put sink on saltshaker, then put ladle on sinkbasin 

# CurrentScene
# Subtasks"""

        for subtask in subtasks:
            prompt += f"\n{self.subtask_to_str(subtask)}"

        prompt+="\n# Output"

        _, txt = self.llm.query_model(prompt, self.model)

        task_nl = None
        lines = txt.splitlines()
        for i in reversed(range(len(lines))):
            line = lines[i].strip()
            if line != "":
                task_nl = line
                break

        if not task_nl or task_nl.strip().lower().startswith("no"):
            return None

        return task_nl

    def check_subtasks(self, subtasks):
        # 包含重复的 subtask
        if len(subtasks) > len(set([json.dumps(subtask, sort_keys=True) for subtask in subtasks])):
            return False
        
        # 将同一件东西搬来搬去
        mving_objects = []
        for subtask in subtasks:
            if subtask["skill"] == "PutIn" or subtask["skill"] == "PutOn":
                if subtask["objects"][0] in mving_objects:
                    return False
                mving_objects.append(subtask["objects"][0])

        # 先 Break 再 Wash 同一件东西
        broken_objects = []
        for subtask in subtasks:
            if subtask["skill"] == "Break":
                broken_objects.append(subtask["objects"][0])

            if subtask["skill"] == "Wash" and subtask["objects"][0] in broken_objects:
                return False
        
        # 将 PutIn / PutOn 再 Wash 同一件东西
        mving_objects = []
        for subtask in subtasks:
            if subtask["skill"] == "PutIn" or subtask["skill"] == "PutOn":
                mving_objects.append(subtask["objects"][0])

            if subtask["skill"] == "Wash" and subtask["objects"][0] in mving_objects:
                return False

        # 先打开容器 再放入物品
        opened_containers = []
        for subtask in subtasks:
            if subtask["skill"] == "Open":
                opened_containers.append(subtask["objects"][0])

            if subtask["skill"] == "PutIn" and subtask["objects"][1] in opened_containers:
                return False

        # 先放入物品 再打开容器
        containers = []
        for subtask in subtasks:
            if subtask["skill"] == "PutIn":
                containers.append(subtask["objects"][1])
            if subtask["skill"] == "Open" and subtask["objects"][0] in containers:
                return False
            
        return True

    def create_tasks(self, foor_plan: int, count: int, complexity: int = 0) -> List[Tuple[List[Dict], List[str]]]:
        """
        Generate multiple unique tasks.

        Args:
            foor_plan: floor plan number
            count: number of tasks to generate
            complexity: task complexity level

        Returns:
            List of (subtasks, robot_names) tuples (may be fewer than count if duplicates exhausted)
        """

        CREATED_FILE = Path("data/final_test_new_created_subtasks.jsonl")
        CREATED_FILE.parent.mkdir(parents=True, exist_ok=True)

        created_set = set()
        created_set.add(json.dumps([], sort_keys=True))
        if CREATED_FILE.exists():
            with open(CREATED_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        created_set.add(line)

        task_folder = f"data/final_test_new_0530_{complexity}"
        task_folder_path = Path(task_folder)
        task_folder_path.mkdir(parents=True, exist_ok=True)
        TASK_FILE = task_folder_path.joinpath(f"FloorPlan{foor_plan}.jsonl")

        MAX_RETRIES = 30

        for _ in range(count):
            task_found = False

            for _ in range(MAX_RETRIES):
                try:
                    subtasks, assigned_robots, selected_robots = self.create_singe_task(foor_plan, created_set, complexity)
                    subtasks_key = json.dumps(subtasks, sort_keys=True)
                    if subtasks_key in created_set:
                        continue

                    created_set.add(subtasks_key)
                    with open(CREATED_FILE, "a", encoding="utf-8") as f:
                        f.write(subtasks_key + "\n")

                    # 包含重复的 subtask
                    if not self.check_subtasks(subtasks):
                        continue

                    task_nl = self.check_and_tran2nl(subtasks)
                    if not task_nl:
                        continue
                    
                    object_states = self.get_task_final_state(subtasks)
                    result= {
                        "task":task_nl,
                        "robot list":[int(r["name"].replace("robot", "")) for r in selected_robots],
                        "object_states":object_states,
                        "trans":0,
                        "max_trans":0,
                        "subtasks": subtasks,
                        "assigned_robots": [int(r.replace("robot", "")) for r in assigned_robots]
                        }

                    with open(TASK_FILE, "a", encoding="utf-8") as f:
                        f.write(f"{json.dumps(result)}\n")
                    task_found = True
                    break
                except (ObjectPropertiesError, FileNotFoundError):
                    raise
                except Exception:
                    continue

            if not task_found:
                break

    
    # ---------- 技能匹配逻辑 ----------
    def get_applicable_skills(
        self,
        obj: str,
        all_objects: List[str],
        skill_sets: Dict[str, List[str]],
    ) -> List[Dict]:
        """
        返回当前对象可以参与的所有技能描述。
        每项为字典：
        - 单对象技能: {'skill': 技能名, 'type': 'single'}
        - 双对象技能: {'skill': 技能名, 'role': 'obj1'/'obj2', 'needed_set': 集合名称}
        """
        skills = []
        openable_objects = skill_sets["openable_objects"]
        switchable_objects = skill_sets["switchable_objects"]
        washable_objects = skill_sets["washable_objects"]
        breakable_objects = skill_sets["breakable_objects"]
        sliceable_objects = skill_sets["sliceable_objects"]
        pickupable_objects = skill_sets["pickupable_objects"]
        has_placing_surface_objects = skill_sets["has_placing_surface_objects"]
        openable_containers = skill_sets["openable_containers"]

        # 单对象技能
        if obj in openable_objects:
            skills.append({'skill': 'Open', 'type': 'single'})
        if obj in switchable_objects:
            skills.append({'skill': 'SwitchOn', 'type': 'single'})
        if obj in washable_objects and "Sink" in all_objects:
            skills.append({'skill': 'Wash', 'type': 'single'})
        if obj in breakable_objects:
            skills.append({'skill': 'Break', 'type': 'single'})
        if obj in sliceable_objects and "Knife" in all_objects:
            skills.append({'skill': 'Slice', 'type': 'single'})

        # 双对象技能 PutOn
        if obj in pickupable_objects:
            skills.append({
                'skill': 'PutOn',
                'type': 'double',
                'role': 'obj1',
                'needed_set': 'has_placing_surface_objects'
            })
        if obj in has_placing_surface_objects:
            skills.append({
                'skill': 'PutOn',
                'type': 'double',
                'role': 'obj2',
                'needed_set': 'pickupable_objects'
            })

        # 双对象技能 PutIn
        if obj in pickupable_objects:
            skills.append({
                'skill': 'PutIn',
                'type': 'double',
                'role': 'obj1',
                'needed_set': 'openable_containers'
            })
        if obj in openable_containers:
            skills.append({
                'skill': 'PutIn',
                'type': 'double',
                'role': 'obj2',
                'needed_set': 'pickupable_objects'
            })

        return skills


    def sample_second_object(
        self,
        all_objects: List[str],
        needed_set_name: str,
        skill_sets: Dict[str, List[str]],
        exclude: Optional[str] = None,
    ) -> str:
        """从指定集合中随机抽取一个对象，可排除某个对象。"""
        needed_set = skill_sets[needed_set_name]
        candidates = [o for o in all_objects if o != exclude and o in needed_set]
        if not candidates:
            raise ValueError(f"没有足够的候选对象")
        return random.choice(candidates)


    def generate_task(
        self,
        all_objects: List[str],
        num_subtasks: int,
        skill_sets: Dict[str, List[str]],
        keep_prob: float = 0.5,
        seed: Optional[int] = None,
    ) -> List[Dict]:
        """
        生成一系列子任务。

        参数:
            num_subtasks: 需要的子任务数量
            keep_prob: 每次生成后保留当前对象的概率（0~1）

        返回:
            list[dict]: 每个子任务包含 'skill' 和 'objects' 字段
        """

        if seed is not None:
            random.seed(seed)

        subtasks = []
        current_obj = None

        while len(subtasks) < num_subtasks:
            if current_obj is None or not self.get_applicable_skills(current_obj, all_objects, skill_sets):
                while True:
                    current_obj = random.choice(all_objects)
                    if self.get_applicable_skills(current_obj, all_objects, skill_sets):
                        break

            applicable = self.get_applicable_skills(current_obj, all_objects, skill_sets)

            filtered_applicable = [
                skill for skill in applicable
                if skill['skill'] not in ('PutOn', 'PutIn') or random.random() > 0.8
            ]

            # 当前 current_obj 无法抽取出合适的 skill，重新抽 current_obj 
            if not filtered_applicable:
                current_obj = None
                continue

            retry = 0
            while retry < 3:
                choice = random.choice(filtered_applicable)

                if choice['type'] == 'single':
                    # 单对象技能
                    subtask = {
                        'skill': choice['skill'],
                        'objects': [current_obj]
                    }
                else:
                    # 双对象技能，需要抽取第二个对象
                    needed_set = choice['needed_set']
                    try:
                        second_obj = self.sample_second_object(
                            all_objects,
                            needed_set,
                            skill_sets,
                            exclude=current_obj,
                        )
                    except ValueError:
                        # 无可选对象，换一个技能重试（简单从 applicable 中另选）
                        retry+=1
                        continue

                    if choice['role'] == 'obj1':
                        objects = [current_obj, second_obj]
                    else:  # role == 'obj2'
                        objects = [second_obj, current_obj]

                    subtask = {
                        'skill': choice['skill'],
                        'objects': objects
                    }

                    # 超过 50 % current_obj 变成 second_obj
                    if random.random() > keep_prob:
                        current_obj = second_obj
                break

            # 当前 current_obj 无法抽取出合适的 skill，重新抽 current_obj 
            if retry == 3:
                current_obj = None
                continue

            subtasks.append(subtask)

            # 达到目标数量则终止
            if len(subtasks) == num_subtasks:
                break

            # 决定是否保留当前对象
            if random.random() > keep_prob:
                current_obj = None # 不保留 current_obj，下一轮将重新随机抽取
                

        return subtasks

if __name__ == "__main__":
    # seed = int(time.time())
    # random.seed(seed)
    # print(random.sample(range(1, 31), 5))
    # print(random.sample(range(201, 231), 5))
    # print(random.sample(range(301, 331), 5))
    # print(random.sample(range(401, 431), 5))

    # [8, 6, 14, 201, 211, 218, 306, 310, 322, 405, 412, 428, 16, 203, 212, 28, 309, 312, 404, 408, 425]
    data_engine = DataEngine()
    for base in [0, 200, 300, 400]:
        for floor_plan in range(1, 31):
            # data_engine.create_tasks(base + floor_plan, 5)
            data_engine.create_tasks(base + floor_plan, 30, 1)

   
