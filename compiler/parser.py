"""Recursive-descent parser: tokens -> AST (see ast.py).

Operator precedence, lowest to highest:
    ||
    &&
    == != < > <= >=
    |
    ^
    &
    << >>
    + -
    * / %
    as            (cast:  x as f64)
    unary         - ! ~ & *
    postfix       call()  index[]  slice[a..b]  .field
Bitwise operators bind tighter than comparisons (unlike C), so
`x & 1 == 0` means `(x & 1) == 0`.
"""

from . import ast as A
from .errors import CompileError
from .lexer import lex, decode_escapes

ASSIGN_OPS = {"=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">>="}

BINARY_PREC = [
    ["||"],
    ["&&"],
    ["==", "!=", "<", ">", "<=", ">="],
    ["|"],
    ["^"],
    ["&"],
    ["<<", ">>"],
    ["+", "-"],
    ["*", "/", "%"],
]


class Parser:
    def __init__(self, toks):
        self.toks = toks
        self.pos = 0
        self.no_struct = False  # disallow `Name {` struct literals (in if/while/for/match headers)

    # ------------------------------------------------------------ helpers
    @property
    def tok(self):
        return self.toks[self.pos]

    def peek(self, k=1):
        return self.toks[min(self.pos + k, len(self.toks) - 1)]

    def next(self):
        t = self.toks[self.pos]
        if t.kind != "EOF":
            self.pos += 1
        return t

    def at(self, value, kind=None):
        t = self.tok
        if kind is None:
            return t.kind in ("PUNCT", "KW") and t.value == value
        return t.kind == kind and (value is None or t.value == value)

    def accept(self, value):
        if self.at(value):
            return self.next()
        return None

    def error(self, msg, tok=None):
        tok = tok or self.tok
        raise CompileError(msg, tok.file, tok.line, tok.col)

    def describe(self, t):
        if t.kind == "EOF":
            return "end of file"
        if t.kind == "PUNCT" and t.value == ";":
            return "end of line"
        return repr(t.value) if t.kind not in ("STRING", "RAWSTRING") else "string"

    def expect(self, value):
        if not self.at(value):
            self.error("expected %r, found %s" % (value, self.describe(self.tok)))
        return self.next()

    def expect_ident(self, what="identifier"):
        if self.tok.kind != "IDENT":
            self.error("expected %s, found %s" % (what, self.describe(self.tok)))
        return self.next()

    def skip_semis(self):
        while self.at(";"):
            self.next()

    def expect_lbrace(self):
        # tolerate a newline before '{' (Allman style)
        if self.at(";") and self.peek().is_("PUNCT", "{"):
            self.next()
        return self.expect("{")

    def end_stmt(self):
        if self.accept(";"):
            return
        if self.at("}") or self.tok.kind == "EOF":
            return
        if self.pos > 0 and self.toks[self.pos - 1].is_("PUNCT", "}"):
            return  # `if x { ... } return y` on one line
        self.error("expected end of statement, found %s" % self.describe(self.tok))

    # ------------------------------------------------------------ program
    def parse_program(self):
        decls = []
        self.skip_semis()
        while self.tok.kind != "EOF":
            decls.append(self.parse_top())
            self.skip_semis()
        return decls

    def parse_top(self):
        t = self.tok
        if self.at("fn"):
            return self.parse_fn(None)
        if self.at("extern"):
            self.next()
            if not self.at("fn"):
                self.error("expected 'fn' after 'extern'")
            f = self.parse_fn(None, is_extern=True)
            return f
        if self.at("struct"):
            return self.parse_struct()
        if self.at("enum"):
            return self.parse_enum()
        if self.at("impl"):
            return self.parse_impl()
        if self.at("let") or self.at("var") or self.at("const"):
            is_const = self.at("const")
            d = self.parse_var_decl()
            self.end_stmt()
            return A.GlobalDecl(t, d, is_const)
        if self.at("import"):
            self.next()
            s = self.next()
            if s.kind != "STRING":
                self.error("expected string after import", s)
            self.end_stmt()
            return ("import", s.value, s)
        if self.at("cinclude"):
            self.next()
            s = self.next()
            if s.kind != "STRING":
                self.error("expected header name string after cinclude", s)
            self.end_stmt()
            return A.CInclude(t, s.value)
        if self.at("link"):
            self.next()
            s = self.next()
            if s.kind != "STRING":
                self.error("expected library name string after link", s)
            self.end_stmt()
            return A.Link(t, s.value)
        self.error("expected a declaration (fn, struct, enum, impl, let, var, const, import), found %s" % self.describe(t))

    def parse_fn(self, owner, is_extern=False):
        t = self.expect("fn")
        name_tok = self.expect_ident("function name")
        name = name_tok.value
        self.expect("(")
        params = []
        has_self = False
        variadic = False
        while not self.at(")"):
            if self.at("..."):
                if not is_extern:
                    self.error("variadic '...' is only allowed on extern functions")
                self.next()
                variadic = True
                break
            pt = self.expect_ident("parameter name")
            if pt.value == "self" and not params and not has_self:
                if owner is None:
                    self.error("'self' parameter outside of impl block", pt)
                has_self = True
            else:
                self.expect(":")
                params.append(A.Param(pt, pt.value, self.parse_type()))
            if not self.accept(","):
                break
        self.expect(")")
        ret = None
        if self.accept("->"):
            ret = self.parse_type()
        body = None
        if is_extern:
            self.end_stmt()
        else:
            body = self.parse_block()
        d = A.FnDecl(t, name, params, ret, body, owner, has_self, variadic, is_extern)
        d.name_tok = name_tok
        d.end_line = body.end_line if body else t.line
        return d

    def parse_struct(self):
        t = self.expect("struct")
        name_tok = self.expect_ident("struct name")
        name = name_tok.value
        self.expect_lbrace()
        fields = []
        self.skip_seps()
        while not self.at("}"):
            ft = self.expect_ident("field name")
            self.expect(":")
            fields.append(A.Param(ft, ft.value, self.parse_type()))
            if not (self.at(",") or self.at(";") or self.at("}")):
                self.error("expected ',' or newline after field")
            self.skip_seps()
        end = self.expect("}")
        d = A.StructDecl(t, name, fields)
        d.name_tok, d.end_line = name_tok, end.line
        return d

    def skip_seps(self):
        while self.at(",") or self.at(";"):
            self.next()

    def parse_enum(self):
        t = self.expect("enum")
        name_tok = self.expect_ident("enum name")
        name = name_tok.value
        self.expect_lbrace()
        variants = []
        self.skip_seps()
        while not self.at("}"):
            vt = self.expect_ident("enum variant")
            val = None
            if self.accept("="):
                val = self.parse_expr()
            variants.append((vt.value, val, vt))
            if not (self.at(",") or self.at(";") or self.at("}")):
                self.error("expected ',' or newline after enum variant")
            self.skip_seps()
        end = self.expect("}")
        d = A.EnumDecl(t, name, variants)
        d.name_tok, d.end_line = name_tok, end.line
        return d

    def parse_impl(self):
        t = self.expect("impl")
        name_tok = self.expect_ident("type name")
        name = name_tok.value
        self.expect_lbrace()
        methods = []
        self.skip_semis()
        while not self.at("}"):
            methods.append(self.parse_fn(name))
            self.skip_semis()
        end = self.expect("}")
        d = A.ImplDecl(t, name, methods)
        d.name_tok, d.end_line = name_tok, end.line
        return d

    # ------------------------------------------------------------ types
    def parse_type(self):
        t = self.tok
        if self.accept("*"):
            return A.TPtr(t, self.parse_type())
        if self.accept("["):
            if self.accept("]"):
                return A.TSlice(t, self.parse_type())
            size = self.parse_expr()
            self.expect("]")
            return A.TArray(t, size, self.parse_type())
        if t.kind == "IDENT":
            self.next()
            if t.value == "list" and self.at("["):
                self.next()
                elem = self.parse_type()
                self.expect("]")
                return A.TList(t, elem)
            return A.TName(t, t.value)
        self.error("expected a type, found %s" % self.describe(t))

    # ------------------------------------------------------------ statements
    def parse_block(self):
        t = self.expect_lbrace()
        stmts = []
        self.skip_semis()
        while not self.at("}"):
            if self.tok.kind == "EOF":
                self.error("unexpected end of file, missing '}'")
            stmts.append(self.parse_stmt())
            self.skip_semis()
        end = self.expect("}")
        b = A.Block(t, stmts)
        b.end_line = end.line
        return b

    def parse_var_decl(self):
        t = self.next()  # let / var / const
        mutable = t.value == "var"
        name_tok = self.expect_ident("variable name")
        name = name_tok.value
        ty = None
        if self.accept(":"):
            ty = self.parse_type()
        val = None
        if self.accept("="):
            val = self.parse_expr()
        if ty is None and val is None:
            self.error("variable '%s' needs a type or an initial value" % name, t)
        if t.value in ("let", "const") and val is None:
            self.error("'%s' requires an initial value (use 'var' for zero-initialized variables)" % t.value, t)
        d = A.VarDecl(t, name, ty, val, mutable)
        d.name_tok = name_tok
        return d

    def parse_stmt(self):
        t = self.tok
        if self.at("let") or self.at("var") or self.at("const"):
            d = self.parse_var_decl()
            self.end_stmt()
            return d
        if self.at("if"):
            s = self.parse_if()
            self.end_stmt()
            return s
        if self.at("while"):
            self.next()
            cond = self.parse_header_expr()
            body = self.parse_block()
            self.end_stmt()
            return A.While(t, cond, body)
        if self.at("for"):
            s = self.parse_for()
            self.end_stmt()
            return s
        if self.at("return"):
            self.next()
            val = None
            if not (self.at(";") or self.at("}")):
                val = self.parse_expr()
            self.end_stmt()
            return A.Return(t, val)
        if self.at("break"):
            self.next()
            self.end_stmt()
            return A.Break(t)
        if self.at("continue"):
            self.next()
            self.end_stmt()
            return A.Continue(t)
        if self.at("defer"):
            self.next()
            if self.at("{"):
                inner = self.parse_block()
                self.end_stmt()
            else:
                inner = self.parse_stmt()
            if isinstance(inner, (A.Return, A.Break, A.Continue, A.Defer)):
                self.error("cannot defer return/break/continue/defer", t)
            return A.Defer(t, inner)
        if self.at("match"):
            s = self.parse_match()
            self.end_stmt()
            return s
        if self.at("{"):
            b = self.parse_block()
            self.end_stmt()
            return b
        return self.parse_simple_stmt()

    def parse_simple_stmt(self):
        t = self.tok
        e = self.parse_expr()
        if self.tok.kind == "PUNCT" and self.tok.value in ASSIGN_OPS:
            op = self.next().value
            v = self.parse_expr()
            self.end_stmt()
            return A.Assign(t, op, e, v)
        self.end_stmt()
        return A.ExprStmt(t, e)

    def parse_header_expr(self):
        old = self.no_struct
        self.no_struct = True
        try:
            return self.parse_expr()
        finally:
            self.no_struct = old

    def parse_if(self):
        t = self.expect("if")
        cond = self.parse_header_expr()
        then = self.parse_block()
        els = None
        # allow `}` and `else` on separate lines
        if self.at(";") and self.peek().is_("KW", "else"):
            self.next()
        if self.accept("else"):
            if self.at("if"):
                els = self.parse_if()
            else:
                els = self.parse_block()
        return A.If(t, cond, then, els)

    def parse_for(self):
        t = self.expect("for")
        first = self.expect_ident("loop variable")
        second = None
        if self.accept(","):
            second = self.expect_ident("loop variable")
        self.expect("in")
        lo = self.parse_header_expr()
        if self.accept(".."):
            if second:
                self.error("range loops take a single variable", second)
            hi = self.parse_header_expr()
            body = self.parse_block()
            s = A.ForRange(t, first.value, lo, hi, body)
            s.var_tok = first
            return s
        body = self.parse_block()
        if second:
            s = A.ForEach(t, first.value, second.value, lo, body)
            s.idx_tok, s.var_tok = first, second
        else:
            s = A.ForEach(t, None, first.value, lo, body)
            s.idx_tok, s.var_tok = None, first
        return s

    def parse_match(self):
        t = self.expect("match")
        val = self.parse_header_expr()
        self.expect_lbrace()
        arms = []
        self.skip_semis()
        while not self.at("}"):
            at = self.tok
            if self.tok.is_("IDENT", "_"):
                self.next()
                pats = None
            else:
                pats = [self.parse_expr()]
                while self.accept(","):
                    pats.append(self.parse_expr())
            self.expect("=>")
            if self.at("{"):
                body = self.parse_block()
                self.end_stmt()
            else:
                body = A.Block(self.tok, [self.parse_stmt()])
            arms.append(A.MatchArm(at, pats, body))
            self.skip_semis()
        self.expect("}")
        return A.Match(t, val, arms)

    # ------------------------------------------------------------ expressions
    def parse_expr(self):
        return self.parse_binary(0)

    def parse_binary(self, level):
        if level == len(BINARY_PREC):
            return self.parse_cast()
        left = self.parse_binary(level + 1)
        ops = BINARY_PREC[level]
        while self.tok.kind == "PUNCT" and self.tok.value in ops:
            op = self.next()
            right = self.parse_binary(level + 1)
            left = A.Binary(op, op.value, left, right)
        return left

    def parse_cast(self):
        e = self.parse_unary()
        while self.at("as"):
            t = self.next()
            e = A.Cast(t, e, self.parse_type())
        return e

    def parse_unary(self):
        t = self.tok
        if t.kind == "PUNCT" and t.value in ("-", "!", "~", "&", "*"):
            self.next()
            operand = self.parse_unary()
            # fold negative literals so `-128 as i8` and i64 min work nicely
            if t.value == "-" and isinstance(operand, (A.IntLit, A.FloatLit)):
                operand.value = -operand.value
                operand.line, operand.col = t.line, t.col
                return operand
            return A.Unary(t, t.value, operand)
        return self.parse_postfix()

    def parse_postfix(self):
        e = self.parse_primary()
        while True:
            t = self.tok
            if self.at("("):
                self.next()
                args = []
                while not self.at(")"):
                    args.append(self.parse_expr_nested())
                    if not self.accept(","):
                        break
                self.expect(")")
                e = A.Call(t, e, args)
            elif self.at("["):
                self.next()
                if self.accept(".."):
                    hi = None if self.at("]") else self.parse_expr_nested()
                    self.expect("]")
                    e = A.SliceExpr(t, e, None, hi)
                    continue
                idx = self.parse_expr_nested()
                if self.accept(".."):
                    hi = None if self.at("]") else self.parse_expr_nested()
                    self.expect("]")
                    e = A.SliceExpr(t, e, idx, hi)
                    continue
                self.expect("]")
                e = A.Index(t, e, idx)
            elif self.at("."):
                self.next()
                name = self.expect_ident("field or method name")
                e = A.Field(name, e, name.value)
            else:
                return e

    def parse_expr_nested(self):
        old = self.no_struct
        self.no_struct = False
        try:
            return self.parse_expr()
        finally:
            self.no_struct = old

    def parse_primary(self):
        t = self.tok
        if t.kind == "INT":
            self.next()
            return A.IntLit(t, t.value)
        if t.kind == "FLOAT":
            self.next()
            return A.FloatLit(t, t.value)
        if t.kind == "CHAR":
            self.next()
            return A.CharLit(t, t.value)
        if t.kind == "STRING":
            self.next()
            return self.parse_string(t)
        if t.kind == "RAWSTRING":
            self.next()
            return A.StrLit(t, t.value.encode("utf-8"))
        if self.at("true") or self.at("false"):
            self.next()
            return A.BoolLit(t, t.value == "true")
        if self.at("null"):
            self.next()
            return A.NullLit(t)
        if self.at("("):
            self.next()
            e = self.parse_expr_nested()
            self.expect(")")
            return e
        if self.at("["):
            self.next()
            elems = []
            while not self.at("]"):
                elems.append(self.parse_expr_nested())
                if not self.accept(","):
                    break
            self.expect("]")
            return A.ArrayLit(t, elems)
        if t.kind == "IDENT":
            self.next()
            if t.value in ("sizeof", "new", "make") and self.at("("):
                self.next()
                ty = self.parse_type()
                args = []
                while self.accept(","):
                    args.append(self.parse_expr_nested())
                self.expect(")")
                return A.TypeCall(t, t.value, ty, args)
            if self.at("{") and not self.no_struct:
                return self.parse_struct_lit(t)
            return A.Ident(t, t.value)
        self.error("expected an expression, found %s" % self.describe(t))

    def parse_struct_lit(self, name_tok):
        self.expect("{")
        inits = []
        self.skip_seps()
        while not self.at("}"):
            ft = self.expect_ident("field name")
            self.expect(":")
            inits.append((ft.value, self.parse_expr_nested(), ft))
            if not (self.at(",") or self.at(";") or self.at("}")):
                self.error("expected ',' after field value")
            self.skip_seps()
        self.expect("}")
        return A.StructLit(name_tok, name_tok.value, inits)

    def parse_string(self, t):
        """Split "a {expr} b {x:.2}" into literal bytes and FmtArg parts."""
        raw = t.value
        parts = []
        lit = []
        i = 0

        def mkerr(m):
            return CompileError(m, t.file, t.line, t.col)

        def flush():
            if lit:
                parts.append(decode_escapes("".join(lit), mkerr))
                lit.clear()

        while i < len(raw):
            c = raw[i]
            if c == "\\" and i + 1 < len(raw):
                lit.append(raw[i:i + 2])
                i += 2
            elif raw.startswith("{{", i):
                lit.append("{")
                i += 2
            elif raw.startswith("}}", i):
                lit.append("}")
                i += 2
            elif c == "{":
                depth, j = 1, i + 1
                while j < len(raw) and depth:
                    if raw[j] == "{":
                        depth += 1
                    elif raw[j] == "}":
                        depth -= 1
                    j += 1
                if depth:
                    raise mkerr("unclosed '{' in string (use '{{' for a literal brace)")
                inner = raw[i + 1:j - 1]
                flush()
                parts.append(self.parse_interp(inner, t, i))
                i = j
            elif c == "}":
                raise mkerr("unmatched '}' in string (use '}}' for a literal brace)")
            else:
                lit.append(c)
                i += 1
        flush()
        if all(isinstance(p, bytes) for p in parts):
            return A.StrLit(t, b"".join(parts))
        return A.InterpStr(t, parts)

    def parse_interp(self, inner, t, offset):
        spec = None
        k = inner.rfind(":")
        if k >= 0:
            import re
            cand = inner[k + 1:].strip()
            if re.fullmatch(r"-?0?\d*(\.\d+)?[xXobe]?", cand) and cand:
                spec = cand
                inner = inner[:k]
        if not inner.strip():
            raise CompileError("empty {} in string (use '{{' for a literal brace)", t.file, t.line, t.col)
        try:
            toks = lex(inner, t.file)
        except CompileError as e:
            raise CompileError("in string interpolation {%s}: %s (write '{{' for a literal brace, "
                               "or use a `raw string`)" % (inner.strip(), e.msg), t.file, t.line, t.col)
        for tk in toks:
            tk.line = t.line
            tk.col = t.col + offset + 1 + (tk.col - 1)
        # lexer may append ';' at end
        toks = [tk for tk in toks if not (tk.kind == "PUNCT" and tk.value == ";")]
        sub = Parser(toks)
        e = sub.parse_expr()
        if sub.tok.kind != "EOF":
            sub.error("unexpected %s in string interpolation" % sub.describe(sub.tok))
        return A.FmtArg(t, e, spec)


def parse(src, file):
    p = Parser(lex(src, file))
    return p.parse_program()
