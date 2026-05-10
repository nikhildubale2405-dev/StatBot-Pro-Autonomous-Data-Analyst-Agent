from __future__ import annotations

import argparse
import ast
import contextlib
import io
import json
import math
import time
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


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


def json_safe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        as_float = float(value)
        return None if math.isnan(as_float) or math.isinf(as_float) else as_float
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (pd.Timestamp,)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    return value


def validate_generated_code(code: str) -> None:
    tree = ast.parse(code)
    for node in ast.walk(tree):
        if isinstance(node, BLOCKED_NODES):
            raise ValueError(f"Blocked Python construct: {type(node).__name__}")
        if isinstance(node, ast.Name) and node.id in BLOCKED_NAMES:
            raise ValueError(f"Blocked name: {node.id}")
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("__") or node.attr in BLOCKED_ATTRS:
                raise ValueError(f"Blocked attribute access: {node.attr}")
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in BLOCKED_NAMES:
                raise ValueError(f"Blocked call: {func.id}")
            if isinstance(func, ast.Attribute) and func.attr in BLOCKED_ATTRS:
                raise ValueError(f"Blocked method call: {func.attr}")
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "savefig"
                and not (isinstance(func.value, ast.Name) and func.value.id == "plt")
            ):
                raise ValueError("Charts must be saved with plt.savefig.")


def load_dataframe(input_file: Path) -> pd.DataFrame:
    suffix = input_file.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(input_file)
    if suffix in {".xlsx", ".xls"}:
        return pd.read_excel(input_file, sheet_name=0)
    raise ValueError(f"Unsupported file extension: {suffix}")


def table_payload(name: str, value: Any, max_rows: int = 100) -> dict[str, Any]:
    if isinstance(value, pd.Series):
        frame = value.reset_index()
    elif isinstance(value, pd.DataFrame):
        frame = value.copy()
    else:
        frame = pd.DataFrame(value)

    frame = frame.head(max_rows)
    return {
        "name": str(name),
        "columns": [str(col) for col in frame.columns],
        "rows": json_safe(frame.to_dict(orient="records")),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--script", required=True)
    parser.add_argument("--input-file", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    started = time.monotonic()
    script_path = Path(args.script)
    input_file = Path(args.input_file)
    output_dir = Path(args.output_dir)

    result: dict[str, Any] = {
        "success": False,
        "stdout": "",
        "stderr": "",
        "error": None,
        "insights": [],
        "tables": [],
        "charts": [],
        "execution_time": 0,
    }

    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        code = script_path.read_text(encoding="utf-8")
        validate_generated_code(code)
        df = load_dataframe(input_file)
        input_resolved = input_file.resolve()

        def _ensure_input_path(path: Any) -> Path:
            candidate = Path(path).resolve()
            if candidate != input_resolved:
                raise ValueError("Generated code may only read the uploaded input file.")
            return candidate

        original_read_csv = pd.read_csv
        original_read_excel = pd.read_excel

        def restricted_read_csv(path: Any, *read_args: Any, **read_kwargs: Any) -> pd.DataFrame:
            return original_read_csv(_ensure_input_path(path), *read_args, **read_kwargs)

        def restricted_read_excel(path: Any, *read_args: Any, **read_kwargs: Any) -> pd.DataFrame:
            return original_read_excel(_ensure_input_path(path), *read_args, **read_kwargs)

        pd.read_csv = restricted_read_csv
        pd.read_excel = restricted_read_excel

        def emit_insight(text: Any) -> None:
            result["insights"].append(str(text))

        def emit_table(name: str, value: Any, max_rows: int = 100) -> None:
            result["tables"].append(table_payload(name, value, max_rows=max_rows))

        def emit_chart(path: Any, title: str | None = None) -> None:
            chart_path = Path(path).resolve()
            output_root = output_dir.resolve()
            if output_root not in chart_path.parents:
                raise ValueError("Charts must be saved inside OUTPUT_DIR.")
            if not chart_path.exists():
                raise ValueError(f"Chart file was not found: {chart_path.name}")
            result["charts"].append({"title": title, "path": f"{output_dir.name}/{chart_path.name}"})

        safe_builtins = dict(__builtins__ if isinstance(__builtins__, dict) else __builtins__.__dict__)
        for name in BLOCKED_NAMES:
            safe_builtins.pop(name, None)

        globals_dict = {
            "__builtins__": safe_builtins,
            "df": df,
            "pd": pd,
            "np": np,
            "plt": plt,
            "DATA_PATH": input_file,
            "OUTPUT_DIR": output_dir,
            "emit_insight": emit_insight,
            "emit_table": emit_table,
            "emit_chart": emit_chart,
        }

        stdout_buffer = io.StringIO()
        with contextlib.redirect_stdout(stdout_buffer):
            exec(compile(code, str(script_path), "exec"), globals_dict, globals_dict)

        result["stdout"] = stdout_buffer.getvalue()
        result["success"] = True
    except Exception as exc:
        result["error"] = str(exc)
        result["stderr"] = f"{type(exc).__name__}: {exc}"
    finally:
        result["execution_time"] = round(time.monotonic() - started, 4)
        print(json.dumps(json_safe(result), ensure_ascii=False))

    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
