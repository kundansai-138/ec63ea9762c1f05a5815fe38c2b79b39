"""Record tokenizer class -> core instance -> test provenance on the baseline.

Direct configuration transfers are discovered from the pinned AST and checked
against the core's plain slot assignments. Unknown collection consumption,
untraced core objects, reflection escapes and incomplete contexts fail closed.
No patched sources, sample changes, expected failures or test durations are read.
"""
from __future__ import annotations
import argparse, ast, builtins, gzip, hashlib, importlib.abc, importlib.machinery, inspect, json, os, platform, sys, threading, time, types
from collections import defaultdict
from pathlib import Path
import pytest
from build_map import DependencyRecorder, PINNED_COMMIT
from build_callable_map import verify_source
from tokenizer_dependencies import CONTRACT, literal_shape, membership_only_alias

READ = "_blast_tokenizer_read_721c"
CALL = "_blast_tokenizer_call_721c"
ESCAPE = "_blast_tokenizer_escape_721c"
CORE = "_blast_tokenizer_core_721c"
BUILTINS = {"getattr", "hasattr", "vars", "dir"}
REFLECT = {"__dict__"}
INSPECT = {"getmembers", "getmembers_static", "getattr_static"}
INSPECT_FUNCTIONS = tuple(getattr(inspect, name) for name in sorted(INSPECT))


class NoCoverage:
    def switch_context(self, context): pass


def core_transfers(source):
    """Only direct self.FIELD arguments copied unchanged into core slots qualify."""
    tokens = ast.parse((source / "sqlglot/tokens.py").read_text())
    core = ast.parse((source / "sqlglot/tokenizer_core.py").read_text())
    cls = next(n for n in tokens.body if isinstance(n, ast.ClassDef) and n.name == "Tokenizer")
    init = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "_init_core")
    if len(init.body) != 1 or not isinstance(init.body[0], ast.Return):
        raise ValueError("unrecognized tokenizer core factory")
    call = init.body[0].value
    if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name) or call.func.id != "TokenizerCore" or call.args:
        raise ValueError("unrecognized tokenizer core call")
    core_cls = next(n for n in core.body if isinstance(n, ast.ClassDef) and n.name == "TokenizerCore")
    core_init = next(n for n in core_cls.body if isinstance(n, ast.FunctionDef) and n.name == "__init__")
    parents = {id(child): node for node in ast.walk(core_init) for child in ast.iter_child_nodes(node)}
    transfers = {}
    sites = set()
    for keyword in call.keywords:
        value = keyword.value
        if not (keyword.arg and isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name)
                and value.value.id == "self" and value.attr.isupper()):
            continue
        reads = [n for n in ast.walk(core_init) if isinstance(n, ast.Name) and n.id == keyword.arg and isinstance(n.ctx, ast.Load)]
        if len(reads) != 1:
            continue
        assignment = parents[id(reads[0])]
        if (not isinstance(assignment, ast.Assign) or assignment.value is not reads[0] or len(assignment.targets) != 1
                or not isinstance(assignment.targets[0], ast.Attribute) or assignment.targets[0].attr != keyword.arg
                or not isinstance(assignment.targets[0].value, ast.Name) or assignment.targets[0].value.id != "self"):
            continue
        transfers[value.attr] = keyword.arg
        sites.add(("sqlglot/tokens.py", value.lineno, value.col_offset))
    if not transfers:
        raise ValueError("no proven core transfers")
    return transfers, sites, ("sqlglot/tokens.py", call.lineno, call.col_offset)


