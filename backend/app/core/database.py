from collections.abc import Generator

from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine

from app.core.config import get_settings

settings = get_settings()
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args)


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    _ensure_legacy_columns()


def _ensure_legacy_columns() -> None:
    inspector = inspect(engine)
    if "usersession" not in inspector.get_table_names():
        return
    session_columns = {column["name"] for column in inspector.get_columns("usersession")}
    if "user_id" not in session_columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE usersession ADD COLUMN user_id VARCHAR"))


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session
