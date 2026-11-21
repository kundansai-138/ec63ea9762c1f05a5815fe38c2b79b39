"""Trace class-qualified schema dependencies on the unchanged pinned baseline.

Tracks ordinary and reflective Python reads, shared fixture contexts, inherited
attribute owners, and the proven arg_types -> required-set construction. Unknown
collection consumption disables the affected optimization. No patches or labels
are inputs. The full baseline suite must pass before an artifact is emitted.
"""
from __future__ import annotations
import argparse, ast, builtins, gzip, importlib.abc, importlib.machinery, json, os, platform, sys, threading, time
from collections import defaultdict
from pathlib import Path
import pytest
from build_map import DependencyRecorder, PINNED_COMMIT
from build_callable_map import verify_source
from class_dependencies import boolean_schema, derived_schema_read

READ = "_blast_schema_read_91ac"
CALL = "_blast_schema_call_91ac"
ESCAPE = "_blast_schema_escape_91ac"
FIELDS = {"arg_types", "required_args"}
REFLECT = {"__dict__"}
BUILTINS = {"getattr", "hasattr", "vars", "dir"}

class NoCoverage:
    def switch_context(self, context): pass

class Recorder(DependencyRecorder):
    def __init__(self, source, tracked):
        super().__init__(source, tracked)
        self.cov = NoCoverage()
        self.reads = defaultdict(set)
        self.edges = defaultdict(set)
        self.unsafe_collection = set()
        self.candidates = {}
        self.derived_sites = {}
        self.classes = {}
        self.background_thread = False
        self.opaque_contexts = set()

    def instrument_classes(self): pass

    def class_key(self, cls):
        if cls in self.classes: return self.classes[cls]
        try:
            if type(cls) is not type: return None
            module = sys.modules.get(type.__getattribute__(cls, "__module__"))
            path = self.relative(getattr(module, "__file__", ""))
            name = type.__getattribute__(cls, "__qualname__")
            key = path + "::" + name if path and path.startswith("sqlglot/") else None
        except (AttributeError, TypeError): key = None
        self.classes[cls] = key
        return key

    def owner(self, obj, name):
        cls = obj if isinstance(obj, type) else type(obj)
        if type(cls) is not type: return None
        for base in type.__getattribute__(cls, "__mro__"):
            if name in type.__getattribute__(base, "__dict__"):
                key = self.class_key(base)
                return key + "::" + name if key else None
        return None

    def observed(self, obj, name, site):
        if not self.active: return
        if threading.current_thread() is not threading.main_thread(): self.background_thread = True
        key = self.owner(obj, name)
        if key is None: return
        if self.context == "collection":
            derived = self.derived_sites.get(tuple(site))
            if derived and name == "arg_types" and isinstance(obj, type):
                target = self.class_key(obj)
                if target: self.edges[key].add(target + "::" + derived)
                else: self.unsafe_collection.add(key)
            else: self.unsafe_collection.add(key)
        else:
            self.reads[key].add(self.context)

    def read(self, obj, name, path, line, col):
        if name in FIELDS:
            self.observed(obj, name, (path,line,col))
        elif name in REFLECT:
            for field in FIELDS: self.observed(obj, field, (path,line,col))
        return getattr(obj,name)

    def call(self, function, path, line, col, *args, **kwargs):
        if (function is getattr or function is hasattr) and len(args) >= 2 and isinstance(args[1],str):
            if args[1] in FIELDS: self.observed(args[0],args[1],(path,line,col))
            elif args[1] in REFLECT:
                for name in FIELDS: self.observed(args[0],name,(path,line,col))
        elif (function is vars or function is dir) and args:
            for name in FIELDS: self.observed(args[0],name,(path,line,col))
        return function(*args, **kwargs)

    def escape(self, value, force=False):
        # A builtin passed around as a value may be called by code outside our
        # import hook. Keep that entire context, rather than lose its reads.
        if self.active and (force or any(value is f for f in (getattr,hasattr,vars,dir,builtins,builtins.__dict__))):
            self.opaque_contexts.add(self.context)
        return value

    def record(self, pytest_args):
        finder=Finder(self)
        if any(hasattr(builtins,n) for n in (READ,CALL,ESCAPE)): raise RuntimeError("observer collision")
        setattr(builtins,READ,self.read); setattr(builtins,CALL,self.call); setattr(builtins,ESCAPE,self.escape)
        sys.meta_path.insert(0,finder); sys.addaudithook(self.audit); self.active=True
        try: return int(pytest.main(pytest_args,plugins=[self]))
        finally:
            self.active=False; sys.meta_path.remove(finder)
            delattr(builtins,READ); delattr(builtins,CALL); delattr(builtins,ESCAPE)

    def artifact(self, status, elapsed):
        numbers={n:i for i,n in enumerate(self.nodes)}
        contexts={c:{numbers[n] for n in ns if n in numbers} for c,ns in self.context_tests.items()}
        incomplete=set(self.incomplete)
        for context in self.untraced_subprocess_contexts: incomplete.update(self.context_tests.get(context,self.nodes))
        for context in self.opaque_contexts: incomplete.update(self.context_tests.get(context,self.nodes))
        if self.background_thread: incomplete.update(self.nodes)
        attributes={}
        modules = {self.relative(getattr(module,"__file__","")):module for module in list(sys.modules.values()) if module is not None}
        for key, expected_keys in self.candidates.items():
            path, qualname, attribute = key.split("::")
            obj = modules.get(path)
            for part in qualname.split("."):
                obj = vars(obj).get(part) if obj is not None and (isinstance(obj,type) or hasattr(obj,"__dict__")) else None
            plain = type(obj) is type and self.class_key(obj) == path+"::"+qualname
            raw = type.__getattribute__(obj,"__dict__").get(attribute) if plain else None
            plain = plain and type(raw) is dict and list(raw) == expected_keys and all(type(v) is bool for v in raw.values())
            reachable={key}; pending=[key]
            while pending:
                for other in self.edges.get(pending.pop(),()):
                    if other not in reachable: reachable.add(other); pending.append(other)
            indices=set()
            for dependency in reachable:
                for context in self.reads.get(dependency,()): indices.update(contexts.get(context,set(numbers.values())))
            attributes[key]={"contract":"boolean-schema-v1","readers":sorted(indices),"dependencies":sorted(reachable),
                             "unsafe_collection":not plain or "collection" in self.opaque_contexts or bool(reachable & self.unsafe_collection)}
        return {"version":1,"commit":PINNED_COMMIT,"tests":self.nodes,"incomplete":sorted(incomplete),
                "attributes":attributes,"metadata":{"complete":status==0 and not self.collection_errors,
                "pytest_exit_code":status,"collection_errors":self.collection_errors,"outcomes":dict(self.outcomes),
                "python":platform.python_version(),"pytest":pytest.__version__,"background_thread":self.background_thread,
                "opaque_reflection_contexts":sorted(self.opaque_contexts),
                "elapsed_seconds":round(elapsed,3),"candidate_attributes":len(attributes),
                "scope_dependencies":"shared fixture setup and module teardown propagated",
                "worker_execution":"baseline process pools inlined with ForkingPickler transport",
                "instrumentation":"class-qualified Python attribute and builtin reflection reads; recognized derived schema sets"}}

