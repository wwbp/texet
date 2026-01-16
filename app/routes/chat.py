from fastapi import APIRouter, BackgroundTasks, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_auth
from app.db import get_async_session
from app.schemas import ChatQueuedResponse, ChatRequest
from app.services.chat import process_chat

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post(
    "",
    response_model=ChatQueuedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_auth)],
)
async def chat(
    payload: ChatRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_async_session),
) -> ChatQueuedResponse:
    return await process_chat(session, payload, background_tasks)
