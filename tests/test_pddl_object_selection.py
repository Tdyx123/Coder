import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import pddlrun_llmseparate as v1
import pddlrun_llmseparate_v2 as v2

DOMAIN = '(define (domain test) (:predicates (is-openable ?o) (object-open ?o) (at-location ?o ?p)))'


class ObjectSelectionTest(unittest.TestCase):
    def context(self, module, objects, names, text=None):
        manager = module.TaskManager.__new__(module.TaskManager)
        with patch.object(manager, '_load_floor_ai2thor_metadata', return_value=objects), patch.object(manager, '_allaction_pddl_type_names', return_value={'object'}):
            args = {} if text is None else {'task_text': text}
            return manager._build_key_object_pddl_context([{'name': n} for n in names], DOMAIN, **args)

    def drawers(self):
        return [dict(objectType='Drawer', objectId=f'Drawer|{i}', openable=True, isOpen=i == 9) for i in range(1, 18)]

    def test_type_limit_and_stable_numbering(self):
        for module in (v1, v2):
            with self.subTest(module=module.__name__):
                ctx = self.context(module, self.drawers(), ['Drawer'])
                self.assertEqual([s['object'] for s in ctx['states']], ['Drawer_1', 'Drawer_2', 'Drawer_3'])
                self.assertEqual([b['count'] for b in ctx['object_id_bindings']], [17] * 3)

    def test_explicit_instances_and_id_are_additional_and_deduplicated(self):
        for module in (v1, v2):
            with self.subTest(module=module.__name__):
                ctx = self.context(module, self.drawers(), ['Drawer'], 'Open Drawer_9, Drawer 11, Drawer|17, Drawer_9')
                self.assertEqual([s['object'] for s in ctx['states']], ['Drawer_1', 'Drawer_2', 'Drawer_3', 'Drawer_9', 'Drawer_11', 'Drawer_17'])

    def test_parent_beyond_limit_has_facts_evidence_and_binding(self):
        objects = self.drawers() + [dict(objectType='Apple', objectId='Apple|1', parentReceptacles=['Drawer|9'])]
        for module in (v1, v2):
            with self.subTest(module=module.__name__):
                ctx = self.context(module, objects, ['Drawer', 'Apple'])
                states = {s['object']: s for s in ctx['states']}
                self.assertEqual(set(states), {'Drawer_1', 'Drawer_2', 'Drawer_3', 'Drawer_9', 'Apple'})
                self.assertIn('(object-open Drawer_9)', states['Drawer_9']['facts'])
                self.assertIn('(at-location Apple Drawer_9)', states['Apple']['facts'])
                binding = next(b for b in ctx['object_id_bindings'] if b['object'] == 'Drawer_9')
                self.assertEqual(binding['object_id'], 'Drawer|9')
                self.assertIn('parentReceptacle', binding['roles'])
                self.assertTrue(any('Drawer_9' in str(e) for e in ctx['evidence']))

    def test_types_count_independently_and_contexts_are_isolated(self):
        objects = self.drawers() + [dict(objectType='Apple', objectId=f'Apple|{i}') for i in range(2)]
        for module in (v1, v2):
            with self.subTest(module=module.__name__):
                ctx = self.context(module, objects, ['Drawer', 'Apple'], 'Drawer_9')
                self.assertEqual(len(ctx['states']), 6)
                other = self.context(module, objects, ['Drawer'], 'Open Drawer')
                self.assertEqual(len(other['states']), 3)

    def test_context_union_preserves_extras_without_changing_subtasks(self):
        from pddl_object_selection import merge_object_contexts
        for module in (v1, v2):
            with self.subTest(module=module.__name__):
                first = self.context(module, self.drawers(), ['Drawer'], 'Drawer_9')
                second = self.context(module, self.drawers(), ['Drawer'], 'Drawer_17')
                merged = merge_object_contexts([first, second])
                self.assertEqual({s['object'] for s in merged['states']},
                                 {'Drawer_1', 'Drawer_2', 'Drawer_3', 'Drawer_9', 'Drawer_17'})
                self.assertEqual(len(merged['object_id_bindings']), 5)
                self.assertEqual(len(first['states']), 4)
                self.assertEqual(len(second['states']), 4)

    def test_explicit_id_does_not_match_prefix_and_short_types_are_kept(self):
        for module in (v1, v2):
            with self.subTest(module=module.__name__):
                ctx = self.context(module, self.drawers(), [], 'Drawer|17')
                self.assertEqual([s['object'] for s in ctx['states']], ['Drawer_17'])
                ctx = self.context(module, self.drawers()[:2], ['Drawer'])
                self.assertEqual([s['object'] for s in ctx['states']], ['Drawer_1', 'Drawer_2'])

    def test_related_container_cycle_terminates_and_floor_is_excluded(self):
        objects = [dict(objectType='Apple', objectId='Apple|1', parentReceptacles=['Box|1']), dict(objectType='Box', objectId='Box|1', parentReceptacles=['Apple|1'], openable=True), dict(objectType='Mug', objectId='Mug|1', parentReceptacles=['Floor|1']), dict(objectType='Floor', objectId='Floor|1')]
        for module in (v1, v2):
            with self.subTest(module=module.__name__):
                ctx = self.context(module, objects, ['Apple', 'Mug'])
                self.assertEqual({s['object'] for s in ctx['states']}, {'Apple', 'Box', 'Mug'})


if __name__ == '__main__':
    unittest.main()
