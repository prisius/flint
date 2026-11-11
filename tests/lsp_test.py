#!/usr/bin/env python3
"""Smoke test for `flint lsp`: drives the server over stdio like an editor would."""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = """struct Vec2 { x: f64, y: f64 }
impl Vec2 {
    fn len(self) -> f64 { return self.x + self.y }
}
fn main() {
    var v = Vec2 { x: 1.0, y: 2.0 }
    let unused = 5
    v.x = 3.0
    println(v.len())
    v.
}
"""


def main():
    p = subprocess.Popen([sys.executable, os.path.join(ROOT, "flint"), "lsp"],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    n = [0]

    def send(method, params, req=True):
        msg = {"jsonrpc": "2.0", "method": method, "params": params}
        if req:
            n[0] += 1
            msg["id"] = n[0]
        b = json.dumps(msg).encode()
        p.stdin.write(b"Content-Length: %d\r\n\r\n" % len(b) + b)
        p.stdin.flush()
        return n[0] if req else None

    def recv():
        length = 0
        while True:
            line = p.stdout.readline().strip()
            if not line:
                break
            if line.lower().startswith(b"content-length"):
                length = int(line.split(b":")[1])
        return json.loads(p.stdout.read(length))

    def request(method, params):
        i = send(method, params)
        while True:
            m = recv()
            if m.get("id") == i:
                return m.get("result")

    uri = "file://" + os.path.join(ROOT, "tests", "_lsp_tmp.fl")
    doc = {"textDocument": {"uri": uri}}
    request("initialize", {"capabilities": {}})
    send("initialized", {}, req=False)
    send("textDocument/didOpen", {"textDocument": {"uri": uri, "languageId": "flint", "version": 1, "text": SRC}}, req=False)
    diag = recv()
    fails = 0

    def check(name, cond, got):
        nonlocal fails
        print("  %s %s" % ("ok  " if cond else "FAIL", name))
        if not cond:
            fails += 1
            print("      got:", json.dumps(got)[:400])

    msgs = [d["message"] for d in diag["params"]["diagnostics"]]
    check("diagnostics: parse error reported", any("expected" in m for m in msgs), msgs)

    fixed = SRC.replace("    v.\n", "")
    send("textDocument/didChange", {"textDocument": {"uri": uri, "version": 2}, "contentChanges": [{"text": fixed}]}, req=False)
    diag = recv()
    msgs = [d["message"] for d in diag["params"]["diagnostics"]]
    check("diagnostics: unused variable warning", any("unused variable 'unused'" in m for m in msgs), msgs)

    h = request("textDocument/hover", dict(doc, position={"line": 8, "character": 15}))
    check("hover on method call", h and "fn Vec2.len(self) -> f64" in h["contents"]["value"], h)
    h = request("textDocument/hover", dict(doc, position={"line": 7, "character": 4}))
    check("hover on variable", h and "var v: Vec2" in h["contents"]["value"], h)

    d = request("textDocument/definition", dict(doc, position={"line": 8, "character": 15}))
    check("definition of method", d and d["range"]["start"]["line"] == 2, d)
    d = request("textDocument/definition", dict(doc, position={"line": 7, "character": 6}))
    check("definition of field", d and d["range"]["start"] == {"line": 0, "character": 14}, d)

    r = request("textDocument/references", dict(doc, position={"line": 5, "character": 8}, context={"includeDeclaration": True}))
    check("references of v (3)", r and len(r) == 3, r)

    with_dot = fixed.replace("    println(v.len())\n", "    println(v.len())\n    v.\n")
    send("textDocument/didChange", {"textDocument": {"uri": uri, "version": 3}, "contentChanges": [{"text": with_dot}]}, req=False)
    recv()
    c = request("textDocument/completion", dict(doc, position={"line": 9, "character": 6}))
    labels = {i["label"] for i in c["items"]}
    check("member completion after 'v.'", {"x", "y", "len"} <= labels, sorted(labels))
    c = request("textDocument/completion", dict(doc, position={"line": 8, "character": 4}))
    labels = {i["label"] for i in c["items"]}
    check("general completion", {"v", "println", "Vec2", "main", "while"} <= labels, sorted(labels)[:30])

    send("textDocument/didChange", {"textDocument": {"uri": uri, "version": 3}, "contentChanges": [{"text": fixed}]}, req=False)
    recv()
    s = request("textDocument/documentSymbol", doc)
    check("document symbols", [x["name"] for x in s] == ["Vec2", "impl Vec2", "main"], s)

    send("textDocument/didChange", {"textDocument": {"uri": uri, "version": 4}, "contentChanges": [{"text": "fn main() {\nprintln(1);\n}\n"}]}, req=False)
    recv()
    f = request("textDocument/formatting", dict(doc, options={"tabSize": 4, "insertSpaces": True}))
    check("formatting", f and f[0]["newText"] == "fn main() {\n    println(1)\n}\n", f)

    request("shutdown", None)
    send("exit", None, req=False)
    p.wait(timeout=5)
    print("%s" % ("LSP OK" if not fails else "%d LSP checks failed" % fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
