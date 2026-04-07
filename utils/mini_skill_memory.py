from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone as _dt_timezone
from typing import Any

from utils.mini_skill_storage import (
    _get_memory_storage_key,
    _get_persona_storage_key,
    _get_user_memory_storage_key_for,
    _get_user_persona_storage_key_for,
    _storage_get_json,
    _storage_get_text,
    _storage_set_json,
    _storage_set_text,
)
from utils.tools import _shorten_text


def _dt_beijing(ts: float | None = None) -> datetime:
    epoch = float(ts if ts is not None else time.time())
    try:
        try:
            from zoneinfo import ZoneInfo  # type: ignore
        except Exception:
            ZoneInfo = None  # type: ignore
        if ZoneInfo is not None:
            return datetime.fromtimestamp(epoch, tz=ZoneInfo("Asia/Shanghai"))
    except Exception:
        pass
    return datetime.fromtimestamp(epoch, tz=_dt_timezone.utc).astimezone(_dt_timezone(timedelta(hours=8)))


def _beijing_date(ts: float | None = None) -> str:
    return _dt_beijing(ts).strftime("%Y-%m-%d")


def _beijing_hm(ts: float | None = None) -> str:
    return _dt_beijing(ts).strftime("%H:%M")


def _storage_delete(storage: Any, key: str) -> None:
    try:
        storage.delete(key)
    except Exception:
        return


def _daily_rel_path(*, user_id: str, day: str) -> str:
    uid = str(user_id or "").strip() or "global_user"
    return f"memory/{uid}/{day}.md"


def _delete_daily_memory(storage: Any, session: Any, *, user_id: str, day: str) -> None:
    _storage_delete(storage, _get_memory_storage_key(session, _daily_rel_path(user_id=user_id, day=day)))


def _reset_role(
    *,
    storage: Any,
    session: Any,
    onboarding_key: str,
    identity_key: str,
    user_key: str,
    soul_key: str,
    memory_key: str,
    users_index_key: str | None = None,
    keep_daily_days: int = 30,
) -> None:
    _storage_set_json(storage, onboarding_key, {"stage": 0, "completed": False, "reset_at": int(time.time())})
    _storage_delete(storage, identity_key)
    _storage_delete(storage, soul_key)
    _storage_delete(storage, user_key)
    _storage_delete(storage, memory_key)
    idx_key = str(users_index_key or "").strip() or _get_persona_storage_key(session, "users_index")
    idx = _storage_get_json(storage, idx_key)
    users = idx.get("users") if isinstance(idx.get("users"), list) else []
    for uid in users:
        u = str(uid or "").strip()
        if not u:
            continue
        _storage_delete(storage, _get_user_persona_storage_key_for(session, u, "USER.md"))
        _storage_delete(storage, _get_user_persona_storage_key_for(session, u, "user_onboarding"))
        _storage_delete(storage, _get_memory_storage_key(session, f"daily_gc_last_day:{u}"))
        _storage_delete(storage, _get_user_memory_storage_key_for(session, u, "MEMORY.md"))
    _storage_delete(storage, idx_key)
    try:
        now_dt = _dt_beijing()
        for days_ago in range(0, max(0, int(keep_daily_days))):
            day = (now_dt - timedelta(days=days_ago)).strftime("%Y-%m-%d")
            for uid in users:
                u = str(uid or "").strip()
                if not u:
                    continue
                _delete_daily_memory(storage, session, user_id=u, day=day)
    except Exception:
        return


def _gc_daily_memory(
    *,
    storage: Any,
    session: Any,
    user_id: str,
    today: str,
    keep_days: int = 30,
    scan_days: int = 366,
) -> None:
    uid = str(user_id or "").strip() or "global_user"
    marker_key = _get_memory_storage_key(session, f"daily_gc_last_day:{uid}")
    last = _storage_get_text(storage, marker_key).strip()
    if last == today:
        return
    dt_now = _dt_beijing()
    keep = max(0, int(keep_days))
    scan = max(0, int(scan_days))
    for days_ago in range(keep, keep + scan):
        day = (dt_now - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        _delete_daily_memory(storage, session, user_id=uid, day=day)
    _storage_set_text(storage, marker_key, today)


def _append_daily_dialogue(
    *,
    storage: Any,
    session: Any,
    user_id: str,
    user_text: str,
    assistant_text: str,
    keep_days: int = 30,
) -> None:
    uid = str(user_id or "").strip() or "global_user"
    today = _beijing_date()
    daily_key = _get_memory_storage_key(session, _daily_rel_path(user_id=uid, day=today))
    existing = _storage_get_text(storage, daily_key)
    header = f"# {today}\n\n"
    if not existing.strip():
        existing = header
    ts = _beijing_hm()
    u = _shorten_text(str(user_text or "").strip().replace("\r", "").replace("\n", " "), 500)
    a = _shorten_text(str(assistant_text or "").strip().replace("\r", "").replace("\n", " "), 800)
    entry = f"- {ts} USER: {u}\n- {ts} ASSISTANT: {a}\n\n"
    merged = existing + entry
    if len(merged) > 20000:
        merged = merged[-20000:]
        if not merged.lstrip().startswith("#"):
            merged = header + merged
    _storage_set_text(storage, daily_key, merged)
    _gc_daily_memory(storage=storage, session=session, user_id=uid, today=today, keep_days=keep_days)
