"""single_open_conversation_per_user

Revision ID: 9228797f839a
Revises: d740e0090bdc
Create Date: 2026-06-11 03:40:43.265864
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = '9228797f839a'
down_revision = 'd740e0090bdc'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index(
        op.f('ux_conversations_owner_open_day'),
        table_name='conversations',
        postgresql_where="(((status)::text = 'open'::text) AND (day_number IS NOT NULL))",
    )
    # The no-day index was silently cascade-dropped by d740e0090bdc when it removed
    # the day_identifier column its predicate referenced, so it may not exist.
    op.execute("DROP INDEX IF EXISTS ux_conversations_owner_open_no_day")

    # Merge each user's per-day open conversations into their earliest one so the
    # new one-open-conversation-per-user invariant holds before its index exists.
    op.execute(
        """
        CREATE TEMP TABLE conv_keepers AS
        SELECT DISTINCT ON (owner_speaker_id) id AS keeper_id, owner_speaker_id
        FROM conversations
        WHERE status = 'open'
        ORDER BY owner_speaker_id, created_at, id
        """
    )
    op.execute(
        """
        UPDATE utterances u
        SET conversation_id = k.keeper_id
        FROM conversations c
        JOIN conv_keepers k ON c.owner_speaker_id = k.owner_speaker_id
        WHERE u.conversation_id = c.id
          AND c.status = 'open'
          AND c.id <> k.keeper_id
        """
    )
    op.execute(
        """
        UPDATE conversations c
        SET last_activity_at = g.max_last_activity
        FROM (
            SELECT k.keeper_id, max(c2.last_activity_at) AS max_last_activity
            FROM conversations c2
            JOIN conv_keepers k ON c2.owner_speaker_id = k.owner_speaker_id
            WHERE c2.status = 'open'
            GROUP BY k.keeper_id
        ) g
        WHERE c.id = g.keeper_id
        """
    )
    op.execute(
        """
        DELETE FROM conversations c
        USING conv_keepers k
        WHERE c.owner_speaker_id = k.owner_speaker_id
          AND c.status = 'open'
          AND c.id <> k.keeper_id
        """
    )
    op.execute("DROP TABLE conv_keepers")

    # Per-request prompt data now lives on the bot utterance's generation snapshot.
    # jsonb_typeof guard: the `-` operator raises "cannot delete from scalar" on
    # rows whose meta is a bare JSON scalar rather than an object.
    op.execute(
        """
        UPDATE conversations
        SET meta = meta - 'texet_instruction_prompt' - 'texet_day_number'
                        - 'texet_user_local_time'
        WHERE meta IS NOT NULL AND jsonb_typeof(meta) = 'object'
        """
    )

    op.create_index(
        'ux_conversations_owner_open',
        'conversations',
        ['owner_speaker_id'],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )
    op.drop_column('conversations', 'day_number')


def downgrade() -> None:
    # Schema-only downgrade: merged conversations and stripped meta are not restored.
    op.add_column(
        'conversations',
        sa.Column('day_number', sa.INTEGER(), autoincrement=False, nullable=True),
    )
    op.drop_index(
        'ux_conversations_owner_open',
        table_name='conversations',
        postgresql_where=sa.text("status = 'open'"),
    )
    op.create_index(
        op.f('ux_conversations_owner_open_day'),
        'conversations',
        ['owner_speaker_id', 'day_number'],
        unique=True,
        postgresql_where="(((status)::text = 'open'::text) AND (day_number IS NOT NULL))",
    )
