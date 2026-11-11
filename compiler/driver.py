"""Command-line driver: load files (+imports), check, generate C, call the C compiler."""

import os
import shlex
import shutil
import subprocess
import sys
import tempfile

from . import ast as A
from .checker import check
from .codegen import generate
from .errors import CompileError
from .parser import parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root (holds std/)

USAGE = """\
Flint compiler

usage:
  flint run   <file.fl> [flags] [-- program args]   compile and run
  flint build <file.fl> [flags] [-o output]         compile to an executable
  flint emit  <file.fl> [flags]                     print the generated C
  flint check <file.fl>                             type-check only
  flint lint  <file.fl>                             type-check + warnings (exit 1 if any)
  flint fmt   <file.fl...> [--check] [--stdout]     format in place (- reads stdin)
  flint lsp                                         language server on stdio (for editors)

flags:
  --release     maximum speed: -O3 -march=native, no bounds checks
  --debug       -O0 -g, address/UB sanitizers, #line mapping to .fl source
  --cc <cmd>    C compiler to use (default: $CC or cc)
  --keep-c      keep the generated .c file next to the output
"""


def load_program(path, overlay=None, abs_paths=False):
    """Parse `path` and everything it imports. Returns a flat list of decls.

    overlay: {abs_path: source} for unsaved editor buffers.
    abs_paths: report absolute file names (editor tooling) instead of relative ones.
    """
    overlay = overlay or {}
    decls = []
    seen = set()

    def resolve_import(name, from_file, tok):
        cands = []
        base = name if name.endswith(".fl") else name + ".fl"
        cands.append(os.path.join(os.path.dirname(os.path.abspath(from_file)), base))
        cands.append(os.path.join(ROOT, base))
        for c in cands:
            if os.path.isfile(c):
                return os.path.abspath(c)
        raise CompileError("cannot find import '%s'" % name, tok.file, tok.line, tok.col)

    def load(p):
        p = os.path.abspath(p)
        if p in seen:
            return
        seen.add(p)
        if p in overlay:
            src = overlay[p]
        else:
            try:
                with open(p, encoding="utf-8") as f:
                    src = f.read()
            except OSError as e:
                raise CompileError("cannot read %s: %s" % (p, e.strerror))
        rel = os.path.relpath(p)
        name = p if abs_paths or rel.startswith("..") else rel
        file_decls = parse(src, name)
        own = []
        for d in file_decls:
            if isinstance(d, tuple) and d[0] == "import":
                load(resolve_import(d[1], p, d[2]))   # imported decls come first
            else:
                own.append(d)
        decls.extend(own)

    load(path)
    return decls


def compile_to_c(path, debug=False):
    decls = load_program(path)
    ck = check(decls)
    return generate(ck, debug_lines=debug), ck


def cc_command(cc, c_file, out, mode, links):
    cmd = shlex.split(cc) + ["-std=gnu11", "-w", "-fwrapv", "-fno-strict-aliasing"]
    if mode == "release":
        cmd += ["-O3", "-march=native", "-DFL_NOCHECK"]
    elif mode == "debug":
        cmd += ["-O0", "-g", "-fsanitize=address,undefined", "-fno-omit-frame-pointer"]
    else:
        cmd += ["-O2"]
    cmd += [c_file, "-o", out, "-lm"] + ["-l" + l for l in links]
    return cmd


def build(path, out, mode, cc, keep_c):
    code, ck = compile_to_c(path, debug=(mode == "debug"))
    if keep_c:
        c_file = out + ".c"
        tmpdir = None
    else:
        tmpdir = tempfile.mkdtemp(prefix="flint-")
        c_file = os.path.join(tmpdir, os.path.basename(out) + ".c")
    with open(c_file, "w") as f:
        f.write(code)
    cmd = cc_command(cc, c_file, out, mode, ck.links)
    try:
        r = subprocess.run(cmd)
    except FileNotFoundError:
        raise CompileError("C compiler '%s' not found; install clang or gcc, or pass --cc" % cc)
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
    if r.returncode != 0:
        raise CompileError("C compiler failed (this is a Flint compiler bug if your code type-checked); "
                           "rerun with `flint emit` to inspect the C")
    return out


def fmt_main(argv):
    from .formatter import format_source
    check_only = "--check" in argv
    to_stdout = "--stdout" in argv
    files = [a for a in argv if not a.startswith("--")]
    if not files:
        print("usage: flint fmt <file.fl...> [--check] [--stdout]   (use - for stdin)", file=sys.stderr)
        return 2
    changed = 0
    for f in files:
        if f == "-":
            sys.stdout.write(format_source(sys.stdin.read()))
            continue
        with open(f, encoding="utf-8") as fh:
            src = fh.read()
        out = format_source(src)
        if to_stdout:
            sys.stdout.write(out)
        elif out != src:
            changed += 1
            if check_only:
                print("would reformat %s" % f)
            else:
                with open(f, "w", encoding="utf-8") as fh:
                    fh.write(out)
    return 1 if (check_only and changed) else 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    cmd = argv.pop(0)
    if cmd == "lsp":
        from .lsp import serve
        return serve()
    if cmd == "fmt":
        return fmt_main(argv)
    prog_args = []
    if "--" in argv:
        k = argv.index("--")
        prog_args = argv[k + 1:]
        argv = argv[:k]
    mode, cc, out, keep_c, files = "default", os.environ.get("CC", "cc"), None, False, []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--release":
            mode = "release"
        elif a == "--debug":
            mode = "debug"
        elif a == "--keep-c":
            keep_c = True
        elif a == "--cc":
            i += 1
            cc = argv[i]
        elif a == "-o":
            i += 1
            out = argv[i]
        elif a.startswith("-"):
            print("unknown flag %s\n\n%s" % (a, USAGE), file=sys.stderr)
            return 2
        else:
            files.append(a)
        i += 1
    if cmd == "run" and len(files) > 1:
        prog_args = files[1:] + prog_args
        files = files[:1]
    if len(files) != 1 or cmd not in ("run", "build", "emit", "check", "lint"):
        print(USAGE, file=sys.stderr)
        return 2
    src = files[0]
    try:
        if cmd in ("check", "lint"):
            ck = check(load_program(src))
            if cmd == "lint":
                for w in sorted(ck.warnings, key=lambda w: (w.file or "", w.line, w.col)):
                    print(w.format().replace(": error: ", ": warning: "), file=sys.stderr)
                return 1 if ck.warnings else 0
            return 0
        if cmd == "emit":
            code, _ = compile_to_c(src, debug=(mode == "debug"))
            sys.stdout.write(code)
            return 0
        if cmd == "build":
            out = out or os.path.splitext(os.path.basename(src))[0]
            build(src, out, mode, cc, keep_c)
            return 0
        # run
        tmpdir = tempfile.mkdtemp(prefix="flint-run-")
        try:
            exe = os.path.join(tmpdir, os.path.splitext(os.path.basename(src))[0])
            build(src, exe, mode, cc, keep_c)
            sys.stdout.flush()
            return subprocess.run([exe] + prog_args).returncode
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    except CompileError as e:
        print(e.format(), file=sys.stderr)
        return 1
