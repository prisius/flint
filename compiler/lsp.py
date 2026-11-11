"""Flint language server (LSP over stdio, no dependencies).   Start with: flint lsp

Features: diagnostics (errors + lint warnings), hover, go-to-definition,
find references, completion (incl. `.` members), document symbols, formatting.

How it works: on every open/change the whole program (file + imports, using the
unsaved buffer contents) is parsed and type-checked in "lib mode" (no main()
required). The checker records a `Ref` for every name occurrence (see
checker.Ref): position, hover text, definition position and type. All
position-based features are lookups in that list.
"""

import json
import os
import re
import sys
import traceback
from urllib.parse import quote, unquote, urlparse

from . import ast as A
from . import types as T
from .checker import BUILTINS, BUILTIN_DOCS, LIST_METHODS, STR_METHODS, Checker
from .driver import load_program
from .errors import CompileError
from .formatter import format_source
from .lexer import KEYWORDS
from .parser import parse

# LSP enums
SEV_ERROR, SEV_WARNING = 1, 2
CK = {"method": 2, "function": 3, "field": 5, "variable": 6, "class": 7, "keyword": 14,
      "enum": 13, "enum_member": 20, "constant": 21, "struct": 22, "type": 25}
SK = {"class": 5, "method": 6, "field": 8, "enum": 10, "function": 12, "constant": 14,
      "variable": 13, "struct": 23, "enum_member": 22}

TYPE_NAMES = list(T.C_PRIM) + list(T.ALIASES) + ["str", "list"]


def uri_to_path(uri):
    return os.path.abspath(unquote(urlparse(uri).path))


def path_to_uri(path):
    return "file://" + quote(os.path.abspath(path))


def log(*a):
    print("[flint-lsp]", *a, file=sys.stderr, flush=True)


class Analysis:
    def __init__(self):
        self.refs = []
        self.checker = None
        self.files = set()


