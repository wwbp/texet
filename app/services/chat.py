from sqlalchemy.ext.asyncio import AsyncSession

from app.db_ops import (
    create_conversation,
    create_utterance,
    get_or_create_bot_speaker,
    get_or_create_speaker,
)
from app.schemas import ChatRequest, ChatResponse


async def _generate_reply(message: str) -> str:
    return f"echo:{message}"


async def process_chat(session: AsyncSession, payload: ChatRequest) -> ChatResponse:
    async with session.begin():
        speaker = await get_or_create_speaker(
            session, payload.user_id, meta={"type": "user"}
        )
        bot = await get_or_create_bot_speaker(session, payload.user_id)

        conversation = await create_conversation(session, speaker.id)

        user_utterance = await create_utterance(
            session,
            conversation.id,
            speaker.id,
            payload.message,
        )

        bot_text = await _generate_reply(payload.message)
        bot_utterance = await create_utterance(
            session,
            conversation.id,
            bot.id,
            bot_text,
            reply_to_id=user_utterance.id,
        )

    return ChatResponse(
        conversation_id=conversation.id,
        reply_utterance_id=bot_utterance.id,
        text=bot_text,
    )
