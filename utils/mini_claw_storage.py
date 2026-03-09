from __future__ import annotations

import json
import time
from typing import Any

from utils.mini_claw_constants import (
    APPROVAL_KEY_PREFIX,
    HISTORY_KEY_PREFIX,
    MEMORY_KEY_PREFIX,
    PERSONA_KEY_PREFIX,
    SESSION_DIR_KEY_PREFIX,
)
from utils.tools import _safe_get


def _pick_first_nonempty_str(values: list[Any]) -> str:
    for v in values:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _get_app_storage_id(session: Any) -> str:
    sid = _pick_first_nonempty_str(
        [
            _safe_get(session, "app_id"),
            _safe_get(session, "appId"),
            _safe_get(session, "app"),
        ]
    )
    return sid or "global_app"


def _get_conversation_storage_id(session: Any) -> str:
    sid = _pick_first_nonempty_str(
        [
            _safe_get(session, "conversation_id"),
            _safe_get(session, "chat_id"),
            _safe_get(session, "task_id"),
            _safe_get(session, "id"),
            _safe_get(session, "session_id"),
            _safe_get(session, "app_run_id"),
        ]
    )
    return sid or "global_conversation"


def _get_history_storage_key(session: Any) -> str:
    return HISTORY_KEY_PREFIX + _get_conversation_storage_id(session)


def _get_session_dir_storage_key(session: Any) -> str:
    return SESSION_DIR_KEY_PREFIX + _get_conversation_storage_id(session)


def _get_persona_storage_key(session: Any, name: str) -> str:
    suffix = str(name or "").strip() or "default"
    return PERSONA_KEY_PREFIX + _get_app_storage_id(session) + ":" + suffix


def _get_memory_storage_key(session: Any, name: str) -> str:
    suffix = str(name or "").strip() or "default"
    return MEMORY_KEY_PREFIX + _get_app_storage_id(session) + ":" + suffix


def _get_approval_storage_key(session: Any, name: str) -> str:
    suffix = str(name or "").strip() or "default"
    return APPROVAL_KEY_PREFIX + _get_app_storage_id(session) + ":" + suffix


def _get_conversation_approval_storage_key(session: Any, name: str) -> str:
    suffix = str(name or "").strip() or "default"
    return APPROVAL_KEY_PREFIX + _get_conversation_storage_id(session) + ":" + suffix


def _storage_get_text(storage: Any, key: str) -> str:
    try:
        val = storage.get(key)
        if not val:
            return ""
        if isinstance(val, bytes):
            return val.decode("utf-8", errors="ignore")
        if isinstance(val, str):
            return val
        return ""
    except Exception:
        return ""


def _storage_set_text(storage: Any, key: str, text: str) -> None:
    try:
        storage.set(key, (text or "").encode("utf-8"))
    except Exception:
        return


def _storage_get_json(storage: Any, key: str) -> dict[str, Any]:
    raw = _storage_get_text(storage, key).strip()
    if not raw:
        return {}
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {}
    except Exception:
        return {}


def _storage_set_json(storage: Any, key: str, value: dict[str, Any] | None) -> None:
    if not value:
        _storage_set_text(storage, key, "")
        return
    try:
        _storage_set_text(storage, key, json.dumps(value, ensure_ascii=False))
    except Exception:
        _storage_set_text(storage, key, "")
        return


def _append_history_turn(
    storage: Any,
    *,
    history_key: str,
    user_text: str,
    assistant_text: str,
    max_turns: int = 50,
) -> None:
    state = _storage_get_json(storage, history_key)
    turns = state.get("turns")
    if not isinstance(turns, list):
        turns = []
    turns.append(
        {
            "user": str(user_text or ""),
            "assistant": str(assistant_text or ""),
            "created_at": int(time.time()),
        }
    )
    if max_turns < 1:
        max_turns = 1
    if len(turns) > max_turns:
        turns = turns[-max_turns:]
    _storage_set_json(storage, history_key, {"turns": turns})