class Server:
    def __init__(self, inp, out):
        self.inp, self.out = inp, out
        self.docs = {}          # uri -> text
        self.analyses = {}      # uri -> Analysis
        self.published = {}     # root uri -> set of uris it published diagnostics to
        self.last_symbols = {}  # uri -> symbols from the last parseable version
        self.running = True

    # ------------------------------------------------------------ transport
    def read_message(self):
        length = None
        while True:
            line = self.inp.readline()
            if not line:
                return None
            line = line.decode("ascii", "replace").strip()
            if not line:
                break
            if line.lower().startswith("content-length:"):
                length = int(line.split(":", 1)[1])
        if length is None:
            return None
        return json.loads(self.inp.read(length).decode("utf-8"))

    def send(self, msg):
        msg["jsonrpc"] = "2.0"
        body = json.dumps(msg).encode("utf-8")
        self.out.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
        self.out.flush()

    def notify(self, method, params):
        self.send({"method": method, "params": params})

    def serve(self):
        while self.running:
            msg = self.read_message()
            if msg is None:
                break
            method, mid = msg.get("method"), msg.get("id")
            try:
                result = self.dispatch(method, msg.get("params") or {})
                if mid is not None:
                    self.send({"id": mid, "result": result})
            except Exception as e:  # never crash the editor session
                log("error handling", method, "\n" + traceback.format_exc())
                if mid is not None:
                    self.send({"id": mid, "error": {"code": -32603, "message": str(e)}})
        return 0

    def dispatch(self, method, p):
        if method == "initialize":
            return {
                "capabilities": {
                    "textDocumentSync": {"openClose": True, "change": 1, "save": {"includeText": False}},
                    "hoverProvider": True,
                    "definitionProvider": True,
                    "referencesProvider": True,
                    "documentSymbolProvider": True,
                    "documentFormattingProvider": True,
                    "completionProvider": {"triggerCharacters": ["."]},
                },
                "serverInfo": {"name": "flint-lsp", "version": "0.1"},
            }
        if method == "shutdown":
            return None
        if method == "exit":
            self.running = False
            return None
        if method == "textDocument/didOpen":
            td = p["textDocument"]
            self.docs[td["uri"]] = td["text"]
            self.analyze(td["uri"])
        elif method == "textDocument/didChange":
            uri = p["textDocument"]["uri"]
            self.docs[uri] = p["contentChanges"][-1]["text"]
            self.analyze(uri)
        elif method == "textDocument/didSave":
            self.analyze(p["textDocument"]["uri"])
        elif method == "textDocument/didClose":
            uri = p["textDocument"]["uri"]
            self.docs.pop(uri, None)
            self.analyses.pop(uri, None)
            for u in self.published.pop(uri, set()):
                self.notify("textDocument/publishDiagnostics", {"uri": u, "diagnostics": []})
        elif method == "textDocument/hover":
            return self.hover(p)
        elif method == "textDocument/definition":
            return self.definition(p)
        elif method == "textDocument/references":
            return self.references(p)
        elif method == "textDocument/documentSymbol":
            return self.symbols(p)
        elif method == "textDocument/formatting":
            return self.formatting(p)
        elif method == "textDocument/completion":
            return self.completion(p)
        return None

    # ------------------------------------------------------------ analysis
    def text_of(self, path):
        uri = path_to_uri(path)
        if uri in self.docs:
            return self.docs[uri]
        try:
            with open(path, encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""

    def word_range(self, path, line, col):
        """0-based LSP range for the word starting at 1-based (line, col)."""
        lines = self.text_of(path).split("\n")
        l0, c0 = max(line - 1, 0), max(col - 1, 0)
        text = lines[l0] if l0 < len(lines) else ""
        end = c0
        while end < len(text) and (text[end].isalnum() or text[end] == "_"):
            end += 1
        if end == c0:
            end = min(c0 + 1, max(len(text), c0 + 1))
        return {"start": {"line": l0, "character": c0}, "end": {"line": l0, "character": end}}

    def analyze(self, uri):
        path = uri_to_path(uri)
        overlay = {uri_to_path(u): t for u, t in self.docs.items()}
        diags = {}
        old = self.analyses.get(uri)
        an = Analysis()
        err_line = None

        def add(path_, e, sev):
            p_ = os.path.abspath(e.file) if e.file else path
            line = e.line if e.file else 1
            d = {"range": self.word_range(p_, line, e.col or 1), "severity": sev,
                 "source": "flint", "message": e.msg}
            diags.setdefault(p_, []).append(d)

        try:
            decls = load_program(path, overlay, abs_paths=True)
            ck = Checker(decls, lib_mode=True)
            an.checker = ck
            try:
                ck.check()
                for w in ck.warnings:
                    add(None, w, SEV_WARNING)
            except CompileError as e:
                add(None, e, SEV_ERROR)
                if e.file and os.path.abspath(e.file) == path:
                    err_line = e.line
            an.refs = ck.refs
        except CompileError as e:  # parse error: keep everything from the last good run
            add(None, e, SEV_ERROR)
            if old:
                an.checker, an.refs = old.checker, old.refs
        if err_line is not None and old:
            # keep stale info for the part of the file the checker never reached
            have = {(r.file, r.line, r.col) for r in an.refs}
            an.refs = an.refs + [r for r in old.refs if r.file == path and r.line >= err_line
                                 and (r.file, r.line, r.col) not in have]
        self.analyses[uri] = an

        uris = {path_to_uri(p_) for p_ in diags} | {uri}
        for u in self.published.get(uri, set()) - uris:
            self.notify("textDocument/publishDiagnostics", {"uri": u, "diagnostics": []})
        for u in uris:
            self.notify("textDocument/publishDiagnostics", {"uri": u, "diagnostics": diags.get(uri_to_path(u), [])})
        self.published[uri] = uris

    def ref_at(self, p):
        uri = p["textDocument"]["uri"]
        an = self.analyses.get(uri)
        if not an:
            return None, None
        path = uri_to_path(uri)
        line, col = p["position"]["line"] + 1, p["position"]["character"] + 1
        best = None
        for r in an.refs:
            if r.file == path and r.line == line and r.col <= col <= r.col + r.length:
                if best is None or r.col >= best.col:
                    best = r
        return best, an

    def loc(self, file, line, col, length):
        return {"uri": path_to_uri(file), "range": {
            "start": {"line": line - 1, "character": col - 1},
            "end": {"line": line - 1, "character": col - 1 + length}}}

    # ------------------------------------------------------------ features
    def hover(self, p):
        r, _ = self.ref_at(p)
        if r is None or not r.hover:
            return None
        code, _, doc = r.hover.partition("\n\n")
        value = "```flint\n%s\n```" % code
        if doc:
            value += "\n\n" + doc
        return {"contents": {"kind": "markdown", "value": value}}

    def definition(self, p):
        r, _ = self.ref_at(p)
        if r is None or r.defpos is None:
            return None
        f, l, c = r.defpos
        return self.loc(f, l, c, r.length)

    def references(self, p):
        r, an = self.ref_at(p)
        if r is None:
            return []
        target = r.defpos or (r.file, r.line, r.col)
        include_decl = p.get("context", {}).get("includeDeclaration", True)
        out, seen = [], set()
        for x in an.refs:
            if x.defpos == target:
                is_decl = (x.file, x.line, x.col) == target
                key = (x.file, x.line, x.col)
                if (include_decl or not is_decl) and key not in seen:
                    seen.add(key)
                    out.append(self.loc(x.file, x.line, x.col, x.length))
        return out

    def formatting(self, p):
        uri = p["textDocument"]["uri"]
        text = self.docs.get(uri)
        if text is None:
            return []
        new = format_source(text)
        if new == text:
            return []
        n = text.count("\n")
        return [{"range": {"start": {"line": 0, "character": 0}, "end": {"line": n + 1, "character": 0}},
                 "newText": new}]

    def symbols(self, p):
        uri = p["textDocument"]["uri"]
        path = uri_to_path(uri)
        text = self.docs.get(uri, "")
        lines = text.split("\n")
        try:
            decls = parse(text, path)
        except CompileError:
            return self.last_symbols.get(uri, [])

        def rng(l1, c1, l2=None):
            l2 = l1 if l2 is None else l2
            end_char = len(lines[l2 - 1]) if 0 < l2 <= len(lines) else 0
            return {"start": {"line": l1 - 1, "character": max(c1 - 1, 0)}, "end": {"line": l2 - 1, "character": end_char}}

        def sel(tok, name):
            return {"start": {"line": tok.line - 1, "character": tok.col - 1},
                    "end": {"line": tok.line - 1, "character": tok.col - 1 + len(name)}}

        def sym(name, kind, node, tok, end_line=None, children=None, detail=None):
            s = {"name": name, "kind": kind, "range": rng(node.line, node.col, end_line or tok.line),
                 "selectionRange": sel(tok, name)}
            if children:
                s["children"] = children
            if detail:
                s["detail"] = detail
            return s

        out = []
        for d in decls:
            if isinstance(d, A.FnDecl):
                out.append(sym(d.name, SK["function"], d, d.name_tok, d.end_line))
            elif isinstance(d, A.StructDecl):
                kids = [sym(f.name, SK["field"], f, f) for f in d.fields]
                out.append(sym(d.name, SK["struct"], d, d.name_tok, d.end_line, kids))
            elif isinstance(d, A.EnumDecl):
                kids = [sym(n, SK["enum_member"], t, t) for (n, _, t) in d.variants]
                out.append(sym(d.name, SK["enum"], d, d.name_tok, d.end_line, kids))
            elif isinstance(d, A.ImplDecl):
                kids = [sym(m.name, SK["method"], m, m.name_tok, m.end_line) for m in d.methods]
                out.append(sym("impl " + d.name, SK["class"], d, d.name_tok, d.end_line, kids))
            elif isinstance(d, A.GlobalDecl):
                vd = d.decl
                out.append(sym(vd.name, SK["constant"] if d.is_const else SK["variable"], vd, vd.name_tok))
        self.last_symbols[uri] = out
        return out

    # ------------------------------------------------------------ completion
    def completion(self, p):
        uri = p["textDocument"]["uri"]
        an = self.analyses.get(uri)
        text = self.docs.get(uri, "")
        lines = text.split("\n")
        l0, c0 = p["position"]["line"], p["position"]["character"]
        prefix = lines[l0][:c0] if l0 < len(lines) else ""
        ck = an.checker if an else None
        path = uri_to_path(uri)

        m = re.search(r"([A-Za-z_][\w.\[\]()]*)\.(\w*)$", prefix)
        if m and ck is not None and not re.search(r"\d\.$", prefix[:m.end(1) + 1]):
            return {"isIncomplete": False, "items": self.member_items(m.group(1), ck, an, path, l0 + 1)}

        items, seen = [], set()

        def add(label, kind, detail=None, doc=None):
            if label in seen:
                return
            seen.add(label)
            it = {"label": label, "kind": kind}
            if detail:
                it["detail"] = detail
            if doc:
                it["documentation"] = {"kind": "markdown", "value": doc}
            items.append(it)

        if an:
            for name, kind, detail in self.locals_before(an, path, l0 + 1):
                add(name, kind, detail)
        if ck is not None:
            for f in ck.fns.values():
                add(f.name, CK["function"], Checker.fn_sig(f))
            for s in ck.structs.values():
                add(s.name, CK["struct"], "struct")
            for e in ck.enums.values():
                add(e.name, CK["enum"], "enum")
            for v in ck.globals.values():
                add(v.name, CK["constant"] if v.kind == "const" else CK["variable"], Checker.var_hover(v))
        for b in sorted(BUILTINS | {"sizeof", "new", "make"}):
            sig, _, doc = BUILTIN_DOCS.get(b, b).partition("\n\n")
            add(b, CK["function"], sig, doc or None)
        for t in TYPE_NAMES:
            add(t, CK["type"], "type")
        for k in sorted(KEYWORDS):
            add(k, CK["keyword"])
        return {"isIncomplete": False, "items": items}

    def locals_before(self, an, path, line):
        """Variables/params declared earlier in the function that encloses `line`."""
        ck = an.checker
        fn_start = 0
        if ck is not None:
            for f in ck.fn_list:
                d = f.decl
                if d.body is not None and d.file == path and d.line <= line <= getattr(d, "end_line", d.line):
                    fn_start = d.line
        out = []
        if not fn_start:
            return out
        for r in an.refs:
            if r.file != path or not (fn_start <= r.line <= line) or r.defpos != (r.file, r.line, r.col):
                continue
            if r.hover.startswith(("let ", "var ")):
                out.append((r.name, CK["variable"], r.hover))
            elif r.hover.startswith(r.name + ": "):
                out.append((r.name, CK["variable"], r.hover))
        return out

    def var_type(self, name, an, path, line):
        best = None
        for r in an.refs:
            if r.file == path and r.name == name and r.ty is not None and r.line <= line:
                h = r.hover
                if h.startswith(("let " + name + ":", "var " + name + ":", "const " + name + ":",
                                 name + ": ", "self: ")):
                    if best is None or (r.line, r.col) >= (best.line, best.col):
                        best = r
        if best is None and name == "self":
            for r in an.refs:
                if r.file == path and r.hover.startswith("self: ") and r.line <= line:
                    best = r
        return best.ty if best else None

    def member_items(self, chain, ck, an, path, line):
        segs = []
        depth, cur = 0, ""
        for ch in chain:
            if ch in "([":
                depth += 1
            elif ch in ")]":
                depth -= 1
            if ch == "." and depth == 0:
                segs.append(cur)
                cur = ""
            else:
                cur += ch
        segs.append(cur)

        items = []
        first = re.match(r"\w+", segs[0])
        if not first:
            return items
        name = first.group(0)
        rest_first = segs[0][len(name):]
        if len(segs) == 1 and not rest_first:
            if name in ck.structs and self.var_type(name, an, path, line) is None:
                for m in ck.structs[name].methods.values():
                    if not m.has_self:
                        items.append({"label": m.name, "kind": CK["function"], "detail": Checker.fn_sig(m)})
                return items
            if name in ck.enums:
                e = ck.enums[name]
                return [{"label": v, "kind": CK["enum_member"], "detail": "%s.%s = %d" % (e.name, v, n)}
                        for v, n in e.variants.items()]
        t = self.var_type(name, an, path, line)
        t = self.apply_suffix(t, rest_first)
        for seg in segs[1:]:
            mm = re.match(r"(\w+)(.*)", seg)
            if not mm or t is None:
                return items
            t = self.member_type(t, mm.group(1))
            t = self.apply_suffix(t, mm.group(2))
        if t is None:
            return items
        st = t.elem if t.is_ptr() and isinstance(t.elem, T.StructT) else t
        if isinstance(st, T.StructT):
            for fname, fty in st.fields.items():
                items.append({"label": fname, "kind": CK["field"], "detail": "%s: %s" % (fname, fty)})
            for m in st.methods.values():
                if m.has_self:
                    items.append({"label": m.name, "kind": CK["method"], "detail": Checker.fn_sig(m)})
        if isinstance(t, (T.StrT, T.Slice, T.Array, T.ListT)):
            items.append({"label": "len", "kind": CK["field"], "detail": "len: i64"})
            items.append({"label": "ptr", "kind": CK["field"], "detail": "ptr: *%s" % T.elem_type(t)})
        if isinstance(t, T.ListT):
            items.append({"label": "cap", "kind": CK["field"], "detail": "cap: i64"})
            for m in sorted(LIST_METHODS):
                items.append({"label": m, "kind": CK["method"], "detail": "list method"})
        if t == T.STR:
            for m in sorted(STR_METHODS):
                items.append({"label": m, "kind": CK["method"], "detail": "str method"})
        return items

    @staticmethod
    def apply_suffix(t, suffix):
        if t is None:
            return None
        for part in re.findall(r"\[[^\]]*\]|\([^)]*\)", suffix):
            if part.startswith("["):
                t = T.elem_type(t) if not t.is_ptr() else t.elem
                if t is None:
                    return None
        return t

    @staticmethod
    def member_type(t, name):
        st = t.elem if t.is_ptr() and isinstance(t.elem, T.StructT) else t
        if isinstance(st, T.StructT):
            if name in st.fields:
                return st.fields[name]
            if name in st.methods:
                return st.methods[name].ret
        if name in ("len", "cap"):
            return T.I64
        if t == T.STR and name in ("trim", "clone"):
            return T.STR
        if t == T.STR and name == "split":
            return T.ListT(T.STR)
        if isinstance(t, T.ListT) and name in ("pop", "remove"):
            return t.elem
        return None


def serve():
    return Server(sys.stdin.buffer, sys.stdout.buffer).serve()
