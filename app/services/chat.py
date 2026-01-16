from fastapi import BackgroundTasks
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from app.constants import (
    UTTERANCE_STATUS_FAILED,
    UTTERANCE_STATUS_QUEUED,
    UTTERANCE_STATUS_RECEIVED,
    UTTERANCE_STATUS_SENT,
)
from app.db import get_sessionmaker
from app.db_ops import (
    create_pending_utterance,
    create_utterance,
    get_or_create_bot_speaker,
    get_or_create_conversation,
    get_or_create_speaker,
)
from app.models import Utterance
from app.schemas import ChatQueuedResponse, ChatRequest, SmsOutboundRequest
from app.services.sms import send_sms

ERROR_MAX_CHARS = 500


def _preprocess_message(message: str) -> str:
    return message.strip()


async def _generate_reply(message: str) -> str:
    return f"echo:{message}"


def _postprocess_reply(message: str) -> str:
    return message.strip()


async def _run_pipeline(message: str) -> str:
    preprocessed = _preprocess_message(message)
    generated = await _generate_reply(preprocessed)
    return _postprocess_reply(generated)


def _format_error(exc: Exception) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    return message[:ERROR_MAX_CHARS]


async def _fetch_utterance(session: AsyncSession, utterance_id: str) -> Utterance:
    utterance = await session.get(Utterance, utterance_id)
    if not utterance:
        raise RuntimeError(f"Utterance not found: {utterance_id}")
    return utterance


def _background_sessionmaker(
    session: AsyncSession,
) -> async_sessionmaker[AsyncSession]:
    bind = session.bind
    if bind is None:
        return get_sessionmaker()
    if isinstance(bind, AsyncConnection):
        engine = bind.engine
    else:
        engine = bind
    if not isinstance(engine, AsyncEngine):
        return get_sessionmaker()
    return async_sessionmaker(engine, expire_on_commit=False)


async def _run_deferred_reply(
    user_id: str,
    user_utterance_id: str,
    bot_utterance_id: str,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as session:
        try:
            user_utterance = await _fetch_utterance(session, user_utterance_id)
            if not user_utterance.text:
                raise RuntimeError("User utterance text missing.")

            reply_text = await _run_pipeline(user_utterance.text)

            bot_utterance = await _fetch_utterance(session, bot_utterance_id)
            bot_utterance.text = reply_text
            bot_utterance.status = UTTERANCE_STATUS_QUEUED
            bot_utterance.error = None
            await session.commit()

            outbound = SmsOutboundRequest(user_id=user_id, message=reply_text)
            await send_sms(outbound)

            bot_utterance.status = UTTERANCE_STATUS_SENT
            bot_utterance.error = None
            await session.commit()
        except Exception as exc:
            await session.rollback()
            bot_utterance = await session.get(Utterance, bot_utterance_id)
            if bot_utterance:
                bot_utterance.status = UTTERANCE_STATUS_FAILED
                bot_utterance.error = _format_error(exc)
                await session.commit()


async def process_chat(
    session: AsyncSession,
    payload: ChatRequest,
    background_tasks: BackgroundTasks,
) -> ChatQueuedResponse:
    async with session.begin():
        speaker = await get_or_create_speaker(
            session, payload.user_id, meta={"type": "user"}
        )
        bot = await get_or_create_bot_speaker(session, payload.user_id)

        conversation = await get_or_create_conversation(session, speaker.id)

        user_utterance = await create_utterance(
            session,
            conversation.id,
            speaker.id,
            payload.message,
            status=UTTERANCE_STATUS_RECEIVED,
        )

        bot_utterance = await create_pending_utterance(
            session,
            conversation.id,
            bot.id,
            reply_to_id=user_utterance.id,
        )

    sessionmaker = _background_sessionmaker(session)
    background_tasks.add_task(
        _run_deferred_reply,
        payload.user_id,
        user_utterance.id,
        bot_utterance.id,
        sessionmaker,
    )

    return ChatQueuedResponse(
        conversation_id=conversation.id,
        reply_utterance_id=bot_utterance.id,
        status=UTTERANCE_STATUS_QUEUED,
    )
