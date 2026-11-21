"""Independent executable witnesses for tokenizer provenance proof boundaries."""
import ast
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tokenizer_dependencies import membership_only_alias

try:
    import coverage, pytest
except ImportError:
    BUILDER_AVAILABLE = False
else:
    BUILDER_AVAILABLE = True


class MembershipNamespaceWitnesses(unittest.TestCase):
    def test_namespace_reflection_is_not_key_only(self):
        for expression in ("locals()['value']['x']", "vars()['value']['x']",
                           "eval(\"value['x']\")"):
            source = ("def derive(cls):\n    value = cls.NUMERIC_ESCAPES\n"
                      "    if 'x' in value: pass\n    return " + expression + "\n")
            namespace = {}
            exec(source, namespace)
            a = type('A', (), {'NUMERIC_ESCAPES': {'x': 16}})
            b = type('B', (), {'NUMERIC_ESCAPES': {'x': 8}})
            self.assertNotEqual(namespace['derive'](a), namespace['derive'](b))
            function = ast.parse(source).body[0]
            self.assertFalse(membership_only_alias(function, function.body[0]), expression)

    def test_frame_reflection_and_aliases_are_not_key_only(self):
        bodies = ("read = locals\n    return read()['value']['x']",
                  "return sys._getframe().f_locals['value']['x']",
                  "return inspect.currentframe().f_locals['value']['x']")
        for body in bodies:
            source = ("def derive(cls):\n    value = cls.NUMERIC_ESCAPES\n"
                      "    if 'x' in value: pass\n    " + body + "\n")
            function = ast.parse(source).body[0]
            self.assertFalse(membership_only_alias(function, function.body[0]), body)

    def test_actual_key_only_derivation_does_not_depend_on_values(self):
        source = ("def derive(cls):\n    value = cls.NUMERIC_ESCAPES\n"
                  "    if 'x' in value: return 'hex'\n"
                  "    if '0' in value: return 'octal'\n    return ''\n")
        namespace = {}
        exec(source, namespace)
        before = type('Before', (), {'NUMERIC_ESCAPES': {'x': (16, 2, 2, 255)}})
        after = type('After', (), {'NUMERIC_ESCAPES': {'x': (8, 1, 4, 65535)}})
        self.assertEqual(namespace['derive'](before), namespace['derive'](after))
        function = ast.parse(source).body[0]
        self.assertTrue(membership_only_alias(function, function.body[0]))

    def test_global_alias_can_escape_without_a_local_value_read(self):
        source = ("value = {}\ndef derive(cls):\n    global value\n"
                  "    value = cls.NUMERIC_ESCAPES\n    return 'x' in value\n"
                  "def observe(): return value['x']\n")
        namespace = {}
        exec(source, namespace)
        observed = []
        for value in (16, 8):
            candidate = type('Candidate', (), {'NUMERIC_ESCAPES': {'x': value}})
            self.assertTrue(namespace['derive'](candidate))
            observed.append(namespace['observe']())
        self.assertNotEqual(*observed)
        function = ast.parse(source).body[1]
        self.assertFalse(membership_only_alias(function, function.body[1]))

    def test_nonlocal_alias_can_escape_without_a_local_value_read(self):
        source = ("def make():\n    value = {}\n    def derive(cls):\n"
                  "        nonlocal value\n        value = cls.NUMERIC_ESCAPES\n"
                  "        return 'x' in value\n    def observe(): return value['x']\n"
                  "    return derive, observe\n")
        namespace = {}
        exec(source, namespace)
        derive, observe = namespace['make']()
        observed = []
        for value in (16, 8):
            candidate = type('Candidate', (), {'NUMERIC_ESCAPES': {'x': value}})
            self.assertTrue(derive(candidate))
            observed.append(observe())
        self.assertNotEqual(*observed)
        function = ast.parse(source).body[0].body[1]
        self.assertFalse(membership_only_alias(function, function.body[1]))


