from typing import Any, cast

import httpx
from fastapi import BackgroundTasks
from kani import ChatMessage, Kani  # type: ignore[import-untyped]
from kani.engines.openai import OpenAIEngine  # type: ignore[import-untyped]
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from app.config import (
    UTTERANCE_STATUS_FAILED,
    UTTERANCE_STATUS_QUEUED,
    UTTERANCE_STATUS_RECEIVED,
    UTTERANCE_STATUS_SENT,
    get_openai_api_key,
    get_openai_model,
    get_sms_outbound_url,
    get_sms_timeout_seconds,
)
from app.db import get_sessionmaker
from app.models.response import Utterance
from app.response.crud import (
    build_chat_history,
    create_queued_utterance,
    create_utterance,
    get_or_create_bot_speaker,
    get_or_create_conversation,
    get_or_create_speaker,
    get_or_create_system_prompt,
)
from app.response.schemas import (
    ChatQueuedResponse,
    ChatRequest,
    ResponseQueuedResponse,
    ResponseRequest,
)


async def _generate_reply(chat_history: list[ChatMessage], query: str, system_prompt: str) -> str:
    api_key = get_openai_api_key()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set.")
    model = get_openai_model()
    if not model:
        raise RuntimeError("OPENAI_MODEL is not set.")

    engine = OpenAIEngine(api_key=api_key, model=model)
    kani = Kani(engine=engine, system_prompt=system_prompt)
    kani.chat_history = chat_history

    try:
        reply = await kani.chat_round(query)
    except Exception as exc:
        raise RuntimeError(f"Kani generation failed: {exc}") from exc
    finally:
        await engine.close()

    if not reply.text:
        raise RuntimeError("Kani reply was empty.")
    return cast(str, reply.text)


async def _send_sms(user_id: str, message: str) -> None:
    url = get_sms_outbound_url()
    if not url:
        raise RuntimeError("SMS_OUTBOUND_URL is not set.")
    timeout = get_sms_timeout_seconds()
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, json={"user_id": user_id, "message": message})
        response.raise_for_status()


async def _run_deferred_reply(
    user_id: str,
    user_utterance_id: str,
    bot_utterance_id: str,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as session:
        try:
            user_utterance = await session.get(Utterance, user_utterance_id)
            if not user_utterance or user_utterance.text is None:
                raise RuntimeError("User utterance text missing.")

            chat_history = await build_chat_history(
                session,
                conversation_id=user_utterance.conversation_id,
                user_id=user_id,
                up_to_timestamp=user_utterance.timestamp,
                exclude_utterance_id=user_utterance.id,
            )

            system_prompt = await get_or_create_system_prompt(
                session, user_utterance.conversation_id
            )
            reply_text = await _generate_reply(chat_history, user_utterance.text, system_prompt)

            bot_utterance = await session.get(Utterance, bot_utterance_id)
            if not bot_utterance:
                raise RuntimeError(f"Utterance not found: {bot_utterance_id}")
            bot_utterance.text = reply_text
            bot_utterance.error = None
            await session.commit()

            await _send_sms(user_id, reply_text)

            bot_utterance.status = UTTERANCE_STATUS_SENT
            await session.commit()
        except Exception as exc:
            await session.rollback()
            failed_utterance = await session.get(Utterance, bot_utterance_id)
            if failed_utterance:
                message = str(exc).strip() or exc.__class__.__name__
                failed_utterance.status = UTTERANCE_STATUS_FAILED
                failed_utterance.error = message[:500]
                await session.commit()


async def process_chat(
    session: AsyncSession,
    payload: ChatRequest,
    background_tasks: BackgroundTasks,
    meta: dict[str, Any] | None = None,
) -> ChatQueuedResponse:
    async with session.begin():
        speaker = await get_or_create_speaker(session, payload.user_id, meta={"type": "user"})
        bot = await get_or_create_bot_speaker(session, payload.user_id)

        conversation = await get_or_create_conversation(session, speaker.id)

        user_utterance = await create_utterance(
            session,
            conversation.id,
            speaker.id,
            payload.message,
            meta=meta,
            status=UTTERANCE_STATUS_RECEIVED,
        )

        bot_utterance = await create_queued_utterance(
            session,
            conversation.id,
            bot.id,
            reply_to_id=user_utterance.id,
        )

    bind = session.bind
    if bind is None:
        sessionmaker = get_sessionmaker()
    else:
        engine = bind.engine if isinstance(bind, AsyncConnection) else bind
        sessionmaker = (
            async_sessionmaker(engine, expire_on_commit=False)
            if isinstance(engine, AsyncEngine)
            else get_sessionmaker()
        )
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


async def process_response(
    session: AsyncSession,
    payload: ResponseRequest,
    background_tasks: BackgroundTasks,
) -> ResponseQueuedResponse:
    chat_request = ChatRequest(user_id=payload.user_id, message=payload.input)
    chat_response = await process_chat(
        session, chat_request, background_tasks, meta=payload.metadata
    )
    return ResponseQueuedResponse(
        id=chat_response.reply_utterance_id,
        object="response",
        status=chat_response.status,
        conversation_id=chat_response.conversation_id,
        mode=payload.mode,
    )
