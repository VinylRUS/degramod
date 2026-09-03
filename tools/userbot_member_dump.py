#!/usr/bin/env python3
"""
userbot_member_dump.py — Dump members of Telegram chats into CSV files
(one CSV per chat) using a personal Telethon session.

Supports SOCKS5/SOCKS4/HTTP proxies via Telethon's built-in proxy support.
(MTProto proxy is NOT supported by Python clients — use a SOCKS5 bridge or
a full-tunnel VPN if Telegram-IPs are blocked in your network.)

Usage:
    python userbot_member_dump.py                  # all chats from config.yaml
    python userbot_member_dump.py --resume         # skip chats already done
    python userbot_member_dump.py --dry-run        # admin check + count only, no CSV
    python userbot_member_dump.py --only main-chat # only specific slugs

First run will prompt for phone / SMS code / 2FA password to create the .session
file. Subsequent runs are non-interactive.

CSV columns:
    chat_id, user_id, username, first_name, last_name,
    is_bot, is_deleted, status, dumped_at

status values:
    member  — normal participant (ChannelParticipant, ChannelParticipantSelf)
    admin   — administrator (ChannelParticipantAdmin)
    owner   — creator of the chat (ChannelParticipantCreator)
    banned  — kicked/banned (ChannelParticipantBanned)
    left    — left the chat but still listed (ChannelParticipantLeft)
"""

import argparse
import asyncio
import csv
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.functions.channels import (
    GetFullChannelRequest,
    GetParticipantRequest,
    GetParticipantsRequest,
)
from telethon.tl.types import (
    ChannelParticipant,
    ChannelParticipantAdmin,
    ChannelParticipantBanned,
    ChannelParticipantCreator,
    ChannelParticipantLeft,
    ChannelParticipantSelf,
    ChannelParticipantsSearch,
)

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.yaml"
ENV_PATH = SCRIPT_DIR / ".env"
DUMPS_DIR = SCRIPT_DIR / "dumps"
PROGRESS_PATH = DUMPS_DIR / ".progress.json"

MAX_MEMBERS_PER_CHAT = 200_000

CSV_HEADER = [
    "chat_id", "user_id", "username",
    "first_name", "last_name",
    "is_bot", "is_deleted", "status", "dumped_at",
]


# --------------------------------------------------------------------------
# Config / env loading
# --------------------------------------------------------------------------

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"config.yaml not found at {CONFIG_PATH}")
    with CONFIG_PATH.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    for key in ("session", "chats", "output", "rate"):
        if key not in cfg:
            raise ValueError(f"Missing required key '{key}' in config.yaml")

    for i, c in enumerate(cfg["chats"]):
        if "id" not in c or "slug" not in c:
            raise ValueError(f"chats[{i}] must have both 'id' and 'slug'")
        if not isinstance(c["id"], int):
            raise ValueError(f"chats[{i}].id must be int (use -100... prefix for supergroups)")
        if not c["slug"]:
            raise ValueError(f"chats[{i}].slug is empty")

    out = cfg["output"]
    out.setdefault("dir", "./dumps")
    out["dir"] = (SCRIPT_DIR / out["dir"]).resolve()
    out.setdefault("csv_encoding", "utf-8")
    out.setdefault("delimiter", ",")

    rate = cfg["rate"]
    rate.setdefault("sec_between_requests", 1.5)
    rate.setdefault("flood_wait_extra", 1)
    rate.setdefault("max_consecutive_floods", 10)
    rate.setdefault("skip_if_not_admin", True)

    proxy = cfg.setdefault("proxy", {})
    proxy.setdefault("enabled", False)
    proxy.setdefault("type", "mtproto")
    proxy.setdefault("host", "")
    proxy.setdefault("port", 1080)
    proxy.setdefault("secret", "")
    proxy.setdefault("username", "")
    proxy.setdefault("password", "")

    return cfg


def load_env() -> tuple[int, str]:
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH)
    api_id = os.getenv("TG_API_ID")
    api_hash = os.getenv("TG_API_HASH")
    if not api_id or not api_hash:
        raise RuntimeError(
            "TG_API_ID / TG_API_HASH missing in .env. "
            "Get them at https://my.telegram.org/apps (see README.md)."
        )
    try:
        api_id = int(api_id)
    except ValueError as _e:
        raise RuntimeError("TG_API_ID must be an integer") from _e
    return api_id, api_hash


