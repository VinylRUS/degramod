"""v5_1_0_rules_url

Revision ID: a1b2c3d4e5f6
Revises: 2334dcf313d1
Create Date: 2026-08-22 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '2334dcf313d1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Добавляет chat_settings.rules_url (v5.1.0, команда /rules)."""
    op.add_column('chat_settings', sa.Column('rules_url', sa.String(), nullable=True))


def downgrade() -> None:
    """Убирает chat_settings.rules_url."""
    op.drop_column('chat_settings', 'rules_url')
