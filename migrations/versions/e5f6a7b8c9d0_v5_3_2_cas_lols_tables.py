"""v5_3_2_cas_lols_tables

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-08-30 00:30:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'e5f6a7b8c9d0'
down_revision: Union[str, Sequence[str], None] = 'd4e5f6a7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """v5.3.2: таблицы ночного CAS/LOLS-свипа.

    • cas_verdicts — кэш вердиктов: один юзер = один запрос к внешнему
      API в 30 дней (TTL проверяется в коде по checked_at).
    • chat_members_seen — «сидящие» для свипа: Bot API не умеет
      перечислять участников, бот знает только тех, кто писал.
    • cas_ignore — ложные срабатывания: разбан CAS-бана добавляет юзера
      сюда автоматически (revoke_user_ban hook).
    """
    op.create_table(
        'cas_verdicts',
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('checked_at', sa.DateTime(), nullable=False),
        sa.Column('source', sa.String(length=16), nullable=False),
        sa.Column('is_banned', sa.Boolean(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('user_id'),
    )
    op.create_table(
        'chat_members_seen',
        sa.Column('chat_id', sa.BigInteger(), nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('first_seen_at', sa.DateTime(), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('chat_id', 'user_id'),
    )
    op.create_index(
        'ix_chat_members_seen_chat_last_seen',
        'chat_members_seen',
        ['chat_id', 'last_seen_at'],
        unique=False,
    )
    op.create_table(
        'cas_ignore',
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('added_by', sa.BigInteger(), nullable=True),
        sa.Column('comment', sa.Text(), nullable=True),
        sa.Column('added_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('user_id'),
    )


def downgrade() -> None:
    """Убирает таблицы ночного свипа."""
    op.drop_table('cas_ignore')
    op.drop_index(
        'ix_chat_members_seen_chat_last_seen',
        table_name='chat_members_seen',
    )
    op.drop_table('chat_members_seen')
    op.drop_table('cas_verdicts')
