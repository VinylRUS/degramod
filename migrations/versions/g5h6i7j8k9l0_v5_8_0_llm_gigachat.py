"""v5_8_0_llm_gigachat

Revision ID: g5h6i7j8k9l0
Revises: a3b4c5d6e7f8
Create Date: 2026-11-04 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'g5h6i7j8k9l0'
down_revision: Union[str, Sequence[str], None] = 'a3b4c5d6e7f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """v5.8.0: GigaChat-интеграция.

    • chat_settings += llm_expand_reason — per-chat тумблер авторазвёртки
      причины наказания. Глобальный рубильник — в llm_settings.is_active.
      Оба по умолчанию выключены (фича opt-in).

    • Новая таблица llm_settings — singleton (id=1) с зашифрованным через
      Fernet ключом GigaChat. Шифрование тем же ключом, что и GitHub PAT
      (см. ``db._load_or_create_enc_key``). На проде Fernet-ключ лежит в
      ``/app/data/.github_enc_key`` с правами 0600; при потере теряются
      одновременно и PAT, и GigaChat-ключ.

    • Seed singleton-строки llm_settings id=1 — на ней будут UPDATE'ы
      из веб-панели /admin/settings.
    """
    op.add_column(
        'chat_settings',
        sa.Column('llm_expand_reason', sa.Boolean(),
                  nullable=False, server_default='0'),
    )
    op.create_table(
        'llm_settings',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('api_key_encrypted', sa.Text(), nullable=True),
        sa.Column('model_name', sa.String(64), nullable=True),
        sa.Column('timeout_seconds', sa.Integer(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('updated_by', sa.String(64), nullable=True),
    )
    # Singleton seed — без него UPDATE из веб-формы падает на
    # "no row found" (роут использует UPDATE, не upsert).
    op.execute(
        "INSERT INTO llm_settings (id, is_active) VALUES (1, 0)"
    )


def downgrade() -> None:
    """v5.8.0: rollback.

    Удаляет колонку и таблицу. Данные ключа GigaChat будут потеряны —
    при откате нужно перезавести ключ через /admin/settings.
    """
    op.drop_column('chat_settings', 'llm_expand_reason')
    op.drop_table('llm_settings')
