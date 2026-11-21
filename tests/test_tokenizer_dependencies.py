"""Contracts, inheritance and cached core dependency witnesses."""
import ast, json, sys, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tokenizer_dependencies import PIN, CONTRACT, literal_shape, membership_only_alias, tokenizer_attribute_tests


def assignment(value, name="NUMERIC_ESCAPES"):
    return ast.parse(name + " = " + value).body[0]


class TokenizerContracts(unittest.TestCase):
    def setUp(self):
        self.before = assignment("{'x': (16, 2, 2, 255), 'u': (16, 4, 4, 65535)}")
        self.after = assignment("{'x': (8, 1, 3, 255), 'u': (16, 4, 6, 65535)}")
        self.key = "sqlglot/dialects/example.py::Example.Tokenizer::NUMERIC_ESCAPES"
        self.mapping = {"tests":["one","two","three"],"tokenizer_attribute_profile":{
            "version":1,"commit":PIN,"tests":["one","two","three"],"incomplete":[],
            "metadata":{"complete":True,"background_thread":False},"attributes":{self.key:{
                "contract":CONTRACT,"shape":literal_shape(self.before.value),"readers":[0,1],"unsafe_collection":False}}}}

    def select(self, after=None):
        return tokenizer_attribute_tests(self.before, after or self.after, "sqlglot/dialects/example.py", "Example.Tokenizer", self.mapping)

    def test_unchanged_shape_values_retain_all_readers(self):
        self.assertEqual(self.select(), {0,1})
        self.mapping = json.loads(json.dumps(self.mapping))
        self.assertEqual(self.select(), {0,1})

    def test_key_order_names_shape_and_type_changes_fall_back(self):
        for value in ("{}", "{'u': (16, 4, 4, 65535), 'x': (16, 2, 2, 255)}",
                      "{'z': (16, 2, 2, 255), 'u': (16, 4, 4, 65535)}",
                      "{'x': (16, 2, 2), 'u': (16, 4, 4, 65535)}",
                      "{'x': (True, 2, 2, 255), 'u': (16, 4, 4, 65535)}"):
            self.assertIsNone(self.select(assignment(value)))

    def test_unsupported_literals_fall_back(self):
        for value in ("{'x':1,'x':2}", "{**other}", "{1:2}", "{1,2}", "None", "1.5", "call()"):
            self.assertIsNone(literal_shape(ast.parse(value, mode="eval").body))

    def test_unknown_collection_consumers_fall_back(self):
        self.mapping["tokenizer_attribute_profile"]["attributes"][self.key]["unsafe_collection"] = True
        self.assertIsNone(self.select())

    def test_incomplete_contexts_are_always_selected(self):
        self.mapping["tokenizer_attribute_profile"]["incomplete"] = ["three"]
        self.assertEqual(self.select(), {0,1,2})

    def test_profile_binding_and_metadata_are_required(self):
        p = self.mapping["tokenizer_attribute_profile"]
        p["tests"] = list(reversed(p["tests"])); self.assertIsNone(self.select())
        p["tests"] = self.mapping["tests"]; p["commit"] = "wrong"; self.assertIsNone(self.select())
        p["commit"] = PIN; p["metadata"]["complete"] = False; self.assertIsNone(self.select())
        p["metadata"]["complete"] = True; p["metadata"]["background_thread"] = True; self.assertIsNone(self.select())

    def test_unmapped_class_falls_back(self):
        self.assertIsNone(tokenizer_attribute_tests(self.before,self.after,"sqlglot/dialects/example.py","Other.Tokenizer",self.mapping))

    def test_changed_annotation_falls_back(self):
        before = ast.parse("NUMERIC_ESCAPES: dict = {'x':(16,2,2,255)}").body[0]
        after = ast.parse("NUMERIC_ESCAPES: object = {'x':(16,2,2,255)}").body[0]
        self.assertIsNone(tokenizer_attribute_tests(before,after,"sqlglot/dialects/example.py","Example.Tokenizer",self.mapping))

    def test_key_membership_proof_rejects_value_reads_aliases_and_nested_uses(self):
        good = "def derive(cls):\n    value = cls.NUMERIC_ESCAPES\n    if 'x' in value: return 'hex'\n    if 'u' not in value: return ''\n"
        tree = ast.parse(good).body[0]
        self.assertTrue(membership_only_alias(tree, tree.body[0]))
        for extra in ("    result = value\n", "    value = {}\n", "    return value['x']\n", "    consume(value)\n", "    def nested(): return 'x' in value\n"):
            tree = ast.parse(good + extra).body[0]
            self.assertFalse(membership_only_alias(tree,tree.body[0]))

    def test_numeric_tuple_values_leave_import_escape_formats_invariant(self):
        def derive(value):
            return 'hex' if 'x' in value else 'octal' if '0' in value else 'unicode' if 'u' in value else ''
        self.assertEqual(derive(ast.literal_eval(self.before.value)), derive(ast.literal_eval(self.after.value)))


if __name__ == "__main__": unittest.main()
