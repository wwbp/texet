from app.schemas import ChatRequest, ChatResponse


async def process_chat(payload: ChatRequest) -> ChatResponse:
    return ChatResponse(user_id=payload.user_id, message=payload.message, status="received")
