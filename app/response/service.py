import asyncio
import datetime
import inspect
import logging
from typing import Any, cast

import httpx
from fastapi import HTTPException
from kani import ChatMessage, Kani  # type: ignore[import-untyped]
from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import (
    BEDROCK_DEFAULT_MODEL,
    DEFAULT_LLM_PROVIDER,
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
    get_max_queue_depth,
    get_mock_llm_latency_ms,
    get_mock_moderation_latency_ms,
    get_mock_sms_latency_ms,
    get_moderation_alert_emails,
    get_openai_api_key,
    get_public_app_url,
    get_sms_outbound_authorization,
    get_sms_outbound_url,
    get_sms_timeout_seconds,
    get_worker_max_attempts,
    mock_external_apis,
)
from app.engines.factory import create_engine as _create_engine
from app.models.response import PromptIssue, Utterance
from app.queue import count_pending_replies, notify_reply_queued
from app.response.crud import (
    build_chat_history,
    create_queued_utterance,
    create_utterance,
    get_daily_prompt,
    get_instruction_template,
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
from app.response.utils import (
    extract_utc_offset,
    strip_bracketed_segments,
    week_bounds_utc,
    week_start_for,
)

_logger = logging.getLogger(__name__)


PROMPT_ISSUE_DAY_NUMBER_INVALID = "day_number_invalid"
PROMPT_ISSUE_DAILY_PROMPT_MISSING = "daily_prompt_missing"


def _coerce_day_number(raw: Any) -> tuple[int | None, str | None]:
    """Return (day_number, problem) from raw request metadata.

    Numeric strings are accepted: a hub that quotes the field would otherwise
    silently cost a study every one of its daily prompts, since a dropped
    section is invisible in the reply. The deviation is still reported so the
    hub gets fixed. Booleans need their own branch because isinstance(True, int)
    is True in Python, and True would reach the integer day_number column.
    """
    if raw is None:
        return None, None
    if isinstance(raw, bool):
        return None, f"day_number must be an integer, got boolean {raw!r}."
    if isinstance(raw, int):
        return raw, None
    if isinstance(raw, str):
        candidate = raw.strip()
        if candidate.lstrip("-").isdigit():
            value = int(candidate)
            return value, f"day_number arrived as the string {raw!r}; coerced to {value}."
        return None, f"day_number is not numeric: {raw!r}."
    return None, f"day_number has unsupported type {type(raw).__name__}: {raw!r}."


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


async def _generate_reply(
    chat_history: list[ChatMessage],
    query: str,
    system_prompt: str,
    *,
    provider: str = DEFAULT_LLM_PROVIDER,
    model_id: str = BEDROCK_DEFAULT_MODEL,
    usage_out: dict[str, int] | None = None,
) -> str:
    """Generate one reply. When usage_out is given it is filled with the
    provider's token counts, left untouched if the provider reported none."""
    if mock_external_apis():
        await asyncio.sleep(get_mock_llm_latency_ms() / 1000)
        return f"[mock {provider}/{model_id}] Echo: {query[:120]}"

    engine = _create_engine(provider, model_id)
    kani = Kani(engine=engine, system_prompt=system_prompt, chat_history=chat_history)

    try:
        reply = await kani.chat_round(query)
    except Exception as exc:
        raise RuntimeError(f"Kani generation failed: {exc}") from exc
    finally:
        if usage_out is not None:
            last_usage = getattr(engine, "last_usage", None)
            if last_usage:
                usage_out.update(last_usage)
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
        "version": 2,
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
    if mock_external_apis():
        await asyncio.sleep(get_mock_sms_latency_ms() / 1000)
        return

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
    if mock_external_apis():
        await asyncio.sleep(get_mock_moderation_latency_ms() / 1000)
        return False, "", "", 0.0

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
    """Record a generation error, retrying until the reply runs out of attempts.

    A raised exception used to be terminal, so a transient provider error left
    the participant with no reply and no retry — while a *killed* worker was
    retried, because reclaim_stale rescues rows stuck in 'processing'. Leaving
    the claim in place puts both failure modes on that same path: the claim
    expires after WORKER_RECLAIM_SECONDS and the reply is requeued, which also
    spaces retries out instead of hammering a provider that is already failing.

    Caveat: the retry re-runs generation, so an error raised after the SMS was
    handed off (the commit that follows it) can deliver a duplicate message. A
    rare duplicate beats silence for a study participant.
    """
    await session.rollback()
    failed_utterance = await session.get(Utterance, bot_utterance_id)
    if not failed_utterance:
        return

    message = (str(exc).strip() or exc.__class__.__name__)[:500]
    failed_utterance.error = message
    # Reaching here means an attempt just happened, so count at least one even
    # if the caller never went through claim_next_reply (which is what
    # increments the column).
    attempts_made = max(failed_utterance.attempts, 1)
    if attempts_made < get_worker_max_attempts():
        _logger.warning(
            "Reply %s failed on attempt %d; leaving the claim to expire for retry: %s",
            bot_utterance_id,
            attempts_made,
            message,
        )
    else:
        failed_utterance.status = UTTERANCE_STATUS_FAILED
        failed_utterance.claimed_at = None
        _logger.error(
            "Reply %s failed permanently after %d attempts: %s",
            bot_utterance_id,
            attempts_made,
            message,
        )
    await session.commit()


async def _process_queued_reply(
    sessionmaker: async_sessionmaker[AsyncSession],
    user_id: str,
    user_utterance: Utterance,
    bot_utterance: Utterance,
) -> None:
    if user_utterance.text is None:
        raise RuntimeError("User utterance text missing.")

    now_utc = datetime.datetime.now(datetime.UTC)

    user_local_time: str | None = None
    if user_utterance.meta:
        raw_time = user_utterance.meta.get("user_local_time")
        if isinstance(raw_time, str):
            user_local_time = raw_time

    # The same local week the summariser uses. Keeping these on separate clocks
    # would, in the offset-sized window around the UTC boundary, have the reply
    # ask for a week the summariser had not written under that key — the
    # participant's memory would silently come back empty.
    offset = extract_utc_offset(user_utterance.meta)
    current_week_start = week_start_for(now_utc, offset)
    prev_week_start = current_week_start - datetime.timedelta(days=7)
    week_start_dt, _ = week_bounds_utc(current_week_start, offset)

    day_number, day_number_problem = _coerce_day_number(
        (user_utterance.meta or {}).get("day_number")
    )
    issues: list[tuple[str, str]] = []
    if day_number_problem:
        _logger.error(
            "Bad day_number from the hub for user=%s utterance=%s: %s",
            user_id,
            user_utterance.id,
            day_number_problem,
        )
        issues.append((PROMPT_ISSUE_DAY_NUMBER_INVALID, day_number_problem))

    # Gather all prompt/history context in one short-lived session so no pool
    # connection is held during the moderation/LLM calls below — connection
    # demand scales with pool_size / hold_seconds (see docs/load-testing.md).
    async with sessionmaker() as session:
        prev_summary = await get_weekly_summary(session, user_id, prev_week_start)
        sp = await get_latest_system_prompt(session)
        base_prompt = await get_or_create_system_prompt(session)
        instruction_template = await get_instruction_template(session)
        daily_prompt = (
            await get_daily_prompt(session, day_number) if day_number is not None else None
        )
        if day_number is not None and daily_prompt is None:
            detail = f"No daily prompt is configured for day {day_number}."
            _logger.error(
                "Missing daily prompt for user=%s utterance=%s: %s",
                user_id,
                user_utterance.id,
                detail,
            )
            issues.append((PROMPT_ISSUE_DAILY_PROMPT_MISSING, detail))
        for kind, detail in issues:
            session.add(
                PromptIssue(
                    kind=kind,
                    user_id=user_id,
                    utterance_id=user_utterance.id,
                    detail=detail,
                )
            )
        chat_history = await build_chat_history(
            session,
            conversation_id=user_utterance.conversation_id,
            user_id=user_id,
            up_to_timestamp=user_utterance.timestamp,
            exclude_utterance_id=user_utterance.id,
            since_timestamp=week_start_dt,
            annotate_days=True,
        )
        await session.commit()

    provider = sp.provider if sp else DEFAULT_LLM_PROVIDER
    model_id = sp.model_id if sp else BEDROCK_DEFAULT_MODEL
    daily_content = daily_prompt.content if daily_prompt else None

    blocked, _, blocked_category, blocked_score = await _moderate_message(user_utterance)
    if blocked:
        moderation_notice = _moderation_notice("user", blocked_category, blocked_score)
        async with sessionmaker() as session:
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
        moderation_meta = {
            "texet_moderation_source": "user",
            "texet_moderation_category": blocked_category,
            "texet_moderation_score": blocked_score,
        }
        async with sessionmaker() as session:
            db_user_utterance = await session.get(Utterance, user_utterance.id)
            if db_user_utterance:
                db_user_utterance.status = UTTERANCE_STATUS_MODERATED
                db_user_utterance.meta = _merge_meta(db_user_utterance.meta, moderation_meta)
            await _persist_and_send_bot_reply(
                session=session,
                user_id=user_id,
                bot_utterance_id=bot_utterance.id,
                stored_text=moderation_notice,
                delivered_text=moderation_notice,
                final_status=UTTERANCE_STATUS_MODERATED,
                in_reply_to_utterance_id=user_utterance.id,
                meta_updates=moderation_meta,
            )
        return

    system_prompt = compose_instruction_prompt(
        base=base_prompt,
        daily_content=daily_content,
        weekly_summary=prev_summary,
        user_local_time=user_local_time,
        day_number=day_number,
        template=instruction_template,
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

    usage: dict[str, int] = {}
    reply_text = await _generate_reply(
        chat_history,
        user_utterance.text,
        system_prompt,
        provider=provider,
        model_id=model_id,
        usage_out=usage,
    )

    # What the participant receives is pruned; what is stored stays raw, so the
    # study record and the texet_generation snapshot still show what the model
    # actually produced. Moderation runs on the raw text — the artifact is not
    # what makes a reply unsafe, and scoring the delivered copy would score
    # something the model never said.
    delivered_reply = strip_bracketed_segments(reply_text)
    if not delivered_reply or not delivered_reply.strip():
        # Nothing but an artifact. An empty SMS is worse than a late one, so
        # this takes the generation-failure path and is retried.
        raise RuntimeError("Reply contained nothing but bracketed segments.")

    reply_blocked, _, blocked_category, blocked_score = await _moderate_text(reply_text)
    if reply_blocked:
        moderation_notice = _moderation_notice("bot", blocked_category, blocked_score)
        async with sessionmaker() as session:
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

    async with sessionmaker() as session:
        await _persist_and_send_bot_reply(
            session=session,
            user_id=user_id,
            bot_utterance_id=bot_utterance.id,
            stored_text=reply_text,
            delivered_text=delivered_reply,
            final_status=UTTERANCE_STATUS_SENT,
            in_reply_to_utterance_id=user_utterance.id,
            meta_updates=_merge_meta(
                {"texet_generation": generation_snapshot},
                {"texet_usage": usage} if usage else None,
            ),
        )


async def _run_deferred_reply(
    user_id: str,
    user_utterance_id: str,
    bot_utterance_id: str,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    try:
        async with sessionmaker() as session:
            user_utterance = await session.get(Utterance, user_utterance_id)
            if not user_utterance:
                raise RuntimeError("User utterance missing.")
            bot_utterance = await session.get(Utterance, bot_utterance_id)
            if not bot_utterance:
                raise RuntimeError("Bot utterance missing.")
        await _process_queued_reply(sessionmaker, user_id, user_utterance, bot_utterance)
    except Exception as exc:
        async with sessionmaker() as session:
            await _mark_bot_utterance_failed(session, bot_utterance_id, exc)


async def process_chat(
    session: AsyncSession,
    payload: ChatRequest,
    meta: dict[str, Any] | None = None,
) -> ChatQueuedResponse:
    max_depth = get_max_queue_depth()
    async with session.begin():
        if max_depth:
            pending = await count_pending_replies(session)
            if pending >= max_depth:
                raise HTTPException(
                    status_code=503,
                    detail="Reply queue is full. Retry later.",
                    headers={"Retry-After": "30"},
                )

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

        # Wake listening workers (delivered on commit); fallback poll covers
        # any missed notification.
        await notify_reply_queued(session)

    # The reply is picked up by the worker service (app.worker) via app.queue.
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
) -> ResponseQueuedResponse:
    if bool((payload.metadata or {}).get("is_initial")):
        return await _persist_initial_bot_message(session, payload)

    chat_request = ChatRequest(user_id=payload.user_id, message=payload.input)
    chat_response = await process_chat(session, chat_request, meta=payload.metadata)
    return ResponseQueuedResponse(
        id=chat_response.reply_utterance_id,
        object="response",
        status=chat_response.status,
        conversation_id=chat_response.conversation_id,
        mode=payload.mode,
        user_utterance_id=chat_response.user_utterance_id,
    )
