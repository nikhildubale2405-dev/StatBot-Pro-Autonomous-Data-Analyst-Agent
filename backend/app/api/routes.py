from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlmodel import Session, select

from app.core.config import get_settings
from app.core.database import get_session
from app.models.db import ChartArtifact, ConversationMessage, UploadedFile, UserSession, utc_now
from app.schemas import ChatRequest, ChatResponse, FileProfile, SessionResponse, UploadResponse
from app.services.agent_service import AgentService
from app.services.file_service import dataframe_from_path, profile_dataframe, save_upload

router = APIRouter()


@router.post("/upload", response_model=UploadResponse)
def upload_file(
    file: UploadFile = File(...),
    session_id: str | None = Form(default=None),
    db: Session = Depends(get_session),
) -> UploadResponse:
    session = None
    if session_id:
        session = db.get(UserSession, session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found.")

    stored_name, path, size_bytes = save_upload(file)
    try:
        df = dataframe_from_path(path)
    except Exception as exc:
        path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"Could not read dataset: {exc}") from exc

    if session is None:
        session = UserSession(title=Path(file.filename or "Dataset").stem or "Dataset analysis")
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
def chat(request: ChatRequest, db: Session = Depends(get_session)) -> ChatResponse:
    session = db.get(UserSession, request.session_id)
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
def get_session_detail(session_id: str, db: Session = Depends(get_session)) -> SessionResponse:
    session = db.get(UserSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")
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
def get_profile(file_id: str, db: Session = Depends(get_session)) -> FileProfile:
    uploaded = db.get(UploadedFile, file_id)
    if uploaded is None:
        raise HTTPException(status_code=404, detail="File not found.")
    return FileProfile(**uploaded.profile)


@router.get("/files/{file_id}/chart/{chart_id}")
def get_chart(file_id: str, chart_id: str, db: Session = Depends(get_session)) -> FileResponse:
    settings = get_settings()
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