class Transformer(ast.NodeTransformer):
    def __init__(self,recorder,path):
        self.r,self.path=recorder,path
        self.direct_calls=set()
        self.builtin_aliases=set()

    def inspect(self,tree):
        self.direct_calls={id(n.func) for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name)}
        self.builtin_aliases={a.asname or a.name for n in ast.walk(tree)
                             if isinstance(n,ast.ImportFrom) and n.module=="builtins"
                             for a in n.names if a.name in BUILTINS}
        def scan(node,qual="",in_function=False):
            if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)):
                if node.name == "__init_subclass__":
                    for statement in node.body:
                        derived=derived_schema_read(statement)
                        if derived:
                            read,target,_=derived
                            self.r.derived_sites[(self.path,read.lineno,read.col_offset)]=target
                in_function=True
            if isinstance(node,ast.ClassDef):
                qual=(qual+"." if qual else "")+node.name
                if not in_function and not node.decorator_list and not node.keywords:
                    for member in node.body:
                        if not isinstance(member,(ast.Assign,ast.AnnAssign)): continue
                        targets=member.targets if isinstance(member,ast.Assign) else [member.target]
                        if len(targets)!=1 or not isinstance(targets[0],ast.Name) or targets[0].id!="arg_types": continue
                        if boolean_schema(member.value) is None: continue
                        # Class-local references bypass attribute access tracing.
                        if any(isinstance(n,ast.Name) and isinstance(n.ctx,ast.Load) and n.id=="arg_types"
                               for other in node.body if other is not member for n in ast.walk(other)): continue
                        self.r.candidates[self.path+"::"+qual+"::arg_types"]=boolean_schema(member.value)
            for child in ast.iter_child_nodes(node): scan(child,qual,in_function)
        scan(tree)

    def visit_Attribute(self,node):
        self.generic_visit(node)
        if isinstance(node.ctx,ast.Load) and node.attr in FIELDS|REFLECT:
            return ast.copy_location(ast.Call(func=ast.Name(id=READ,ctx=ast.Load()),
                args=[node.value,ast.Constant(node.attr),ast.Constant(self.path),ast.Constant(node.lineno),ast.Constant(node.col_offset)],keywords=[]),node)
        if isinstance(node.ctx,ast.Load) and node.attr in BUILTINS|{"__getattribute__"}:
            return ast.copy_location(ast.Call(func=ast.Name(id=ESCAPE,ctx=ast.Load()),
                args=[node,ast.Constant(node.attr=="__getattribute__")],keywords=[]),node)
        return node

    def visit_Name(self,node):
        if (isinstance(node.ctx,ast.Load) and (node.id=="__builtins__" or node.id in self.builtin_aliases
                or node.id in BUILTINS and id(node) not in self.direct_calls)):
            return ast.copy_location(ast.Call(func=ast.Name(id=ESCAPE,ctx=ast.Load()),args=[node],keywords=[]),node)
        return node

    def visit_Call(self,node):
        self.generic_visit(node)
        if isinstance(node.func,ast.Name) and node.func.id in BUILTINS and node.args:
            return ast.copy_location(ast.Call(func=ast.Name(id=CALL,ctx=ast.Load()),args=[node.func,
                ast.Constant(self.path),ast.Constant(node.lineno),ast.Constant(node.col_offset),*node.args],keywords=node.keywords),node)
        return node

