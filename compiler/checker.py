"""Type checker / semantic analysis.

Walks the AST, resolves names and types, reports errors, and annotates nodes
for the code generator:

  every Expr        .ty        semantic Type
                    .coerce    (optional) Type to implicitly convert to
                               (array/list -> slice), applied by codegen
  Ident             .sym       VarInfo
  Field             .kind      'enum' | 'field' | 'ptr_field' | 'len' | 'ptr' | 'cap'
  Call              .kind      'fn' | 'method' | 'static' | 'builtin' | 'list_method' | 'str_method'
                    .fn        FnInfo (fn/method/static)
                    .self_mode 'addr' | 'ptr' | 'temp' (method)
  Index/SliceExpr   .kind      'slice' | 'list' | 'str' | 'array' | 'ptr'
  ForEach           .kind, .idx_var, .elem_var, .iter_lvalue
  ForRange          .var
  VarDecl           .var

Results consumed by codegen: structs, enums, functions, globals, needed
composite types (slices/arrays/lists), fmt_types, includes, links.
"""

from . import ast as A
from . import types as T
from .errors import CompileError, err_at

C_RESERVED = set("""
auto break case char const continue default do double else enum extern float for goto if inline int long
register restrict return short signed sizeof static struct switch typedef union unsigned void volatile while
_Bool _Complex _Imaginary bool true false NULL main malloc calloc realloc free printf fprintf sprintf snprintf
puts fputs fwrite fflush memcpy memmove memcmp memset strlen strcmp exit abort stdin stdout stderr errno
assert int8_t uint8_t int16_t uint16_t int32_t uint32_t int64_t uint64_t size_t FILE asm typeof
""".split())

BUILTINS = {"print", "println", "eprint", "eprintln", "assert", "panic", "exit", "free", "read_line",
            "parse_int", "parse_float", "time_ns", "read_file", "write_file", "args"}

LIST_METHODS = {"push", "pop", "clear", "remove", "insert", "reserve"}
STR_METHODS = {"find", "contains", "starts_with", "ends_with", "trim", "clone", "cstr", "split", "eq"}


BUILTIN_DOCS = {
    "print": "fn print(values...)\n\nPrint values separated by spaces (no newline).",
    "println": "fn println(values...)\n\nPrint values separated by spaces, then a newline.",
    "eprint": "fn eprint(values...)\n\nLike print, to stderr.",
    "eprintln": "fn eprintln(values...)\n\nLike println, to stderr.",
    "assert": "fn assert(cond: bool, msg: str = \"\")\n\nPanic with location if cond is false.",
    "panic": "fn panic(msg: str)\n\nPrint location + message to stderr, exit(101).",
    "exit": "fn exit(code: i32)",
    "free": "fn free(x: *T | []T | str | list[T])\n\nRelease heap memory.",
    "read_line": "fn read_line() -> str\n\nRead one line from stdin (no newline). `.ptr == null` at EOF.",
    "parse_int": "fn parse_int(s: str) -> i64",
    "parse_float": "fn parse_float(s: str) -> f64",
    "time_ns": "fn time_ns() -> i64\n\nMonotonic clock in nanoseconds.",
    "read_file": "fn read_file(path: str) -> str\n\nWhole file; `.ptr == null` on failure.",
    "write_file": "fn write_file(path: str, data: str) -> bool",
    "args": "fn args() -> []str\n\nCommand-line arguments.",
    "sizeof": "fn sizeof(T) -> i64",
    "new": "fn new(T, value?: T) -> *T\n\nZeroed (or initialized) heap allocation.",
    "make": "fn make([]T | list[T], n: i64)\n\nHeap slice of n zeroed elements / list with capacity n.",
}


class Ref:
    """A name occurrence in source, for editor tooling (hover / definition / references)."""
    __slots__ = ("file", "line", "col", "length", "hover", "defpos", "ty", "name")

    def __init__(self, file, line, col, length, hover, defpos, ty, name):
        self.file, self.line, self.col, self.length = file, line, col, length
        self.hover, self.defpos, self.ty, self.name = hover, defpos, ty, name


class VarInfo:
    def __init__(self, name, cname, ty, mutable, kind="local", const_value=None, node=None):
        self.node = node            # token/node of the declaring name (for tooling)
        self.used = False
        self.mutated = False
        self.has_init = True
        self.name = name
        self.cname = cname
        self.ty = ty
        self.mutable = mutable
        self.kind = kind            # local | global | const
        self.const_value = const_value


class FnInfo:
    def __init__(self, decl, name, cname, params, ret, owner=None):
        self.decl = decl
        self.name = name
        self.cname = cname
        self.params = params        # list of (name, Type)
        self.ret = ret
        self.owner = owner          # StructT for methods
        self.has_self = decl.has_self if decl else False
        self.variadic = decl.variadic if decl else False
        self.is_extern = decl.is_extern if decl else False
        self.locals = []            # VarInfo, filled while checking body


def is_literal(e):
    return isinstance(e, (A.IntLit, A.FloatLit, A.CharLit))


