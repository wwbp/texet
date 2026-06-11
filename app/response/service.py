import asyncio
import datetime
import inspect
import logging
from typing import Any, cast

import httpx
from fastapi import BackgroundTasks
from kani import ChatMessage, Kani  # type: ignore[import-untyped]
from openai import AsyncOpenAI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from app.config import (
    MODERATION_VALUES_FOR_BLOCKED,
    UTTERANCE_STATUS_FAILED,
    UTTERANCE_STATUS_MODERATED,
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
    get_public_app_url,
    get_sms_outbound_authorization,
    get_sms_outbound_url,
    get_sms_timeout_seconds,
)
from app.db import get_sessionmaker
from app.engines.factory import create_engine as _create_engine
from app.models.response import Utterance
from app.response.crud import (
    bot_speaker_id,
    build_chat_history,
    create_queued_utterance,
    create_utterance,
    get_daily_prompt,
    get_latest_system_prompt,
    get_or_create_bot_speaker,
    get_or_create_conversation,
    get_or_create_speaker,
    get_or_create_system_prompt,
    get_weekly_summary,
)
from app.response.prompt import compose_instruction_prompt
from app.response.schemas import (
    ChatQueuedResponse,
    ChatRequest,
    ResponseQueuedResponse,
    ResponseRequest,
)
from app.response.utils import week_start_utc

_logger = logging.getLogger(__name__)

_USER_QUEUE_LOCKS: dict[str, asyncio.Lock] = {}
_USER_QUEUE_LOCKS_GUARD = asyncio.Lock()


