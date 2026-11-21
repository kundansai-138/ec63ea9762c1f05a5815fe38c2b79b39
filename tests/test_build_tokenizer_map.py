"""Independent witnesses for cached-instance, inheritance and opaque reads."""
import ast, inspect, sys, types, unittest
from collections import defaultdict
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from build_tokenizer_map import Recorder
from tokenizer_dependencies import literal_shape


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.module = types.ModuleType("sqlglot.fake_tokenizer")
        self.module.__file__ = "/baseline/sqlglot/fake_tokenizer.py"
        self.base = type("Tokenizer", (), {"__module__":self.module.__name__, "NUMERIC_ESCAPES":{"x":(16,2,2,255)}, "FLAG":"abc"})
        self.child = type("Child", (self.base,), {"__module__":self.module.__name__})
        self.other = type("Other", (self.base,), {"__module__":self.module.__name__, "NUMERIC_ESCAPES":{"x":(8,1,3,255)}})
        self.core_module = types.ModuleType("sqlglot.tokenizer_core")
        class Core:
            def __init__(self, numeric_escapes): self.numeric_escapes = numeric_escapes
        self.core_type = Core
        self.core_module.TokenizerCore = Core
        self.patch = patch.dict(sys.modules, {self.module.__name__:self.module, "sqlglot.tokenizer_core":self.core_module})
        self.patch.start(); self.addCleanup(self.patch.stop)
        r = self.r = object.__new__(Recorder)
        r.prefix = "/baseline/"; r.source = Path("/baseline"); r.context = "collection"; r.active = True
        r.transfers = {"NUMERIC_ESCAPES":"numeric_escapes", "FLAG":"flag"}; r.core_fields = set(r.transfers.values())
        r.cores = {}; r.classes = {}; r.reads = defaultdict(set); r.unsafe_collection = set()
        r.key_only_sites = set(); r.transfer_sites = {("tokens.py",1,0)}; r.unknown_core_contexts = set()
        r.opaque_contexts = set(); r.background_thread = False
        self.key = "sqlglot/fake_tokenizer.py::Tokenizer::NUMERIC_ESCAPES"
        r.candidates = {self.key:(literal_shape(ast.parse("{'x':(16,2,2,255)}",mode="eval").body),self.base.NUMERIC_ESCAPES)}

    def test_inherited_owner_is_the_defining_class(self):
        self.assertEqual(self.r.owner(self.child(),"NUMERIC_ESCAPES"),self.key)
        self.assertEqual(self.r.owner(self.other(),"NUMERIC_ESCAPES"),"sqlglot/fake_tokenizer.py::Other::NUMERIC_ESCAPES")

    def test_custom_metaclass_descendant_keeps_ordinary_base_owner(self):
        class Meta(type): pass
        child = Meta("Custom", (self.base,), {})
        self.assertEqual(self.r.owner(child(),"NUMERIC_ESCAPES"),self.key)

    def test_collection_core_survives_into_a_different_test_context(self):
        tokenizer = self.child()
        value = self.r.read(tokenizer,"NUMERIC_ESCAPES","tokens.py",1,0)
        core = self.r.core(self.core_type, tokenizer, numeric_escapes=value)
        self.assertNotIn(self.key,self.r.unsafe_collection)
        self.r.context = "test_cached"
        self.assertIs(self.r.read(core,"numeric_escapes","core.py",5,0),value)
        self.assertEqual(self.r.reads[self.key],{"test_cached"})

    def test_unknown_collection_core_consumption_is_unsafe(self):
        core = self.r.core(self.core_type,self.child(),numeric_escapes=self.base.NUMERIC_ESCAPES)
        self.r.read(core,"numeric_escapes","unproven.py",5,0)
        self.assertIn(self.key,self.r.unsafe_collection)

    def test_untracked_or_copied_core_read_keeps_whole_context(self):
        core = self.core_type(numeric_escapes=self.base.NUMERIC_ESCAPES)
        self.r.context = "test_copied"
        self.r.read(core,"numeric_escapes","core.py",5,0)
        self.assertEqual(self.r.unknown_core_contexts,{"test_copied"})

    def test_reflection_keeps_cached_core_readers(self):
        core = self.r.core(self.core_type,self.child(),numeric_escapes=self.base.NUMERIC_ESCAPES)
        self.r.context = "test_reflection"
        self.r.call(vars,"test.py",1,0,core)
        self.assertEqual(self.r.reads[self.key],{"test_reflection"})

    def test_stdlib_reflection_keeps_class_and_cached_core_readers(self):
        core = self.r.core(self.core_type,self.child(),numeric_escapes=self.base.NUMERIC_ESCAPES)
        self.r.context = "test_inspect"
        self.r.call(inspect.getmembers,"test.py",1,0,self.child)
        self.r.call(inspect.getmembers_static,"test.py",2,0,core)
        self.r.call(inspect.getattr_static,"test.py",3,0,core,"numeric_escapes")
        self.assertEqual(self.r.reads[self.key],{"test_inspect"})

    def test_stdlib_reflection_alias_escape_keeps_whole_context(self):
        self.r.context = "test_inspect_alias"
        self.r.escape(inspect.getmembers)
        self.assertEqual(self.r.opaque_contexts,{"test_inspect_alias"})

    def test_membership_exemption_applies_only_to_dictionary_candidates(self):
        site = ("dialect.py",1,0); self.r.key_only_sites.add(site)
        self.r.read(self.child(),"NUMERIC_ESCAPES",*site)
        self.assertNotIn(self.key,self.r.unsafe_collection)
        key = "sqlglot/fake_tokenizer.py::Tokenizer::FLAG"
        self.r.candidates[key] = (["str"],"abc")
        self.r.read(self.child(),"FLAG",*site)
        self.assertIn(key,self.r.unsafe_collection)

    def test_builtin_alias_escape_keeps_whole_context(self):
        self.r.context = "test_alias"
        self.r.escape(getattr)
        self.assertEqual(self.r.opaque_contexts,{"test_alias"})


if __name__ == "__main__": unittest.main()
