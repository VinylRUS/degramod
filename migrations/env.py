"""Alembic env.py — v4.8.9: настроен на SQLAlchemy async engine из db.py.

Логика:
  1. Импортирует `db.Base.metadata` как target_metadata (чтобы autogenerate
     видел все наши модели).
  2. Использует `db.engine` (async engine на aiosqlite) для online-миграций
     через asyncio.run() — если вызываем из CLI (`alembic upgrade head`).
  3. Если уже в running event loop (например, bot.py lifespan вызывает
     init_db_with_fallback()) — fallback на sync engine (sqlite3).

Для SQLite используем batch mode (см. 03_TASK_v4.8.9.md §4 — грабли №1):
SQLite не поддерживает некоторые ALTER TABLE операции, и Alembic через
batch mode временно копирует таблицу, применяет изменения, и подменяет.
"""
from __future__ import annotations

import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Добавляем v488_work/ в sys.path, чтобы импортировать db.
_THIS_DIR = Path(__file__).resolve().parent
_V488_WORK = _THIS_DIR.parent
sys.path.insert(0, str(_V488_WORK))

import db  # noqa: E402
from db import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Override sqlalchemy.url — берём из db.DATABASE_URL.
config.set_main_option("sqlalchemy.url", db.DATABASE_URL)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — генерация SQL без подключения к БД."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Реальный запуск миграций через connection (sync API)."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online_async() -> None:
    """Run migrations in 'online' mode через async engine (aiosqlite)."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online_sync() -> None:
    """Синхронный запуск миграций — fallback если уже в async-context.

    v4.8.9: bot.py lifespan startup вызывает init_db_with_fallback() внутри
    running event loop. asyncio.run() в этом случае падает с RuntimeError.
    Решение — использовать sync engine (sqlite3) для миграций. Это работает
    потому что SQLite — файловая БД, и sync/async доступ могут сосуществовать
    (с busy_timeout=30000 в обоих случаях).
    """
    from sqlalchemy import create_engine

    url = config.get_main_option("sqlalchemy.url")
    # Конвертируем sqlite+aiosqlite:// в sqlite:// (sync driver).
    sync_url = url.replace("sqlite+aiosqlite://", "sqlite://")
    connectable = create_engine(sync_url, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        do_run_migrations(connection)
    connectable.dispose()


def run_migrations_online() -> None:
    """Точка входа для online-migrations.

    Если уже в running event loop — sync engine. Иначе — asyncio.run + async.
    """
    try:
        asyncio.get_running_loop()
        run_migrations_online_sync()
    except RuntimeError:
        asyncio.run(run_migrations_online_async())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