class Recorder(DependencyRecorder):
    def __init__(self, source, tracked):
        super().__init__(source, tracked)
        self.cov = NoCoverage()
        self.transfers, self.transfer_sites, self.core_site = core_transfers(source)
        self.core_fields = set(self.transfers.values())
        self.reads = defaultdict(set)
        self.unsafe_collection = set()
        self.candidates = {}
        self.key_only_sites = set()
        self.classes = {}
        self.cores = {}
        self.background_thread = False
        self.opaque_contexts = set()
        self.unknown_core_contexts = set()
        self.builder_hashes = {name:hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
            for name in ("build_tokenizer_map.py","tokenizer_dependencies.py","build_map.py","build_callable_map.py")}

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
        # Use raw MRO lookup even on a subclass with a custom metaclass. Its
        # inherited ordinary tokenizer owner must not disappear from the map.
        for base in type.__getattribute__(cls, "__mro__"):
            if name in type.__getattribute__(base, "__dict__"):
                key = self.class_key(base)
                return key + "::" + name if key else None
        return None

    def observe_key(self, key, site, transferable=False):
        if key is None: return
        if self.context == "collection":
            candidate = self.candidates.get(key)
            keys_proven = tuple(site) in self.key_only_sites and candidate is not None and candidate[0][0] == "Dict"
            if not transferable and not keys_proven:
                self.unsafe_collection.add(key)
        else:
            self.reads[key].add(self.context)

    def observed(self, obj, name, site):
        if not self.active: return
        if threading.current_thread() is not threading.main_thread(): self.background_thread = True
        if name in self.transfers:
            self.observe_key(self.owner(obj, name), site, tuple(site) in self.transfer_sites)
        if name in self.core_fields:
            record = self.cores.get(id(obj))
            if record is not None and record[0] is obj:
                self.observe_key(record[1].get(name), site)
            else:
                module = sys.modules.get("sqlglot.tokenizer_core")
                core_type = getattr(module, "TokenizerCore", None)
                if core_type is not None and isinstance(obj, core_type):
                    self.unknown_core_contexts.add(self.context)

    def read(self, obj, name, path, line, col):
        if name in REFLECT:
            for field in self.transfers.keys() | self.core_fields: self.observed(obj, field, (path,line,col))
        else: self.observed(obj, name, (path,line,col))
        return getattr(obj, name)

    def core(self, function, tokenizer, *args, **kwargs):
        instance = function(*args, **kwargs)
        module = sys.modules.get("sqlglot.tokenizer_core")
        if function is not getattr(module, "TokenizerCore", None) or type(instance) is not function:
            self.opaque_contexts.add(self.context)
            return instance
        provenance = {dest:self.owner(tokenizer, source) for source,dest in self.transfers.items()}
        # Retain objects to prevent Python object-id reuse from aliasing provenance.
        self.cores[id(instance)] = (instance, provenance)
        return instance

    def call(self, function, path, line, col, *args, **kwargs):
        if function is getattr or function is hasattr or function is inspect.getattr_static:
            target = args[0] if args else kwargs.get("obj")
            name = args[1] if len(args) >= 2 else kwargs.get("attr")
            if isinstance(name, str):
                if name in REFLECT:
                    for field in self.transfers.keys() | self.core_fields: self.observed(target, field, (path,line,col))
                else: self.observed(target, name, (path,line,col))
        elif function is vars or function is dir or function is inspect.getmembers or function is inspect.getmembers_static:
            target = args[0] if args else kwargs.get("object")
            for name in self.transfers.keys() | self.core_fields: self.observed(target, name, (path,line,col))
        # getattr(inspect, "getmembers") (and builtin equivalents) may pass the
        # returned reflector to uninstrumented code or store it as an alias.
        return self.escape(function(*args, **kwargs))

    def escape(self, value, force=False):
        if self.active and (force or any(value is f for f in (getattr,hasattr,vars,dir,builtins,builtins.__dict__,*INSPECT_FUNCTIONS))):
            self.opaque_contexts.add(self.context)
        return value

    def record(self, pytest_args):
        finder = Finder(self)
        callbacks = {READ:self.read, CALL:self.call, ESCAPE:self.escape, CORE:self.core}
        if any(hasattr(builtins, n) for n in callbacks): raise RuntimeError("observer collision")
        for name, callback in callbacks.items(): setattr(builtins, name, callback)
        sys.meta_path.insert(0, finder); sys.addaudithook(self.audit); self.active = True
        try: return int(pytest.main(pytest_args, plugins=[self]))
        finally:
            self.active = False; sys.meta_path.remove(finder)
            for name in callbacks: delattr(builtins, name)

    def artifact(self, status, elapsed):
        numbers = {n:i for i,n in enumerate(self.nodes)}
        contexts = {c:{numbers[n] for n in ns if n in numbers} for c,ns in self.context_tests.items()}
        incomplete = set(self.incomplete)
        for context in self.untraced_subprocess_contexts | self.opaque_contexts | self.unknown_core_contexts:
            incomplete.update(self.context_tests.get(context, self.nodes))
        if self.background_thread: incomplete.update(self.nodes)
        modules = {self.relative(getattr(m, "__file__", "")):m for m in list(sys.modules.values()) if m is not None}
        token_base = getattr(sys.modules.get("sqlglot.tokens"), "Tokenizer", None)
        core_cls = getattr(sys.modules.get("sqlglot.tokenizer_core"), "TokenizerCore", None)
        core_plain = type(core_cls) is type and "__setattr__" not in vars(core_cls) and "__new__" not in vars(core_cls)
        attributes = {}
        for key, (shape, expected) in self.candidates.items():
            path, qualname, attribute = key.split("::")
            obj = modules.get(path)
            for part in qualname.split("."):
                obj = vars(obj).get(part) if obj is not None and (isinstance(obj,type) or hasattr(obj,"__dict__")) else None
            plain = type(obj) is type and token_base is not None and issubclass(obj, token_base) and self.class_key(obj) == path + "::" + qualname
            raw = type.__getattribute__(obj, "__dict__").get(attribute) if plain else None
            slot = vars(core_cls).get(self.transfers[attribute]) if core_plain else None
            plain = plain and type(raw) is type(expected) and raw == expected and isinstance(slot, types.MemberDescriptorType)
            indices = set()
            for context in self.reads.get(key, ()): indices.update(contexts.get(context, set(numbers.values())))
            attributes[key] = {"contract":CONTRACT, "shape":shape, "readers":sorted(indices),
                "core_field":self.transfers[attribute], "unsafe_collection":not plain or key in self.unsafe_collection
                    or "collection" in self.opaque_contexts or "collection" in self.unknown_core_contexts}
        return {"version":1, "commit":PINNED_COMMIT, "tests":self.nodes, "incomplete":sorted(incomplete), "attributes":attributes,
            "metadata":{"complete":status == 0 and not self.collection_errors, "pytest_exit_code":status,
                "collection_errors":self.collection_errors, "outcomes":dict(self.outcomes), "python":platform.python_version(),
                "pytest":pytest.__version__, "background_thread":self.background_thread,
                "builder_sha256":self.builder_hashes,
                "opaque_reflection_contexts":sorted(self.opaque_contexts), "unknown_core_contexts":sorted(self.unknown_core_contexts),
                "elapsed_seconds":round(elapsed,3), "candidate_attributes":len(attributes), "retained_core_instances":len(self.cores),
                "scope_dependencies":"shared fixture setup and module teardown propagated",
                "worker_execution":"baseline process pools inlined with ForkingPickler transport",
                "instrumentation":"class-qualified direct fields and cached core instance provenance; unchanged dictionary key membership"}}


