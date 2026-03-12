from contextlib import suppress
from typing import Any, cast

import httpx
from fastapi import BackgroundTasks
from kani import ChatMessage, Kani  # type: ignore[import-untyped]
from kani.engines.openai import OpenAIEngine  # type: ignore[import-untyped]
from openai import OpenAI
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from app.config import (
    MODERATION_VALUES_FOR_BLOCKED,
    UTTERANCE_STATUS_FAILED,
    UTTERANCE_STATUS_QUEUED,
    UTTERANCE_STATUS_RECEIVED,
    UTTERANCE_STATUS_SENT,
    get_mail_from,
    get_mail_password,
    get_mail_port,
    get_mail_server,
    get_mail_ssl_tls,
    get_mail_starttls,
    get_mail_use_credentials,
    get_mail_username,
    get_mail_validate_certs,
    get_moderation_alert_emails,
    get_openai_api_key,
    get_openai_model,
    get_sms_outbound_authorization,
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


async def _send_sms(user_id: str, message: str, utterance_id: str) -> None:
    url = get_sms_outbound_url()
    if not url:
        raise RuntimeError("SMS_OUTBOUND_URL is not set.")
    auth_header = get_sms_outbound_authorization()
    headers = {"Authorization": auth_header} if auth_header else None
    timeout = get_sms_timeout_seconds()
    async with httpx.AsyncClient(timeout=timeout) as client:
        payload = {
            "participant_id": user_id,
            "message": message,
            "message_type": "sent",
            "utterance_id": utterance_id,
        }
        response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()


async def _moderate_message(utterance: Utterance) -> tuple[bool, str, str, float]:
    api_key = get_openai_api_key()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set.")
    if utterance.text is None:
        raise RuntimeError("Utterance text is not set.")
    text = utterance.text

    client = OpenAI(api_key=api_key)
    moderation_response = client.moderations.create(
        input=text,
        model="omni-moderation-latest",
    )

    # OpenAI may return category scores as a typed object; normalize to a plain dict.
    raw_category_scores = moderation_response.results[0].category_scores
    if raw_category_scores is None:
        return False, "", "", 0.0
    category_scores = (
        raw_category_scores.model_dump()
        if hasattr(raw_category_scores, "model_dump")
        else cast(dict[str, float], raw_category_scores)
    )

    # moderation score represents tolerance
    for category, score in category_scores.items():
        if score > MODERATION_VALUES_FOR_BLOCKED.get(category, 1.0):
            blocked_status = f"Blocked due to {category} content with score {score:.2f}."
            return True, blocked_status.strip(), category, float(score)
    return False, "", "", 0.0


async def _send_moderation_email(
    user_id: str,
    utterance_id: str,
    blocked_category: str,
    blocked_score: float,
    recent_chat_history: list[ChatMessage],
) -> None:
    recipients = get_moderation_alert_emails()
    if not recipients:
        return

    mail_username = get_mail_username()
    mail_password = get_mail_password()
    mail_from = get_mail_from()
    mail_server = get_mail_server()
    if not (mail_username and mail_password and mail_from and mail_server):
        return

    from fastapi_mail import ConnectionConfig, FastMail, MessageSchema, MessageType

    conf = ConnectionConfig(
        MAIL_USERNAME=mail_username,
        MAIL_PASSWORD=mail_password,
        MAIL_FROM=mail_from,
        MAIL_PORT=get_mail_port(),
        MAIL_SERVER=mail_server,
        MAIL_STARTTLS=get_mail_starttls(),
        MAIL_SSL_TLS=get_mail_ssl_tls(),
        USE_CREDENTIALS=get_mail_use_credentials(),
        VALIDATE_CERTS=get_mail_validate_certs(),
    )

    history_lines: list[str] = []
    for idx, message in enumerate(recent_chat_history, start=1):
        role = message.role.value
        text = str(message.content).replace("\n", " ").strip()
        history_lines.append(f"{idx}. role={role} text={text}")

    body = "\n".join(
        [
            "event=moderation_blocked",
            f"user_id={user_id}",
            f"utterance_id={utterance_id}",
            f"category={blocked_category}",
            f"score={blocked_score:.4f}",
            f"recent_messages={len(history_lines)}",
            *history_lines,
        ]
    )

    message = MessageSchema(
        subject=f"[texet] moderation blocked user={user_id} category={blocked_category}",
        recipients=recipients,
        body=body,
        subtype=MessageType.plain,
    )
    await FastMail(conf).send_message(message)


async def _persist_and_send_bot_reply(
    session: AsyncSession,
    user_id: str,
    bot_utterance_id: str,
    reply_text: str,
) -> None:
    bot_utterance = await session.get(Utterance, bot_utterance_id)
    if not bot_utterance:
        raise RuntimeError(f"Utterance not found: {bot_utterance_id}")

    bot_utterance.text = reply_text
    bot_utterance.error = None
    await session.commit()

    await _send_sms(user_id, reply_text, bot_utterance_id)

    bot_utterance.status = UTTERANCE_STATUS_SENT
    await session.commit()


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

            blocked, reason, blocked_category, blocked_score = await _moderate_message(
                user_utterance
            )
            if blocked:
                reply_text = reason
                blocked_history = await build_chat_history(
                    session,
                    conversation_id=user_utterance.conversation_id,
                    user_id=user_id,
                    up_to_timestamp=user_utterance.timestamp,
                )
                with suppress(Exception):
                    await _send_moderation_email(
                        user_id=user_id,
                        utterance_id=user_utterance.id,
                        blocked_category=blocked_category,
                        blocked_score=blocked_score,
                        recent_chat_history=blocked_history[-5:],
                    )
            else:
                chat_history = await build_chat_history(
                    session,
                    conversation_id=user_utterance.conversation_id,
                    user_id=user_id,
                    up_to_timestamp=user_utterance.timestamp,
                    exclude_utterance_id=user_utterance.id,
                )

                system_prompt = await get_or_create_system_prompt(session)
                reply_text = await _generate_reply(chat_history, user_utterance.text, system_prompt)

            await _persist_and_send_bot_reply(
                session=session,
                user_id=user_id,
                bot_utterance_id=bot_utterance_id,
                reply_text=reply_text,
            )
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
