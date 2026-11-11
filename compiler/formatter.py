"""Source formatter (`flint fmt`).

Deliberately conservative: it only changes whitespace and trailing semicolons, so
it can never change what a program means.
  - indentation: 4 spaces per level; all brackets opened on one line share one level
  - a line ending in a binary operator indents the next line one extra level
  - trailing whitespace and trailing ';' are removed
  - runs of blank lines collapse to one; no blank lines at start/end of file
  - lines inside /* block comments */ and multi-line `raw strings` are left untouched
"""

OPEN = "([{"
CLOSE = ")]}"
CONT_ENDINGS = ("+", "-", "*", "/", "%", "&", "|", "^", "=", "<", ">", "!", "~", "..")


def scan_code(line, state):
    """Split a line into (code_without_strings_and_comments, state_at_end_of_line).

    state is None, "comment" (inside /* */) or "raw" (inside a `raw string`).
    String/char contents are replaced by spaces so brackets inside them are ignored.
    """
    out = []
    i, n = 0, len(line)
    while i < n:
        if state is not None:
            close = "*/" if state == "comment" else "`"
            end = line.find(close, i)
            if end < 0:
                return "".join(out), state
            if state == "raw":
                out.append("`")
            i = end + len(close)
            state = None
            continue
        c = line[i]
        if line.startswith("//", i):
            break
        if line.startswith("/*", i):
            state = "comment"
            i += 2
            continue
        if c == "`":
            out.append("`")
            state = "raw"
            i += 1
            continue
        if c in "\"'":
            q = c
            out.append(q)
            i += 1
            while i < n and line[i] != q:
                if line[i] == "\\":
                    i += 1
                i += 1
                out.append(" ")
            out.append(q)
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out), state


def format_source(src):
    lines = src.replace("\r\n", "\n").replace("\t", "    ").split("\n")
    out = []
    stack = []            # indent level of each open bracket
    state = None          # None | "comment" | "raw" carried across lines
    continuation = False  # previous code line ended with an operator
    blank_run = 0

    for raw in lines:
        stripped = raw.strip()
        if state is not None:
            # inside a block comment or raw string: keep the line exactly (raw strings are data)
            code, new_state = scan_code(raw, state)
            out.append(raw if state == "raw" else raw.rstrip())
            blank_run = 0
            if new_state is None:
                state = None
                # the rest of the line after the closing delimiter may open brackets
                for ch in code:
                    if ch in OPEN:
                        stack.append((stack[-1] if stack else 0) + 1)
                    elif ch in CLOSE and stack:
                        stack.pop()
            continue
        if not stripped:
            blank_run += 1
            if blank_run == 1 and out:
                out.append("")
            continue
        blank_run = 0

        code, still_state = scan_code(stripped, None)
        # leading closers dedent this line
        k = 0
        lead = code.lstrip()
        while k < len(lead) and lead[k] in CLOSE:
            if stack:
                stack.pop()
            k += 1
        base = stack[-1] if stack else 0
        level = base + (1 if continuation and k == 0 else 0)

        # trailing ';' is redundant (newline ends a statement)
        text = stripped
        if code.rstrip().endswith(";"):
            # remove the last ';' that belongs to code (before any // comment)
            cpos = code.rstrip().rfind(";")
            text = (text[:cpos] + text[cpos + 1:]).rstrip()
            if not text:
                continue
            code, still_state = scan_code(text, None)

        out.append("    " * level + text)

        # update bracket stack with the rest of the line
        new_level = base + 1
        for c in code[k:]:  # leading closers were already popped
            if c in OPEN:
                stack.append(new_level)
            elif c in CLOSE and stack:
                stack.pop()
        state = still_state
        tail = code.rstrip()
        continuation = bool(tail) and tail.endswith(CONT_ENDINGS) and not tail.endswith(("->",)) or tail.endswith("=>")

    while out and out[-1] == "":
        out.pop()
    return "\n".join(out) + "\n"
