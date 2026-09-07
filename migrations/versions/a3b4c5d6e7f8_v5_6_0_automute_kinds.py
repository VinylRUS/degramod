"""v5_6_0_automute_kinds

Revision ID: a3b4c5d6e7f8
Revises: f2b3c4d5e6f7
Create Date: 2026-09-08 10:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a3b4c5d6e7f8'
down_revision: Union[str, Sequence[str], None] = 'f2b3c4d5e6f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """v5.6.0: виды автомьютов (kind в PK) + decay счётчика.

    • automute_counters += kind, PK становится (chat_id, user_id, kind).
      До v5.6.0 счётчик был один на юзера в чате, и все четыре источника
      автомьюта (варны, via-bot, стикер-паки, word/link) его складывали:
      муты за спам ботами удлиняли автомьют за варны. Легаси-строки
      переносятся в kind='warns' — этот счётчик был виден в панели и
      сбрасывался через !resetmc.
    • chat_settings += automute_decay_days: за каждые N дней без автомьюта
      счётчик уменьшается на 1. 0 = отключено (поведение до v5.6.0).

    PRIMARY KEY в SQLite через ALTER TABLE не меняется — таблица
    пересобирается вручную (create → copy → drop → rename).
    """
    # SQLite не меняет PRIMARY KEY через ALTER TABLE. batch_alter_table здесь
    # не годится: он пересобирает таблицу по отражённой схеме, где PK уже есть,
    # и create_primary_key дал бы вторую. Поэтому руками: create → copy → drop
    # → rename, тем же кодом, что и легаси-путь в init_db().
    op.execute("""
        CREATE TABLE automute_counters_v560 (
            chat_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            kind VARCHAR(16) NOT NULL DEFAULT 'warns',
            count INTEGER NOT NULL DEFAULT 0,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (chat_id, user_id, kind)
        )
    """)
    op.execute(
        "INSERT INTO automute_counters_v560 "
        "(chat_id, user_id, kind, count, updated_at) "
        "SELECT chat_id, user_id, 'warns', count, updated_at "
        "FROM automute_counters"
    )
    op.execute("DROP TABLE automute_counters")
    op.execute("ALTER TABLE automute_counters_v560 RENAME TO automute_counters")
    op.add_column(
        'chat_settings',
        sa.Column('automute_decay_days', sa.Integer(), nullable=False,
                  server_default='0'),
    )


def downgrade() -> None:
    """Схлопывает счётчики обратно в один на (chat_id, user_id).

    Виды кроме 'warns' теряются: в старой схеме им негде жить.
    """
    op.drop_column('chat_settings', 'automute_decay_days')
    op.execute("""
        CREATE TABLE automute_counters_v484 (
            chat_id BIGINT NOT NULL,
            user_id BIGINT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (chat_id, user_id)
        )
    """)
    op.execute(
        "INSERT INTO automute_counters_v484 (chat_id, user_id, count, updated_at) "
        "SELECT chat_id, user_id, count, updated_at FROM automute_counters "
        "WHERE kind = 'warns'"
    )
    op.execute("DROP TABLE automute_counters")
    op.execute("ALTER TABLE automute_counters_v484 RENAME TO automute_counters")
