from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlmodel import Session, select

from app.core.config import get_settings
from app.core.database import get_session
from app.core.security import create_access_token, decode_access_token, hash_password, verify_password
from app.models.db import ChartArtifact, ConversationMessage, UploadedFile, User, UserSession, utc_now
from app.schemas import AuthResponse, ChatRequest, ChatResponse, FileProfile, LoginRequest, SessionResponse, SignupRequest, UploadResponse, UserResponse
from app.services.agent_service import AgentService
from app.services.file_service import dataframe_from_path, profile_dataframe, save_upload

router = APIRouter()
bearer_scheme = HTTPBearer(auto_error=False)


def public_user(user: User) -> UserResponse:
    return UserResponse(id=user.id, name=user.name, email=user.email)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_session),
) -> User:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Authentication required.")
    try:
        payload = decode_access_token(credentials.credentials)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired token.") from exc
    user = db.get(User, payload["sub"])
    if user is None:
        raise HTTPException(status_code=401, detail="User not found.")
    return user


def require_owned_session(db: Session, session_id: str, user: User) -> UserSession:
    session = db.get(UserSession, session_id)
    if session is None or session.user_id != user.id:
        raise HTTPException(status_code=404, detail="Session not found.")
    return session


def require_owned_file(db: Session, file_id: str, user: User) -> UploadedFile:
    uploaded = db.get(UploadedFile, file_id)
    if uploaded is None:
        raise HTTPException(status_code=404, detail="File not found.")
    require_owned_session(db, uploaded.session_id, user)
    return uploaded


@router.post("/auth/signup", response_model=AuthResponse)
def signup(request: SignupRequest, db: Session = Depends(get_session)) -> AuthResponse:
    existing = db.exec(select(User).where(User.email == request.email)).first()
    if existing is not None:
        raise HTTPException(status_code=409, detail="An account with this email already exists.")
    user = User(email=request.email, name=request.name, password_hash=hash_password(request.password))
    db.add(user)
    db.commit()
    db.refresh(user)
    return AuthResponse(access_token=create_access_token(user.id), user=public_user(user))


@router.post("/auth/login", response_model=AuthResponse)
def login(request: LoginRequest, db: Session = Depends(get_session)) -> AuthResponse:
    user = db.exec(select(User).where(User.email == request.email)).first()
    if user is None or not verify_password(request.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    return AuthResponse(access_token=create_access_token(user.id), user=public_user(user))


@router.get("/auth/me", response_model=UserResponse)
def me(current_user: User = Depends(get_current_user)) -> UserResponse:
    return public_user(current_user)


@router.post("/upload", response_model=UploadResponse)
def upload_file(
    file: UploadFile = File(...),
    session_id: str | None = Form(default=None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> UploadResponse:
    session = None
    if session_id:
        session = require_owned_session(db, session_id, current_user)

    stored_name, path, size_bytes = save_upload(file)
    try:
        df = dataframe_from_path(path)
    except Exception as exc:
        path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"Could not read dataset: {exc}") from exc

    if session is None:
        session = UserSession(user_id=current_user.id, title=Path(file.filename or "Dataset").stem or "Dataset analysis")
        db.add(session)
        db.commit()
        db.refresh(session)

    profile = profile_dataframe(df, file.filename or stored_name)
    uploaded = UploadedFile(
        session_id=session.id,
        original_name=file.filename or stored_name,
        stored_name=stored_name,
        content_type=file.content_type,
        size_bytes=size_bytes,
        profile=profile,
    )
    db.add(uploaded)
    session.updated_at = utc_now()
    db.add(session)
    db.commit()
    db.refresh(uploaded)
    return UploadResponse(session_id=session.id, file_id=uploaded.id, profile=FileProfile(**profile))


@router.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_session)) -> ChatResponse:
    session = require_owned_session(db, request.session_id, current_user)
    uploaded = db.get(UploadedFile, request.file_id)
    if session is None or uploaded is None or uploaded.session_id != session.id:
        raise HTTPException(status_code=404, detail="Session or file not found.")

    user_message = ConversationMessage(
        session_id=session.id,
        file_id=uploaded.id,
        role="user",
        content=request.question,
    )
    db.add(user_message)
    db.commit()

    service = AgentService(db)
    response = service.answer_question(
        session_id=session.id,
        file_id=uploaded.id,
        stored_name=uploaded.stored_name,
        profile=uploaded.profile,
        question=request.question,
    )

    assistant_message = ConversationMessage(
        session_id=session.id,
        file_id=uploaded.id,
        role="assistant",
        content=response["answer"],
        payload={"tables": response.get("tables", []), "charts": response.get("charts", []), "error": response.get("error")},
    )
    db.add(assistant_message)
    session.updated_at = utc_now()
    db.add(session)
    db.commit()
    db.refresh(assistant_message)

    response = service.persist_outputs(session_id=session.id, file_id=uploaded.id, message_id=assistant_message.id, response=response)
    assistant_message.payload = {"tables": response.get("tables", []), "charts": response.get("charts", []), "error": response.get("error")}
    db.add(assistant_message)
    db.commit()

    if response.get("error") and not response.get("tables") and not response.get("charts"):
        response["answer"] = f"{response['answer']}\n\n{response['error']}"

    return ChatResponse(
        session_id=session.id,
        file_id=uploaded.id,
        message_id=assistant_message.id,
        answer=response["answer"],
        tables=response.get("tables", []),
        charts=response.get("charts", []),
        stdout=response.get("stdout", ""),
        retry_count=response.get("retry_count", 0),
    )


@router.get("/sessions/{session_id}", response_model=SessionResponse)
def get_session_detail(session_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_session)) -> SessionResponse:
    session = require_owned_session(db, session_id, current_user)
    messages = db.exec(select(ConversationMessage).where(ConversationMessage.session_id == session.id).order_by(ConversationMessage.created_at)).all()
    files = db.exec(select(UploadedFile).where(UploadedFile.session_id == session.id).order_by(UploadedFile.created_at)).all()
    return SessionResponse(
        id=session.id,
        title=session.title,
        created_at=session.created_at,
        updated_at=session.updated_at,
        messages=[
            {"id": msg.id, "role": msg.role, "content": msg.content, "payload": msg.payload, "created_at": msg.created_at}
            for msg in messages
        ],
        files=[
            {
                "id": item.id,
                "original_name": item.original_name,
                "size_bytes": item.size_bytes,
                "created_at": item.created_at,
                "profile": item.profile,
            }
            for item in files
        ],
    )


@router.get("/files/{file_id}/profile", response_model=FileProfile)
def get_profile(file_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_session)) -> FileProfile:
    uploaded = require_owned_file(db, file_id, current_user)
    return FileProfile(**uploaded.profile)


@router.get("/files/{file_id}/chart/{chart_id}")
def get_chart(file_id: str, chart_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_session)) -> FileResponse:
    settings = get_settings()
    require_owned_file(db, file_id, current_user)
    chart = db.get(ChartArtifact, chart_id)
    if chart is None or chart.file_id != file_id:
        raise HTTPException(status_code=404, detail="Chart not found.")
    path = (settings.output_dir / chart.relative_path).resolve()
    output_root = settings.output_dir.resolve()
    if output_root not in path.parents:
        raise HTTPException(status_code=400, detail="Invalid chart path.")
    if not path.exists():
        raise HTTPException(status_code=404, detail="Chart file is missing.")
    return FileResponse(path, media_type="image/png", filename=path.name)
