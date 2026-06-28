import re


SPECIAL_TASK_SKILLS = (
    "RunMicrowave",
    "RunCoffeeMachine",
    "RunToaster",
    "CookByStoveBurner",
    "HeatByStoveBurner",
    "FillWater",
    "ColdObject",
    "PrepareEgg",
)

SPECIAL_TASK_SKILL_SET = frozenset(SPECIAL_TASK_SKILLS)
SPECIAL_TASK_ROBOT_SKILL_OVERRIDES = {
    "PrepareEgg": "BreakEgg",
}


def robot_skill_for_special_task_skill(skill: str) -> str:
    return SPECIAL_TASK_ROBOT_SKILL_OVERRIDES.get(skill, skill)


def canonical_skill_key(skill: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(skill).lower())


SPECIAL_TASK_SKILL_ALIASES = {
    canonical_skill_key(skill): skill
    for skill in SPECIAL_TASK_SKILLS
}

SPECIAL_TASK_SKILL_PROMPT_RULE = (
    "# - These task skills require the corresponding robot skill in addition to any base action skills: "
    + ", ".join(
        f"{skill}->{robot_skill_for_special_task_skill(skill)}"
        if robot_skill_for_special_task_skill(skill) != skill
        else skill
        for skill in SPECIAL_TASK_SKILLS
    )
    + "."
)
