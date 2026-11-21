"""Check structural comparisons independently against Python's AST dump."""
import ast
import copy
import itertools
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ast_equal import ast_equal
from dependency_safety import unsafe_functions


class AstEqualityTests(unittest.TestCase):
    def test_comments_and_locations_do_not_change_syntax(self):
        first = ast.parse("def f(x):\n    return x + 1\n")
        second = ast.parse("# docs\n\ndef f(x):\n    # explanation\n    return x + 1 # more\n")
        self.assertTrue(ast_equal(first, second))

    def test_constant_types_and_signed_zero_remain_distinct(self):
        values = [None, False, True, 0, 1, 0.0, -0.0, 1.0, 1j, -0j,
                  "1", b"1", Ellipsis]
        for left, right in itertools.product(values, repeat=2):
            before, after = ast.Constant(value=left), ast.Constant(value=right)
            with self.subTest(left=repr(left), right=repr(right)):
                self.assertEqual(ast_equal(before, after), ast.dump(before) == ast.dump(after))

    def test_parsed_structures_match_dump_equality(self):
        examples = [
            "def f(x=1, *, y=None):\n    return x + y\n",
            "@classmethod\ndef f(cls, x: int) -> int:\n    return x\n",
            "async def f(x):\n    await x\n",
            "def f(x):\n    yield from x\n",
            "class C(Base):\n    x: int = 1\n",
            "x = [a + 1 for a in values if a]\n",
            "try:\n    call()\nexcept Exception as exc:\n    raise\nfinally:\n    clean()\n",
            "match x:\n    case {'y': y}:\n        print(y)\n",
            "from module import value as name\n",
            "x = lambda a: a + 1\n",
            "x = f'value: {value!r:10}'\n",
        ]
        trees = [ast.parse(source) for source in examples]
        trees += [ast.parse("# comment\n" + source) for source in examples]
        for before, after in itertools.product(trees, repeat=2):
            self.assertEqual(ast_equal(before, after), ast.dump(before) == ast.dump(after))

    def test_child_change_is_detected_without_mutating_tree(self):
        before = ast.parse("def f(x):\n    return x + 1\n")
        original = ast.dump(before, include_attributes=True)
        after = copy.deepcopy(before)
        after.body[0].body[0].value.right.value = 2
        self.assertFalse(ast_equal(before, after))
        self.assertEqual(ast.dump(before, include_attributes=True), original)

    def test_dependency_analysis_accepts_existing_tree_without_changes(self):
        source = "CACHE={}\ndef write():\n    CACHE['x']=1\n"
        tree = ast.parse(source)
        original = ast.dump(tree, include_attributes=True)
        self.assertEqual(unsafe_functions(source), unsafe_functions(tree))
        self.assertEqual(ast.dump(tree, include_attributes=True), original)


if __name__ == "__main__":
    unittest.main()