def build_proxy(cfg: dict):
    """Build Telethon proxy tuple from config, or return None if disabled.

    Telethon supports: 'mtproto', 'socks5', 'socks4', 'http'.
      MTProto:    ("mtproto", host, port, secret)
      SOCKS/HTTP: ("socks5",  host, port[, username, password])

    NOTE: 'mtproto' only works if the `python-socks` package is installed AND
    the proxy server speaks MTProto on the wire. For most setups (Flowseal
    TG WS Proxy, etc.), you actually want 'socks5' pointing at a SOCKS5 bridge.
    """
    p = cfg.get("proxy", {})
    if not p.get("enabled"):
        return None

    ptype = (p.get("type") or "mtproto").lower()
    host = p.get("host", "").strip()
    port = int(p.get("port", 1080))
    secret = p.get("secret", "").strip()
    username = (p.get("username") or "").strip() or None
    password = (p.get("password") or "").strip() or None

    if not host:
        raise ValueError("proxy.enabled=true but proxy.host is empty")

    if ptype == "mtproto":
        if not secret:
            raise ValueError("proxy.type=mtproto but proxy.secret is empty")
        return ("mtproto", host, port, secret)
    if ptype in ("socks5", "socks4", "http"):
        if username and password:
            return (ptype, host, port, username, password)
        if username or password:
            raise ValueError(
                f"proxy.type={ptype} requires both username AND password, or neither"
            )
        return (ptype, host, port)
    raise ValueError(
        f"Unsupported proxy.type='{ptype}'. Use one of: mtproto, socks5, socks4, http."
    )


# --------------------------------------------------------------------------
# Progress (.progress.json) — resume on abort
# --------------------------------------------------------------------------

