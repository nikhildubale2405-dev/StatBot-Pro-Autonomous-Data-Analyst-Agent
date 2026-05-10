from __future__ import annotations

import ast


class UnsafeCodeError(ValueError):
    pass


BLOCKED_NODES = (
    ast.Import,
    ast.ImportFrom,
    ast.With,
    ast.AsyncWith,
    ast.Try,
    ast.Lambda,
    ast.ClassDef,
    ast.AsyncFunctionDef,
)

BLOCKED_NAMES = {
    "__import__",
    "breakpoint",
    "compile",
    "dir",
    "eval",
    "exec",
    "delattr",
    "getattr",
    "globals",
    "hasattr",
    "input",
    "locals",
    "open",
    "quit",
    "setattr",
    "exit",
    "vars",
}

BLOCKED_ATTRS = {
    "remove",
    "unlink",
    "rmdir",
    "chmod",
    "chown",
    "mkdir",
    "system",
    "popen",
    "spawn",
    "walk",
    "rmtree",
    "copytree",
    "copyfile",
    "dump",
    "dumps",
    "save",
    "to_clipboard",
    "to_csv",
    "to_excel",
    "to_feather",
    "to_gbq",
    "to_hdf",
    "to_json",
    "to_markdown",
    "to_orc",
    "to_parquet",
    "to_pickle",
    "to_sql",
    "to_stata",
    "to_xml",
    "touch",
    "write",
    "write_bytes",
    "write_text",
}


def validate_generated_code(code: str) -> None:
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise UnsafeCodeError(f"Generated code is not valid Python: {exc}") from exc

    for node in ast.walk(tree):
        if isinstance(node, BLOCKED_NODES):
            raise UnsafeCodeError(f"Blocked Python construct: {type(node).__name__}")
        if isinstance(node, ast.Name) and node.id in BLOCKED_NAMES:
            raise UnsafeCodeError(f"Blocked name: {node.id}")
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("__") or node.attr in BLOCKED_ATTRS:
                raise UnsafeCodeError(f"Blocked attribute access: {node.attr}")
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in BLOCKED_NAMES:
                raise UnsafeCodeError(f"Blocked call: {func.id}")
            if isinstance(func, ast.Attribute) and func.attr in BLOCKED_ATTRS:
                raise UnsafeCodeError(f"Blocked method call: {func.attr}")
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "savefig"
                and not (isinstance(func.value, ast.Name) and func.value.id == "plt")
            ):
                raise UnsafeCodeError("Charts must be saved with plt.savefig.")
