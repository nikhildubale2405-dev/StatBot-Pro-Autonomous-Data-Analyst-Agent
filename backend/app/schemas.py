from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class FileProfile(BaseModel):
    file_name: str
    shape: dict[str, int]
    columns: list[str]
    data_types: dict[str, str]
    numeric_like_columns: list[str] = Field(default_factory=list)
    missing_values: dict[str, int]
    sample_rows: list[dict[str, Any]]
    summary_statistics: dict[str, Any]


class UploadResponse(BaseModel):
    session_id: str
    file_id: str
    profile: FileProfile


class ChatRequest(BaseModel):
    session_id: str
    file_id: str
    question: str = Field(min_length=1, max_length=2000)

    @field_validator("session_id", "file_id", "question")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Value cannot be empty.")
        return cleaned


class TableResult(BaseModel):
    name: str
    columns: list[str]
    rows: list[dict[str, Any]]


class ChartResult(BaseModel):
    id: str
    title: str | None = None
    url: str


class ChatResponse(BaseModel):
    session_id: str
    file_id: str
    message_id: str
    answer: str
    tables: list[TableResult] = Field(default_factory=list)
    charts: list[ChartResult] = Field(default_factory=list)
    stdout: str = ""
    retry_count: int = 0


class MessageResponse(BaseModel):
    id: str
    role: str
    content: str
    payload: dict[str, Any]
    created_at: datetime


class SessionResponse(BaseModel):
    id: str
    title: str
    created_at: datetime
    updated_at: datetime
    messages: list[MessageResponse]
    files: list[dict[str, Any]]