def load_progress() -> dict:
    if not PROGRESS_PATH.exists():
        return {}
    try:
        with PROGRESS_PATH.open(encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_progress(progress: dict) -> None:
    DUMPS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PROGRESS_PATH.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(progress, f, indent=2, ensure_ascii=False)
    tmp.replace(PROGRESS_PATH)


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

def setup_logging(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("dumper")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    return logger


# --------------------------------------------------------------------------
# CSV row helpers
# --------------------------------------------------------------------------

def safe_str(x) -> str:
    return "" if x is None else str(x)


def fmt_bool(b) -> str:
    return "true" if b else "false"


def user_to_row(chat_id: int, user, status: str, dumped_at: str) -> dict:
    """Build CSV row from a Telethon User object + participant status."""
    return {
        "chat_id": chat_id,
        "user_id": user.id,
        "username": safe_str(getattr(user, "username", None)),
        "first_name": safe_str(getattr(user, "first_name", None)),
        "last_name": safe_str(getattr(user, "last_name", None)),
        "is_bot": fmt_bool(getattr(user, "bot", False)),
        "is_deleted": fmt_bool(getattr(user, "deleted", False)),
        "status": status,
        "dumped_at": dumped_at,
    }


def participant_info(p) -> tuple[int | None, str]:
    """Return (user_id, status_str) for any ChannelParticipant subtype.

    status values:
        member, admin, owner, banned, left
    """
    if isinstance(p, ChannelParticipantCreator):
        return p.user_id, "owner"
    if isinstance(p, ChannelParticipantAdmin):
        return p.user_id, "admin"
    if isinstance(p, ChannelParticipantSelf):
        return p.user_id, "member"
    if isinstance(p, ChannelParticipant):
        return p.user_id, "member"
    if isinstance(p, ChannelParticipantBanned):
        # Banned participants have `peer` instead of `user_id`
        peer = getattr(p, "peer", None)
        uid = getattr(peer, "user_id", None) if peer is not None else None
        return uid, "banned"
    if isinstance(p, ChannelParticipantLeft):
        peer = getattr(p, "peer", None)
        uid = getattr(peer, "user_id", None) if peer is not None else None
        return uid, "left"
    # Fallback: try user_id, then peer.user_id, status unknown
    uid = getattr(p, "user_id", None)
    if uid is None:
        peer = getattr(p, "peer", None)
        if peer is not None:
            uid = getattr(peer, "user_id", None)
    return uid, "unknown"


def slugify(s: str) -> str:
    keep = []
    for ch in s:
        if ch.isalnum() or ch in "-_":
            keep.append(ch)
        else:
            keep.append("_")
    return "".join(keep).strip("_") or "chat"


# --------------------------------------------------------------------------
# Core dump logic
# --------------------------------------------------------------------------

async def check_admin(client, entity, me_id: int) -> tuple[bool, str]:
    """Return (is_admin, status_str)."""
    try:
        me_input = await client.get_input_entity(me_id)
        result = await client(GetParticipantRequest(
            channel=entity,
            participant=me_input,
        ))
        participant = result.participant
        if isinstance(participant, (ChannelParticipantAdmin, ChannelParticipantCreator)):
            return True, type(participant).__name__.replace("ChannelParticipant", "")
        return False, type(participant).__name__.replace("ChannelParticipant", "")
    except Exception as e:
        return False, f"get_participant failed: {e}"


async def dump_chat(client, chat: dict, cfg: dict, me, logger: logging.Logger,
                    run_ts: str, dry_run: bool = False) -> dict | None:
    chat_id = chat["id"]
    slug = slugify(chat["slug"])
    rate = cfg["rate"]
    out_dir: Path = cfg["output"]["dir"]
    encoding = cfg["output"]["csv_encoding"]
    delimiter = cfg["output"]["delimiter"]
    sleep_sec = rate["sec_between_requests"]
    flood_extra = rate["flood_wait_extra"]
    max_floods = rate["max_consecutive_floods"]
    skip_if_not_admin = rate.get("skip_if_not_admin", True)

    logger.info(f"=== {slug} ({chat_id}) ===")

    try:
        entity = await client.get_entity(chat_id)
    except Exception as e:
        logger.error(f"{slug}: get_entity failed: {e}")
        return {"slug": slug, "chat_id": chat_id, "aborted": True, "dumped": 0}

    is_adm, status_str = await check_admin(client, entity, me.id)
    if not is_adm:
        if skip_if_not_admin:
            logger.warning(f"{slug}: not admin (status={status_str}), skipping")
            return {"slug": slug, "chat_id": chat_id, "skipped": True,
                    "reason": f"not admin ({status_str})"}
        logger.warning(f"{slug}: not admin (status={status_str}), "
                       f"continuing anyway — Telegram may truncate to ~200")

    try:
        count = getattr(entity, "participants_count", None)
        if not count:
            full = await client(GetFullChannelRequest(channel=entity))
            count = full.full_chat.participants_count or 0
    except Exception as e:
        logger.error(f"{slug}: get member count failed: {e}")
        return {"slug": slug, "chat_id": chat_id, "skipped": True,
                "reason": f"count failed: {e}"}

    logger.info(f"{slug}: {count} members reported by Telegram")

    if dry_run:
        logger.info(f"{slug}: dry-run, no CSV written")
        return {"slug": slug, "chat_id": chat_id, "count": count,
                "is_admin": is_adm, "dry_run": True}

    if count > MAX_MEMBERS_PER_CHAT:
        logger.error(f"{slug}: count {count} > safety limit {MAX_MEMBERS_PER_CHAT}, skipping")
        return {"slug": slug, "chat_id": chat_id, "skipped": True,
                "reason": f"too large ({count} > {MAX_MEMBERS_PER_CHAT})"}

    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"{slug}_{chat_id}_{run_ts}.csv"
    dumped_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    collected = 0
    status_counts: dict[str, int] = {}
    offset = 0
    floods_total = 0
    floods_in_a_row = 0
    start = time.monotonic()
    last_log_t = start

    with csv_path.open("w", encoding=encoding, newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=CSV_HEADER, delimiter=delimiter,
            quoting=csv.QUOTE_MINIMAL, quotechar='"',
        )
        writer.writeheader()

        while True:
            try:
                result = await client(GetParticipantsRequest(
                    channel=entity,
                    filter=ChannelParticipantsSearch(""),
                    offset=offset,
                    limit=200,
                    hash=0,
                ))
            except FloodWaitError as e:
                floods_total += 1
                floods_in_a_row += 1
                logger.warning(
                    f"{slug}: FloodWait {e.seconds}s at offset {offset} "
                    f"(#{floods_total}, in-a-row={floods_in_a_row})"
                )
                if floods_in_a_row > max_floods:
                    logger.error(f"{slug}: too many consecutive FloodWaits, aborting chat")
                    return {
                        "slug": slug, "chat_id": chat_id, "count": count,
                        "dumped": collected, "aborted": True,
                        "csv_path": str(csv_path),
                    }
                await asyncio.sleep(e.seconds + flood_extra)
                continue
            except Exception as e:
                logger.error(f"{slug}: GetParticipants failed at offset {offset}: {e}")
                return {
                    "slug": slug, "chat_id": chat_id, "count": count,
                    "dumped": collected, "aborted": True,
                    "csv_path": str(csv_path),
                }

            floods_in_a_row = 0

            raw_count = len(result.participants)
            if raw_count == 0:
                logger.info(f"{slug}: empty batch at offset {offset}, done")
                break

            users_by_id = {u.id: u for u in result.users}
            rows_written = 0
            for p in result.participants:
                uid, status = participant_info(p)
                if uid is None:
                    continue
                u = users_by_id.get(uid)
                if u is not None:
                    writer.writerow(user_to_row(chat_id, u, status, dumped_at))
                    rows_written += 1
                    status_counts[status] = status_counts.get(status, 0) + 1
            f.flush()
            collected += rows_written

            now = time.monotonic()
            if now - last_log_t > 15:
                pct = (collected / count * 100) if count else 0
                logger.info(
                    f"{slug}: {collected}/{count} ({pct:.1f}%), "
                    f"{floods_total} FloodWait total"
                )
                last_log_t = now

            if raw_count < 200:
                logger.info(f"{slug}: short batch ({raw_count}), done")
                break

            offset += 200
            if offset > count + 400:
                logger.warning(
                    f"{slug}: offset {offset} exceeded count+400 "
                    f"({count + 400}), stopping"
                )
                break
            await asyncio.sleep(sleep_sec)

    elapsed = time.monotonic() - start
    pct = (collected / count * 100) if count else 0
    status_summary = ", ".join(
        f"{k}={v}" for k, v in sorted(status_counts.items())
    ) or "none"
    logger.info(
        f"{slug}: done — {collected}/{count} ({pct:.1f}%), "
        f"{floods_total} FloodWait, {elapsed:.0f}s, "
        f"file={csv_path.name}"
    )
    logger.info(f"{slug}: status breakdown — {status_summary}")

    return {
        "slug": slug, "chat_id": chat_id, "count": count,
        "dumped": collected, "aborted": False,
        "csv_path": str(csv_path), "elapsed_sec": elapsed,
        "status_counts": status_counts,
    }


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

async def main_async(args) -> int:
    cfg = load_config()
    api_id, api_hash = load_env()

    run_ts = datetime.now().strftime("%Y-%m-%d_%H%M")
    log_path = DUMPS_DIR / f"run_{run_ts}.log"
    logger = setup_logging(log_path)
    logger.info(f"=== run started, ts={run_ts} ===")
    logger.info(f"output dir: {cfg['output']['dir']}")
    logger.info(f"chats in config: {len(cfg['chats'])}")

    session_name = cfg["session"]["name"]
    session_path = str(SCRIPT_DIR / session_name)
    logger.info(f"session file: {session_path}.session")

    proxy = build_proxy(cfg)
    if proxy is None:
        logger.info("proxy: disabled (direct connection)")
    else:
        p = cfg["proxy"]
        logger.info(f"proxy: enabled, type={p['type']}, host={p['host']}:{p['port']}")

    if args.only:
        wanted = set(args.only)
        cfg["chats"] = [c for c in cfg["chats"] if c["slug"] in wanted]
        logger.info(
            f"--only: filtered to {len(cfg['chats'])} chats: "
            f"{[c['slug'] for c in cfg['chats']]}"
        )

    if not cfg["chats"]:
        logger.error("no chats to process")
        return 1

    progress = load_progress() if args.resume else {}
    if args.resume:
        logger.info(f"--resume: progress loaded from {PROGRESS_PATH}")

    client = TelegramClient(
        session_path,
        api_id,
        api_hash,
        proxy=proxy,
    )

    try:
        await client.start()
    except Exception as e:
        logger.error(f"failed to start client: {e}")
        return 1

    try:
        me = await client.get_me()
        logger.info(
            f"logged in as id={me.id} "
            f"username=@{getattr(me, 'username', None)} "
            f"name={getattr(me, 'first_name', None)}"
        )

        # Warm-up: fetch all dialogs to populate entity cache (access_hash).
        # Without this, get_entity(chat_id) fails on fresh sessions because
        # Telethon hasn't seen these channels yet.
        logger.info("warming up: fetching dialogs to populate entity cache...")
        try:
            dialogs = await client.get_dialogs(limit=500)
            logger.info(f"warm-up done: {len(dialogs)} dialogs cached")
            found_ids = set()
            for d in dialogs:
                if d.entity and hasattr(d.entity, "id"):
                    found_ids.add(d.entity.id)
            for chat in cfg["chats"]:
                raw_id = chat["id"]
                if raw_id < -1000000000000:
                    raw_id = int(str(abs(raw_id))[3:])
                if raw_id in found_ids or chat["id"] in found_ids:
                    logger.info(f"  {chat['slug']}: found in dialogs ✓")
                else:
                    logger.warning(f"  {chat['slug']}: NOT found in dialogs — "
                                   f"are you actually a member/admin?")
        except Exception as e:
            logger.warning(f"warm-up failed (continuing anyway): {e}")

        summary = []
        for chat in cfg["chats"]:
            chat_key = str(chat["id"])
            if args.resume and progress.get(chat_key, {}).get("done"):
                logger.info(f"{chat['slug']}: already done (resume), skipping")
                continue

            try:
                result = await dump_chat(
                    client, chat, cfg, me, logger,
                    run_ts, dry_run=args.dry_run,
                )
            except Exception as e:
                logger.exception(f"{chat['slug']}: unexpected error: {e}")
                result = {"slug": chat["slug"], "chat_id": chat["id"],
                          "aborted": True, "error": str(e)}

            if result is None:
                progress[chat_key] = {
                    "slug": chat["slug"], "done": False, "skipped": True,
                }
            elif result.get("skipped"):
                progress[chat_key] = {
                    "slug": chat["slug"], "done": False,
                    "skipped": True, "reason": result.get("reason"),
                }
            elif result.get("aborted"):
                progress[chat_key] = {
                    "slug": chat["slug"], "done": False,
                    "aborted": True, "dumped": result.get("dumped", 0),
                    "count": result.get("count"),
                }
            else:
                progress[chat_key] = {
                    "slug": chat["slug"], "done": True,
                    "dumped": result.get("dumped", 0),
                    "count": result.get("count"),
                    "csv_path": result.get("csv_path"),
                    "status_counts": result.get("status_counts", {}),
                }
            save_progress(progress)
            summary.append(result)

        # Final summary
        logger.info("=== run finished ===")
        done = sum(1 for r in summary if r and not r.get("aborted")
                   and not r.get("skipped") and not r.get("dry_run"))
        aborted = sum(1 for r in summary if r and r.get("aborted"))
        skipped = sum(1 for r in summary if r and r.get("skipped"))
        total_rows = sum(
            (r or {}).get("dumped", 0) for r in summary
            if r and not r.get("dry_run")
        )
        logger.info(
            f"summary: done={done}, aborted={aborted}, "
            f"skipped={skipped}, total rows dumped={total_rows}"
        )
        logger.info(f"log file: {log_path}")
        logger.info(f"progress file: {PROGRESS_PATH}")
        return 0 if aborted == 0 else 2
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


def parse_args():
    p = argparse.ArgumentParser(
        description="Telegram chat member dumper (Telethon userbot)"
    )
    p.add_argument("--resume", action="store_true",
                   help="skip chats already marked done in progress.json")
    p.add_argument("--dry-run", action="store_true",
                   help="only check admin rights + member count, no CSV")
    p.add_argument("--only", nargs="+",
                   help="only process chats with these slugs")
    return p.parse_args()


def main():
    args = parse_args()
    try:
        rc = asyncio.run(main_async(args))
        sys.exit(rc)
    except KeyboardInterrupt:
        print("\ninterrupted")
        sys.exit(130)
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
