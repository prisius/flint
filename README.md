# Flint

**Flint is a small, fast systems language that feels like C without the hassle.**
It compiles to a single optimized C file and then to a native executable, so it runs
at C speed. It removes the parts of C that slow you down: header files, forward
declarations, `->` versus `.`, `printf` format strings, manual string handling,
dangling `else`, and silent out-of-bounds bugs.

```flint
struct Vec2 { x: f64, y: f64 }

impl Vec2 {
    fn len(self) -> f64 { return sqrt(self.x * self.x + self.y * self.y) }
}

cinclude "math.h"
extern fn sqrt(x: f64) -> f64

fn main() {
    var points: list[Vec2]
    points.push(Vec2 { x: 3.0, y: 4.0 })
    points.push(Vec2 { x: 6.0, y: 8.0 })
    for i, p in points {
        println("point {i}: {p} has length {p.len():.2}")
    }
}
```

```
point 0: Vec2 { x: 3.0, y: 4.0 } has length 5.00
point 1: Vec2 { x: 6.0, y: 8.0 } has length 10.00
```

---

## Contents

1. [Quick start](#1-quick-start)
2. [Why Flint (vs C)](#2-why-flint-vs-c)
3. [Language tour](#3-language-tour)
4. [Reference](#4-reference)
5. [Calling C](#5-calling-c)
6. [Build modes and performance](#6-build-modes-and-performance)
   - [Editor support: Neovim, LSP, linter, formatter](#editor-support-neovim-lsp-linter-formatter)
7. [Project layout](#7-project-layout)
8. [AI / contributor guide: how to extend Flint](#8-ai--contributor-guide-how-to-extend-flint)
9. [Known limitations and roadmap](#9-known-limitations-and-roadmap)

---

## 1. Quick start

**Requirements:** Python 3.8 or newer, and a C compiler (`cc`, `clang` or `gcc`). There are no other dependencies.

```sh
./flint run examples/hello.fl              # compile and run
./flint build examples/life.fl -o life     # build an executable
./flint build app.fl --release             # maximum speed
./flint run app.fl --debug                 # sanitizers and source-line debugging
./flint emit app.fl > app.c                # look at the generated C
./flint check app.fl                       # type-check only
./flint lint app.fl                        # type-check + warnings
./flint fmt app.fl                         # format in place (--check for CI)
./flint lsp                                # language server (editors)
python3 tests/run.py                       # run the test suite
```

To use `flint` from anywhere, add the folder to your PATH or create a symlink:
`ln -s "$PWD/flint" /usr/local/bin/flint`.

Program arguments go after `--` (or after the file name): `./flint run prog.fl -- a b c`.

---

## 2. Why Flint (vs C)

| C hassle | Flint |
|---|---|
| Header files and forward declarations | None. Declare things in any order. `import "file"` to split code. |
| `printf("%lld %s\n", ...)` | `println("{count} {name}")`, with type-checked interpolation |
| `p->x` vs `p.x` | Always `p.x`; pointers are dereferenced automatically for fields and methods |
| `char*` strings, `strlen`, `strcat` | A real `str` type (pointer and length): `+`, `==`, `.len`, slicing, `.split()`, `.find()` |
| Arrays decay to pointers and lose their length | Arrays are values; slices `[]T` carry their length; `.len` everywhere |
| Buffer overflows | Bounds-checked indexing, which you can turn off with `--release` |
| Hand-written growable arrays | Built-in `list[T]` with `.push()`, `.pop()`, `.remove()` and more |
| `goto cleanup` | `defer free(buf)` |
| `if (x = 5)` bug | Assignment is a statement, not an expression |
| `x & 1 == 0` precedence trap | Bitwise operators bind tighter than comparisons |
| Dangling else, missing braces | Braces are always required; no parentheses needed around conditions |
| Semicolons | Optional. A newline ends a statement. |
| Debugging prints of structs | `println(my_struct)` prints every field |
| Speed | The same speed. It *is* C under the hood (one translation unit, `-O2` or `-O3`). |

---

## 3. Language tour

### Variables

```flint
let x = 10            // immutable, type inferred (i64)
var y = 2.5           // mutable (f64)
var z: i32 = 7        // explicit type
var buf: [256]u8      // no initializer means zero-initialized
const SIZE = 64       // compile-time constant (usable as an array size)
```

`let` cannot be reassigned. `var` can. Variables without an initializer are
always zeroed, so Flint has no uninitialized garbage.

### Functions

```flint
fn add(a: i32, b: i32) -> i32 {
    return a + b
}

fn greet(name: str) {          // no return type means void
    println("hi {name}")
}
```

The program starts at `fn main()`. It may also be written as
`fn main(args: []str)`, return an integer exit code (`fn main() -> i32`), or both.

### Control flow

```flint
if x > 5 {
    ...
} else if x == 5 {
    ...
} else {
    ...
}

while running { ... }
while true { if done { break } }

for i in 0..10 { }              // 0 to 9 (end is exclusive)
for x in my_slice { }           // iterate over a slice, array, list or str
for i, x in my_list { }         // with an index

match cmd {
    "quit", "exit" => running = false
    "help" => {
        show_help()
    }
    _ => println("unknown: {cmd}")
}
```

`match` works on integers, chars, bools, enums and `str`. The arms do not fall through.

### Structs and methods

```flint
struct Player {
    name: str
    hp: i32
    pos: Vec2
}

impl Player {
    fn new(name: str) -> Player {              // static: Player.new("ann")
        return Player { name: name, hp: 100 }  // omitted fields are zeroed
    }
    fn hurt(self, dmg: i32) {                  // self is a pointer: it can modify the player
        self.hp -= dmg
    }
    fn alive(self) -> bool { return self.hp > 0 }
}

var p = Player.new("ann")
p.hurt(30)
println(p)            // Player { name: "ann", hp: 70, pos: Vec2 { x: 0.0, y: 0.0 } }
```

### Enums

```flint
enum State { Idle, Running, Done = 10 }

let s = State.Running
println(s)                 // Running
println(s as i32)          // 1
```

### Pointers and the heap

```flint
let n = new(Node)                                 // zeroed heap allocation: *Node
let m = new(Node, Node { value: 5, next: null })  // allocate and initialize
n.next = m                                        // no -> needed
let v = *n                                        // dereference (copy)
let addr = &v                                     // address-of
free(n)
free(m)

let buf = make([]u8, 1024)      // heap slice, zeroed
defer free(buf)
```

### Arrays, slices and lists

```flint
var a: [4]i32 = [1, 2, 3, 4]    // fixed array: a value type, can live on the stack
let s: []i32 = a                // array to slice (a view; no copy)
let mid = a[1..3]               // slicing gives []i32 holding [2, 3]
let tail = s[2..]               // open-ended slices: [..n] and [n..]

var xs: list[str]               // growable list; empty and ready to use
xs.push("a")
xs.push("b")
let last = xs.pop()
println(xs.len, xs)             // 1 ["a"]
total(xs)                       // a list[T] converts to []T automatically
free(xs)
```

### Strings

```flint
let name = "Ada"
let msg = "Hello, {name}! You have {count} messages"   // interpolation
let s = name + " Lovelace"                             // concatenation
if s == "Ada Lovelace" { }                             // equality compares the content
let c = s[0]                                           // char 'A'
let sub = s[4..]                                       // "Lovelace" (no copy)
for ch in s { }                                        // iterate over chars (bytes)
let parts = "a,b,c".split(",")                         // list[str]
```

Use `{{` and `}}` for literal braces.

Interpolation format specs work like printf, but are checked at compile time:

| spec | meaning | example |
|---|---|---|
| `{x:.2}` | 2 decimal places | `3.14` |
| `{x:8}` | right-align to width 8 | `      42` |
| `{x:-8}` | left-align to width 8 | `42      ` |
| `{x:05}` | zero-pad | `00042` |
| `{x:x}` / `{x:X}` / `{x:o}` / `{x:b}` | hex / HEX / octal / binary | `ff` |
| `{x:08.3}` | combined | `0003.142` |
| `{x:.1e}` | scientific notation | `1.5e+03` |

### Defer

`defer` runs a statement or block when the enclosing block exits. That includes
exits through `return`, `break` and `continue`. Deferred statements run in
reverse order (last in, first out).

```flint
fn process(path: str) -> bool {
    let data = read_file(path)
    defer free(data)
    if data.len == 0 { return false }    // free(data) still runs
    ...
    return true
}
```

### Modules

```flint
import "std/math"       // looks in this file's folder, then in the Flint install folder
import "utils"          // utils.fl next to this file
```

Imports are flat: every declaration from the imported file becomes visible. Each
file is loaded only once, even if several files import it.

Standard library modules:

| Module | Contents |
|---|---|
| `std/math` | `sqrt`, `pow`, `sin`, `cos`, `floor` and the rest of `math.h`; `PI`, `E`, `TAU`; `min_`/`max_`/`clamp_` helpers |
| `std/rand` | `Rng.new(seed)`, `.next()`, `.range(lo, hi)`, `.float()` (xorshift64*) |
| `std/net` | TCP: `TcpListener.bind(port)`, `.accept()`; `TcpConn.read(buf) -> str`, `.write(s)`, `.close()` |

A tiny TCP echo server:

```flint
import "std/net"

fn main() {
    let server = TcpListener.bind(9000)
    while true {
        var conn = server.accept()
        var buf: [4096]char
        let msg = conn.read(buf)        // a str view into buf
        conn.write(msg)
        conn.close()
    }
}
```

For a complete example (routing, query strings, JSON, status codes, `defer`
cleanup) see `examples/http_server.fl`: `./flint run examples/http_server.fl`, then
open http://localhost:8080.

---

## 4. Reference

### Types

| Type | Meaning | C equivalent |
|---|---|---|
| `i8 i16 i32 i64`, `u8 u16 u32 u64` | integers | `int8_t` and friends |
| `int`, `uint`, `byte` | aliases for `i64`, `u64`, `u8` | |
| `f32 f64` (alias `float` = `f64`) | floating point | `float`, `double` |
| `bool` | `true` or `false` | `bool` |
| `char` | one byte; prints as a character | `char` |
| `str` | string view `{ptr: *char, len: i64}` | `fl_str` |
| `*T` | pointer (`null` for none) | `T*` |
| `[N]T` | fixed array (a value type) | struct wrapping `T[N]` |
| `[]T` | slice `{ptr, len}` | `fl_slice_T` |
| `list[T]` | growable `{ptr, len, cap}` | `fl_list_T` |
| `void` | no value (return type only) | `void` |

**Literals:** `42`, `1_000_000`, `0xff`, `0b1010`, `3.14`, `1e9`, `'a'`, `'\n'`,
`"text\t{expr}"`, `true`, `false`, `null`, `[1, 2, 3]`, `Point { x: 1, y: 2 }`.
String escapes are `\n \t \r \0 \\ \" \' \e \xNN`. An integer literal takes whatever
numeric type its context expects. Without context it is `i64`, and a float literal is `f64`.

**Implicit conversions:** between integer types, from integer to float, from `f32` to
`f64` and back, `null` to any pointer, `*void` to and from any pointer, and `[N]T` or
`list[T]` to `[]T`. Float to integer always needs `as`:
`let n = f as i32`. In arithmetic, the operands are widened to the larger type.

**Casts (`as`):** between numbers, between enums and integers, between pointers,
between pointers and `i64`/`u64`, from bool to an integer, and between `str` and
`[]char`/`[]u8`/`[]i8` (a zero-copy view of the same bytes; useful for I/O buffers:
`let text = buf[..n] as str`).

### Operators (lowest to highest precedence)

```
||
&&
== != < > <= >=          (these also work on str)
|
^
&
<< >>
+ -                      (+ also concatenates str)
* / %
as
unary: - ! ~ & *
postfix: f(x)  a[i]  a[lo..hi]  x.field  x.method()
```

The assignment operators `= += -= *= /= %= &= |= ^= <<= >>=` are statements.
`s += "more"` works on strings.

### Fields on built-in types

| Expression | Type | Works on |
|---|---|---|
| `x.len` | `i64` | `str`, `[]T`, `[N]T`, `list[T]` |
| `x.ptr` | `*T` (`*char` for str) | `str`, `[]T`, `[N]T`, `list[T]` |
| `x.cap` | `i64` | `list[T]` |

### Built-in functions

| Function | Description |
|---|---|
| `print(a, b, ...)` / `println(...)` | Print the values separated by spaces. `println` adds a newline. Any type works. |
| `eprint(...)` / `eprintln(...)` | The same, but to stderr |
| `assert(cond)` / `assert(cond, "msg")` | Panic with the file and line if `cond` is false |
| `panic("msg")` | Print the location and message to stderr, then exit with code 101 |
| `exit(code)` | Exit the process |
| `new(T)` / `new(T, value)` | Heap-allocate a zeroed or initialized `T`; returns `*T` |
| `make([]T, n)` | Heap-allocate a zeroed slice of `n` elements |
| `make(list[T], cap)` | An empty list with capacity reserved in advance |
| `free(x)` | Free a pointer, slice, heap string, or list (which is then reset to empty) |
| `sizeof(T)` | Size in bytes (`i64`) |
| `read_line()` | One line from stdin without the trailing newline. At end of input it returns a `str` whose `.ptr == null`. |
| `read_file(path)` | The whole file as `str`; `.ptr == null` if the file cannot be read |
| `write_file(path, data)` | Returns `true` on success |
| `parse_int(s)` / `parse_float(s)` | Parse a number (surrounding whitespace is allowed; `0x` prefixes work) |
| `time_ns()` | Monotonic clock in nanoseconds (`i64`) |
| `args()` | Command-line arguments as `[]str` |

### `list[T]` methods

`push(v)`, `pop() -> T`, `insert(i, v)`, `remove(i) -> T`, `clear()`, `reserve(n)`.
A list is empty and ready to use when declared. Indexing `xs[i]` is bounds-checked.

### `str` methods

`find(sub) -> i64` (`-1` if not found), `contains(sub)`, `starts_with(p)`, `ends_with(s)`,
`eq(other)`, `trim() -> str` (a view, no copy), `clone() -> str` (a heap copy),
`cstr() -> *char` (a heap copy with a null terminator, for C), `split(sep) -> list[str]`
(the parts are views into the original string).

### Memory rules

- There is no garbage collector. Memory works like C: what you `new` or `make` you `free`.
  `defer` keeps cleanup next to the allocation.
- Functions that **allocate** strings: `+`, interpolated `"...{x}..."` (when not passed
  directly to `print`), `clone`, `cstr`, `read_line`, `read_file`. `split` allocates the
  list but not the parts.
- String literals live in static memory. **Never `free` a literal or a slice of a literal.**
- Slices and `str` values are *views*. Copying one does not copy the data.
- Structs, arrays, slices and lists are copied **by value** when assigned or passed
  (for a list, only its header is copied). Pass `*T` to share and modify.
  Methods already receive `self` as a pointer.

### Runtime safety

The default and `--debug` builds check every index and slice of `[]T`, `[N]T`,
`list[T]` and `str`:

```
main.fl:12:19: panic: index out of bounds: index 3, length 3
```

`pop()` on an empty list panics. Pointer indexing (`p[i]`) is never checked.
`--debug` additionally enables the AddressSanitizer and UndefinedBehaviorSanitizer,
which catch use-after-free, leaks and similar bugs.

### Syntax rules worth knowing

- A newline ends a statement whenever the line could end there (the same rule as Go).
  To continue an expression on the next line, end the line with an operator,
  `,` or `(`. Newlines inside `(...)` and `[...]` are ignored.
- Both brace styles work: `fn main() {` and `fn main()` with `{` on the next line,
  and `} else {` as well as `}` with `else` on the next line.
- Conditions must be `bool`. Write `if n != 0`, not `if n`.
- An interpolation like `{expr}` may not contain string literals. Put the string in a
  variable first.
- A struct literal is not allowed directly inside an `if`, `while`, `for` or `match`
  header. Wrap it in parentheses: `if p == (Point { x: 1, y: 2 })`. (Comparing structs
  with `==` is not supported anyway; compare their fields.)
- Comments are `// line` and `/* block */`.

---

## 5. Calling C

Any C function can be called. Declare its signature with `extern fn` and include
its header with `cinclude`:

```flint
cinclude "stdlib.h"
cinclude "./mylib.h"        // a path starting with ./ or / becomes #include "..."
link "sqlite3"              // adds -lsqlite3 to the link command

extern fn rand() -> i32
extern fn getenv(name: *char) -> *char
extern fn printf(fmt: *char, ...) -> i32    // variadic

fn main() {
    let home = getenv("HOME".ptr)            // literals are null-terminated
    let name = "world"
    printf("hi %.*s\n".ptr, name.len as i32, name.ptr)
    let owned = name.cstr()                  // heap copy with a null terminator
    defer free(owned)
}
```

- `str` → C string: use `s.ptr` for literals and for strings Flint allocated (both are
  null-terminated). Use `s.cstr()` for slices or views.
- C string → `str`: there is no built-in for this yet; see the roadmap.
- `extern fn` names are used exactly as written. Your own Flint functions are emitted
  as `fl_<name>`, so they never clash with libc.
- `std/math.fl` already binds most of `math.h`: `import "std/math"`.
- A local header path in `cinclude "./x.h"` is resolved relative to the `.fl` file.
  When a C library's structs differ between platforms, put a few small `static` C
  functions in a header next to your code (see `std/net.h`).

---

## 6. Build modes and performance

| Mode | C flags | Bounds checks | Use for |
|---|---|---|---|
| default | `-O2` | on | everyday development |
| `--release` | `-O3 -march=native -DFL_NOCHECK` | off | maximum speed |
| `--debug` | `-O0 -g -fsanitize=address,undefined` with `#line` directives | on | debugging (gdb or lldb shows `.fl` lines) |

The whole program, including the runtime, is emitted as **one C translation
unit**, so the C compiler can inline across all of it. Every function is `static`,
there are no hidden allocations (except the ones listed above), and there is no
garbage collector or virtual machine.

Benchmark (`benchmarks/bench.fl` against the same algorithm hand-written in
`benchmarks/bench.c` at `-O3`; Apple Silicon): a 50M sieve, `fib(38)`, and a 300M-step
float loop.

```
c:     ~1150 ms
flint: ~1125 ms   (--release, and the same with bounds checks on)
```

`CC=gcc ./flint build ...` or `--cc gcc` selects the C compiler.

### Editor support: Neovim, LSP, linter, formatter

**Language server** (`flint lsp`, stdio, no dependencies) provides:

| Feature | Notes |
|---|---|
| Diagnostics | compile errors + lint warnings, live as you type (unsaved buffers included) |
| Hover | types of variables, signatures of functions/methods, struct layouts, built-in docs |
| Go to definition / references | variables, params, functions, methods, fields, structs, enums, variants (across imports) |
| Completion | locals, globals, functions, types, keywords, built-ins; members after `.` (fields, methods, list/str methods) |
| Document symbols | functions, structs + fields, enums + variants, impl methods, globals |
| Formatting | same as `flint fmt` |

**Linter** (`flint lint file.fl`, also shown in the editor as warnings):
unused variables (prefix with `_` to silence), `var` that is never modified
(should be `let`), unreachable code after `return`/`break`/`continue`/`panic`.

**Formatter** (`flint fmt file.fl`, `--check`, `--stdout`, `-` for stdin): only
touches whitespace and trailing `;`: 4-space indentation by bracket depth,
continuation indent after a trailing operator, collapses blank-line runs.
Comments and everything else are preserved, so it can never change behavior.

**Neovim (0.11+)**: the plugin is in `editors/nvim` (filetype detection, syntax
highlighting with interpolation, indentation, comment string, LSP setup, format on
save, commands). With lazy.nvim:

```lua
-- ~/.config/nvim/lua/plugins/flint.lua
return {
  {
    dir = "/path/to/flint/editors/nvim",
    name = "flint.nvim",
    lazy = false,
    config = function()
      require("flint").setup({
        -- flint = "/path/to/flint",  -- default: the repo's ./flint script
        -- format_on_save = true,
        -- keymaps = true,
      })
    end,
  },
}
```

Without a plugin manager: `vim.opt.rtp:append("/path/to/flint/editors/nvim")` then
`require("flint").setup()`.

| Command | Key (in .fl buffers) | Does |
|---|---|---|
| `:FlintRun [args]` | `<leader>cr` | save, compile and run in a terminal split |
| `:FlintBuild [--release]` | `<leader>cb` | build an executable |
| `:FlintEmit` | `<leader>ce` | show the generated C in a split |
| `:FlintLint` | `<leader>cl` | lint into the quickfix list |

Standard LSP keys work as usual (`K` hover, `gd` definition, `grr` references,
`gO` symbols, `[d`/`]d` diagnostics, `<C-x><C-o>` or nvim-cmp completion).

**Other editors:** any LSP client works. Run `flint lsp` for files with the `.fl` extension.

---

## 7. Project layout

```
flint                 CLI entry script (Python)
compiler/
  lexer.py            source -> tokens (automatic semicolon insertion)
  parser.py           tokens -> AST (recursive descent; string interpolation parsing)
  ast.py              AST node classes
  types.py            semantic types (Prim, Ptr, Slice, Array, ListT, StructT, EnumT, StrT)
  checker.py          name resolution, type checking, annotates the AST
  codegen.py          annotated AST -> C source
  runtime.h           C runtime inlined into every program (str, lists, printing, panics, I/O)
  driver.py           CLI, imports, invokes the C compiler
  formatter.py        `flint fmt` (whitespace-only, safe)
  lsp.py              `flint lsp` language server (JSON-RPC over stdio)
  errors.py           CompileError
std/                  standard library: math, rand, net (TCP: net.fl + net.h C shim)
examples/             hello, fizzbuzz, life, stack (RPN), wordcount, http_server
tests/
  run.py              test runner
  *.fl + *.expected   output tests
  errors/*.fl         programs that must fail to compile (first line: // expect-error: ...)
benchmarks/           Flint vs C
editors/nvim/         Neovim plugin (ftdetect, syntax, indent, ftplugin, lua/flint LSP setup)
```

---

## 8. AI / contributor guide: how to extend Flint

> This section is written for an AI assistant (or a human) who is adding features.
> Read it fully before changing code.

### Pipeline

```
.fl source ─lexer─▶ tokens ─parser─▶ AST ─checker─▶ annotated AST ─codegen─▶ C file ─cc─▶ binary
```

1. **lexer.py**: `lex(src, file)` returns a `Token` list. A `;` token is inserted at a
   newline when the previous token can end a statement (`ENDS_STATEMENT*` sets) and
   we are not inside `(` or `[`. String tokens keep their **raw** text; the parser
   decodes the escapes. Keywords live in `KEYWORDS`. Multi-character punctuation must
   come before its prefixes in `PUNCT`.
2. **parser.py**: `Parser` is a recursive-descent parser. The entry point is
   `parse_program()`. Expression precedence comes from the `BINARY_PREC` table, then
   `parse_cast` → `parse_unary` → `parse_postfix` → `parse_primary`. `no_struct`
   disables `Name {` struct literals inside `if`/`while`/`for`/`match` headers.
   `parse_string` splits interpolated strings into `bytes` parts and `FmtArg` parts.
   An `import` is returned as a tuple `("import", path, tok)`, which the driver resolves.
3. **ast.py**: each node class is created with `node(Name, *fields)`. All nodes have
   `file`, `line` and `col`.
4. **checker.py**: `Checker.check()` runs in phases: register type names → evaluate
   consts → enum values and struct fields → function signatures (including methods) →
   global variables → function bodies. It **annotates** the nodes (see the docstring at
   the top of the file): `e.ty` on every expression, plus `kind`, `sym`, `fn`,
   `self_mode`, `coerce` and so on. It also collects `needed` (composite types whose
   C typedef must be emitted) and `fmt_types` (types that need a generated printer).
   Errors are raised with `err_at(node, msg)`.
5. **types.py**: each type has `key` (equality), `mangle` (safe in C identifiers) and
   `c` (the C spelling). Always register composite types with `checker.need(t)`.
6. **codegen.py**: `CGen.generate()` writes the sections in the order given in the
   module docstring. `expr(e)` returns a C expression string and `gen_stmt(s)` emits
   lines. GNU statement expressions `({ ... })` are allowed. `self.tmp()` creates unique
   temporary names. `self.frames` tracks `defer` scopes: `return`, `break` and
   `continue` call `run_defers`.
7. **runtime.h**: plain C, `static` functions with the `fl_` prefix. Generic lists use
   the `FL_LIST_TYPE` and `FL_LIST_IMPL` macros, and slices use `FL_SLICE_TYPE`.

### Naming conventions in the generated C

| Flint | C |
|---|---|
| `fn foo` | `fl_foo` |
| method `T.m` | `T__m(T* self, ...)` |
| global `g` | `fl_g` |
| local `x` | `x` (renamed `x_2` when shadowed or when it clashes with C/libc) |
| struct `T` | `T` |
| enum variant `E.V` | `E_V` |
| `[]T` / `list[T]` / `[N]T` | `fl_slice_<m>` / `fl_list_<m>` / `fl_arr_a<N>_<m>` |
| `str` | `fl_str` |

### Recipes

**Add a built-in function** (example: `abs_diff(a, b)`):
1. `checker.py`: add the name to `BUILTINS`, and add a branch in `check_builtin` that
   validates the arguments and returns the result type.
2. `codegen.py`: handle it in `gen_builtin` (or add it to the `simple` map if it is a
   direct call to a runtime function).
3. `runtime.h`: implement `static ... fl_abs_diff(...)` if it needs C code.
4. Document it in README §4 and add a test.

**Add a `str` or `list` method:** add it to `STR_METHODS` or `LIST_METHODS` in
`checker.py`, add its signature to the `sig` dict in `check_str_method` or
`check_list_method`, and implement `fl_str_<name>` in `runtime.h` or
`fl_list_##M##_<name>` inside `FL_LIST_IMPL`. Codegen picks it up automatically by
name. List methods that can panic receive a location string; see `pop`, `remove`
and `insert` in `gen_call`.

**Add a statement** (example: `loop { }`):
1. lexer: add the keyword to `KEYWORDS`.
2. ast: `Loop = node("Loop", "body")`.
3. parser: add a branch in `parse_stmt`.
4. checker: add a branch in `check_stmt` (bump `loop_depth` around the body if
   `break` must work), and update `terminates()` if relevant.
5. codegen: add a branch in `gen_stmt`; use `self.gen_block(body, loop=True)` so that
   `break`, `continue` and `defer` work.

**Add an operator:** add it to `PUNCT` (lexer) and to a level of `BINARY_PREC`
(parser), give it typing rules in `check_binary`, and emit it in `gen_binary` if
plain C syntax does not fit.

**Add a type** (example: `map[K]V`): add a class in `types.py` with `key`, `mangle` and
`c`; parse it in `parse_type` and a new `T*` node; resolve it in `Checker.resolve` and
call `need()`. Emit its typedef in `generate()` with the slices and lists, and its
functions after the struct definitions (they need complete element types). Then add
indexing, iteration (`ForEach`, `elem_type`) and printing (`need_fmt`,
`gen_formatter`).

**Editor tooling hooks:** whenever the checker resolves a name it calls
`self.ref(node, name, hover_text, def_node, type)`. The LSP builds hover,
definition, references and completion from that list, so new syntax that resolves
names should call `ref()` too. To add a lint, call `self.warn(node, msg)` in the
checker (see the end of `check_fn` and `check_block`). LSP features live in
`compiler/lsp.py` (`Server.dispatch`), and `tests/lsp_test.py` drives the server
like an editor would. Highlighting for a new keyword goes in
`editors/nvim/syntax/flint.vim`.

**Add a standard library module:** write `std/<name>.fl` in Flint itself. Use
`cinclude` and `extern fn` for C functions. Users load it with `import "std/<name>"`.

### Rules for changes

- Every user error must go through `err_at(node, "clear message with a hint")`. The C
  compiler should **never** fail on code that type-checked. If it does, that is a
  compiler bug (the driver reports it that way).
- Keep the generated C warning-free in spirit (it is compiled with `-w`, but the
  `--debug` sanitizer build must stay clean).
- Each time something is evaluated twice in generated C, use `is_pure()` or a
  statement-expression temporary.
- After any change, run `python3 tests/run.py`. When you add a feature, add a test:
  extend `tests/features.fl` (then `python3 tests/run.py --update` and **review the diff**
  of `.expected`), add a new `tests/<name>.fl` with a `<name>.expected` file, or add an
  error case `tests/errors/<name>.fl` whose first line is `// expect-error: <substring>`.
- Keep this README in sync: the language reference (§3–§5) is the spec.

### Quick prompt for AI assistants

> You are working on Flint, a C-like language whose compiler is written in Python
> (`compiler/`) and compiles to C. Read `README.md` §8 for the architecture and the
> recipes. Make the change across the lexer, parser, checker, codegen and runtime as
> needed, add tests in `tests/`, run `python3 tests/run.py` until everything passes,
> and update the README reference.

---

## 9. Known limitations and roadmap

These are not implemented yet. Each one is a good next feature, listed roughly from
easiest to hardest:

- [ ] `str_from_c(p: *char) -> str` built-in, and `str` ↔ number helpers (`to_str` is
      already possible with `"{n}"`)
- [ ] `loop { }`, labeled `break`, and `for` with a step (`for i in 0..n by 2`)
- [ ] `else if` support inside `match` guards, and range patterns (`1..5 =>`)
- [ ] struct equality (`==`) generated field by field
- [ ] function pointers / first-class functions (`fn(i32) -> i32` type)
- [ ] `map[K]V` built-in hash map
- [ ] generics (`fn max[T](a: T, b: T) -> T`), compiled by monomorphization (one copy per type)
- [ ] tagged unions / `Option[T]` / `Result[T, E]` with `match` destructuring
- [ ] multiple return values (`fn divmod(a, b) -> (i64, i64)`)
- [ ] namespaced imports (`import "math" as m`)
- [ ] `extern struct` for C structs, and C macros/constants
- [ ] threads and atomics
- [ ] a self-hosted compiler (rewrite `compiler/` in Flint)

Current limitations:

- String literals cannot appear inside `{interpolation}`.
- `defer` re-runs its statement at scope exit; it does not capture argument values at
  the `defer` line, the way Go does.
- `str` indexing works on bytes, not Unicode characters.
- Arrays returned from a function call must be stored in a variable before they are
  sliced or converted to `[]T`.
