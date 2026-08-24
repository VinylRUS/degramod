"""v5_3_0_channel_whitelist

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-08-24 18:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, Sequence[str], None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Белый список каналов + тумблер удаления их сообщений (v5.3.0)."""
    op.create_table(
        'channel_whitelist',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('chat_id', sa.BigInteger(), nullable=False),
        sa.Column('channel_id', sa.BigInteger(), nullable=True),
        sa.Column('channel_username', sa.String(), nullable=True),
        sa.Column('title', sa.String(), nullable=True),
        sa.Column('note', sa.String(), nullable=True),
        sa.Column('added_by_mod_id', sa.BigInteger(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('chat_id', 'channel_id',
                            name='uq_channel_whitelist_chat_id'),
        sa.UniqueConstraint('chat_id', 'channel_username',
                            name='uq_channel_whitelist_chat_username'),
    )
    # Тумблер per-chat, по умолчанию выключен: цена ошибки — массовое
    # удаление чужих сообщений, поэтому фича не включается сама нигде.
    op.add_column(
        'chat_settings',
        sa.Column('delete_channel_messages', sa.Boolean(),
                  nullable=False, server_default=sa.text('0')),
    )


def downgrade() -> None:
    """Убирает белый список каналов и тумблер."""
    op.drop_column('chat_settings', 'delete_channel_messages')
    op.drop_table('channel_whitelist')
