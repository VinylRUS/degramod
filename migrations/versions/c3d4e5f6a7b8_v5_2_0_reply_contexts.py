"""v5_2_0_reply_contexts

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-08-24 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, Sequence[str], None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Создаёт reply_contexts — снимок родителя для сообщений-реплаев (v5.2.0)."""
    op.create_table(
        'reply_contexts',
        sa.Column('chat_id', sa.BigInteger(), nullable=False),
        sa.Column('message_id', sa.BigInteger(), nullable=False),
        sa.Column('parent_message_id', sa.BigInteger(), nullable=False),
        sa.Column('parent_user_id', sa.BigInteger(), nullable=True),
        sa.Column('parent_username', sa.String(length=255), nullable=True),
        sa.Column('parent_first_name', sa.String(length=255), nullable=True),
        sa.Column('parent_last_name', sa.String(length=255), nullable=True),
        sa.Column('parent_sender_chat_id', sa.BigInteger(), nullable=True),
        sa.Column('parent_sender_chat_title', sa.String(length=255), nullable=True),
        sa.Column('parent_text', sa.Text(), nullable=True),
        sa.Column('parent_media_type', sa.String(length=16), nullable=True),
        sa.Column('parent_file_id', sa.String(length=512), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('chat_id', 'message_id'),
    )
    # TTL-чистка ходит по created_at — без индекса это full scan таблицы,
    # которая растёт быстрее всех остальных в базе.
    op.create_index(
        'ix_reply_contexts_created_at', 'reply_contexts', ['created_at'],
    )


def downgrade() -> None:
    """Убирает reply_contexts."""
    op.drop_index('ix_reply_contexts_created_at', table_name='reply_contexts')
    op.drop_table('reply_contexts')
