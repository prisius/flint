"""Lexer: source text -> list of Tokens.

Newlines are significant only as statement terminators: like Go, a ';' is
inserted automatically at a newline when the last token on the line could end
a statement (identifier, literal, ')', ']', '}', return/break/continue).
Newlines inside (...) and [...] are ignored.
"""

from .errors import CompileError

KEYWORDS = {
    "fn", "let", "var", "const", "if", "else", "while", "for", "in", "return",
    "break", "continue", "struct", "enum", "impl", "import", "extern", "true",
    "false", "null", "as", "defer", "match", "cinclude", "link",
}

# Longest first so that greedy matching works.
PUNCT = [
    "<<=", ">>=", "...", "..",
    "==", "!=", "<=", ">=", "&&", "||", "<<", ">>", "->", "=>",
    "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=",
    "+", "-", "*", "/", "%", "&", "|", "^", "~", "!", "=", "<", ">",
    "(", ")", "[", "]", "{", "}", ",", ";", ":", ".",
]

ENDS_STATEMENT = {"IDENT", "INT", "FLOAT", "STRING", "CHAR"}
ENDS_STATEMENT_KW = {"true", "false", "null", "return", "break", "continue"}
ENDS_STATEMENT_PUNCT = {")", "]", "}"}


class Token:
    __slots__ = ("kind", "value", "file", "line", "col")

    def __init__(self, kind, value, file, line, col):
        self.kind = kind      # IDENT INT FLOAT STRING CHAR KW PUNCT EOF
        self.value = value
        self.file = file
        self.line = line
        self.col = col

    def __repr__(self):
        return "Token(%s,%r,%d:%d)" % (self.kind, self.value, self.line, self.col)

    def is_(self, kind, value=None):
        return self.kind == kind and (value is None or self.value == value)


def lex(src, file="<input>"):
    toks = []
    i, line, col = 0, 1, 1
    n = len(src)
    depth = 0  # nesting of ( and [

    def err(msg):
        raise CompileError(msg, file, line, col)

    def add(kind, value, l, c):
        toks.append(Token(kind, value, file, l, c))

    def needs_semi():
        if not toks:
            return False
        t = toks[-1]
        if t.kind in ENDS_STATEMENT:
            return True
        if t.kind == "KW" and t.value in ENDS_STATEMENT_KW:
            return True
        if t.kind == "PUNCT" and t.value in ENDS_STATEMENT_PUNCT:
            return True
        return False

    while i < n:
        ch = src[i]
        if ch == "\n":
            if depth == 0 and needs_semi():
                add("PUNCT", ";", line, col)
            i += 1
            line += 1
            col = 1
            continue
        if ch in " \t\r":
            i += 1
            col += 1
            continue
        if src.startswith("//", i):
            while i < n and src[i] != "\n":
                i += 1
            continue
        if src.startswith("/*", i):
            end = src.find("*/", i + 2)
            if end < 0:
                err("unterminated block comment")
            chunk = src[i:end + 2]
            nl = chunk.count("\n")
            if nl:
                if depth == 0 and needs_semi():
                    add("PUNCT", ";", line, col)
                line += nl
                col = len(chunk) - chunk.rfind("\n")
            else:
                col += len(chunk)
            i = end + 2
            continue

        sl, sc = line, col
        if ch.isalpha() or ch == "_":
            j = i
            while j < n and (src[j].isalnum() or src[j] == "_"):
                j += 1
            word = src[i:j]
            add("KW" if word in KEYWORDS else "IDENT", word, sl, sc)
            col += j - i
            i = j
            continue
        if ch.isdigit():
            j = i
            is_float = False
            if src.startswith(("0x", "0X"), i):
                j += 2
                while j < n and (src[j] in "0123456789abcdefABCDEF_"):
                    j += 1
                text = src[i:j].replace("_", "")
                value = int(text, 16)
            elif src.startswith(("0b", "0B"), i):
                j += 2
                while j < n and src[j] in "01_":
                    j += 1
                value = int(src[i + 2:j].replace("_", ""), 2)
            else:
                while j < n and (src[j].isdigit() or src[j] == "_"):
                    j += 1
                # '..' is the range operator, not a decimal point
                if j < n and src[j] == "." and not src.startswith("..", j) and j + 1 < n and src[j + 1].isdigit():
                    is_float = True
                    j += 1
                    while j < n and (src[j].isdigit() or src[j] == "_"):
                        j += 1
                if j < n and src[j] in "eE":
                    k = j + 1
                    if k < n and src[k] in "+-":
                        k += 1
                    if k < n and src[k].isdigit():
                        is_float = True
                        j = k
                        while j < n and src[j].isdigit():
                            j += 1
                text = src[i:j].replace("_", "")
                value = float(text) if is_float else int(text)
            if j < n and (src[j].isalpha() or src[j] == "_"):
                err("invalid number literal")
            add("FLOAT" if is_float else "INT", value, sl, sc)
            col += j - i
            i = j
            continue
        if ch == '"':
            j = i + 1
            while j < n and src[j] != '"':
                if src[j] == "\\":
                    j += 1
                if j < n and src[j] == "\n":
                    err("unterminated string literal")
                j += 1
            if j >= n:
                err("unterminated string literal")
            add("STRING", src[i + 1:j], sl, sc)  # raw; escapes decoded by parser
            col += j + 1 - i
            i = j + 1
            continue
        if ch == "'":
            j = i + 1
            if j < n and src[j] == "\\":
                j += 2
            else:
                j += 1
            if j >= n or src[j] != "'":
                err("bad character literal")
            raw = src[i + 1:j]
            val = decode_escapes(raw, lambda m: CompileError(m, file, sl, sc))
            if len(val) != 1:
                err("character literal must be a single byte")
            add("CHAR", val[0], sl, sc)
            col += j + 1 - i
            i = j + 1
            continue
        for p in PUNCT:
            if src.startswith(p, i):
                if p in "([":
                    depth += 1
                elif p in ")]":
                    depth = max(0, depth - 1)
                add("PUNCT", p, sl, sc)
                i += len(p)
                col += len(p)
                break
        else:
            err("unexpected character %r" % ch)

    if needs_semi():
        add("PUNCT", ";", line, col)
    add("EOF", None, line, col)
    return toks


ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", "\\": "\\", '"': '"', "'": "'", "e": "\x1b"}


def decode_escapes(raw, make_err):
    """Decode backslash escapes; returns bytes (UTF-8 for normal text)."""
    out = bytearray()
    i = 0
    while i < len(raw):
        c = raw[i]
        if c == "\\":
            i += 1
            if i >= len(raw):
                raise make_err("bad escape at end of literal")
            e = raw[i]
            if e in ESCAPES:
                out += ESCAPES[e].encode()
            elif e == "x":
                h = raw[i + 1:i + 3]
                try:
                    out.append(int(h, 16))
                except ValueError:
                    raise make_err("bad \\x escape")
                i += 2
            else:
                raise make_err("unknown escape \\%s" % e)
        else:
            out += c.encode("utf-8")
        i += 1
    return bytes(out)