class Transformer(ast.NodeTransformer):
    def __init__(self, recorder, path):
        self.r, self.path = recorder, path
        self.direct_calls = set()
        self.builtin_aliases = set()
        self.inspect_aliases = set()
        self.safe_dict_reads = set()

    def inspect(self, tree):
        self.direct_calls = {id(n.func) for n in ast.walk(tree) if isinstance(n,ast.Call)}
        self.builtin_aliases = {a.asname or a.name for n in ast.walk(tree) if isinstance(n,ast.ImportFrom) and n.module == "builtins"
                               for a in n.names if a.name in BUILTINS}
        self.inspect_aliases = {a.asname or a.name for n in ast.walk(tree) if isinstance(n,ast.ImportFrom) and n.module == "inspect"
                               for a in n.names if a.name in INSPECT}
        # Reading only an unrelated key of a class dictionary cannot observe a
        # candidate value. Other class/core reflection remains conservative.
        for node in ast.walk(tree):
            if (isinstance(node,ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0],(ast.In,ast.NotIn))
                    and isinstance(node.left,ast.Constant) and type(node.left.value) is str
                    and node.left.value not in self.r.transfers and node.left.value not in self.r.core_fields
                    and isinstance(node.comparators[0],ast.Attribute) and node.comparators[0].attr == "__dict__"):
                self.safe_dict_reads.add(id(node.comparators[0]))
        def scan(node, qual="", in_function=False):
            if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)):
                for statement in ast.walk(node):
                    if membership_only_alias(node, statement):
                        read = statement.value
                        if read.attr in self.r.transfers:
                            self.r.key_only_sites.add((self.path,read.lineno,read.col_offset))
                in_function = True
            if isinstance(node,ast.ClassDef):
                qual = (qual + "." if qual else "") + node.name
                if not in_function and not node.decorator_list and not node.keywords:
                    for member in node.body:
                        if not isinstance(member,(ast.Assign,ast.AnnAssign)): continue
                        targets = member.targets if isinstance(member,ast.Assign) else [member.target]
                        if len(targets) != 1 or not isinstance(targets[0],ast.Name) or targets[0].id not in self.r.transfers: continue
                        name = targets[0].id; shape = literal_shape(member.value)
                        if shape is None: continue
                        if any(isinstance(n,ast.Name) and isinstance(n.ctx,ast.Load) and n.id == name
                               for other in node.body if other is not member for n in ast.walk(other)): continue
                        self.r.candidates[self.path + "::" + qual + "::" + name] = (shape,ast.literal_eval(member.value))
            for child in ast.iter_child_nodes(node): scan(child,qual,in_function)
        scan(tree)

    def visit_Attribute(self,node):
        safe_dict = id(node) in self.safe_dict_reads
        direct_call = id(node) in self.direct_calls
        self.generic_visit(node)
        if isinstance(node.ctx,ast.Load) and node.attr in self.r.transfers.keys() | self.r.core_fields | REFLECT and not safe_dict:
            return ast.copy_location(ast.Call(func=ast.Name(id=READ,ctx=ast.Load()), args=[node.value,ast.Constant(node.attr),
                ast.Constant(self.path),ast.Constant(node.lineno),ast.Constant(node.col_offset)],keywords=[]),node)
        if isinstance(node.ctx,ast.Load) and node.attr in BUILTINS | {"__getattribute__"}:
            return ast.copy_location(ast.Call(func=ast.Name(id=ESCAPE,ctx=ast.Load()),args=[node,ast.Constant(node.attr == "__getattribute__")],keywords=[]),node)
        if isinstance(node.ctx,ast.Load) and node.attr in INSPECT and not direct_call:
            return ast.copy_location(ast.Call(func=ast.Name(id=ESCAPE,ctx=ast.Load()),args=[node],keywords=[]),node)
        return node

    def visit_Name(self,node):
        if isinstance(node.ctx,ast.Load) and (node.id == "__builtins__" or node.id in self.builtin_aliases
                or node.id in (BUILTINS | self.inspect_aliases) and id(node) not in self.direct_calls):
            return ast.copy_location(ast.Call(func=ast.Name(id=ESCAPE,ctx=ast.Load()),args=[node],keywords=[]),node)
        return node

    def visit_Call(self,node):
        is_core = (self.path,node.lineno,node.col_offset) == self.r.core_site
        inspect_call = isinstance(node.func,ast.Attribute) and node.func.attr in INSPECT or isinstance(node.func,ast.Name) and node.func.id in self.inspect_aliases
        self.generic_visit(node)
        if is_core:
            return ast.copy_location(ast.Call(func=ast.Name(id=CORE,ctx=ast.Load()),args=[node.func,ast.Name(id="self",ctx=ast.Load()),*node.args],keywords=node.keywords),node)
        if inspect_call or isinstance(node.func,ast.Name) and node.func.id in BUILTINS and node.args:
            return ast.copy_location(ast.Call(func=ast.Name(id=CALL,ctx=ast.Load()),args=[node.func,ast.Constant(self.path),
                ast.Constant(node.lineno),ast.Constant(node.col_offset),*node.args],keywords=node.keywords),node)
        return node


