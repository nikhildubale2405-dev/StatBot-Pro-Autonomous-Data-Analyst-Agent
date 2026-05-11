from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import Column, JSON, String
from sqlmodel import Field, SQLModel


def new_id() -> str:
    return str(uuid4())


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class UserSession(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    user_id: str | None = Field(default=None, index=True)
    title: str = "Untitled analysis"
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class User(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    email: str = Field(sa_column=Column(String, unique=True, index=True, nullable=False))
    name: str
    password_hash: str
    created_at: datetime = Field(default_factory=utc_now)


class UploadedFile(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    session_id: str = Field(index=True)
    original_name: str
    stored_name: str
    content_type: str | None = None
    size_bytes: int = 0
    profile: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)


class ConversationMessage(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    session_id: str = Field(index=True)
    file_id: str | None = Field(default=None, index=True)
    role: str
    content: str
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)


class AgentRunAttempt(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    session_id: str = Field(index=True)
    file_id: str = Field(index=True)
    question: str
    attempt_number: int
    generated_code: str
    success: bool = False
    stdout: str = ""
    stderr: str = ""
    error_message: str | None = None
    execution_time: float | None = None
    created_at: datetime = Field(default_factory=utc_now)


class GeneratedOutput(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    session_id: str = Field(index=True)
    file_id: str = Field(index=True)
    message_id: str | None = Field(default=None, index=True)
    output_type: str
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)


class ChartArtifact(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    session_id: str = Field(index=True)
    file_id: str = Field(index=True)
    message_id: str | None = Field(default=None, index=True)
    title: str | None = None
    relative_path: str
    created_at: datetime = Field(default_factory=utc_now)
