class CompileError(Exception):
    """An error in the user's Flint program, with a source location."""

    def __init__(self, msg, file=None, line=0, col=0):
        super().__init__(msg)
        self.msg = msg
        self.file = file
        self.line = line
        self.col = col

    def format(self):
        if self.file:
            return "%s:%d:%d: error: %s" % (self.file, self.line, self.col, self.msg)
        return "error: %s" % self.msg


def err_at(node, msg):
    """Raise a CompileError located at an AST node or token."""
    raise CompileError(msg, getattr(node, "file", None), getattr(node, "line", 0), getattr(node, "col", 0))