class Loader(importlib.machinery.SourceFileLoader):
    def __init__(self,name,path,recorder): super().__init__(name,path); self.r = recorder
    def get_code(self,fullname):
        path = self.get_filename(fullname); relative = self.r.relative(path)
        tree = ast.parse(self.get_data(path),filename=path)
        transformer = Transformer(self.r,relative); transformer.inspect(tree)
        tree = transformer.visit(tree); ast.fix_missing_locations(tree)
        return compile(tree,path,"exec",dont_inherit=True)


class Finder(importlib.abc.MetaPathFinder):
    def __init__(self,recorder): self.r = recorder
    def find_spec(self,fullname,path=None,target=None):
        if fullname not in {"sqlglot","tests"} and not fullname.startswith(("sqlglot.","tests.")): return None
        spec = importlib.machinery.PathFinder.find_spec(fullname,path)
        if spec and isinstance(spec.loader,importlib.machinery.SourceFileLoader) and self.r.relative(spec.origin):
            spec.loader = Loader(fullname,spec.origin,self.r); return spec
        return None


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for flag in ("source","source-manifest","map","output"): p.add_argument("--" + flag,type=Path,required=True)
    args = p.parse_args(); source = args.source.resolve(); output = args.output.resolve()
    manifest = json.loads(args.source_manifest.read_text()); verify_source(source,manifest)
    mapping = json.load(gzip.open(args.map,"rt"))
    if mapping.get("commit") != PINNED_COMMIT: raise ValueError("wrong baseline")
    os.environ.update(SKIP_INTEGRATION="0",PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",PYTHONDONTWRITEBYTECODE="1")
    os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH","")
    sys.path.insert(0,str(source)); os.chdir(source)
    if any(n == "sqlglot" or n.startswith("sqlglot.") for n in sys.modules): raise RuntimeError("baseline preimported")
    recorder = Recorder(source,sorted(manifest["files"])); started = time.monotonic()
    status = recorder.record(["-q","--assert=plain","--tb=short","-p","no:cacheprovider","tests"])
    verify_source(source,manifest)
    profile = recorder.artifact(status,time.monotonic() - started)
    if status or recorder.collection_errors or recorder.nodes != mapping["tests"]: raise RuntimeError("instrumented baseline did not pass in full")
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open("wb") as dest:
        with gzip.GzipFile(fileobj=dest,mode="wb",mtime=0) as stream: stream.write(json.dumps(profile,sort_keys=True,separators=(",",":")).encode())
    print(json.dumps(profile["metadata"]),flush=True)


if __name__ == "__main__": main()
