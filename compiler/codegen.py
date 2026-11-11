"""C code generator: checked AST -> a single C translation unit.

Output layout:
    runtime.h (inlined)          fl_str, string builder, panics, lists, I/O
    user #includes               from `cinclude`
    enums
    forward typedefs             structs and array wrappers
    slice / list typedefs
    struct + array definitions   topologically sorted by by-value use
    list function instances
    formatter prototypes         fl_fmt_<T> for printing composite types
    function prototypes
    globals
    formatter definitions
    function definitions
    fl_init_globals(), main()

The whole program is one C file, so the C compiler can inline across
everything. GNU statement expressions `({ ... })` are used freely (gcc/clang).
"""

import os
import re

from . import ast as A
from . import types as T
from .errors import CompileError, err_at

RUNTIME = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runtime.h")
PREDEFINED = {"[]str", "list[str]"}  # defined inside runtime.h


def c_string(data):
    """bytes -> C string literal."""
    out = ['"']
    for b in data:
        c = chr(b)
        if c == '"':
            out.append('\\"')
        elif c == "\\":
            out.append("\\\\")
        elif c == "\n":
            out.append("\\n")
        elif c == "\t":
            out.append("\\t")
        elif c == "?":
            out.append("\\?")  # avoid trigraphs
        elif 32 <= b < 127:
            out.append(c)
        else:
            out.append("\\%03o" % b)
    out.append('"')
    return "".join(out)


def int_lit(value, ty):
    if ty.is_float():
        return float_lit(float(value), ty)
    if value == -(2 ** 63):
        s = "(-9223372036854775807LL-1)"
    elif -(2 ** 31) <= value < 2 ** 31:
        s = str(value)
    elif value >= 2 ** 63:
        s = "%dULL" % value
    else:
        s = "%dLL" % value
    if ty.key in ("i32", "bool"):
        return s
    return "((%s)%s)" % (ty.c, s)


def float_lit(value, ty):
    s = repr(float(value))
    if s in ("inf", "-inf", "nan"):
        s = {"inf": "HUGE_VAL", "-inf": "(-HUGE_VAL)", "nan": "NAN"}[s]
        return "((float)%s)" % s if ty.key == "f32" else s
    if "." not in s and "e" not in s:
        s += ".0"
    return s + "f" if ty.key == "f32" else s


