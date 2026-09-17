"""Bound instance expansion without changing scene-wide object identities."""

from copy import deepcopy
import re

DEFAULT_INSTANCES_PER_TYPE = 3


def select_context_objects(manager, floor_objects, numbering, key_types, task_text):
    """Return scene-ordered selected metadata and the directly selected IDs."""
    by_id = {item['objectId']: item for item in floor_objects if item.get('objectId')}
    selected = set()
    counts = {}
    # Keep metadata without IDs distinct; normal scene objects are deduplicated by ID.
    def identity(item):
        return item.get('objectId') or id(item)

    for item in floor_objects:
        type_key = manager._object_match_key(item.get('objectType', ''))
        counts[type_key] = counts.get(type_key, 0) + 1
        entry = manager._object_numbering_entry_for_metadata(item, numbering)
        object_id = item.get('objectId')
        explicit_id = bool(object_id and re.search(
            r'(?<![\w|.+-])' + re.escape(object_id) + r'(?![\w|.+-])', task_text
        ))
        explicit_token = bool(entry['multiple'] and manager._text_contains_name(task_text, entry['object']))
        if (type_key in key_types and counts[type_key] <= DEFAULT_INSTANCES_PER_TYPE) or explicit_id or explicit_token:
            selected.add(identity(item))
    direct = set(selected)
    pending = [item for item in floor_objects if identity(item) in selected]
    for item in pending:
        parent_id = manager._first_parent_receptacle(item)
        parent = by_id.get(parent_id)
        if parent is None or manager._object_match_key(parent.get('objectType', '')) == 'floor':
            continue
        if identity(parent) not in selected:
            selected.add(identity(parent))
            pending.append(parent)
    result = []
    seen = set()
    for item in floor_objects:
        key = identity(item)
        if key in selected and key not in seen:
            result.append(item)
            seen.add(key)
    return result, direct


def merge_object_contexts(contexts):
    """Union subtask contexts, preserving facts, bindings, roles and evidence."""
    result = {'states': [], 'object_id_bindings': [], 'evidence': []}
    for field, identity in (('states', 'object'), ('object_id_bindings', 'object_id')):
        indexed = {}
        for context in contexts:
            for item in context.get(field, []):
                key = item[identity]
                if key not in indexed:
                    indexed[key] = deepcopy(item)
                else:
                    for name in ('facts', 'related_objects', 'roles'):
                        for value in item.get(name, []):
                            values = indexed[key].setdefault(name, [])
                            if value not in values:
                                values.append(deepcopy(value))
        result[field] = list(indexed.values())
    for context in contexts:
        for evidence in context.get('evidence', []):
            if evidence not in result['evidence']:
                result['evidence'].append(deepcopy(evidence))
    return result
