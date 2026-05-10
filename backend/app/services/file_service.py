from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pandas as pd
from fastapi import HTTPException, UploadFile

from app.core.config import get_settings
from app.utils.json import json_safe

ALLOWED_EXTENSIONS = {".csv", ".xlsx", ".xls"}


def dataframe_from_path(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(path)
    if suffix in {".xlsx", ".xls"}:
        return pd.read_excel(path, sheet_name=0)
    raise ValueError(f"Unsupported file type: {suffix}")


def profile_dataframe(df: pd.DataFrame, file_name: str) -> dict:
    summary = df.describe(include="all").to_dict()
    numeric_like_columns = []
    for col in df.select_dtypes(exclude="number").columns:
        cleaned = df[col].astype(str).str.replace(",", "", regex=False).str.strip()
        numeric_ratio = pd.to_numeric(cleaned, errors="coerce").notna().mean()
        if numeric_ratio >= 0.75:
            numeric_like_columns.append(str(col))

    return json_safe(
        {
            "file_name": file_name,
            "shape": {"rows": int(df.shape[0]), "columns": int(df.shape[1])},
            "columns": [str(col) for col in df.columns],
            "data_types": {str(col): str(dtype) for col, dtype in df.dtypes.items()},
            "numeric_like_columns": numeric_like_columns,
            "missing_values": {str(col): int(count) for col, count in df.isna().sum().items()},
            "sample_rows": df.head(8).to_dict(orient="records"),
            "summary_statistics": summary,
        }
    )


def save_upload(upload: UploadFile) -> tuple[str, Path, int]:
    settings = get_settings()
    original_name = Path(upload.filename or "dataset").name
    suffix = Path(original_name).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Only CSV, XLSX, and XLS files are supported.")

    stored_name = f"{uuid4()}{suffix}"
    target = settings.upload_dir / stored_name
    bytes_written = 0
    try:
        with target.open("wb") as destination:
            while chunk := upload.file.read(1024 * 1024):
                bytes_written += len(chunk)
                if bytes_written > settings.max_upload_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"File is too large. Maximum upload size is {settings.max_upload_bytes // (1024 * 1024)} MB.",
                    )
                destination.write(chunk)
    except Exception:
        target.unlink(missing_ok=True)
        raise

    if bytes_written == 0:
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    return stored_name, target, bytes_written