@unittest.skipUnless(BUILDER_AVAILABLE, 'requires map-builder dependencies')
class TokenizerProvenanceWitnesses(unittest.TestCase):
    def test_cached_inherited_reflective_and_untracked_core_dependencies(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            (source / 'sqlglot').mkdir()
            (source / 'tests').mkdir()
            (source / 'sqlglot/__init__.py').write_text('')
            (source / 'tests/__init__.py').write_text('')
            (source / 'sqlglot/tokenizer_core.py').write_text(textwrap.dedent('''
                class TokenizerCore:
                    __slots__ = ('numeric_escapes',)
                    def __init__(self, numeric_escapes):
                        self.numeric_escapes = numeric_escapes
            '''))
            (source / 'sqlglot/tokens.py').write_text(textwrap.dedent('''
                from sqlglot.tokenizer_core import TokenizerCore
                class Tokenizer:
                    NUMERIC_ESCAPES = {'x': (16, 2, 2, 255)}
                    def __init__(self):
                        self._core = self._init_core()
                    def _init_core(self):
                        return TokenizerCore(numeric_escapes=self.NUMERIC_ESCAPES)
            '''))
            config = source / 'sqlglot/config.py'
            config.write_text(textwrap.dedent('''
                from sqlglot.tokens import Tokenizer
                class A(Tokenizer):
                    NUMERIC_ESCAPES = {'x': (16, 2, 2, 255)}
                class B(Tokenizer):
                    NUMERIC_ESCAPES = {'x': (8, 1, 3, 255)}
                class Child(A): pass
                CACHED = Child()
                class Meta(type): pass
                class ChildMeta(A, metaclass=Meta): pass
                class MetaBase(Tokenizer, metaclass=Meta): pass
                class InheritedMeta(MetaBase):
                    NUMERIC_ESCAPES = {'x': (16, 2, 2, 255)}
                class LocalAlias(Tokenizer):
                    NUMERIC_ESCAPES = {'x': (16, 2, 2, 255)}
                    SAVED = NUMERIC_ESCAPES
            '''))
            (source / 'tests/test_reads.py').write_text(textwrap.dedent('''
                import copy
                import inspect
                import builtins
                from sqlglot.config import A, B, Child, ChildMeta, CACHED
                def test_cached(): assert CACHED._core.numeric_escapes['x'][0] == 16
                def test_inherited(): assert Child.NUMERIC_ESCAPES['x'][0] == 16
                def test_metaclass_inherited(): assert ChildMeta.NUMERIC_ESCAPES['x'][0] == 16
                def test_other(): assert B.NUMERIC_ESCAPES['x'][0] == 8
                def test_members(): assert dict(inspect.getmembers(A))['NUMERIC_ESCAPES']['x'][0] == 16
                def test_keyword_members(): assert dict(inspect.getmembers(object=A))['NUMERIC_ESCAPES']['x'][0] == 16
                def test_static_members(): assert dict(inspect.getmembers_static(A))['NUMERIC_ESCAPES']['x'][0] == 16
                def test_keyword_static_members(): assert dict(inspect.getmembers_static(object=A))['NUMERIC_ESCAPES']['x'][0] == 16
                def test_core_members(): assert dict(inspect.getmembers(CACHED._core))['numeric_escapes']['x'][0] == 16
                def test_static_attribute(): assert inspect.getattr_static(A, 'NUMERIC_ESCAPES')['x'][0] == 16
                def test_keyword_static_attribute(): assert inspect.getattr_static(obj=A, attr='NUMERIC_ESCAPES')['x'][0] == 16
                def test_members_alias():
                    reflect = inspect.getmembers
                    assert dict(reflect(A))['NUMERIC_ESCAPES']['x'][0] == 16
                def test_imported_members():
                    from inspect import getmembers as reflect
                    assert dict(reflect(A))['NUMERIC_ESCAPES']['x'][0] == 16
                def test_dynamic_members():
                    reflect = getattr(inspect, 'getmembers')
                    assert dict(reflect(A))['NUMERIC_ESCAPES']['x'][0] == 16
                def test_dynamic_builtin():
                    reflect = getattr(builtins, 'getattr')
                    assert reflect(A, 'NUMERIC_ESCAPES')['x'][0] == 16
                def test_copy():
                    copied = copy.copy(CACHED._core)
                    assert copied.numeric_escapes['x'][0] == 16
                def test_empty_vars():
                    untouched = 7
                    assert vars()['untouched'] == 7
            '''))
            runner = '''import sys,json,os
from pathlib import Path
from build_tokenizer_map import Recorder
source=Path(sys.argv[1]); sys.path.insert(0,str(source)); os.chdir(source)
r=Recorder(source,[]); result=r.record(['-q','--assert=plain','-p','no:cacheprovider','tests'])
print(json.dumps(r.artifact(result,0))); sys.exit(result)
'''
            environment = {**os.environ, 'PYTHONPATH': str(ROOT),
                           'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1', 'PYTHONDONTWRITEBYTECODE': '1'}

            def record():
                result = subprocess.run([sys.executable, '-B', '-c', runner, str(source)],
                                        capture_output=True, text=True, env=environment, timeout=40)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return json.loads(result.stdout.splitlines()[-1])

            profile = record()
            attributes, nodes = profile['attributes'], profile['tests']
            a = attributes['sqlglot/config.py::A::NUMERIC_ESCAPES']
            a_readers = {nodes[i].rsplit('::', 1)[-1] for i in a['readers']}
            incomplete = {node.rsplit('::', 1)[-1] for node in profile['incomplete']}
            self.assertFalse(a['unsafe_collection'])
            self.assertTrue({'test_cached', 'test_inherited', 'test_metaclass_inherited'} <= a_readers)
            self.assertNotIn('test_other', a_readers | incomplete)
            self.assertNotIn('test_empty_vars', incomplete)
            for name in ('test_members', 'test_static_members', 'test_core_members',
                         'test_static_attribute', 'test_members_alias', 'test_imported_members',
                         'test_keyword_members', 'test_keyword_static_members',
                         'test_keyword_static_attribute', 'test_dynamic_members', 'test_dynamic_builtin'):
                self.assertIn(name, a_readers | incomplete, name)
            self.assertIn('test_copy', incomplete)
            self.assertTrue(attributes['sqlglot/config.py::InheritedMeta::NUMERIC_ESCAPES']['unsafe_collection'])
            self.assertNotIn('sqlglot/config.py::LocalAlias::NUMERIC_ESCAPES', attributes)

            # Import-time reflection can materialize a value even when no test
            # enters a tokenizer. It must disable narrowing for that source.
            config.write_text(config.read_text() + "\nimport inspect\nCAPTURED=dict(inspect.getmembers(A))['NUMERIC_ESCAPES']['x'][0]\n")
            profile = record()
            self.assertTrue(profile['attributes']['sqlglot/config.py::A::NUMERIC_ESCAPES']['unsafe_collection'])


if __name__ == '__main__':
    unittest.main()
