"""AST node definitions.

Every node carries a source location (file, line, col). The checker fills in
extra attributes on expression nodes (always `ty`, plus a few node-specific
ones documented below) that the code generator relies on.
"""


class Node:
    fields = ()

    def __init__(self, tok, *args):
        self.file = tok.file if tok is not None else None
        self.line = tok.line if tok is not None else 0
        self.col = tok.col if tok is not None else 0
        if len(args) != len(self.fields):
            raise TypeError("%s expects %s" % (type(self).__name__, self.fields))
        for k, v in zip(self.fields, args):
            setattr(self, k, v)

    def __repr__(self):
        return "%s(%s)" % (type(self).__name__, ", ".join("%s=%r" % (f, getattr(self, f)) for f in self.fields))


def node(name, *fields):
    cls = type(name, (Node,), {"fields": fields})
    return cls


# ---------------------------------------------------------------- type syntax
# Unresolved type expressions as written in source; checker turns them into
# flint.types.Type objects.
TName = node("TName", "name")            # i32, str, Vec2
TPtr = node("TPtr", "elem")              # *T
TSlice = node("TSlice", "elem")          # []T
TArray = node("TArray", "size", "elem")  # [N]T   (size: Expr, must be const)
TList = node("TList", "elem")            # list[T]

# ---------------------------------------------------------------- expressions
IntLit = node("IntLit", "value")
FloatLit = node("FloatLit", "value")
BoolLit = node("BoolLit", "value")
CharLit = node("CharLit", "value")
NullLit = node("NullLit")
StrLit = node("StrLit", "data")                 # data: bytes (no interpolation)
InterpStr = node("InterpStr", "parts")          # parts: list of bytes | FmtArg
FmtArg = node("FmtArg", "expr", "spec")         # spec: str or None ({x:.2})
Ident = node("Ident", "name")                   # checker sets: sym
Binary = node("Binary", "op", "left", "right")
Unary = node("Unary", "op", "operand")          # - ! ~ & *
Call = node("Call", "callee", "args")           # checker sets: kind, target, ...
TypeCall = node("TypeCall", "name", "type", "args")  # sizeof(T) new(T) make([]T, n)
Index = node("Index", "obj", "index")
SliceExpr = node("SliceExpr", "obj", "lo", "hi")     # a[lo..hi], lo/hi may be None
Field = node("Field", "obj", "name")            # checker sets: kind
Cast = node("Cast", "expr", "type")
StructLit = node("StructLit", "name", "inits")  # inits: list of (name, expr, tok)
ArrayLit = node("ArrayLit", "elems")

# ---------------------------------------------------------------- statements
Block = node("Block", "stmts")
VarDecl = node("VarDecl", "name", "type", "value", "mutable")  # checker sets: var
Assign = node("Assign", "op", "target", "value")
ExprStmt = node("ExprStmt", "expr")
If = node("If", "cond", "then", "els")          # els: Block | If | None
While = node("While", "cond", "body")
ForRange = node("ForRange", "var", "lo", "hi", "body")           # for i in a..b
ForEach = node("ForEach", "idx", "var", "iter", "body")          # for [i,] x in xs
Return = node("Return", "value")
Break = node("Break")
Continue = node("Continue")
Defer = node("Defer", "stmt")
Match = node("Match", "value", "arms")          # arms: list of MatchArm
MatchArm = node("MatchArm", "patterns", "body")  # patterns: list[Expr] or None for _

# ---------------------------------------------------------------- declarations
Param = node("Param", "name", "type")
FnDecl = node("FnDecl", "name", "params", "ret", "body", "owner", "has_self", "variadic", "is_extern")
StructDecl = node("StructDecl", "name", "fields")    # fields: list of Param
EnumDecl = node("EnumDecl", "name", "variants")      # variants: list of (name, Expr|None)
ImplDecl = node("ImplDecl", "name", "methods")
GlobalDecl = node("GlobalDecl", "decl", "is_const")  # decl: VarDecl
CInclude = node("CInclude", "header")
Link = node("Link", "lib")
Program = node("Program", "decls")