class Checker:
    def __init__(self, decls, lib_mode=False):
        self.decls = decls
        self.lib_mode = lib_mode    # True: no main() required (editor tooling on modules)
        self.refs = []
        self.warnings = []
        self.structs = {}
        self.enums = {}
        self.fns = {}               # top-level functions & externs
        self.fn_list = []           # all FnInfo incl. methods, in order
        self.globals = {}           # name -> VarInfo
        self.global_decls = []      # (VarInfo, VarDecl) in order
        self.needed = {}            # key -> composite Type (Slice/Array/ListT), in dependency order
        self.fmt_types = {}         # key -> Type needing a generated formatter
        self.includes = []
        self.links = []
        self.scopes = []
        self.cur_fn = None
        self.loop_depth = 0
        self.in_defer = False
        self.used_cnames = set()
        self.const_eval_stack = set()
        self.pending_consts = {}
        self.extern_names = set()
        self.main = None

    # ================================================================ tooling
    @staticmethod
    def defpos(n):
        t = getattr(n, "name_tok", None) or n
        if t is None or not getattr(t, "file", None):
            return None
        return (t.file, t.line, t.col)

    def ref(self, at, name, hover, defnode=None, ty=None):
        if at is None or not getattr(at, "file", None):
            return
        self.refs.append(Ref(at.file, at.line, at.col, len(name), hover,
                             self.defpos(defnode) if defnode is not None else None, ty, name))

    def warn(self, node, msg):
        if node is not None and getattr(node, "file", None):
            self.warnings.append(CompileError(msg, node.file, node.line, node.col))

    @staticmethod
    def fn_sig(f):
        ps = (["self"] if f.has_self else []) + ["%s: %s" % (n, t) for n, t in f.params]
        if f.variadic:
            ps.append("...")
        owner = f.owner.name + "." if f.owner else ""
        ret = "" if f.ret.is_void() else " -> %s" % f.ret
        return "%sfn %s%s(%s)%s" % ("extern " if f.is_extern else "", owner, f.name, ", ".join(ps), ret)

    @staticmethod
    def var_hover(v):
        if v.kind == "const":
            val = v.const_value
            if isinstance(val, bytes):
                val = '"%s"' % val.decode("utf-8", "replace")
            elif isinstance(val, bool):
                val = "true" if val else "false"
            return "const %s: %s = %s" % (v.name, v.ty, val)
        if v.name == "self":
            return "self: %s" % v.ty
        return "%s %s: %s" % ("var" if v.mutable else "let", v.name, v.ty)

    @staticmethod
    def type_hover(t):
        if isinstance(t, T.StructT):
            fields = "".join("\n    %s: %s" % (n, ft) for n, ft in t.fields.items())
            methods = "".join("\n// %s" % Checker.fn_sig(m) for m in t.methods.values())
            return "struct %s {%s\n}%s" % (t.name, fields, methods)
        if isinstance(t, T.EnumT):
            return "enum %s { %s }" % (t.name, ", ".join(t.variants))
        return str(t)

    def mark_mutated(self, e):
        while True:
            if isinstance(e, A.Ident):
                if getattr(e, "sym", None) is not None:
                    e.sym.mutated = True
                return
            if isinstance(e, A.Field) and getattr(e, "kind", None) == "field":
                e = e.obj
            elif isinstance(e, A.Index) and getattr(e, "kind", None) == "array":
                e = e.obj
            else:
                return

    # ================================================================ helpers
    def need(self, t):
        """Register a composite type so codegen emits its C definition."""
        if isinstance(t, (T.Slice, T.Array, T.ListT, T.Ptr)):
            self.need(t.elem)
            if not isinstance(t, T.Ptr) and t.key not in self.needed:
                self.needed[t.key] = t
        return t

    def need_fmt(self, t):
        if isinstance(t, (T.StructT, T.EnumT, T.Slice, T.Array, T.ListT)) and t.key not in self.fmt_types:
            self.fmt_types[t.key] = t
            if isinstance(t, T.StructT):
                for ft in t.fields.values():
                    self.need_fmt(ft)
            elif not isinstance(t, T.EnumT):
                self.need_fmt(t.elem)

    def resolve(self, te):
        if isinstance(te, A.TName):
            name = T.ALIASES.get(te.name, te.name)
            if name in T.PRIMS:
                return T.PRIMS[name]
            if name == "str":
                return T.STR
            if name in self.structs or name in self.enums:
                t = self.structs.get(name) or self.enums.get(name)
                self.ref(te, te.name, self.type_hover(t), t.decl)
                return t
            err_at(te, "unknown type '%s'" % te.name)
        if isinstance(te, A.TPtr):
            return self.need(T.Ptr(self.resolve(te.elem)))
        if isinstance(te, A.TSlice):
            return self.need(T.Slice(self.resolve_value_type(te.elem)))
        if isinstance(te, A.TList):
            return self.need(T.ListT(self.resolve_value_type(te.elem)))
        if isinstance(te, A.TArray):
            n = self.fold(te.size)
            if not isinstance(n, int) or isinstance(n, bool) or n <= 0:
                err_at(te, "array size must be a positive integer constant")
            return self.need(T.Array(self.resolve_value_type(te.elem), n))
        raise AssertionError(te)

    def resolve_value_type(self, te):
        t = self.resolve(te)
        if t.is_void():
            err_at(te, "'void' is not a value type")
        return t

    def cname_for(self, name):
        base = name
        if name in C_RESERVED or name.startswith("fl_") or name.startswith("__") or name in self.extern_names:
            base = name + "_"
        c = base
        k = 1
        while c in self.used_cnames:
            k += 1
            c = "%s_%d" % (base, k)
        self.used_cnames.add(c)
        return c

    def push_scope(self):
        self.scopes.append({})

    def pop_scope(self):
        self.scopes.pop()

    def declare_local(self, node, name, ty, mutable):
        """node: the token of the declared name."""
        if name == "_":
            v = VarInfo(name, self.cname_for("_unused"), ty, mutable, node=node)
        else:
            # shadowing in the same block is allowed (gets a new C name)
            v = VarInfo(name, self.cname_for(name), ty, mutable, node=node)
            self.scopes[-1][name] = v
            self.ref(node, name, self.var_hover(v), node, ty)
        self.cur_fn.locals.append(v)
        return v

    def lookup(self, name):
        for s in reversed(self.scopes):
            if name in s:
                return s[name]
        return self.globals.get(name)

    def type_name_in_scope(self, e):
        """If expr is an identifier naming a struct/enum (not shadowed by a variable), return it."""
        if isinstance(e, A.Ident) and self.lookup(e.name) is None:
            if e.name in self.structs:
                return self.structs[e.name]
            if e.name in self.enums:
                return self.enums[e.name]
        return None

    # ================================================================ constants
    def fold(self, e):
        """Constant-fold an expression; returns a Python value or None."""
        if isinstance(e, (A.IntLit, A.FloatLit, A.BoolLit)):
            return e.value
        if isinstance(e, A.CharLit):
            return e.value
        if isinstance(e, A.StrLit):
            return e.data
        if isinstance(e, A.Ident):
            v = self.globals.get(e.name)
            if v is not None and v.kind == "const":
                return v.const_value
            if e.name in self.pending_consts:
                return self.eval_const(e.name)
            return None
        if isinstance(e, A.Unary):
            v = self.fold(e.operand)
            if v is None:
                return None
            if e.op == "-" and isinstance(v, (int, float)) and not isinstance(v, bool):
                return -v
            if e.op == "~" and isinstance(v, int) and not isinstance(v, bool):
                return ~v
            if e.op == "!" and isinstance(v, bool):
                return not v
            return None
        if isinstance(e, A.Cast):
            v = self.fold(e.expr)
            if v is None or isinstance(v, bytes):
                return None
            name = getattr(e.type, "name", None)
            name = T.ALIASES.get(name, name)
            if name in T.INT_TYPES or name == "char":
                return int(v)
            if name in T.FLOAT_TYPES:
                return float(v)
            return None
        if isinstance(e, A.Binary):
            a, b = self.fold(e.left), self.fold(e.right)
            if a is None or b is None or isinstance(a, bytes) or isinstance(b, bytes):
                return None
            ints = isinstance(a, int) and isinstance(b, int)
            try:
                op = e.op
                if op == "+": return a + b
                if op == "-": return a - b
                if op == "*": return a * b
                if op == "/": return (int(a / b) if ints else a / b)
                if op == "%" and ints: return a - b * int(a / b)
                if op == "<<" and ints: return a << b
                if op == ">>" and ints: return a >> b
                if op == "&" and ints: return a & b
                if op == "|" and ints: return a | b
                if op == "^" and ints: return a ^ b
                if op == "==": return a == b
                if op == "!=": return a != b
                if op == "<": return a < b
                if op == ">": return a > b
                if op == "<=": return a <= b
                if op == ">=": return a >= b
                if op == "&&": return a and b
                if op == "||": return a or b
            except ZeroDivisionError:
                err_at(e, "division by zero in constant expression")
            return None
        return None

    def eval_const(self, name):
        if name in self.globals:
            return self.globals[name].const_value
        if name in self.const_eval_stack:
            err_at(self.pending_consts[name], "constant '%s' depends on itself" % name)
        self.const_eval_stack.add(name)
        d = self.pending_consts[name]
        val = self.fold(d.value)
        if val is None:
            err_at(d, "value of const '%s' must be a compile-time constant" % name)
        ty = self.resolve(d.type) if d.type else None
        if ty is None:
            if isinstance(d.value, A.CharLit):
                ty = T.CHAR
            elif isinstance(val, bool):
                ty = T.BOOL
            elif isinstance(val, int):
                ty = T.I64
            elif isinstance(val, float):
                ty = T.F64
            else:
                ty = T.STR
        elif ty.is_int() and isinstance(val, float):
            err_at(d, "const '%s': float value for integer type" % name)
        if ty.is_float() and isinstance(val, int):
            val = float(val)
        v = self.globals[name] = VarInfo(name, name, ty, False, "const", val, node=getattr(d, "name_tok", d))
        self.ref(v.node, name, self.var_hover(v), v.node, ty)
        self.const_eval_stack.discard(name)
        return val

    # ================================================================ driver
    def check(self):
        decls = self.decls
        self.extern_names = {d.name for d in decls if isinstance(d, A.FnDecl) and d.is_extern}
        self.used_cnames = set(self.extern_names)
        self.pending_consts = {}
        top_names = {}

        def claim(name, d):
            if name in top_names:
                err_at(d, "'%s' is already declared" % name)
            if name in T.PRIMS or name in T.ALIASES or name == "str":
                err_at(d, "'%s' is a built-in type name" % name)
            top_names[name] = d

        # 1. register type names
        for d in decls:
            if isinstance(d, A.StructDecl):
                claim(d.name, d)
                if d.name in C_RESERVED or d.name.startswith("fl_"):
                    err_at(d, "'%s' is reserved" % d.name)
                self.structs[d.name] = T.StructT(d.name, d)
            elif isinstance(d, A.EnumDecl):
                claim(d.name, d)
                if d.name in C_RESERVED or d.name.startswith("fl_"):
                    err_at(d, "'%s' is reserved" % d.name)
                self.enums[d.name] = T.EnumT(d.name, d)
            elif isinstance(d, A.CInclude):
                if d.header not in self.includes:
                    self.includes.append(d.header)
            elif isinstance(d, A.Link):
                if d.lib not in self.links:
                    self.links.append(d.lib)
            elif isinstance(d, A.GlobalDecl):
                claim(d.decl.name, d)
                if d.is_const:
                    self.pending_consts[d.decl.name] = d.decl
            elif isinstance(d, A.FnDecl):
                claim(d.name, d)

        # 2. consts
        for name in list(self.pending_consts):
            self.eval_const(name)

        # 3. enums and struct fields
        for e in self.enums.values():
            nxt = 0
            e.variant_toks = {}
            self.ref(e.decl.name_tok, e.name, "enum " + e.name, e.decl)
            for (vname, vexpr, vtok) in e.decl.variants:
                e.variant_toks[vname] = vtok
                if vname in e.variants:
                    err_at(vtok, "duplicate variant '%s'" % vname)
                if vexpr is not None:
                    v = self.fold(vexpr)
                    if not isinstance(v, int) or isinstance(v, bool):
                        err_at(vtok, "enum value must be an integer constant")
                    nxt = v
                e.variants[vname] = nxt
                self.ref(vtok, vname, "%s.%s = %d" % (e.name, vname, nxt), vtok, e)
                nxt += 1
        for s in self.structs.values():
            s.field_nodes = {}
            for f in s.decl.fields:
                if f.name in s.fields:
                    err_at(f, "duplicate field '%s'" % f.name)
                s.fields[f.name] = self.resolve_value_type(f.type)
                s.field_nodes[f.name] = f
                self.ref(f, f.name, "%s.%s: %s" % (s.name, f.name, s.fields[f.name]), f, s.fields[f.name])

        self.check_struct_cycles()

        # 4. function signatures
        for d in decls:
            if isinstance(d, A.FnDecl):
                self.fns[d.name] = self.signature(d, None)
            elif isinstance(d, A.ImplDecl):
                st = self.structs.get(d.name)
                if st is None:
                    err_at(d, "impl for unknown struct '%s'" % d.name)
                self.ref(d.name_tok, d.name, "struct " + d.name, st.decl)
                for m in d.methods:
                    if m.name in st.methods or m.name in st.fields:
                        err_at(m, "'%s' is already defined on %s" % (m.name, st.name))
                    st.methods[m.name] = self.signature(m, st)

        for s in self.structs.values():
            self.ref(s.decl.name_tok, s.name, self.type_hover(s), s.decl)
        for f in self.fn_list:
            self.ref(f.decl.name_tok, f.name, self.fn_sig(f), f.decl)
        main = self.fns.get("main")
        if self.lib_mode and (main is None or main.is_extern):
            main = None
        elif main is None or main.is_extern:
            raise CompileError("program has no 'fn main()'", decls[0].file if decls and hasattr(decls[0], "file") else None)
        if main is not None and not (main.ret.is_void() or main.ret.is_int()):
            err_at(main.decl, "main must return void or an integer")
        if main is not None and main.params and not (len(main.params) == 1 and main.params[0][1] == T.Slice(T.STR)):
            err_at(main.decl, "main takes no parameters or (args: []str)")
        self.main = main
        self.need(T.Slice(T.STR))

        # 5. global variables (checked in an initializer pseudo-function)
        init_fn = FnInfo(A.FnDecl(None, "__init", [], None, None, None, False, False, False), "__init", "fl_init_globals", [], T.VOID)
        self.init_fn = init_fn
        self.cur_fn = init_fn
        self.scopes = [{}]
        for d in decls:
            if isinstance(d, A.GlobalDecl) and not d.is_const:
                vd = d.decl
                ty = self.check_var_type(vd)
                v = VarInfo(vd.name, "fl_" + vd.name, ty, vd.mutable, "global", node=vd.name_tok)
                self.globals[vd.name] = v
                self.ref(vd.name_tok, vd.name, self.var_hover(v), vd.name_tok, ty)
                vd.var = v
                self.global_decls.append((v, vd))
        self.scopes = []

        # 6. function bodies
        for f in self.fn_list:
            if not f.is_extern:
                self.check_fn(f)
        return self

    def check_struct_cycles(self):
        """A struct may not contain itself by value (directly, or via arrays/other structs)."""
        state = {}

        def visit(t, path):
            if isinstance(t, T.Array):
                return visit(t.elem, path)
            if not isinstance(t, T.StructT):
                return
            if state.get(t.key) == "done":
                return
            if state.get(t.key) == "active":
                names = " -> ".join(p.name for p in path[path.index(t):] + [t])
                err_at(t.decl, "struct contains itself by value (%s); use a pointer (*T) or a list" % names)
            state[t.key] = "active"
            for ft in t.fields.values():
                visit(ft, path + [t])
            state[t.key] = "done"

        for s in self.structs.values():
            visit(s, [])

    def signature(self, d, owner):
        params = []
        seen = set()
        for p in d.params:
            if p.name in seen:
                err_at(p, "duplicate parameter '%s'" % p.name)
            seen.add(p.name)
            params.append((p.name, self.resolve_value_type(p.type)))
        ret = self.resolve(d.ret) if d.ret else T.VOID
        if d.is_extern:
            cname = d.name
        elif owner is not None:
            cname = "%s__%s" % (owner.name, d.name)
        else:
            cname = "fl_" + d.name
        f = FnInfo(d, d.name, cname, params, ret, owner)
        self.fn_list.append(f)
        return f

    def check_fn(self, f):
        self.cur_fn = f
        self.used_cnames = set(self.extern_names)
        self.scopes = [{}]
        self.loop_depth = 0
        f.param_vars = []
        if f.has_self:
            v = VarInfo("self", "self", self.need(T.Ptr(f.owner)), False, node=f.decl.name_tok)
            self.scopes[0]["self"] = v
            self.used_cnames.add("self")
        for (pname, pty), p in zip(f.params, f.decl.params):
            v = VarInfo(pname, self.cname_for(pname), pty, True, node=p)
            v.used = v.mutated = True  # parameters are not linted
            self.scopes[0][pname] = v
            self.ref(p, pname, "%s: %s" % (pname, pty), p, pty)
            f.param_vars.append(v)
        self.check_block(f.decl.body, new_scope=False)
        if not f.ret.is_void() and not self.terminates(f.decl.body):
            err_at(f.decl, "function '%s' may finish without returning a value" % f.name)
        for v in f.locals:
            if v.name.startswith("_"):
                continue
            if not v.used:
                self.warn(v.node, "unused variable '%s' (rename to '_' or '_%s' to silence)" % (v.name, v.name))
            elif v.mutable and not v.mutated and v.has_init:
                self.warn(v.node, "variable '%s' is never modified; declare it with 'let'" % v.name)
        self.scopes = []

    # ================================================================ statements
    def terminates(self, s):
        if isinstance(s, A.Return):
            return True
        if isinstance(s, A.Block):
            return any(self.terminates(x) for x in s.stmts)
        if isinstance(s, A.If):
            return s.els is not None and self.terminates(s.then) and self.terminates(s.els)
        if isinstance(s, A.While):
            return isinstance(s.cond, A.BoolLit) and s.cond.value and not self.has_break(s.body)
        if isinstance(s, A.Match):
            return any(a.patterns is None for a in s.arms) and all(self.terminates(a.body) for a in s.arms)
        if isinstance(s, A.ExprStmt) and isinstance(s.expr, A.Call) and isinstance(s.expr.callee, A.Ident):
            return s.expr.callee.name in ("panic", "exit") and getattr(s.expr, "kind", None) == "builtin"
        return False

    def has_break(self, s):
        if isinstance(s, A.Break):
            return True
        if isinstance(s, A.Block):
            return any(self.has_break(x) for x in s.stmts)
        if isinstance(s, A.If):
            return self.has_break(s.then) or (s.els is not None and self.has_break(s.els))
        if isinstance(s, A.Match):
            return any(self.has_break(a.body) for a in s.arms)
        if isinstance(s, A.Defer):
            return self.has_break(s.stmt)
        return False  # breaks inside nested loops belong to those loops

    def check_block(self, b, new_scope=True):
        if new_scope:
            self.push_scope()
        dead = False
        for s in b.stmts:
            if dead is True:
                self.warn(s, "unreachable code")
                dead = None
            self.check_stmt(s)
            if dead is False and (isinstance(s, (A.Break, A.Continue)) or self.terminates(s)):
                dead = True
        if new_scope:
            self.pop_scope()

    def check_var_type(self, vd):
        if vd.type is not None:
            ty = self.resolve_value_type(vd.type)
            if vd.value is not None:
                self.check_expr(vd.value, ty)
                self.require_assignable(vd.value, ty)
        else:
            ty = self.check_expr(vd.value)
            if ty == T.NULL:
                err_at(vd, "cannot infer the type of '%s' from null; add a type, e.g. `var %s: *T = null`" % (vd.name, vd.name))
            if ty.is_void():
                err_at(vd, "expression has no value")
        return ty

    def check_stmt(self, s):
        if isinstance(s, A.VarDecl):
            ty = self.check_var_type(s)
            s.var = self.declare_local(s.name_tok, s.name, ty, s.mutable)
            s.var.has_init = s.value is not None
        elif isinstance(s, A.Assign):
            tv = self.lookup(s.target.name) if isinstance(s.target, A.Ident) and s.op == "=" else None
            was_used = tv.used if tv is not None else None
            tt = self.check_expr(s.target)
            if tv is not None:
                tv.used = was_used  # plain assignment is not a read
            self.require_mutable_lvalue(s.target)
            if s.op == "=":
                self.check_expr(s.value, tt)
                self.require_assignable(s.value, tt)
            else:
                op = s.op[:-1]
                fake = A.Binary(None, op, s.target, s.value)
                fake.file, fake.line, fake.col = s.file, s.line, s.col
                rt = self.check_binary(fake, tt)
                if tt == T.STR and op == "+":
                    pass
                elif not self.assignable(rt, tt):
                    err_at(s, "cannot use '%s' with %s and %s" % (s.op, tt, s.value.ty))
        elif isinstance(s, A.ExprStmt):
            self.check_expr(s.expr)
        elif isinstance(s, A.If):
            self.check_cond(s.cond)
            self.check_block(s.then)
            if isinstance(s.els, A.If):
                self.check_stmt(s.els)
            elif s.els is not None:
                self.check_block(s.els)
        elif isinstance(s, A.While):
            self.check_cond(s.cond)
            self.loop_depth += 1
            self.check_block(s.body)
            self.loop_depth -= 1
        elif isinstance(s, A.ForRange):
            lt = self.check_expr(s.lo)
            ht = self.check_expr(s.hi, None if is_literal(s.hi) and not is_literal(s.lo) else lt)
            if is_literal(s.lo) and not is_literal(s.hi):
                lt = self.check_expr(s.lo, ht)
            if not (lt.is_int() and ht.is_int()):
                err_at(s, "range bounds must be integers, got %s and %s" % (lt, ht))
            vt = T.wider(lt, ht)
            self.push_scope()
            s.var = self.declare_local(s.var_tok, s.var, vt, False)
            self.loop_depth += 1
            self.check_block(s.body)
            self.loop_depth -= 1
            self.pop_scope()
        elif isinstance(s, A.ForEach):
            it = self.check_expr(s.iter)
            et = T.elem_type(it)
            if et is None:
                err_at(s.iter, "cannot iterate over %s (expected slice, array, list or str)" % it)
            s.kind = type(it).__name__
            s.iter_lvalue = self.is_lvalue(s.iter)
            self.push_scope()
            s.idx_var = self.declare_local(s.idx_tok, s.idx, T.I64, False) if s.idx else None
            s.elem_var = self.declare_local(s.var_tok, s.var, et, False)
            self.loop_depth += 1
            self.check_block(s.body)
            self.loop_depth -= 1
            self.pop_scope()
        elif isinstance(s, A.Return):
            if self.in_defer:
                err_at(s, "cannot return from inside defer")
            rt = self.cur_fn.ret
            if s.value is None:
                if not rt.is_void():
                    err_at(s, "missing return value (function returns %s)" % rt)
            else:
                if rt.is_void():
                    err_at(s, "function does not return a value")
                self.check_expr(s.value, rt)
                self.require_assignable(s.value, rt)
        elif isinstance(s, (A.Break, A.Continue)):
            if self.loop_depth == 0:
                err_at(s, "'%s' outside of a loop" % ("break" if isinstance(s, A.Break) else "continue"))
        elif isinstance(s, A.Defer):
            old = (self.in_defer, self.loop_depth)
            self.in_defer, self.loop_depth = True, 0
            if isinstance(s.stmt, A.Block):
                self.check_block(s.stmt)
            else:
                self.push_scope()
                self.check_stmt(s.stmt)
                self.pop_scope()
            self.in_defer, self.loop_depth = old
        elif isinstance(s, A.Block):
            self.check_block(s)
        elif isinstance(s, A.Match):
            vt = self.check_expr(s.value)
            if not (vt.is_int() or vt == T.STR or vt == T.BOOL or isinstance(vt, T.EnumT)):
                err_at(s.value, "cannot match on %s" % vt)
            seen_default = False
            for arm in s.arms:
                if arm.patterns is None:
                    if seen_default:
                        err_at(arm, "duplicate '_' arm")
                    seen_default = True
                else:
                    for p in arm.patterns:
                        pt = self.check_expr(p, vt)
                        if not self.comparable(vt, pt):
                            err_at(p, "pattern of type %s does not match %s" % (pt, vt))
                self.check_block(arm.body)
        else:
            raise AssertionError("unknown statement %r" % s)

    def check_cond(self, e):
        t = self.check_expr(e, T.BOOL)
        if t != T.BOOL:
            err_at(e, "condition must be bool, got %s (compare explicitly, e.g. `x != 0` or `p != null`)" % t)

    # ================================================================ lvalues
    def is_lvalue(self, e):
        if isinstance(e, A.Ident):
            return e.sym.kind in ("local", "global")
        if isinstance(e, A.Field):
            if e.kind == "field":
                return self.is_lvalue(e.obj)
            return e.kind == "ptr_field"
        if isinstance(e, A.Index):
            if e.kind == "array":
                return self.is_lvalue(e.obj)
            return e.kind in ("slice", "list", "ptr")
        if isinstance(e, A.Unary) and e.op == "*":
            return True
        return False

    def require_mutable_lvalue(self, e):
        if not self.is_lvalue(e):
            if isinstance(e, A.Index) and e.kind == "str":
                err_at(e, "strings are immutable; build a new one or use a []char/[]u8 buffer")
            err_at(e, "cannot assign to this expression")
        root = e
        while True:
            if isinstance(root, A.Ident):
                root.sym.mutated = True
                if not root.sym.mutable:
                    if root.sym.name == "self":
                        return
                    err_at(e, "cannot modify '%s': it is declared with 'let' (use 'var')" % root.sym.name)
                return
            if isinstance(root, A.Field) and root.kind == "field":
                root = root.obj
            elif isinstance(root, A.Index) and root.kind == "array":
                root = root.obj
            else:
                return  # through a pointer/slice/list: allowed

    # ================================================================ type relations
    def assignable(self, src, dst):
        if src == dst:
            return True
        if src.is_numeric() and dst.is_numeric():
            if src.is_float() and dst.is_int():
                return False
            return True
        if src == T.NULL and dst.is_ptr():
            return True
        if src.is_ptr() and dst.is_ptr() and (src.elem.is_void() or dst.elem.is_void()):
            return True
        if isinstance(dst, T.Slice) and isinstance(src, (T.Array, T.ListT)) and src.elem == dst.elem:
            return True
        return False

    def require_assignable(self, e, dst):
        src = e.ty
        if isinstance(e, (A.IntLit, A.CharLit)) and dst.is_numeric():
            return
        if not self.assignable(src, dst):
            hint = ""
            if src.is_float() and dst.is_int():
                hint = " (use `as %s` to truncate)" % dst
            elif src == T.STR and dst.is_ptr():
                hint = " (use `.ptr` or `.cstr()` for a C string)"
            err_at(e, "type mismatch: expected %s, got %s%s" % (dst, src, hint))
        if isinstance(dst, T.Slice) and isinstance(src, (T.Array, T.ListT)):
            if isinstance(src, T.Array) and not self.is_lvalue(e):
                err_at(e, "can only convert a named array to a slice (store it in a variable first)")
            if isinstance(src, T.Array):
                self.mark_mutated(e)
            e.coerce = dst
            self.need(dst)

    def comparable(self, a, b):
        if a == b:
            return a != T.VOID and not isinstance(a, (T.StructT, T.Slice, T.Array, T.ListT))
        if a.is_numeric() and b.is_numeric():
            return True
        if (a.is_ptr() and b == T.NULL) or (b.is_ptr() and a == T.NULL):
            return True
        if a.is_ptr() and b.is_ptr():
            return a.elem.is_void() or b.elem.is_void()
        return False

    # ================================================================ expressions
    def check_expr(self, e, expect=None):
        t = self._check_expr(e, expect)
        e.ty = t
        return t

    def _check_expr(self, e, expect):
        if isinstance(e, A.IntLit):
            if expect is not None and expect.is_numeric():
                return expect
            return T.I64
        if isinstance(e, A.FloatLit):
            if expect is not None and expect.is_float():
                return expect
            return T.F64
        if isinstance(e, A.CharLit):
            return T.CHAR
        if isinstance(e, A.BoolLit):
            return T.BOOL
        if isinstance(e, A.NullLit):
            if expect is not None and expect.is_ptr():
                return expect
            return T.NULL
        if isinstance(e, A.StrLit):
            return T.STR
        if isinstance(e, A.InterpStr):
            for p in e.parts:
                if not isinstance(p, bytes):
                    self.check_fmt_arg(p)
            return T.STR
        if isinstance(e, A.Ident):
            v = self.lookup(e.name)
            if v is None:
                if e.name in self.fns:
                    err_at(e, "'%s' is a function; call it with ()" % e.name)
                if e.name in self.structs or e.name in self.enums:
                    err_at(e, "'%s' is a type, not a value" % e.name)
                err_at(e, "undefined name '%s'" % e.name)
            e.sym = v
            v.used = True
            self.ref(e, e.name, self.var_hover(v), v.node, v.ty)
            return v.ty
        if isinstance(e, A.Binary):
            return self.check_binary(e, expect)
        if isinstance(e, A.Unary):
            return self.check_unary(e, expect)
        if isinstance(e, A.Cast):
            return self.check_cast(e)
        if isinstance(e, A.Field):
            return self.check_field(e)
        if isinstance(e, A.Index):
            return self.check_index(e)
        if isinstance(e, A.SliceExpr):
            return self.check_slice(e)
        if isinstance(e, A.Call):
            return self.check_call(e, expect)
        if isinstance(e, A.TypeCall):
            return self.check_typecall(e)
        if isinstance(e, A.StructLit):
            st = self.structs.get(e.name)
            if st is None or self.lookup(e.name) is not None:
                err_at(e, "unknown struct '%s'" % e.name)
            self.ref(e, e.name, self.type_hover(st), st.decl)
            seen = set()
            for (fname, fexpr, ftok) in e.inits:
                if fname not in st.fields:
                    err_at(ftok, "struct %s has no field '%s'" % (st.name, fname))
                self.ref(ftok, fname, "%s.%s: %s" % (st.name, fname, st.fields[fname]), st.field_nodes[fname])
                if fname in seen:
                    err_at(ftok, "field '%s' given twice" % fname)
                seen.add(fname)
                self.check_expr(fexpr, st.fields[fname])
                self.require_assignable(fexpr, st.fields[fname])
            return st
        if isinstance(e, A.ArrayLit):
            et = expect.elem if isinstance(expect, T.Array) else None
            if not e.elems:
                err_at(e, "empty array literal; give the variable an explicit type, e.g. `var a: [4]i32`")
            if et is None:
                et = self.check_expr(e.elems[0])
                if et == T.NULL or et.is_void():
                    err_at(e, "cannot infer array element type")
            for x in e.elems:
                self.check_expr(x, et)
                self.require_assignable(x, et)
            if isinstance(expect, T.Array) and expect.n != len(e.elems):
                err_at(e, "expected %d elements, got %d" % (expect.n, len(e.elems)))
            return self.need(T.Array(et, len(e.elems)))
        raise AssertionError("unknown expression %r" % e)

    def check_fmt_arg(self, p):
        t = self.check_expr(p.expr)
        if t.is_void():
            err_at(p.expr, "expression has no value to format")
        if p.spec:
            conv = p.spec[-1] if p.spec[-1] in "xXobe" else ""
            if conv in "xXob" and conv and not t.is_int():
                err_at(p, "format '%s' needs an integer, got %s" % (p.spec, t))
            if conv == "e" and not t.is_float():
                err_at(p, "format 'e' needs a float, got %s" % t)
            if "." in p.spec and not (t.is_float() or t == T.STR):
                err_at(p, "precision '%s' needs a float or str, got %s" % (p.spec, t))
            if conv == "" and not (t.is_numeric() or t == T.STR or t == T.BOOL):
                err_at(p, "width format only works on numbers, bool and str, got %s" % t)
        self.need_fmt(t)

    def check_binary(self, e, expect):
        op = e.op
        if op in ("&&", "||"):
            for side in (e.left, e.right):
                if self.check_expr(side, T.BOOL) != T.BOOL:
                    err_at(side, "'%s' needs bool operands, got %s" % (op, side.ty))
            return T.BOOL
        # literal sides adopt the other side's type
        if is_literal(e.left) and not is_literal(e.right):
            rt = self.check_expr(e.right, expect if op not in ("==", "!=", "<", ">", "<=", ">=") else None)
            lt = self.check_expr(e.left, rt if rt.is_numeric() else None)
        else:
            lt = self.check_expr(e.left, expect if op not in ("==", "!=", "<", ">", "<=", ">=", "<<", ">>") else None)
            rt = self.check_expr(e.right, lt if (lt.is_numeric() or lt.is_ptr()) else None)
        if op in ("==", "!=", "<", ">", "<=", ">="):
            if op in ("==", "!="):
                if not self.comparable(lt, rt):
                    err_at(e, "cannot compare %s with %s" % (lt, rt))
            else:
                ok = (lt.is_numeric() and rt.is_numeric()) or (lt.is_ptr() and lt == rt) or (lt == rt == T.STR)
                if not ok:
                    err_at(e, "cannot order-compare %s with %s" % (lt, rt))
            return T.BOOL
        if op == "+" and lt == T.STR and rt == T.STR:
            return T.STR
        if op in ("+", "-") and lt.is_ptr() and rt.is_int():
            return lt
        if op == "-" and lt.is_ptr() and lt == rt:
            return T.I64
        if op in ("&", "|", "^", "<<", ">>", "%"):
            if not (lt.is_int() and rt.is_int()):
                err_at(e, "'%s' needs integer operands, got %s and %s" % (op, lt, rt))
            return lt if op in ("<<", ">>") else T.wider(lt, rt)
        if op in ("+", "-", "*", "/"):
            if not (lt.is_numeric() and rt.is_numeric()):
                err_at(e, "'%s' needs numeric operands, got %s and %s" % (op, lt, rt))
            return T.wider(lt, rt)
        raise AssertionError(op)

    def check_unary(self, e, expect):
        op = e.op
        if op == "&":
            t = self.check_expr(e.operand)
            if not self.is_lvalue(e.operand):
                err_at(e, "can only take the address of a variable, field or element")
            self.mark_mutated(e.operand)
            return self.need(T.Ptr(t))
        t = self.check_expr(e.operand, expect if op in ("-", "~") else None)
        if op == "-":
            if not t.is_numeric():
                err_at(e, "'-' needs a number, got %s" % t)
            return t
        if op == "!":
            if t != T.BOOL:
                err_at(e, "'!' needs bool, got %s" % t)
            return T.BOOL
        if op == "~":
            if not t.is_int():
                err_at(e, "'~' needs an integer, got %s" % t)
            return t
        if op == "*":
            if not t.is_ptr() or t.elem.is_void():
                err_at(e, "cannot dereference %s" % t)
            return t.elem
        raise AssertionError(op)

    def check_cast(self, e):
        target = self.resolve_value_type(e.type)
        src = self.check_expr(e.expr, target if target.is_numeric() else None)
        ok = (
            src == target
            or (src.is_numeric() and target.is_numeric())
            or (src.is_numeric() and isinstance(target, T.EnumT))
            or (isinstance(src, T.EnumT) and target.is_numeric())
            or (src.is_ptr() and target.is_ptr())
            or (src == T.NULL and target.is_ptr())
            or (src.is_ptr() and target.key in ("i64", "u64"))
            or (src.is_int() and target.is_ptr())
            or (src == T.BOOL and target.is_int())
        )
        if not ok:
            err_at(e, "cannot cast %s to %s" % (src, target))
        return target

    def check_field(self, e):
        tn = self.type_name_in_scope(e.obj)
        if isinstance(tn, T.EnumT):
            if e.name not in tn.variants:
                err_at(e, "enum %s has no variant '%s'" % (tn.name, e.name))
            e.kind = "enum"
            e.obj.ty = tn
            self.ref(e.obj, tn.name, self.type_hover(tn), tn.decl)
            self.ref(e, e.name, "%s.%s = %d" % (tn.name, e.name, tn.variants[e.name]), tn.variant_toks[e.name], tn)
            return tn
        if isinstance(tn, T.StructT):
            err_at(e, "'%s.%s' must be called: %s.%s(...)" % (tn.name, e.name, tn.name, e.name))
        t = self.check_expr(e.obj)
        st, kind = t, "field"
        if t.is_ptr() and isinstance(t.elem, T.StructT):
            st, kind = t.elem, "ptr_field"
        if isinstance(st, T.StructT):
            if e.name in st.fields:
                e.kind = kind
                self.ref(e, e.name, "%s.%s: %s" % (st.name, e.name, st.fields[e.name]), st.field_nodes[e.name], st.fields[e.name])
                return st.fields[e.name]
            if e.name in st.methods:
                err_at(e, "'%s' is a method; call it with ()" % e.name)
            err_at(e, "struct %s has no field '%s'" % (st.name, e.name))
        if isinstance(t, (T.StrT, T.Slice, T.Array, T.ListT)):
            if e.name in ("len", "ptr", "cap"):
                self.ref(e, e.name, "%s.%s (built-in field)" % (t, e.name))
            if e.name == "len":
                e.kind = "len"
                return T.I64
            if e.name == "ptr":
                if isinstance(t, T.Array) and not self.is_lvalue(e.obj):
                    err_at(e, "'.ptr' needs a named array")
                self.mark_mutated(e.obj)
                e.kind = "ptr"
                return self.need(T.Ptr(T.elem_type(t)))
            if e.name == "cap" and isinstance(t, T.ListT):
                e.kind = "cap"
                return T.I64
        err_at(e, "%s has no field '%s'" % (t, e.name))

    def check_index(self, e):
        t = self.check_expr(e.obj)
        it = self.check_expr(e.index, T.I64)
        if not it.is_int():
            err_at(e.index, "index must be an integer, got %s" % it)
        if isinstance(t, T.Slice):
            e.kind = "slice"
        elif isinstance(t, T.ListT):
            e.kind = "list"
        elif isinstance(t, T.Array):
            e.kind = "array"
        elif t == T.STR:
            e.kind = "str"
            return T.CHAR
        elif t.is_ptr() and not t.elem.is_void():
            e.kind = "ptr"
            return t.elem
        else:
            err_at(e, "cannot index %s" % t)
        return t.elem

    def check_slice(self, e):
        t = self.check_expr(e.obj)
        for b in (e.lo, e.hi):
            if b is not None and not self.check_expr(b, T.I64).is_int():
                err_at(b, "slice bounds must be integers")
        if t == T.STR:
            e.kind = "str"
            return T.STR
        if isinstance(t, (T.Slice, T.ListT, T.Array)):
            e.kind = {T.Slice: "slice", T.ListT: "list", T.Array: "array"}[type(t)]
            if e.kind == "array" and not self.is_lvalue(e.obj):
                err_at(e, "can only slice a named array")
            if e.kind == "array":
                self.mark_mutated(e.obj)
            return self.need(T.Slice(t.elem))
        err_at(e, "cannot slice %s" % t)

    def check_args(self, e, params, args, variadic=False, what="function"):
        if len(args) < len(params) or (len(args) > len(params) and not variadic):
            err_at(e, "%s expects %d argument(s), got %d" % (what, len(params), len(args)))
        for (pname, pty), a in zip(params, args):
            self.check_expr(a, pty)
            self.require_assignable(a, pty)
        for a in args[len(params):]:
            t = self.check_expr(a)
            if t == T.STR:
                err_at(a, "pass `.ptr` to give a str to a C variadic function")

    def check_call(self, e, expect):
        c = e.callee
        if isinstance(c, A.Ident) and self.lookup(c.name) is None:
            name = c.name
            if name in self.fns:
                f = self.fns[name]
                e.kind, e.fn = "fn", f
                self.ref(c, name, self.fn_sig(f), f.decl, f.ret)
                self.check_args(e, f.params, e.args, f.variadic, "'%s'" % name)
                return f.ret
            if name in BUILTINS:
                e.kind = "builtin"
                self.ref(c, name, BUILTIN_DOCS.get(name, name))
                return self.check_builtin(e, name)
            if name in self.structs:
                err_at(e, "to create a %s use a struct literal: %s { field: value }" % (name, name))
            err_at(c, "undefined function '%s'" % name)
        if isinstance(c, A.Field):
            tn = self.type_name_in_scope(c.obj)
            if isinstance(tn, T.StructT):
                f = tn.methods.get(c.name)
                if f is None:
                    err_at(c, "%s has no method '%s'" % (tn.name, c.name))
                if f.has_self:
                    err_at(c, "'%s' takes self; call it on a value: v.%s(...)" % (c.name, c.name))
                e.kind, e.fn = "static", f
                self.ref(c.obj, tn.name, self.type_hover(tn), tn.decl)
                self.ref(c, c.name, self.fn_sig(f), f.decl, f.ret)
                self.check_args(e, f.params, e.args, False, "'%s.%s'" % (tn.name, c.name))
                return f.ret
            if tn is not None:
                err_at(c, "enum variants are not callable")
            ot = self.check_expr(c.obj)
            st = ot.elem if ot.is_ptr() and isinstance(ot.elem, T.StructT) else ot
            if isinstance(st, T.StructT):
                f = st.methods.get(c.name)
                if f is None:
                    if c.name in st.fields:
                        err_at(c, "'%s' is a field, not a method" % c.name)
                    err_at(c, "%s has no method '%s'" % (st.name, c.name))
                if not f.has_self:
                    err_at(c, "'%s' has no self parameter; call it as %s.%s(...)" % (c.name, st.name, c.name))
                if ot.is_ptr():
                    e.self_mode = "ptr"
                elif self.is_lvalue(c.obj):
                    e.self_mode = "addr"
                    self.mark_mutated(c.obj)
                else:
                    e.self_mode = "temp"
                e.kind, e.fn = "method", f
                self.ref(c, c.name, self.fn_sig(f), f.decl, f.ret)
                self.check_args(e, f.params, e.args, False, "'%s'" % c.name)
                return f.ret
            if isinstance(ot, T.ListT):
                return self.check_list_method(e, ot)
            if ot == T.STR and c.name in STR_METHODS:
                return self.check_str_method(e)
            err_at(c, "%s has no method '%s'" % (ot, c.name))
        err_at(e, "expression is not callable")

    def check_list_method(self, e, lt):
        name = e.callee.name
        if name not in LIST_METHODS:
            err_at(e.callee, "list has no method '%s' (available: %s)" % (name, ", ".join(sorted(LIST_METHODS))))
        if not self.is_lvalue(e.callee.obj):
            err_at(e, "list method needs a list variable")
        self.require_mutable_lvalue(e.callee.obj)
        e.kind = "list_method"
        sig = {
            "push": ([("value", lt.elem)], T.VOID),
            "pop": ([], lt.elem),
            "clear": ([], T.VOID),
            "remove": ([("index", T.I64)], lt.elem),
            "insert": ([("index", T.I64), ("value", lt.elem)], T.VOID),
            "reserve": ([("cap", T.I64)], T.VOID),
        }[name]
        self.ref(e.callee, name, "fn %s.%s(%s)%s" % (lt, name, ", ".join("%s: %s" % p for p in sig[0]),
                                                      "" if sig[1].is_void() else " -> %s" % sig[1]))
        self.check_args(e, sig[0], e.args, False, "'%s'" % name)
        return sig[1]

    def check_str_method(self, e):
        name = e.callee.name
        e.kind = "str_method"
        sig = {
            "find": ([("sub", T.STR)], T.I64),
            "contains": ([("sub", T.STR)], T.BOOL),
            "starts_with": ([("prefix", T.STR)], T.BOOL),
            "ends_with": ([("suffix", T.STR)], T.BOOL),
            "eq": ([("other", T.STR)], T.BOOL),
            "trim": ([], T.STR),
            "clone": ([], T.STR),
            "cstr": ([], self.need(T.Ptr(T.CHAR))),
            "split": ([("sep", T.STR)], self.need(T.ListT(T.STR))),
        }[name]
        self.ref(e.callee, name, "fn str.%s(%s) -> %s" % (name, ", ".join("%s: %s" % p for p in sig[0]), sig[1]))
        self.check_args(e, sig[0], e.args, False, "'%s'" % name)
        return sig[1]

    def check_builtin(self, e, name):
        a = e.args

        def nargs(lo, hi=None):
            hi = lo if hi is None else hi
            if not (lo <= len(a) <= hi):
                want = str(lo) if lo == hi else "%d-%d" % (lo, hi)
                err_at(e, "%s() expects %s argument(s), got %d" % (name, want, len(a)))

        if name in ("print", "println", "eprint", "eprintln"):
            for x in a:
                t = self.check_expr(x)
                if t.is_void():
                    err_at(x, "expression has no value to print")
                self.need_fmt(t)
            return T.VOID
        if name == "assert":
            nargs(1, 2)
            if self.check_expr(a[0], T.BOOL) != T.BOOL:
                err_at(a[0], "assert needs a bool")
            if len(a) == 2 and self.check_expr(a[1]) != T.STR:
                err_at(a[1], "assert message must be a str")
            return T.VOID
        if name == "panic":
            nargs(1)
            if self.check_expr(a[0]) != T.STR:
                err_at(a[0], "panic message must be a str")
            return T.VOID
        if name == "exit":
            nargs(1)
            if not self.check_expr(a[0], T.I32).is_int():
                err_at(a[0], "exit code must be an integer")
            return T.VOID
        if name == "free":
            nargs(1)
            t = self.check_expr(a[0])
            if not (t.is_ptr() or isinstance(t, (T.Slice, T.ListT)) or t == T.STR):
                err_at(a[0], "cannot free %s" % t)
            if isinstance(t, T.ListT):
                if not self.is_lvalue(a[0]):
                    err_at(a[0], "free(list) needs a list variable")
                self.require_mutable_lvalue(a[0])
            return T.VOID
        if name == "read_line":
            nargs(0)
            return T.STR
        if name in ("parse_int", "parse_float"):
            nargs(1)
            if self.check_expr(a[0]) != T.STR:
                err_at(a[0], "%s needs a str" % name)
            return T.I64 if name == "parse_int" else T.F64
        if name == "time_ns":
            nargs(0)
            return T.I64
        if name == "read_file":
            nargs(1)
            if self.check_expr(a[0]) != T.STR:
                err_at(a[0], "read_file needs a str path")
            return T.STR
        if name == "write_file":
            nargs(2)
            for x in a:
                if self.check_expr(x) != T.STR:
                    err_at(x, "write_file(path: str, data: str)")
            return T.BOOL
        if name == "args":
            nargs(0)
            return self.need(T.Slice(T.STR))
        raise AssertionError(name)

    def check_typecall(self, e):
        t = self.resolve_value_type(e.type)
        if e.name == "sizeof":
            if e.args:
                err_at(e, "sizeof takes only a type")
            return T.I64
        if e.name == "new":
            if len(e.args) > 1:
                err_at(e, "new(T) or new(T, value)")
            if e.args:
                self.check_expr(e.args[0], t)
                self.require_assignable(e.args[0], t)
            return self.need(T.Ptr(t))
        if e.name == "make":
            if not isinstance(t, (T.Slice, T.ListT)):
                err_at(e, "make needs a slice or list type: make([]T, n) or make(list[T], capacity)")
            if len(e.args) != 1:
                err_at(e, "make(%s, n) needs a length" % t)
            if not self.check_expr(e.args[0], T.I64).is_int():
                err_at(e.args[0], "length must be an integer")
            return t
        raise AssertionError(e.name)


def check(decls, lib_mode=False):
    return Checker(decls, lib_mode).check()
