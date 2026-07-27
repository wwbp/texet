from __future__ import annotations

import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.response import InstructionTemplate
from app.response.crud import get_instruction_template
from app.response.prompt import DEFAULT_INSTRUCTION_TEMPLATE


@pytest.mark.asyncio
async def test_returns_default_when_table_empty(async_session: AsyncSession) -> None:
    assert await get_instruction_template(async_session) == DEFAULT_INSTRUCTION_TEMPLATE


@pytest.mark.asyncio
async def test_returns_stored_template(async_session: AsyncSession) -> None:
    async with async_session.begin():
        async_session.add(InstructionTemplate(template="{base}\n\n{weekly_summary}"))

    assert await get_instruction_template(async_session) == "{base}\n\n{weekly_summary}"


@pytest.mark.asyncio
async def test_latest_created_template_wins(async_session: AsyncSession) -> None:
    older = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    newer = datetime.datetime(2026, 2, 1, tzinfo=datetime.UTC)

    async with async_session.begin():
        async_session.add(InstructionTemplate(template="OLD {base}", created_at=older))
        async_session.add(InstructionTemplate(template="NEW {base}", created_at=newer))

    assert await get_instruction_template(async_session) == "NEW {base}"


@pytest.mark.asyncio
async def test_blank_template_falls_back_to_default(async_session: AsyncSession) -> None:
    async with async_session.begin():
        async_session.add(InstructionTemplate(template="   "))

    assert await get_instruction_template(async_session) == DEFAULT_INSTRUCTION_TEMPLATE
