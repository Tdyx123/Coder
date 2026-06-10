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


def canonical_skill_key(skill: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(skill).lower())


SPECIAL_TASK_SKILL_ALIASES = {
    canonical_skill_key(skill): skill
    for skill in SPECIAL_TASK_SKILLS
}

SPECIAL_TASK_SKILL_PROMPT_RULE = (
    "# - These task skills require the same-named robot skill in addition to any base action skills: "
    + ", ".join(SPECIAL_TASK_SKILLS)
    + "."
)
