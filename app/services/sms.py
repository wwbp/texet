import os

import httpx

from app.schemas import SmsOutboundRequest

SMS_TIMEOUT_SECONDS = 10.0


def _get_sms_outbound_url() -> str:
    url = os.getenv("SMS_OUTBOUND_URL")
    if not url:
        raise RuntimeError("SMS_OUTBOUND_URL is not set.")
    return url


async def send_sms(payload: SmsOutboundRequest) -> None:
    url = _get_sms_outbound_url()
    async with httpx.AsyncClient(timeout=SMS_TIMEOUT_SECONDS) as client:
        response = await client.post(url, json=payload.model_dump())
        response.raise_for_status()
