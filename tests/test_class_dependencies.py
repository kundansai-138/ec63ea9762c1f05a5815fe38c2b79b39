"""Class schema contracts and direct/derived-reader failure witnesses."""
import ast,sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from class_dependencies import PIN,boolean_schema,derived_schema_read
from test_selector import mapping,file_data,patch,TESTS
from selector import SelectionEngine

SOURCE="class Item:\n    arg_types = {'value': True, 'option': False}\n"
KEY="sqlglot/example.py::Item::arg_types"
def data():
    m=mapping({"sqlglot/example.py":file_data(SOURCE)},classes={"sqlglot/example.py::Item":[0]},collection_attributes=["arg_types"],attribute_readers={"arg_types":[0,1,2]})
    m["class_attribute_profile"]={"version":1,"commit":PIN,"tests":m["tests"],"incomplete":[],"metadata":{"complete":True,"background_thread":False},"attributes":{KEY:{"contract":"boolean-schema-v1","readers":[0,1],"unsafe_collection":False}}}
    return m
def select(m,after,source=SOURCE):
    return SelectionEngine(m,TESTS).select(patch('sqlglot/example.py',source,after))

class ClassDependencyTests(unittest.TestCase):
    def test_boolean_values_with_ordered_unique_keys(self):
        for value in ("{}","{'x':1}","{'x':True,'x':False}","{1:True}","{**other}"):
            self.assertIsNone(boolean_schema(ast.parse(value,mode='eval').body))
        self.assertEqual(boolean_schema(ast.parse("{'x':True,'y':False}",mode='eval').body),['x','y'])
    def test_direct_and_derived_readers_selected(self):
        self.assertEqual(select(data(),SOURCE.replace('False','True')),{0,1})
    def test_derived_set_has_actual_failure_witness(self):
        source="class Base:\n    def __init_subclass__(cls):\n        cls.required_args={k for k,v in cls.arg_types.items() if v}\nclass Item(Base):\n    arg_types={'value':True,'option':FLAG}\n"
        before={}; after={}; exec(source.replace('FLAG','False'),before); exec(source.replace('FLAG','True'),after)
        self.assertNotIn('option',before['Item'].required_args)
        self.assertIn('option',after['Item'].required_args)
    def test_nonboolean_key_and_shape_changes_keep_fallback(self):
        for after in (SOURCE.replace('False','1'),SOURCE.replace("'option'","'renamed'"),SOURCE.replace(", 'option': False",'')):
            self.assertEqual(select(data(),after),{0,1,2})
    def test_invalid_profile_keeps_fallback(self):
        variants=[]
        m=data(); del m['class_attribute_profile']; variants.append(m)
        for key,value in [('complete',False),('background_thread',True)]:
            m=data(); m['class_attribute_profile']['metadata'][key]=value; variants.append(m)
        m=data(); m['class_attribute_profile']['tests']=list(reversed(m['tests'])); variants.append(m)
        m=data(); m['class_attribute_profile']['commit']='other'; variants.append(m)
        for m in variants: self.assertEqual(select(m,SOURCE.replace('False','True')),{0,1,2})
    def test_unknown_collection_consumer_keeps_fallback(self):
        m=data(); m['class_attribute_profile']['attributes'][KEY]['unsafe_collection']=True
        self.assertEqual(select(m,SOURCE.replace('False','True')),{0,1,2})
    def test_incomplete_and_source_readers_retained(self):
        m=data(); m['class_attribute_profile']['incomplete']=[m['tests'][2]]
        self.assertEqual(select(m,SOURCE.replace('False','True')),{0,1,2})
        m=data(); m['files']['sqlglot/example.py']['reads']=[2]
        self.assertEqual(select(m,SOURCE.replace('False','True')),{0,1,2})
    def test_same_attribute_on_other_class_not_shared(self):
        source=SOURCE.replace('Item','Other'); m=data(); m['files']['sqlglot/example.py']['source']=source
        self.assertEqual(select(m,source.replace('False','True'),source),{0,1,2})
    def test_derived_set_contract_rejects_extra_work(self):
        good="cls.required_args={k for k,v in cls.arg_types.items() if v}"
        self.assertEqual(derived_schema_read(ast.parse(good).body[0])[1:3],('required_args','cls'))
        for bad in (good.replace('required_args','untraced_attribute'),good.replace('if v','if v and dangerous()'),good.replace('cls.arg_types','other.arg_types'),good.replace('{k for','{call(k) for')):
            self.assertIsNone(derived_schema_read(ast.parse(bad).body[0]))
if __name__=='__main__': unittest.main()
