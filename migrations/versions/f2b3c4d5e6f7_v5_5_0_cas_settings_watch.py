"""v5_5_0_cas_settings_watch

Revision ID: f2b3c4d5e6f7
Revises: e5f6a7b8c9d0
Create Date: 2026-09-03 06:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'f2b3c4d5e6f7'
down_revision: Union[str, Sequence[str], None] = 'e5f6a7b8c9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """v5.5.0: пороги каскада (cas_settings) + метрики /account в cas_verdicts.

    • cas_settings — singleton (id=1): пороги тиров C1/C2/C3 для каскада
      /account, правятся в веб-панели (/admin/cas).
    • cas_verdicts += spam_factor/offenses/scammer/tier — метрики
      потенциальных (каскад /account) для вкладки «На карандаше».
    """
    op.create_table(
        'cas_settings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('spamfactor_ban', sa.Float(), nullable=False),
        sa.Column('spamfactor_mute', sa.Float(), nullable=False),
        sa.Column('offenses_mute', sa.Integer(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('updated_by', sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.execute(
        "INSERT INTO cas_settings "
        "(id, spamfactor_ban, spamfactor_mute, offenses_mute, updated_at) "
        "VALUES (1, 60.0, 30.0, 10, CURRENT_TIMESTAMP)"
    )
    op.add_column('cas_verdicts', sa.Column('spam_factor', sa.Float(), nullable=True))
    op.add_column('cas_verdicts', sa.Column('offenses', sa.Integer(), nullable=True))
    op.add_column('cas_verdicts', sa.Column('scammer', sa.Boolean(), nullable=True))
    op.add_column('cas_verdicts', sa.Column('tier', sa.String(length=16), nullable=True))


def downgrade() -> None:
    """Убирает пороги и метрики каскада."""
    op.drop_column('cas_verdicts', 'tier')
    op.drop_column('cas_verdicts', 'scammer')
    op.drop_column('cas_verdicts', 'offenses')
    op.drop_column('cas_verdicts', 'spam_factor')
    op.drop_table('cas_settings')
