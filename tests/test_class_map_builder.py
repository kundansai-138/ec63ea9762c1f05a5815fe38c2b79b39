"""Execute a fresh miniature baseline to check qualified dependency tracing."""
import json,os,subprocess,sys,tempfile,textwrap,unittest
from pathlib import Path

try:
    import coverage,pytest
except ImportError:
    AVAILABLE=False
else:
    AVAILABLE=True

@unittest.skipUnless(AVAILABLE,'requires map-builder dependencies')
class QualifiedBuilderTests(unittest.TestCase):
    def test_class_identity_inheritance_import_edges_and_reflection(self):
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp); (source/'sqlglot').mkdir(); (source/'tests').mkdir()
            (source/'tests/__init__.py').write_text('')
            (source/'sqlglot/__init__.py').write_text(textwrap.dedent('''
                class Base:
                    def __init_subclass__(cls):
                        cls.required_args={k for k,v in cls.arg_types.items() if v}
                class A(Base):
                    arg_types={'x':True,'y':False}
                class B(Base):
                    arg_types={'x':True,'y':False}
                class Child(A): pass
                class C(Base):
                    arg_types={'x':True,'y':False}
                CAPTURED=C.arg_types['x']
                class Meta(type): pass
                class Custom(metaclass=Meta): pass
                class InheritedMeta(Custom):
                    arg_types={'x':True,'y':False}
                class LocalAlias:
                    arg_types={'x':True,'y':False}
                    saved=arg_types
            '''))
            (source/'tests/test_reads.py').write_text(textwrap.dedent('''
                from sqlglot import A,B,Child,C,CAPTURED
                def test_a(): assert A.arg_types['x']
                def test_b(): assert 'x' in B.required_args
                def test_child(): assert 'x' in Child.required_args
                def test_reflection(): assert A.__dict__['arg_types']['x']
                def test_dynamic(): assert getattr(A,'arg_types')['x']
                def test_dynamic_dict(): assert getattr(A,'__dict__')['arg_types']['x']
                def test_alias():
                    lookup=getattr
                    assert lookup(A,'arg_types')['x']
                def test_import_alias():
                    from builtins import getattr as lookup
                    assert lookup(A,'arg_types')['x']
                def test_module_builtin():
                    import builtins
                    assert builtins.getattr(A,'arg_types')['x']
                def test_noarg_vars():
                    item=7
                    assert vars()['item']==7
            '''))
            runner="""import sys,json,os
from pathlib import Path
from build_class_map import Recorder
source=Path(sys.argv[1]); sys.path.insert(0,str(source)); os.chdir(source)
r=Recorder(source,[]); result=r.record(['-q','--assert=plain','-p','no:cacheprovider','tests'])
print(json.dumps(r.artifact(result,0))); sys.exit(result)
"""
            env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1]),'PYTEST_DISABLE_PLUGIN_AUTOLOAD':'1','PYTHONDONTWRITEBYTECODE':'1'}
            proc=subprocess.run([sys.executable,'-B','-c',runner,str(source)],capture_output=True,text=True,env=env,timeout=40)
            self.assertEqual(proc.returncode,0,proc.stdout+proc.stderr)
            profile=json.loads(proc.stdout.splitlines()[-1]); attrs=profile['attributes']; nodes=profile['tests']
            a=attrs['sqlglot/__init__.py::A::arg_types']; b=attrs['sqlglot/__init__.py::B::arg_types']
            names={nodes[i].rsplit('::',1)[-1] for i in a['readers']}
            self.assertEqual(names,{'test_a','test_child','test_reflection','test_dynamic','test_dynamic_dict'})
            self.assertEqual({n.rsplit('::',1)[-1] for n in profile['incomplete']},{'test_alias','test_import_alias','test_module_builtin'})
            self.assertEqual({nodes[i].rsplit('::',1)[-1] for i in b['readers']},{'test_b'})
            self.assertIn('sqlglot/__init__.py::Child::required_args',a['dependencies'])
            self.assertFalse(a['unsafe_collection'])
            self.assertTrue(attrs['sqlglot/__init__.py::C::arg_types']['unsafe_collection'])
            self.assertTrue(attrs['sqlglot/__init__.py::InheritedMeta::arg_types']['unsafe_collection'])
            self.assertNotIn('sqlglot/__init__.py::LocalAlias::arg_types',attrs)

            # Escaped collection-time reflection can feed arbitrary derived
            # values. It must invalidate narrowing, including unrelated classes.
            module=source/'sqlglot/__init__.py'
            module.write_text(module.read_text()+"\nLOOKUP=getattr\nHIDDEN=LOOKUP(A,'arg_types')['x']\n")
            proc=subprocess.run([sys.executable,'-B','-c',runner,str(source)],capture_output=True,text=True,env=env,timeout=40)
            self.assertEqual(proc.returncode,0,proc.stdout+proc.stderr)
            profile=json.loads(proc.stdout.splitlines()[-1])
            self.assertTrue(profile['attributes']['sqlglot/__init__.py::A::arg_types']['unsafe_collection'])
            self.assertEqual(set(profile['incomplete']),set(profile['tests']))

if __name__=='__main__': unittest.main()
