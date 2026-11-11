#!/usr/bin/env python3
"""Flint test runner.

  tests/*.fl          compiled and run; stdout must equal <name>.expected
                      (a first line `// exit: N` sets the expected exit code, default 0)
  tests/errors/*.fl   must FAIL to compile; first line `// expect-error: text`
                      must appear in the error message
  examples/*.fl       must compile (type-check + C compile)
  tests/lsp_test.py   language server smoke test
  all .fl files       must be formatted (flint fmt --check)

Usage: python3 tests/run.py [--update]   (--update rewrites .expected files)
"""
import glob
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FLINT = [sys.executable, os.path.join(ROOT, "flint")]


def run(args, **kw):
    return subprocess.run(FLINT + args, capture_output=True, text=True, **kw)


def main():
    update = "--update" in sys.argv
    passed = failed = 0

    def report(ok, name, detail=""):
        nonlocal passed, failed
        if ok:
            passed += 1
            print("  ok    %s" % name)
        else:
            failed += 1
            print("  FAIL  %s\n%s" % (name, detail))

    print("run tests:")
    for f in sorted(glob.glob(os.path.join(HERE, "*.fl"))):
        exp_file = f[:-3] + ".expected"
        with open(f) as fh:
            first = fh.readline()
        want_code = int(first.split(":", 1)[1]) if first.startswith("// exit:") else 0
        r = run(["run", f])
        if update:
            with open(exp_file, "w") as fh:
                fh.write(r.stdout)
        expected = open(exp_file).read() if os.path.exists(exp_file) else None
        ok = r.stdout == expected and r.returncode == want_code
        report(ok, os.path.relpath(f, ROOT), "    exit=%d (want %d)\n    stderr: %s\n    --- got ---\n%s" % (
            r.returncode, want_code, r.stderr.strip(), r.stdout))

    print("error tests:")
    for f in sorted(glob.glob(os.path.join(HERE, "errors", "*.fl"))):
        with open(f) as fh:
            first = fh.readline()
        want = first.split("expect-error:", 1)[1].strip()
        r = run(["check", f])
        report(r.returncode != 0 and want in r.stderr, os.path.relpath(f, ROOT),
               "    want error containing: %s\n    got: %s" % (want, r.stderr.strip()))

    print("examples compile:")
    out = os.path.join(HERE, ".example.bin")
    for f in sorted(glob.glob(os.path.join(ROOT, "examples", "*.fl"))):
        r = run(["build", f, "-o", out])
        report(r.returncode == 0, os.path.relpath(f, ROOT), "    " + r.stderr.strip())
    if os.path.exists(out):
        os.remove(out)

    print("language server:")
    r = subprocess.run([sys.executable, os.path.join(HERE, "lsp_test.py")], capture_output=True, text=True, timeout=60)
    report(r.returncode == 0, "tests/lsp_test.py", r.stdout + r.stderr)

    print("formatting:")
    for f in sorted(glob.glob(os.path.join(ROOT, "**", "*.fl"), recursive=True)):
        r = run(["fmt", "--check", f])
        if r.returncode != 0:
            report(False, os.path.relpath(f, ROOT), "    not formatted: run ./flint fmt " + os.path.relpath(f, ROOT))
    report(True, "all .fl files formatted (flint fmt --check)")

    print("\n%d passed, %d failed" % (passed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
