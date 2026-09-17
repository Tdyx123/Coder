"""Pure capability rules shared by generation and execution."""
import math
from typing import Any, Dict, Optional
from special_task_skills import canonical_skill_key


def normalize_skill_name(value: str) -> str:
    key = canonical_skill_key(value)
    return 'breakegg' if key == 'prepareegg' else key


def finite_nonnegative_number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def capability_failure(robot, action_type: str, *, object_name=None, object_mass=None) -> Optional[Dict[str, Any]]:
    required = normalize_skill_name(action_type)
    if required in {'wait', 'waitonetick', 'waituntil', 'pass', 'done'}:
        return None
    details = {}
    def failure(reason, message):
        return {'reason': reason, 'message': message, 'details': details}
    skills = robot.get('skills')
    if not isinstance(skills, list) or any(not isinstance(s, str) or not s.strip() for s in skills):
        return failure('validation_data_missing', 'Invalid or missing robot skills.')
    if required not in {normalize_skill_name(skill) for skill in skills}:
        details.update(required_skill='BreakEgg' if required == 'breakegg' else action_type,
                       available_skills=skills)
        return failure('missing_skill', f'Robot does not have the skill required by {action_type}.')
    if required != 'pickupobject':
        return None
    capacity = finite_nonnegative_number(robot.get('mass_capacity'))
    if not isinstance(object_name, str) or not object_name or capacity is None:
        return failure('validation_data_missing', 'Invalid PickupObject argument or mass_capacity.')
    details.update(object=object_name, mass_capacity=capacity)
    mass = finite_nonnegative_number(object_mass)
    if mass is None:
        return failure('validation_data_missing', f'Cannot determine finite non-negative mass for {object_name!r}.')
    details['mass'] = mass
    if mass > capacity:
        return failure('mass_exceeded', f'Robot cannot pick up {object_name}: mass {mass} exceeds capacity {capacity}.')
    return None
