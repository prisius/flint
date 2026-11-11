"""Semantic types.

Every type has:
  key   - unique string used for equality and hashing ("i32", "*Vec2", "[]u8")
  mangle- identifier-safe version of key used in generated C names
  c     - the C type name used in generated code
"""

INT_TYPES = ["i8", "u8", "i16", "u16", "i32", "u32", "i64", "u64"]
FLOAT_TYPES = ["f32", "f64"]
# numeric rank used for implicit widening in binary ops
RANK = {n: i for i, n in enumerate(["char"] + INT_TYPES + FLOAT_TYPES)}

C_PRIM = {
    "i8": "int8_t", "u8": "uint8_t", "i16": "int16_t", "u16": "uint16_t",
    "i32": "int32_t", "u32": "uint32_t", "i64": "int64_t", "u64": "uint64_t",
    "f32": "float", "f64": "double", "bool": "bool", "void": "void",
    "char": "char",
}

# Friendly aliases
ALIASES = {"int": "i64", "uint": "u64", "float": "f64", "byte": "u8"}


class Type:
    key = "?"
    mangle = "?"
    c = "?"

    def __eq__(self, other):
        return isinstance(other, Type) and self.key == other.key

    def __hash__(self):
        return hash(self.key)

    def __repr__(self):
        return self.key

    __str__ = __repr__

    # classification helpers
    def is_int(self):
        return isinstance(self, Prim) and (self.key in INT_TYPES or self.key == "char")

    def is_float(self):
        return isinstance(self, Prim) and self.key in FLOAT_TYPES

    def is_numeric(self):
        return self.is_int() or self.is_float()

    def is_signed(self):
        return self.key in ("i8", "i16", "i32", "i64", "char")

    def is_ptr(self):
        return isinstance(self, Ptr)

    def is_void(self):
        return self.key == "void"


class Prim(Type):
    def __init__(self, name):
        self.key = self.mangle = name
        self.c = C_PRIM[name]


class StrT(Type):
    key = mangle = "str"
    c = "fl_str"


class NullT(Type):
    key = mangle = "null"
    c = "void*"


class Ptr(Type):
    def __init__(self, elem):
        self.elem = elem
        self.key = "*" + elem.key
        self.mangle = "p_" + elem.mangle
        self.c = elem.c + "*"


class Slice(Type):
    def __init__(self, elem):
        self.elem = elem
        self.key = "[]" + elem.key
        self.mangle = "s_" + elem.mangle
        self.c = "fl_slice_" + elem.mangle


class Array(Type):
    """Fixed-size array. Wrapped in a C struct so it has value semantics."""

    def __init__(self, elem, n):
        self.elem = elem
        self.n = n
        self.key = "[%d]%s" % (n, elem.key)
        self.mangle = "a%d_%s" % (n, elem.mangle)
        self.c = "fl_arr_" + self.mangle


class ListT(Type):
    """Growable array: {ptr, len, cap}."""

    def __init__(self, elem):
        self.elem = elem
        self.key = "list[%s]" % elem.key
        self.mangle = "l_" + elem.mangle
        self.c = "fl_list_" + elem.mangle


class StructT(Type):
    def __init__(self, name, decl):
        self.name = name
        self.decl = decl
        self.key = self.mangle = self.c = name
        self.fields = {}   # name -> Type (ordered)
        self.methods = {}  # name -> FnInfo


class EnumT(Type):
    def __init__(self, name, decl):
        self.name = name
        self.decl = decl
        self.key = self.mangle = self.c = name
        self.variants = {}  # name -> int value


PRIMS = {n: Prim(n) for n in C_PRIM}
I64 = PRIMS["i64"]
I32 = PRIMS["i32"]
U8 = PRIMS["u8"]
F64 = PRIMS["f64"]
BOOL = PRIMS["bool"]
VOID = PRIMS["void"]
CHAR = PRIMS["char"]
STR = StrT()
NULL = NullT()
VOIDPTR = Ptr(VOID)


def elem_type(t):
    """Element type for indexable/iterable types, else None."""
    if isinstance(t, (Slice, Array, ListT)):
        return t.elem
    if isinstance(t, StrT):
        return CHAR
    return None


def wider(a, b):
    return a if RANK[a.key] >= RANK[b.key] else b
