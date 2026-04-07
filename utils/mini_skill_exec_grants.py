from __future__ import annotations

import time
from typing import Any


def parse_exec_approval_reply(text: str) -> str | None:
    s = str(text or "").strip()
    if not s:
        return None
    s2 = s.replace(" ", "").replace("\t", "").replace("\r", "").replace("\n", "")
    s2 = s2.translate(str.maketrans({"１": "1", "２": "2", "３": "3"}))
    if s2 == "1":
        return "once"
    if s2 == "2":
        return "always"
    if s2 == "3":
        return "deny"
    return None


def coerce_allow_entries(v: Any) -> list[dict[str, Any]]:
    if not v:
        return []
    if isinstance(v, list):
        out: list[dict[str, Any]] = []
        for item in v:
            if isinstance(item, str) and item.strip():
                out.append({"pattern": item.strip()})
            elif isinstance(item, dict):
                pat = str(item.get("pattern") or "").strip()
                if pat:
                    out.append(dict(item))
        return out
    return []


def extract_patterns(entries: Any) -> list[str]:
    out: list[str] = []
    for e in coerce_allow_entries(entries):
        pat = str(e.get("pattern") or "").strip()
        if pat and pat not in out:
            out.append(pat)
    return out


def _ensure_path(d: dict[str, Any], keys: list[str]) -> dict[str, Any]:
    cur: dict[str, Any] = d
    for k in keys:
        nxt = cur.get(k)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[k] = nxt
        cur = nxt
    return cur


def add_allow_entry(
    *,
    store: dict[str, Any],
    scope: str,
    exe: str,
    pattern: str,
    skill_name: str | None,
    command: list[str],
) -> dict[str, Any]:
    exe0 = str(exe or "").strip()
    pat0 = str(pattern or "").strip()
    if not exe0 or not pat0:
        return store
    now_ts = int(time.time())
    root = _ensure_path(store, ["exec"])
    bucket = _ensure_path(root, ["allow"])
    items = bucket.get(exe0)
    entries = coerce_allow_entries(items)
    existing = next((e for e in entries if str(e.get("pattern") or "").strip() == pat0), None)
    if existing is None:
        existing = {"pattern": pat0, "created_at": now_ts}
        entries.append(existing)
    existing["last_used_at"] = now_ts
    existing["last_used_command"] = " ".join([str(x) for x in (command or [])])[:500]
    entries = entries[-200:]
    bucket[exe0] = entries
    return store


def build_exec_override_from_grants(
    *,
    grants: dict[str, Any],
    tool_name: str,
    skill_name: str | None,
    requested_command: list[str],
    exe0: str,
) -> dict[str, Any] | None:
    if not exe0:
        return None
    exec_cfg = grants.get("exec") if isinstance(grants.get("exec"), dict) else {}
    allow = exec_cfg.get("allow")
    has_entry = isinstance(allow, dict) and bool(coerce_allow_entries(allow.get(exe0)))
    if not has_entry:
        return None
    return {"exe": exe0, "allow_not_in_allowlist": True}

