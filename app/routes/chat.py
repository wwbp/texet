from fastapi import APIRouter, Depends

from app.auth import require_auth
from app.schemas import ChatRequest, ChatResponse
from app.services.chat import process_chat

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("", response_model=ChatResponse, dependencies=[Depends(require_auth)])
async def chat(payload: ChatRequest) -> ChatResponse:
    return await process_chat(payload)
