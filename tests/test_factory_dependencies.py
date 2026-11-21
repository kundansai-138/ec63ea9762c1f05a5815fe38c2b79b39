"""Synthetic closure-factory proofs; no challenge patches or answer labels."""
import ast
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from factory_dependencies import factory_intervals


FACTORY = '''def make(*args, **kwargs):
    def apply(receiver):
        return receiver.apply(*args, **kwargs)
    return apply
'''
CALLER = '''from pkg.factories import make
class Owner:
    TABLE = {"key": make("item", enabled=True)}
'''


class FactoryDependencyTests(unittest.TestCase):
    def proof(self, before=CALLER, after=None, factory=FACTORY, collection=(1, 2, 4), persistent=()):
        if after is None:
            after = before.replace('enabled=True', 'enabled=False')
        files = {
            'pkg/__init__.py': {'source': ''},
            'pkg/caller.py': {'source': before},
            'pkg/factories.py': {'source': factory, 'collection_lines': list(collection),
                                 'persistent_functions': list(persistent), 'lines': {'3': [0]}},
        }
        left = next(n for n in ast.walk(ast.parse(before)) if isinstance(n, ast.Assign) and isinstance(n.value, ast.Dict))
        right = next(n for n in ast.walk(ast.parse(after)) if isinstance(n, ast.Assign) and isinstance(n.value, ast.Dict))
        return factory_intervals(left, right, 'pkg/caller.py', files)

    def test_literal_capture_retains_closure_body(self):
        self.assertEqual(self.proof(), [('pkg/factories.py', 3, 3)])

    def test_relative_import_and_alias(self):
        before = CALLER.replace('from pkg.factories import make', 'from .factories import make as factory').replace('make("', 'factory("')
        self.assertEqual(self.proof(before), [('pkg/factories.py', 3, 3)])

    def test_calling_closure_during_collection_rejects(self):
        self.assertIsNone(self.proof(collection=(1, 2, 3, 4)))

    def test_docstring_does_not_hide_collection_calls(self):
        source = FACTORY.replace('        return', '        """Forward the captured arguments."""\n        return')
        self.assertIsNone(self.proof(factory=source, collection=(1, 2, 4, 5)))

    def test_docstring_interval_keeps_executable_body(self):
        source = FACTORY.replace('        return', '        """Forward the captured arguments."""\n        return')
        self.assertEqual(self.proof(factory=source, collection=(1, 2, 5)), [('pkg/factories.py', 3, 4)])

    def test_known_persistent_effect_rejects(self):
        self.assertIsNone(self.proof(persistent=((1, 4),)))

    def test_factory_side_effect_rejects(self):
        self.assertIsNone(self.proof(factory=FACTORY.replace('    return apply', '    print(args)\n    return apply')))

    def test_nested_default_evaluation_rejects(self):
        self.assertIsNone(self.proof(factory=FACTORY.replace('receiver):', 'receiver=1 / args[0]):')))

    def test_nested_decorator_rejects(self):
        self.assertIsNone(self.proof(factory=FACTORY.replace('    def apply', '    @decorate\n    def apply')))

    def test_factory_decorator_rejects(self):
        self.assertIsNone(self.proof(factory='@decorate\n' + FACTORY))

    def test_eager_annotation_rejects(self):
        self.assertIsNone(self.proof(factory=FACTORY.replace('receiver):', 'receiver: args[0]):')))

    def test_postponed_annotation_is_not_evaluated(self):
        source = 'from __future__ import annotations\n' + FACTORY.replace('receiver):', 'receiver: args[0]):')
        self.assertEqual(self.proof(factory=source, collection=(2, 3, 5)), [('pkg/factories.py', 4, 4)])

    def test_changed_key_or_call_shape_rejects(self):
        self.assertIsNone(self.proof(after=CALLER.replace('"key"', '"new"').replace('enabled=True', 'enabled=False')))
        self.assertIsNone(self.proof(after=CALLER.replace('enabled=True', 'different=False')))
        self.assertIsNone(self.proof(after=CALLER.replace('enabled=True', 'enabled="no"')))

    def test_dynamic_argument_rejects(self):
        self.assertIsNone(self.proof(CALLER.replace('"item"', 'source()')))

    def test_mutable_captured_argument_rejects(self):
        self.assertIsNone(self.proof(CALLER.replace('"item"', '["item"]')))

    def test_captured_state_mutation_rejects(self):
        self.assertIsNone(self.proof(factory=FACTORY.replace('        return', '        kwargs.pop("enabled")\n        return')))
        self.assertIsNone(self.proof(factory=FACTORY.replace('receiver.apply(*args, **kwargs)', 'kwargs.pop("enabled")')))

    def test_starred_arguments_reject(self):
        self.assertIsNone(self.proof(CALLER.replace('"item"', '*items')))

    def test_class_shadowing_rejects(self):
        self.assertIsNone(self.proof(CALLER.replace('    TABLE', '    make = replacement\n    TABLE')))

    def test_class_construction_hooks_reject(self):
        self.assertIsNone(self.proof(CALLER.replace('class Owner:', '@inspect_closures\nclass Owner:')))
        self.assertIsNone(self.proof(CALLER.replace('class Owner:', 'class Owner(metaclass=Inspect):')))

    def test_inherited_construction_hooks_reject(self):
        for hook in ('__init_subclass__', '__new__'):
            before = CALLER.replace('class Owner:', 'class Base:\n    def ' + hook + '(cls):\n        pass\nclass Owner(Base):')
            self.assertIsNone(self.proof(before))
        self.assertIsNone(self.proof(CALLER.replace('class Owner:', 'class Base(metaclass=Inspect):\n    pass\nclass Owner(Base):')))
        self.assertIsNone(self.proof(CALLER.replace('class Owner:', '@inspect_closures\nclass Base:\n    pass\nclass Owner(Base):')))

    def test_plain_recursive_inheritance_is_supported(self):
        before = CALLER.replace('class Owner:', 'class Base(object):\n    pass\nclass Middle(Base):\n    pass\nclass Owner(Middle):')
        self.assertEqual(self.proof(before), [('pkg/factories.py', 3, 3)])

    def test_unknown_or_computed_base_rejects(self):
        self.assertIsNone(self.proof(CALLER.replace('class Owner:', 'class Owner(Unknown):')))
        self.assertIsNone(self.proof(CALLER.replace('class Owner:', 'class Owner(factory()):')))

    def test_imported_plain_bases_are_supported(self):
        source = FACTORY + '\nclass Base:\n    pass\n'
        direct = CALLER.replace('class Owner:', 'from pkg.factories import Base\nclass Owner(Base):')
        module = CALLER.replace('class Owner:', 'from pkg import factories\nclass Owner(factories.Base):')
        alias = CALLER.replace('class Owner:', 'import pkg.factories as defs\nclass Owner(defs.Base):')
        for before in (direct, module, alias):
            self.assertEqual(self.proof(before, factory=source), [('pkg/factories.py', 3, 3)])

    def test_imported_base_hooks_reject(self):
        source = FACTORY + '\nclass Base:\n    def __init_subclass__(cls):\n        pass\n'
        before = CALLER.replace('class Owner:', 'from pkg import factories\nclass Owner(factories.Base):')
        self.assertIsNone(self.proof(before, factory=source))

    def test_module_rebinding_rejects(self):
        self.assertIsNone(self.proof(CALLER.replace('class Owner:', 'make = replacement\nclass Owner:')))

    def test_same_line_closure_rejects(self):
        self.assertIsNone(self.proof(factory=FACTORY.replace('def apply(receiver):\n        return', 'def apply(receiver): return')))

    def test_generator_closure_rejects(self):
        self.assertIsNone(self.proof(factory=FACTORY.replace('return receiver.', 'yield receiver.')))

    def test_changed_unrelated_sibling_rejects(self):
        before = CALLER.replace('"key":', '"unchanged": 1, "key":')
        after = before.replace('enabled=True', 'enabled=False').replace('"unchanged": 1', '"unchanged": 2')
        self.assertIsNone(self.proof(before, after))

    def test_live_literal_capture_preserves_construction_and_changes_call(self):
        namespace = {}
        exec(FACTORY, namespace)
        class Receiver:
            def apply(self, item, enabled):
                return item, enabled
        before = namespace['make']('item', enabled=True)
        after = namespace['make']('item', enabled=False)
        self.assertIs(before.__code__, after.__code__)
        self.assertEqual(before(Receiver()), ('item', True))
        self.assertEqual(after(Receiver()), ('item', False))


if __name__ == '__main__':
    unittest.main()