class Loader(importlib.machinery.SourceFileLoader):
    def __init__(self,name,path,recorder): super().__init__(name,path); self.r=recorder
    def get_code(self,fullname):
        path=self.get_filename(fullname); relative=self.r.relative(path)
        tree=ast.parse(self.get_data(path),filename=path)
        transformer=Transformer(self.r,relative); transformer.inspect(tree)
        tree=transformer.visit(tree); ast.fix_missing_locations(tree)
        return compile(tree,path,"exec",dont_inherit=True)

class Finder(importlib.abc.MetaPathFinder):
    def __init__(self,recorder): self.r=recorder
    def find_spec(self,fullname,path=None,target=None):
        if fullname not in {"sqlglot","tests"} and not fullname.startswith(("sqlglot.","tests.")): return None
        spec=importlib.machinery.PathFinder.find_spec(fullname,path)
        if spec and isinstance(spec.loader,importlib.machinery.SourceFileLoader) and self.r.relative(spec.origin):
            spec.loader=Loader(fullname,spec.origin,self.r); return spec
        return None

def main():
    p=argparse.ArgumentParser(description=__doc__)
    for flag in ("source","source-manifest","map","output"): p.add_argument("--"+flag,type=Path,required=True)
    args=p.parse_args(); source=args.source.resolve(); output=args.output.resolve()
    manifest=json.loads(args.source_manifest.read_text()); verify_source(source,manifest)
    mapping=json.load(gzip.open(args.map,"rt"))
    if mapping.get("commit")!=PINNED_COMMIT: raise ValueError("wrong baseline")
    os.environ.update(SKIP_INTEGRATION="0",PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",PYTHONDONTWRITEBYTECODE="1")
    os.environ["PATH"]=str(Path(sys.executable).parent)+os.pathsep+os.environ.get("PATH","")
    sys.path.insert(0,str(source)); os.chdir(source)
    if any(n=="sqlglot" or n.startswith("sqlglot.") for n in sys.modules): raise RuntimeError("baseline preimported")
    recorder=Recorder(source,sorted(manifest["files"])); started=time.monotonic()
    status=recorder.record(["-q","--assert=plain","--tb=short","-p","no:cacheprovider","tests"])
    profile=recorder.artifact(status,time.monotonic()-started)
    if status or recorder.collection_errors or recorder.nodes!=mapping["tests"]: raise RuntimeError("instrumented baseline did not pass in full")
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open("wb") as dest:
        with gzip.GzipFile(fileobj=dest,mode="wb",mtime=0) as stream: stream.write(json.dumps(profile,sort_keys=True,separators=(",",":")).encode())
    print(json.dumps(profile["metadata"]),flush=True)

if __name__=="__main__": main()
