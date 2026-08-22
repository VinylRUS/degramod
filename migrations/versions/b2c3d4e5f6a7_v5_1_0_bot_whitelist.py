"""v5_1_0_bot_whitelist

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-08-22 12:05:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Создаёт bot_whitelist (v5.1.0)."""
    op.create_table(
        'bot_whitelist',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('chat_id', sa.BigInteger(), nullable=False),
        sa.Column('bot_username', sa.String(), nullable=False),
        sa.Column('bot_id', sa.BigInteger(), nullable=True),
        sa.Column('note', sa.String(), nullable=True),
        sa.Column('added_by_mod_id', sa.BigInteger(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('chat_id', 'bot_username',
                            name='uq_bot_whitelist_chat_bot'),
    )


def downgrade() -> None:
    """Удаляет bot_whitelist."""
    op.drop_table('bot_whitelist')