def _merge_meta(
    base: dict[str, Any] | None,
    updates: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not updates:
        return base
    merged: dict[str, Any] = {}
    if base:
        merged.update(base)
    merged.update(updates)
    return merged


def _moderation_notice(source: str, category: str, score: float) -> str:
    if source == "bot":
        return f"A generated reply was moderated due to {category} content with score {score:.2f}."
    return "I can't personally help with that, but your safety matters, and support is available. Call the crisis line at 988 to talk to someone."


async def _get_user_queue_lock(user_id: str) -> asyncio.Lock:
    async with _USER_QUEUE_LOCKS_GUARD:
        lock = _USER_QUEUE_LOCKS.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            _USER_QUEUE_LOCKS[user_id] = lock
        return lock


async def _generate_reply(
    chat_history: list[ChatMessage],
    query: str,
    system_prompt: str,
    *,
    provider: str = "openai",
    model_id: str = "gpt-4o-mini",
) -> str:
    engine = _create_engine(provider, model_id)
    kani = Kani(engine=engine, system_prompt=system_prompt, chat_history=chat_history)

    try:
        reply = await kani.chat_round(query)
    except Exception as exc:
        raise RuntimeError(f"Kani generation failed: {exc}") from exc
    finally:
        await engine.close()

    if not reply.text:
        raise RuntimeError("Kani reply was empty.")
    return cast(str, reply.text)


def _build_generation_snapshot(
    chat_history: list[ChatMessage],
    query: str,
    system_prompt: str,
    *,
    provider: str,
    model_id: str,
    week_start: datetime.date,
    day_number: int | None,
    user_local_time: str | None,
) -> dict[str, Any]:
    return {
        "version": 1,
        "provider": provider,
        "model_id": model_id,
        "system_prompt": system_prompt,
        "chat_history": [
            {
                "role": msg.role.value,
                "content": msg.content if isinstance(msg.content, str) else str(msg.content),
            }
            for msg in chat_history
        ],
        "query": query,
        "week_start": week_start.isoformat(),
        "day_number": day_number,
        "user_local_time": user_local_time,
    }


async def _send_sms(
    user_id: str,
    message: str,
    utterance_id: str,
    in_reply_to_utterance_id: str | None = None,
) -> None:
    url = get_sms_outbound_url()
    if not url:
        raise RuntimeError("SMS_OUTBOUND_URL is not set.")
    auth_header = get_sms_outbound_authorization()
    headers = {"Authorization": auth_header} if auth_header else None
    timeout = get_sms_timeout_seconds()
    async with httpx.AsyncClient(timeout=timeout) as client:
        payload: dict[str, str] = {
            "participant_id": user_id,
            "message": message,
            "message_type": "sent",
            "utterance_id": utterance_id,
        }
        if in_reply_to_utterance_id is not None:
            payload["in_reply_to_utterance_id"] = in_reply_to_utterance_id
        response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()


async def _close_async_openai_client(client: AsyncOpenAI) -> None:
    close_client = getattr(client, "close", None)
    if close_client is None:
        return
    maybe_awaitable = close_client()
    if inspect.isawaitable(maybe_awaitable):
        await maybe_awaitable


async def _moderate_text(text: str) -> tuple[bool, str, str, float]:
    api_key = get_openai_api_key()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set.")

    client = AsyncOpenAI(api_key=api_key)
    try:
        moderation_response = await client.moderations.create(
            input=text,
            model="omni-moderation-latest",
        )
    finally:
        await _close_async_openai_client(client)

    # OpenAI may return category scores as a typed object; normalize to a plain dict.
    if not moderation_response.results:
        return False, "", "", 0.0
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


async def _moderate_message(utterance: Utterance) -> tuple[bool, str, str, float]:
    if utterance.text is None:
        raise RuntimeError("Utterance text is not set.")
    return await _moderate_text(utterance.text)


def _build_moderation_email(
    user_id: str,
    utterance_id: str,
    conversation_id: str,
    speaker_id: str,
    utterance_text: str,
    utterance_timestamp: datetime.datetime,
    blocked_category: str,
    blocked_score: float,
    recent_chat_history: list[ChatMessage],
    admin_base_url: str,
) -> tuple[str, str]:
    """Return (subject, html_body) for a moderation alert email."""
    score_pct = int(blocked_score * 100)
    if blocked_score >= 0.7:
        score_color = "#c0392b"
    elif blocked_score >= 0.4:
        score_color = "#d35400"
    else:
        score_color = "#f39c12"

    def _esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    utterance_html = _esc(utterance_text)
    ts = utterance_timestamp
    timestamp_str = f"{ts.strftime('%b')} {ts.day}, {ts.year} {ts.hour:02d}:{ts.minute:02d} {ts.strftime('%Z') or 'UTC'}"
    admin_prefix = f"{admin_base_url.rstrip('/')}/console/admin" if admin_base_url else None

    links_html = ""
    if admin_prefix:
        links_html = f"""
        <tr><td style="padding:16px 0 8px;border-top:1px solid #e0e0e0;">
          <p style="margin:0 0 8px;font-size:13px;font-weight:600;color:#555;text-transform:uppercase;letter-spacing:.05em;">Admin links</p>
          <table cellpadding="0" cellspacing="0" style="font-size:13px;color:#333;line-height:1.8;">
            <tr><td style="color:#888;width:110px;">Utterance</td><td><a href="{admin_prefix}/utterance/details/{utterance_id}" style="color:#2980b9;">{utterance_id}</a></td></tr>
            <tr><td style="color:#888;">Conversation</td><td><a href="{admin_prefix}/conversation/details/{conversation_id}" style="color:#2980b9;">{conversation_id}</a></td></tr>
            <tr><td style="color:#888;">Speaker</td><td><a href="{admin_prefix}/speaker/details/{speaker_id}" style="color:#2980b9;">{speaker_id}</a></td></tr>
          </table>
        </td></tr>"""

    history_rows = ""
    for msg in recent_chat_history:
        role = msg.role.value
        text = _esc(str(msg.content).replace("\n", " ").strip())
        label_color = "#2c3e50" if role == "user" else "#7f8c8d"
        history_rows += f'<tr><td style="color:{label_color};font-weight:600;width:40px;vertical-align:top;padding:3px 8px 3px 0;">{role}</td><td style="color:#333;padding:3px 0;">{text}</td></tr>'

    history_html = ""
    if history_rows:
        history_html = f"""
        <tr><td style="padding:16px 0 8px;border-top:1px solid #e0e0e0;">
          <p style="margin:0 0 8px;font-size:13px;font-weight:600;color:#555;text-transform:uppercase;letter-spacing:.05em;">Recent context</p>
          <table cellpadding="0" cellspacing="0" style="font-size:13px;width:100%;">{history_rows}</table>
        </td></tr>"""

    body = f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#f5f5f5;font-family:Arial,sans-serif;">
<table cellpadding="0" cellspacing="0" width="100%" style="background:#f5f5f5;padding:24px 0;">
<tr><td align="center">
<table cellpadding="0" cellspacing="0" width="560" style="background:#fff;border-radius:6px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.12);">

  <!-- header bar -->
  <tr><td style="background:{score_color};padding:14px 24px;">
    <span style="font-size:16px;font-weight:700;color:#fff;letter-spacing:.03em;">&#9888; MODERATION ALERT</span>
    <span style="float:right;font-size:14px;color:rgba(255,255,255,.85);">{_esc(blocked_category)} &nbsp;·&nbsp; score {blocked_score:.2f} ({score_pct}%)</span>
  </td></tr>

  <!-- body -->
  <tr><td style="padding:20px 24px;">
    <table cellpadding="0" cellspacing="0" width="100%">

      <!-- flagged message -->
      <tr><td style="padding-bottom:16px;">
        <p style="margin:0 0 4px;font-size:13px;font-weight:600;color:#555;text-transform:uppercase;letter-spacing:.05em;">Flagged message</p>
        <p style="margin:0 0 8px;font-size:12px;color:#999;">{timestamp_str}</p>
        <div style="background:#fef9f0;border-left:4px solid {score_color};padding:12px 16px;font-size:15px;color:#222;line-height:1.5;border-radius:0 4px 4px 0;">{utterance_html}</div>
      </td></tr>

      <!-- meta row -->
      <tr><td style="padding-bottom:16px;border-top:1px solid #e0e0e0;padding-top:16px;">
        <table cellpadding="0" cellspacing="0" style="font-size:13px;color:#333;line-height:1.9;width:100%;">
          <tr><td style="color:#888;width:90px;">User</td><td style="font-family:monospace;">{_esc(user_id)}</td></tr>
          <tr><td style="color:#888;">Category</td><td><strong>{_esc(blocked_category)}</strong></td></tr>
          <tr><td style="color:#888;">Score</td><td><strong style="color:{score_color};">{blocked_score:.2f}</strong> / 1.00</td></tr>
        </table>
      </td></tr>

      {links_html}
      {history_html}

    </table>
  </td></tr>

</table>
</td></tr></table>
</body></html>"""

    subject = f"[texet] {blocked_category} ({score_pct}%) — {user_id}"
    return subject, body


async def _send_moderation_email(
    user_id: str,
    utterance_id: str,
    conversation_id: str,
    speaker_id: str,
    utterance_text: str,
    utterance_timestamp: datetime.datetime,
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
        MAIL_PASSWORD=mail_password,  # type: ignore[arg-type]
        MAIL_FROM=mail_from,
        MAIL_PORT=get_mail_port(),
        MAIL_SERVER=mail_server,
        MAIL_STARTTLS=get_mail_starttls(),
        MAIL_SSL_TLS=get_mail_ssl_tls(),
        USE_CREDENTIALS=get_mail_use_credentials(),
        VALIDATE_CERTS=get_mail_validate_certs(),
    )

    subject, body = _build_moderation_email(
        user_id=user_id,
        utterance_id=utterance_id,
        conversation_id=conversation_id,
        speaker_id=speaker_id,
        utterance_text=utterance_text,
        utterance_timestamp=utterance_timestamp,
        blocked_category=blocked_category,
        blocked_score=blocked_score,
        recent_chat_history=recent_chat_history,
        admin_base_url=get_public_app_url(),
    )

    message = MessageSchema(
        subject=subject,
        recipients=recipients,  # type: ignore[arg-type]
        body=body,
        subtype=MessageType.html,
    )
    await FastMail(conf).send_message(message)


async def _persist_and_send_bot_reply(
    session: AsyncSession,
    user_id: str,
    bot_utterance_id: str,
    stored_text: str,
    delivered_text: str,
    final_status: str,
    in_reply_to_utterance_id: str | None = None,
    meta_updates: dict[str, Any] | None = None,
) -> None:
    bot_utterance = await session.get(Utterance, bot_utterance_id)
    if not bot_utterance:
        raise RuntimeError(f"Utterance not found: {bot_utterance_id}")

    bot_utterance.text = stored_text
    bot_utterance.error = None
    bot_utterance.status = final_status
    bot_utterance.meta = _merge_meta(bot_utterance.meta, meta_updates)

    # Send SMS before committing so a delivery failure triggers rollback via
    # _mark_bot_utterance_failed, leaving the utterance in its original queued
    # state rather than persisting partial data.
    await _send_sms(user_id, delivered_text, bot_utterance_id, in_reply_to_utterance_id)

    await session.commit()


async def _mark_bot_utterance_failed(
    session: AsyncSession,
    bot_utterance_id: str,
    exc: Exception,
) -> None:
    await session.rollback()
    failed_utterance = await session.get(Utterance, bot_utterance_id)
    if failed_utterance:
        message = str(exc).strip() or exc.__class__.__name__
        failed_utterance.status = UTTERANCE_STATUS_FAILED
        failed_utterance.error = message[:500]
        await session.commit()


async def _get_next_queued_bot_utterance(
    session: AsyncSession,
    user_id: str,
) -> Utterance | None:
    result = await session.execute(
        select(Utterance)
        .where(
            Utterance.speaker_id == bot_speaker_id(user_id),
            Utterance.status == UTTERANCE_STATUS_QUEUED,
        )
        .order_by(Utterance.timestamp, Utterance.id)
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _process_queued_reply(
    session: AsyncSession,
    user_id: str,
    user_utterance: Utterance,
    bot_utterance: Utterance,
) -> None:
    if user_utterance.text is None:
        raise RuntimeError("User utterance text missing.")

    blocked, _, blocked_category, blocked_score = await _moderate_message(user_utterance)
    if blocked:
        moderation_notice = _moderation_notice("user", blocked_category, blocked_score)
        blocked_history = await build_chat_history(
            session,
            conversation_id=user_utterance.conversation_id,
            user_id=user_id,
            up_to_timestamp=user_utterance.timestamp,
        )
        try:
            await _send_moderation_email(
                user_id=user_id,
                utterance_id=user_utterance.id,
                conversation_id=user_utterance.conversation_id,
                speaker_id=user_utterance.speaker_id,
                utterance_text=user_utterance.text,
                utterance_timestamp=user_utterance.timestamp,
                blocked_category=blocked_category,
                blocked_score=blocked_score,
                recent_chat_history=blocked_history[-5:],
            )
        except Exception:
            _logger.warning(
                "Moderation email failed for utterance %s",
                user_utterance.id,
                exc_info=True,
            )
        user_utterance.status = UTTERANCE_STATUS_MODERATED
        user_utterance.meta = _merge_meta(
            user_utterance.meta,
            {
                "texet_moderation_source": "user",
                "texet_moderation_category": blocked_category,
                "texet_moderation_score": blocked_score,
            },
        )
        await _persist_and_send_bot_reply(
            session=session,
            user_id=user_id,
            bot_utterance_id=bot_utterance.id,
            stored_text=moderation_notice,
            delivered_text=moderation_notice,
            final_status=UTTERANCE_STATUS_MODERATED,
            in_reply_to_utterance_id=user_utterance.id,
            meta_updates={
                "texet_moderation_source": "user",
                "texet_moderation_category": blocked_category,
                "texet_moderation_score": blocked_score,
            },
        )
        return

    now_utc = datetime.datetime.now(datetime.UTC)
    current_week_start = week_start_utc(now_utc)
    prev_week_start = current_week_start - datetime.timedelta(days=7)
    week_start_dt = datetime.datetime.combine(
        current_week_start, datetime.time.min, tzinfo=datetime.UTC
    )

    prev_summary = await get_weekly_summary(session, user_id, prev_week_start)
    sp = await get_latest_system_prompt(session)
    base_prompt = await get_or_create_system_prompt(session)
    provider = sp.provider if sp else "openai"
    model_id = sp.model_id if sp else "gpt-4o-mini"

    day_number: int | None = None
    user_local_time: str | None = None
    if user_utterance.meta:
        raw = user_utterance.meta.get("day_number")
        if isinstance(raw, int):
            day_number = raw
        raw_time = user_utterance.meta.get("user_local_time")
        if isinstance(raw_time, str):
            user_local_time = raw_time

    daily_prompt = (
        await get_daily_prompt(session, day_number) if day_number is not None else None
    )
    daily_content = daily_prompt.content if daily_prompt else None

    system_prompt = compose_instruction_prompt(
        base=base_prompt,
        daily_content=daily_content,
        weekly_summary=prev_summary,
        user_local_time=user_local_time,
    )

    chat_history = await build_chat_history(
        session,
        conversation_id=user_utterance.conversation_id,
        user_id=user_id,
        up_to_timestamp=user_utterance.timestamp,
        exclude_utterance_id=user_utterance.id,
        since_timestamp=week_start_dt,
    )
    generation_snapshot = _build_generation_snapshot(
        chat_history,
        user_utterance.text,
        system_prompt,
        provider=provider,
        model_id=model_id,
        week_start=current_week_start,
        day_number=day_number,
        user_local_time=user_local_time,
    )

    reply_text = await _generate_reply(
        chat_history, user_utterance.text, system_prompt, provider=provider, model_id=model_id
    )

    reply_blocked, _, blocked_category, blocked_score = await _moderate_text(reply_text)
    if reply_blocked:
        moderation_notice = _moderation_notice("bot", blocked_category, blocked_score)
        await _persist_and_send_bot_reply(
            session=session,
            user_id=user_id,
            bot_utterance_id=bot_utterance.id,
            stored_text=reply_text,
            delivered_text=moderation_notice,
            final_status=UTTERANCE_STATUS_MODERATED,
            in_reply_to_utterance_id=user_utterance.id,
            meta_updates={
                "texet_generation": generation_snapshot,
                "texet_moderation_source": "bot",
                "texet_moderation_category": blocked_category,
                "texet_moderation_score": blocked_score,
                "texet_moderation_notice": moderation_notice,
            },
        )
        return

    await _persist_and_send_bot_reply(
        session=session,
        user_id=user_id,
        bot_utterance_id=bot_utterance.id,
        stored_text=reply_text,
        delivered_text=reply_text,
        final_status=UTTERANCE_STATUS_SENT,
        in_reply_to_utterance_id=user_utterance.id,
        meta_updates={"texet_generation": generation_snapshot},
    )


async def _run_deferred_reply(
    user_id: str,
    user_utterance_id: str,
    bot_utterance_id: str,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as session:
        try:
            user_utterance = await session.get(Utterance, user_utterance_id)
            if not user_utterance:
                raise RuntimeError("User utterance missing.")
            bot_utterance = await session.get(Utterance, bot_utterance_id)
            if not bot_utterance:
                raise RuntimeError("Bot utterance missing.")
            await _process_queued_reply(session, user_id, user_utterance, bot_utterance)
        except Exception as exc:
            await _mark_bot_utterance_failed(session, bot_utterance_id, exc)


async def _drain_user_queue(
    user_id: str,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    lock = await _get_user_queue_lock(user_id)
    async with lock:
        while True:
            async with sessionmaker() as session:
                bot_utterance = await _get_next_queued_bot_utterance(session, user_id)
                if not bot_utterance:
                    return
                try:
                    if not bot_utterance.reply_to_id:
                        raise RuntimeError("Queued bot utterance missing reply_to_id.")
                    user_utterance = await session.get(Utterance, bot_utterance.reply_to_id)
                    if not user_utterance:
                        raise RuntimeError("User utterance missing.")
                    await _process_queued_reply(session, user_id, user_utterance, bot_utterance)
                except Exception as exc:
                    await _mark_bot_utterance_failed(session, bot_utterance.id, exc)


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
        _drain_user_queue,
        payload.user_id,
        sessionmaker,
    )

    return ChatQueuedResponse(
        conversation_id=conversation.id,
        reply_utterance_id=bot_utterance.id,
        user_utterance_id=user_utterance.id,
        status=UTTERANCE_STATUS_QUEUED,
    )


async def _persist_initial_bot_message(
    session: AsyncSession,
    payload: ResponseRequest,
) -> ResponseQueuedResponse:
    async with session.begin():
        speaker = await get_or_create_speaker(session, payload.user_id, meta={"type": "user"})
        bot = await get_or_create_bot_speaker(session, payload.user_id)

        conversation = await get_or_create_conversation(session, speaker.id)

        bot_utterance = await create_utterance(
            session,
            conversation.id,
            bot.id,
            payload.input,
            meta=_merge_meta(payload.metadata, {"texet_hub_initial": True}),
            status=UTTERANCE_STATUS_SENT,
        )

    return ResponseQueuedResponse(
        id=bot_utterance.id,
        object="response",
        status="recorded",
        conversation_id=conversation.id,
        mode=payload.mode,
    )


async def process_response(
    session: AsyncSession,
    payload: ResponseRequest,
    background_tasks: BackgroundTasks,
) -> ResponseQueuedResponse:
    if bool((payload.metadata or {}).get("is_initial")):
        return await _persist_initial_bot_message(session, payload)

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
        user_utterance_id=chat_response.user_utterance_id,
    )
