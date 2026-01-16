import httpx

from app.config import get_sms_outbound_url, get_sms_timeout_seconds
from app.schemas import SmsOutboundRequest


async def send_sms(payload: SmsOutboundRequest) -> None:
    url = get_sms_outbound_url()
    if not url:
        raise RuntimeError("SMS_OUTBOUND_URL is not set.")
    timeout = get_sms_timeout_seconds()
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, json=payload.model_dump())
        response.raise_for_status()