class CGen:
    def __init__(self, ck, debug_lines=False):
        self.ck = ck
        self.debug_lines = debug_lines
        self.lines = []
        self.ind = 0
        self.tmp_n = 0
        self.frames = []
        self.fn = None

    # ------------------------------------------------------------ utilities
    def tmp(self, base="_t"):
        self.tmp_n += 1
        return "%s%d" % (base, self.tmp_n)

    def emit(self, s):
        self.lines.append("    " * self.ind + s)

    def loc(self, node):
        f = os.path.basename(node.file or "?")
        return c_string(("%s:%d:%d" % (f, node.line, node.col)).encode())

    def zero(self, t):
        if t.is_numeric() or t == T.BOOL or isinstance(t, T.EnumT):
            return "0"
        if t.is_ptr():
            return "NULL"
        return "{0}"

    # ------------------------------------------------------------ program
    def generate(self):
        ck = self.ck
        out = []
        with open(RUNTIME) as f:
            out.append(f.read())
        for h in ck.includes:
            if h.startswith("<") or h.startswith('"'):
                out.append("#include %s" % h)
            elif h.startswith(".") or h.startswith("/"):
                out.append('#include "%s"' % h)
            else:
                out.append("#include <%s>" % h)
        out.append("")

        # enums
        for e in ck.enums.values():
            body = ", ".join("%s_%s = %d" % (e.name, n, v) for n, v in e.variants.items())
            out.append("typedef enum %s { %s } %s;" % (e.name, body, e.name))

        # forward declarations
        for s in ck.structs.values():
            out.append("typedef struct %s %s;" % (s.name, s.name))
        for t in ck.needed.values():
            if isinstance(t, T.Array):
                out.append("typedef struct %s %s;" % (t.c, t.c))
        # slices and lists only hold pointers to their element type
        for t in ck.needed.values():
            if t.key in PREDEFINED:
                continue
            if isinstance(t, T.Slice):
                out.append("FL_SLICE_TYPE(%s, %s)" % (t.elem.mangle, t.elem.c))
            elif isinstance(t, T.ListT):
                out.append("FL_LIST_TYPE(%s, %s)" % (t.elem.mangle, t.elem.c))

        # struct and array definitions in dependency order
        for t in self.sorted_aggregates():
            if isinstance(t, T.StructT):
                out.append("struct %s {" % t.name)
                if not t.fields:
                    out.append("    char _empty;")
                for fname, fty in t.fields.items():
                    out.append("    %s %s;" % (fty.c, fname))
                out.append("};")
            else:
                out.append("struct %s { %s a[%d]; };" % (t.c, t.elem.c, t.n))
        for t in ck.needed.values():
            if isinstance(t, T.ListT) and t.key not in PREDEFINED:
                out.append("FL_LIST_IMPL(%s, %s)" % (t.elem.mangle, t.elem.c))
        out.append("")

        # formatter prototypes
        for t in ck.fmt_types.values():
            out.append("static void fl_fmt_%s(fl_sb* sb, %s v);" % (t.mangle, t.c))
        # function prototypes
        for f in ck.fn_list:
            if not f.is_extern:
                out.append(self.fn_header(f) + ";")
        out.append("static void fl_init_globals(void);")
        out.append("")
        for v, vd in ck.global_decls:
            out.append("static %s %s;" % (v.ty.c, v.cname))
        out.append("")

        # formatter bodies
        for t in ck.fmt_types.values():
            out.append(self.gen_formatter(t))

        # functions
        for f in ck.fn_list:
            if not f.is_extern:
                self.lines = []
                self.gen_fn(f)
                out.append("\n".join(self.lines))
                out.append("")

        # globals init
        self.lines = []
        self.fn = ck.init_fn
        self.emit("static void fl_init_globals(void) {")
        self.ind = 1
        self.frames = [{"defers": [], "loop": False}]
        for v, vd in ck.global_decls:
            if vd.value is not None:
                self.emit("%s = %s;" % (v.cname, self.expr_as(vd.value, v.ty)))
        self.ind = 0
        self.emit("}")
        out.append("\n".join(self.lines))

        # entry point
        main = ck.main
        call = "%s(%s)" % (main.cname, "fl_args" if main.params else "")
        out.append("")
        out.append("int main(int argc, char** argv) {")
        out.append("    fl_init_args(argc, argv);")
        out.append("    fl_init_globals();")
        if main.ret.is_void():
            out.append("    %s;" % call)
            out.append("    return 0;")
        else:
            out.append("    return (int)%s;" % call)
        out.append("}")
        return "\n".join(out) + "\n"

    def sorted_aggregates(self):
        """Structs and array wrappers ordered so by-value members come first."""
        items = list(self.ck.structs.values()) + [t for t in self.ck.needed.values() if isinstance(t, T.Array)]
        state = {}
        order = []

        def deps(t):
            if isinstance(t, T.StructT):
                return list(t.fields.values())
            return [t.elem]

        def visit(t, path):
            if not isinstance(t, (T.StructT, T.Array)):
                return
            st = state.get(t.key)
            if st == "done":
                return
            if st == "active":
                names = " -> ".join(p.key for p in path + [t])
                decl = t.decl if isinstance(t, T.StructT) else path[0].decl
                err_at(decl, "struct contains itself by value (%s); use a pointer (*T) or a list" % names)
            state[t.key] = "active"
            for d in deps(t):
                visit(d, path + [t])
            state[t.key] = "done"
            order.append(t)

        for t in items:
            visit(t, [])
        return order

    def fn_header(self, f):
        params = []
        if f.has_self:
            params.append("%s* self" % f.owner.c)
        pv = getattr(f, "param_vars", None)
        for i, (pname, pty) in enumerate(f.params):
            params.append("%s %s" % (pty.c, pv[i].cname if pv else pname))
        return "static %s %s(%s)" % (f.ret.c, f.cname, ", ".join(params) or "void")

    # ------------------------------------------------------------ formatting
    def gen_formatter(self, t):
        L = ["static void fl_fmt_%s(fl_sb* sb, %s v) {" % (t.mangle, t.c)]
        if isinstance(t, T.EnumT):
            L.append("    switch (v) {")
            seen = set()
            for n, val in t.variants.items():
                if val in seen:
                    continue
                seen.add(val)
                L.append("    case %s_%s: fl_sb_put(sb, %s, %d); return;" % (t.name, n, c_string(n.encode()), len(n)))
            L.append("    default: fl_sb_printf(sb, \"%s(%%lld)\", (long long)v);" % t.name)
            L.append("    }")
        elif isinstance(t, T.StructT):
            L.append("    fl_sb_put(sb, %s, %d);" % (c_string((t.name + " {").encode()), len(t.name) + 2))
            for i, (fname, fty) in enumerate(t.fields.items()):
                label = ("%s%s: " % (", " if i else " ", fname)).encode()
                L.append("    fl_sb_put(sb, %s, %d);" % (c_string(label), len(label)))
                L.append("    " + self.fmt_value(fty, "sb", "v.%s" % fname, True))
            L.append("    fl_sb_put(sb, \" }\", 2);")
        else:
            if isinstance(t, T.Array):
                n, el = str(t.n), "v.a[i]"
            else:
                n, el = "v.len", "v.ptr[i]"
            L.append("    fl_sb_char(sb, '[');")
            L.append("    for (int64_t i = 0; i < %s; i++) {" % n)
            L.append("        if (i) fl_sb_put(sb, \", \", 2);")
            L.append("        " + self.fmt_value(t.elem, "sb", el, True))
            L.append("    }")
            L.append("    fl_sb_char(sb, ']');")
        L.append("}")
        return "\n".join(L)

    def fmt_value(self, t, sb, v, quoted=False):
        """C statement appending value `v` of type t to builder `sb`."""
        if t == T.CHAR:
            return "fl_sb_%s(%s, %s);" % ("charq" if quoted else "char", sb, v)
        if t.is_int():
            if t.is_signed():
                return "fl_sb_i64(%s, (int64_t)(%s));" % (sb, v)
            return "fl_sb_u64(%s, (uint64_t)(%s));" % (sb, v)
        if t.key == "f64":
            return "fl_sb_f64(%s, %s);" % (sb, v)
        if t.key == "f32":
            return "fl_sb_f32(%s, %s);" % (sb, v)
        if t == T.BOOL:
            return "fl_sb_bool(%s, %s);" % (sb, v)
        if t == T.STR:
            return "fl_sb_%s(%s, %s);" % ("strq" if quoted else "str", sb, v)
        if t.is_ptr() or t == T.NULL:
            return "fl_sb_ptr(%s, %s);" % (sb, v)
        if isinstance(t, (T.StructT, T.EnumT, T.Slice, T.Array, T.ListT)):
            return "fl_fmt_%s(%s, %s);" % (t.mangle, sb, v)
        raise AssertionError(t)

    def fmt_spec(self, t, sb, v, spec):
        m = re.fullmatch(r"(-)?(0)?(\d*)(?:\.(\d+))?([xXobe])?", spec)
        left, zero, width, prec, conv = m.groups()
        flags = (left or "") + (zero or "")
        width = width or ""
        if t.is_int() and conv == "b":
            return "fl_sb_bin(%s, (uint64_t)(%s), %d, %s, %s);" % (sb, v, int(width or 0), "true" if zero else "false", "true" if left else "false")
        if t.is_int() and t != T.CHAR or (t == T.CHAR and conv):
            if conv in ("x", "X", "o"):
                return 'fl_sb_printf(%s, "%%%s%sll%s", (unsigned long long)(%s));' % (sb, flags, width, conv, v)
            if t.is_signed():
                return 'fl_sb_printf(%s, "%%%s%slld", (long long)(%s));' % (sb, flags, width, v)
            return 'fl_sb_printf(%s, "%%%s%sllu", (unsigned long long)(%s));' % (sb, flags, width, v)
        if t.is_float():
            p = "." + prec if prec is not None else ""
            c = "e" if conv == "e" else ("f" if prec is not None else "g")
            return 'fl_sb_printf(%s, "%%%s%s%s%s", (double)(%s));' % (sb, flags, width, p, c, v)
        if t == T.STR or t == T.CHAR or t == T.BOOL:
            if t == T.STR:
                tv = self.tmp("_s")
                lim = "(int)%s.len" % tv if prec is None else "(int)(%s.len < %s ? %s.len : %s)" % (tv, prec, tv, prec)
                return '{ fl_str %s = %s; fl_sb_printf(%s, "%%%s%s.*s", %s, %s.ptr); }' % (tv, v, sb, left or "", width, lim, tv)
            if t == T.CHAR:
                return 'fl_sb_printf(%s, "%%%s%sc", (%s));' % (sb, left or "", width, v)
            return 'fl_sb_printf(%s, "%%%s%ss", (%s) ? "true" : "false");' % (sb, left or "", width, v)
        return self.fmt_value(t, sb, v)

    def fmt_parts(self, sb, parts):
        """Statements appending an interpolated string's parts."""
        stmts = []
        for p in parts:
            if isinstance(p, bytes):
                if p:
                    stmts.append("fl_sb_put(%s, %s, %d);" % (sb, c_string(p), len(p)))
            else:
                v = self.expr(p.expr)
                if p.spec:
                    stmts.append(self.fmt_spec(p.expr.ty, sb, v, p.spec))
                else:
                    stmts.append(self.fmt_value(p.expr.ty, sb, v))
        return " ".join(stmts)

    # ------------------------------------------------------------ functions
    def gen_fn(self, f):
        self.fn = f
        self.ind = 0
        self.tmp_n = 0
        self.emit(self.fn_header(f) + " {")
        self.ind = 1
        self.frames = []
        self.gen_block_body(f.decl.body, loop=False)
        self.ind = 0
        self.emit("}")

    # ------------------------------------------------------------ statements
    def gen_block_body(self, b, loop):
        self.frames.append({"defers": [], "loop": loop})
        for s in b.stmts:
            self.gen_stmt(s)
        frame = self.frames.pop()
        if frame["defers"] and not (b.stmts and isinstance(b.stmts[-1], (A.Return, A.Break, A.Continue))):
            self.run_defers([frame])

    def run_defers(self, frames):
        """Emit the deferred statements of `frames` (innermost first, LIFO)."""
        for fr in frames:
            for d in reversed(fr["defers"]):
                self.gen_block(d if isinstance(d, A.Block) else A.Block(None, [d]))

    def gen_block(self, b, loop=False, prefix=None):
        self.emit("{")
        self.ind += 1
        if prefix:
            for p in prefix:
                self.emit(p)
        self.gen_block_body(b, loop)
        self.ind -= 1
        self.emit("}")

    def gen_stmt(self, s):
        if self.debug_lines and s.file:
            self.lines.append('#line %d "%s"' % (s.line, os.path.abspath(s.file)))
        if isinstance(s, A.VarDecl):
            v = s.var
            if s.value is None:
                self.emit("%s %s = %s;" % (v.ty.c, v.cname, self.zero(v.ty)))
            else:
                self.emit("%s %s = %s;" % (v.ty.c, v.cname, self.expr_as(s.value, v.ty)))
        elif isinstance(s, A.Assign):
            tgt = self.expr(s.target)
            tt = s.target.ty
            if s.op == "=":
                self.emit("%s = %s;" % (tgt, self.expr_as(s.value, tt)))
            elif tt == T.STR:
                self.emit("%s = fl_str_concat(%s, %s);" % (tgt, tgt, self.expr(s.value)))
            else:
                self.emit("%s %s %s;" % (tgt, s.op, self.expr(s.value)))
        elif isinstance(s, A.ExprStmt):
            self.emit("%s;" % self.expr(s.expr))
        elif isinstance(s, A.If):
            self.gen_if(s, "if")
        elif isinstance(s, A.While):
            self.emit("while (%s)" % self.expr(s.cond))
            self.gen_block(s.body, loop=True)
        elif isinstance(s, A.ForRange):
            v = s.var
            end = self.tmp("_end")
            self.emit("for (%s %s = %s, %s = %s; %s < %s; %s++)" % (
                v.ty.c, v.cname, self.expr_as(s.lo, v.ty), end, self.expr_as(s.hi, v.ty), v.cname, end, v.cname))
            self.gen_block(s.body, loop=True)
        elif isinstance(s, A.ForEach):
            self.gen_foreach(s)
        elif isinstance(s, A.Return):
            self.gen_return(s)
        elif isinstance(s, (A.Break, A.Continue)):
            frames = []
            for fr in reversed(self.frames):
                frames.append(fr)
                if fr["loop"]:
                    break
            self.run_defers(frames)
            self.emit("break;" if isinstance(s, A.Break) else "continue;")
        elif isinstance(s, A.Defer):
            self.frames[-1]["defers"].append(s.stmt)
        elif isinstance(s, A.Block):
            self.gen_block(s)
        elif isinstance(s, A.Match):
            self.gen_match(s)
        else:
            raise AssertionError(s)

    def gen_if(self, s, kw):
        self.emit("%s (%s)" % (kw, self.expr(s.cond)))
        self.gen_block(s.then)
        if isinstance(s.els, A.If):
            self.gen_if_chain(s.els)
        elif s.els is not None:
            self.emit("else")
            self.gen_block(s.els)

    def gen_if_chain(self, s):
        self.emit("else if (%s)" % self.expr(s.cond))
        self.gen_block(s.then)
        if isinstance(s.els, A.If):
            self.gen_if_chain(s.els)
        elif s.els is not None:
            self.emit("else")
            self.gen_block(s.els)

    def gen_return(self, s):
        frames = list(reversed(self.frames))
        has_defers = any(fr["defers"] for fr in frames)
        if s.value is None:
            self.run_defers(frames)
            self.emit("return;")
            return
        val = self.expr_as(s.value, self.fn.ret)
        if not has_defers:
            self.emit("return %s;" % val)
            return
        r = self.tmp("_ret")
        self.emit("{")
        self.ind += 1
        self.emit("%s %s = %s;" % (self.fn.ret.c, r, val))
        self.run_defers(frames)
        self.emit("return %s;" % r)
        self.ind -= 1
        self.emit("}")

    def gen_foreach(self, s):
        it = s.iter.ty
        c = self.tmp("_c")
        i = s.idx_var.cname if s.idx_var else self.tmp("_i")
        ev = s.elem_var
        self.emit("{")
        self.ind += 1
        if isinstance(it, T.Array):
            if s.iter_lvalue:
                self.emit("%s* %s = &(%s);" % (it.c, c, self.expr(s.iter)))
                elem, n = "%s->a[%s]" % (c, i), str(it.n)
            else:
                self.emit("%s %s = %s;" % (it.c, c, self.expr(s.iter)))
                elem, n = "%s.a[%s]" % (c, i), str(it.n)
        elif isinstance(it, T.ListT) and s.iter_lvalue:
            # re-read through a pointer: pushing while iterating stays memory-safe
            self.emit("%s* %s = &(%s);" % (it.c, c, self.expr(s.iter)))
            elem, n = "%s->ptr[%s]" % (c, i), "%s->len" % c
        else:
            self.emit("%s %s = %s;" % (it.c, c, self.expr(s.iter)))
            elem, n = "%s.ptr[%s]" % (c, i), "%s.len" % c
        self.emit("for (int64_t %s = 0; %s < %s; %s++)" % (i, i, n, i))
        self.gen_block(s.body, loop=True, prefix=["%s %s = %s; (void)%s;" % (ev.ty.c, ev.cname, elem, ev.cname)])
        self.ind -= 1
        self.emit("}")

    def gen_match(self, s):
        vt = s.value.ty
        m = self.tmp("_m")
        self.emit("{")
        self.ind += 1
        self.emit("%s %s = %s;" % (vt.c, m, self.expr(s.value)))
        first = True
        default = None
        for arm in s.arms:
            if arm.patterns is None:
                default = arm
                continue
            conds = []
            for p in arm.patterns:
                if vt == T.STR:
                    conds.append("fl_str_eq(%s, %s)" % (m, self.expr(p)))
                else:
                    conds.append("%s == %s" % (m, self.expr(p)))
            self.emit("%s (%s)" % ("if" if first else "else if", " || ".join(conds)))
            self.gen_block(arm.body)
            first = False
        if default is not None:
            if not first:
                self.emit("else")
            self.gen_block(default.body)
        self.ind -= 1
        self.emit("}")

    # ------------------------------------------------------------ expressions
    def is_pure(self, e):
        """Cheap and side-effect free: safe to evaluate twice."""
        if isinstance(e, (A.Ident, A.IntLit)):
            return True
        if isinstance(e, A.Field):
            return e.kind in ("field", "ptr_field") and self.is_pure(e.obj)
        if isinstance(e, A.Unary) and e.op == "*":
            return self.is_pure(e.operand)
        if isinstance(e, A.Index) and e.kind in ("array", "ptr"):
            return self.is_pure(e.obj) and self.is_pure(e.index)
        return False

    def expr_as(self, e, ty):
        return self.expr(e)

    def expr(self, e):
        s = self._expr(e)
        target = getattr(e, "coerce", None)
        if target is not None:
            s = self.coerce(e, s, target)
        return s

    def coerce(self, e, s, target):
        src = e.ty
        if isinstance(src, T.Array):
            if self.is_pure(e):
                return "((%s){(%s).a, %d})" % (target.c, s, src.n)
            t = self.tmp()
            return "({ %s* %s = &(%s); (%s){%s->a, %d}; })" % (src.c, t, s, target.c, t, src.n)
        if isinstance(src, T.ListT):
            if self.is_pure(e):
                return "((%s){(%s).ptr, (%s).len})" % (target.c, s, s)
            t = self.tmp()
            return "({ %s %s = %s; (%s){%s.ptr, %s.len}; })" % (src.c, t, s, target.c, t, t)
        return s

    def _expr(self, e):
        if isinstance(e, A.IntLit):
            return int_lit(e.value, e.ty)
        if isinstance(e, A.FloatLit):
            return float_lit(e.value, e.ty)
        if isinstance(e, A.CharLit):
            c = chr(e.value)
            if 32 <= e.value < 127 and c not in "'\\":
                return "'%s'" % c
            return "((char)%d)" % e.value
        if isinstance(e, A.BoolLit):
            return "true" if e.value else "false"
        if isinstance(e, A.NullLit):
            return "NULL"
        if isinstance(e, A.StrLit):
            return "FL_STR(%s, %d)" % (c_string(e.data), len(e.data))
        if isinstance(e, A.InterpStr):
            sb = self.tmp("_sb")
            return "({ fl_sb %s; fl_sb_init(&%s); %s fl_sb_take(&%s); })" % (sb, sb, self.fmt_parts("&" + sb, e.parts), sb)
        if isinstance(e, A.Ident):
            v = e.sym
            if v.kind == "const":
                return self.const_value(v)
            return v.cname
        if isinstance(e, A.Binary):
            return self.gen_binary(e)
        if isinstance(e, A.Unary):
            return "(%s%s)" % (e.op, self.expr(e.operand))
        if isinstance(e, A.Cast):
            if e.ty == e.expr.ty:
                return self.expr(e.expr)
            return "((%s)(%s))" % (e.ty.c, self.expr(e.expr))
        if isinstance(e, A.Field):
            return self.gen_field(e)
        if isinstance(e, A.Index):
            return self.gen_index(e)
        if isinstance(e, A.SliceExpr):
            return self.gen_slice(e)
        if isinstance(e, A.Call):
            return self.gen_call(e)
        if isinstance(e, A.TypeCall):
            return self.gen_typecall(e)
        if isinstance(e, A.StructLit):
            st = e.ty
            if not e.inits:
                return "((%s){0})" % st.c
            inits = ", ".join(".%s = %s" % (n, self.expr_as(x, st.fields[n])) for (n, x, _) in e.inits)
            return "((%s){%s})" % (st.c, inits)
        if isinstance(e, A.ArrayLit):
            return "((%s){{%s}})" % (e.ty.c, ", ".join(self.expr_as(x, e.ty.elem) for x in e.elems))
        raise AssertionError(e)

    def const_value(self, v):
        val = v.const_value
        if v.ty == T.STR:
            return "FL_STR(%s, %d)" % (c_string(val), len(val))
        if v.ty == T.BOOL:
            return "true" if val else "false"
        if v.ty == T.CHAR:
            return "((char)%d)" % val
        if v.ty.is_float():
            return float_lit(val, v.ty)
        return int_lit(val, v.ty)

    def gen_binary(self, e):
        lt, rt = e.left.ty, e.right.ty
        L, R = self.expr(e.left), self.expr(e.right)
        if lt == T.STR and rt == T.STR:
            if e.op == "+":
                return "fl_str_concat(%s, %s)" % (L, R)
            if e.op == "==":
                return "fl_str_eq(%s, %s)" % (L, R)
            if e.op == "!=":
                return "(!fl_str_eq(%s, %s))" % (L, R)
            return "(fl_str_cmp(%s, %s) %s 0)" % (L, R, e.op)
        return "(%s %s %s)" % (L, e.op, R)

    def gen_field(self, e):
        if e.kind == "enum":
            return "%s_%s" % (e.ty.name, e.name)
        o = self.expr(e.obj)
        if e.kind == "field":
            return "%s.%s" % (o, e.name) if self.is_pure(e.obj) else "(%s).%s" % (o, e.name)
        if e.kind == "ptr_field":
            return "%s->%s" % (o, e.name) if self.is_pure(e.obj) else "(%s)->%s" % (o, e.name)
        ot = e.obj.ty
        if isinstance(ot, T.Array):
            if e.kind == "len":
                return "((int64_t)%d)" % ot.n
            return "(%s).a" % o
        return "(%s).%s" % (o, e.kind)

    def gen_index(self, e):
        o, i = self.expr(e.obj), self.expr(e.index)
        loc = self.loc(e)
        if e.kind == "ptr":
            return "%s[%s]" % (o, i) if self.is_pure(e.obj) else "(%s)[%s]" % (o, i)
        if e.kind == "array":
            return "(%s).a[fl_chk(%s, %d, %s)]" % (o, i, e.obj.ty.n, loc)
        if self.is_pure(e.obj):
            return "(%s).ptr[fl_chk(%s, (%s).len, %s)]" % (o, i, o, loc)
        t = self.tmp()
        return "(*({ %s %s = %s; &%s.ptr[fl_chk(%s, %s.len, %s)]; }))" % (e.obj.ty.c, t, o, t, i, t, loc)

    def gen_slice(self, e):
        o = self.expr(e.obj)
        t, lo, hi = self.tmp(), self.tmp("_lo"), self.tmp("_hi")
        ot = e.obj.ty
        loc = self.loc(e)
        lo_v = self.expr(e.lo) if e.lo is not None else "0"
        if e.kind == "array":
            head = "%s* %s = &(%s);" % (ot.c, t, o)
            ptr, ln = "%s->a" % t, str(ot.n)
        else:
            head = "%s %s = %s;" % (ot.c, t, o)
            ptr, ln = "%s.ptr" % t, "%s.len" % t
        hi_v = self.expr(e.hi) if e.hi is not None else ln
        return "({ %s int64_t %s = %s, %s = %s; fl_chk_range(%s, %s, %s, %s); (%s){%s + %s, %s - %s}; })" % (
            head, lo, lo_v, hi, hi_v, lo, hi, ln, loc, e.ty.c, ptr, lo, hi, lo)

    def gen_args(self, params, args):
        out = [self.expr_as(a, pt) for (pn, pt), a in zip(params, args)]
        out += [self.expr(a) for a in args[len(params):]]
        return out

    def gen_call(self, e):
        k = e.kind
        if k in ("fn", "static"):
            return "%s(%s)" % (e.fn.cname, ", ".join(self.gen_args(e.fn.params, e.args)))
        if k == "method":
            f = e.fn
            obj = e.callee.obj
            args = self.gen_args(f.params, e.args)
            if e.self_mode == "ptr":
                return "%s(%s)" % (f.cname, ", ".join([self.expr(obj)] + args))
            if e.self_mode == "addr":
                return "%s(%s)" % (f.cname, ", ".join(["&(%s)" % self.expr(obj)] + args))
            t = self.tmp()
            return "({ %s %s = %s; %s(%s); })" % (obj.ty.c, t, self.expr(obj), f.cname, ", ".join(["&" + t] + args))
        if k == "list_method":
            lst = e.callee.obj
            m = e.callee.name
            fn = "fl_list_%s_%s" % (lst.ty.elem.mangle, m)
            args = ["&(%s)" % self.expr(lst)] + [self.expr(a) for a in e.args]
            if m in ("pop", "remove", "insert"):
                args.append(self.loc(e))
            return "%s(%s)" % (fn, ", ".join(args))
        if k == "str_method":
            args = [self.expr(e.callee.obj)] + [self.expr(a) for a in e.args]
            return "fl_str_%s(%s)" % (e.callee.name, ", ".join(args))
        if k == "builtin":
            return self.gen_builtin(e)
        raise AssertionError(k)

    def gen_builtin(self, e):
        name = e.callee.name
        a = e.args
        if name in ("print", "println", "eprint", "eprintln"):
            sb = self.tmp("_sb")
            stmts = []
            for i, x in enumerate(a):
                if i:
                    stmts.append("fl_sb_char(&%s, ' ');" % sb)
                if isinstance(x, A.InterpStr):
                    stmts.append(self.fmt_parts("&" + sb, x.parts))
                elif isinstance(x, A.StrLit):
                    stmts.append(self.fmt_parts("&" + sb, [x.data]))
                else:
                    stmts.append(self.fmt_value(x.ty, "&" + sb, self.expr(x)))
            if name.endswith("ln"):
                stmts.append("fl_sb_char(&%s, '\\n');" % sb)
            stream = "stderr" if name.startswith("e") else "stdout"
            return "({ fl_sb %s; fl_sb_init(&%s); %s fl_sb_flush(&%s, %s); })" % (sb, sb, " ".join(stmts), sb, stream)
        if name == "assert":
            msg = self.expr(a[1]) if len(a) > 1 else "FL_STR(\"\", 0)"
            src = "assert"
            return "((%s) ? (void)0 : fl_assert_fail(%s, %s, %s))" % (self.expr(a[0]), self.loc(e), c_string(src.encode()), msg)
        if name == "panic":
            return "fl_panic_str(%s, %s)" % (self.loc(e), self.expr(a[0]))
        if name == "exit":
            return "exit((int)(%s))" % self.expr(a[0])
        if name == "free":
            t = a[0].ty
            if t.is_ptr():
                return "free(%s)" % self.expr(a[0])
            if isinstance(t, T.ListT):
                return "fl_list_%s_free(&(%s))" % (t.elem.mangle, self.expr(a[0]))
            return "free((%s).ptr)" % self.expr(a[0])
        if name == "args":
            return "fl_args"
        simple = {"read_line": "fl_read_line", "parse_int": "fl_parse_int", "parse_float": "fl_parse_float",
                  "time_ns": "fl_time_ns", "read_file": "fl_read_file", "write_file": "fl_write_file"}
        return "%s(%s)" % (simple[name], ", ".join(self.expr(x) for x in a))

    def gen_typecall(self, e):
        if e.name == "sizeof":
            return "((int64_t)sizeof(%s))" % self.ck.resolve(e.type).c
        t = e.ty
        if e.name == "new":
            p = self.tmp("_p")
            if e.args:
                return "({ %s %s = (%s)fl_alloc(sizeof(%s)); *%s = %s; %s; })" % (
                    t.c, p, t.c, t.elem.c, p, self.expr_as(e.args[0], t.elem), p)
            return "((%s)fl_alloc(sizeof(%s)))" % (t.c, t.elem.c)
        if e.name == "make":
            n = self.tmp("_n")
            if isinstance(t, T.Slice):
                return "({ int64_t %s = %s; (%s){(%s*)fl_alloc(%s * (int64_t)sizeof(%s)), %s}; })" % (
                    n, self.expr(e.args[0]), t.c, t.elem.c, n, t.elem.c, n)
            return "({ int64_t %s = %s; (%s){(%s*)fl_alloc(%s * (int64_t)sizeof(%s)), 0, %s}; })" % (
                n, self.expr(e.args[0]), t.c, t.elem.c, n, t.elem.c, n)
        raise AssertionError(e.name)


def generate(ck, debug_lines=False):
    return CGen(ck, debug_lines).generate()
