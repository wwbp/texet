from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_auth
from app.db import get_async_session
from app.schemas import ChatRequest, ChatResponse
from app.services.chat import process_chat

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("", response_model=ChatResponse, dependencies=[Depends(require_auth)])
async def chat(
    payload: ChatRequest,
    session: AsyncSession = Depends(get_async_session),
) -> ChatResponse:
    return await process_chat(session, payload)
