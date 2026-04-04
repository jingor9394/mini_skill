from __future__ import annotations

import re
from typing import Any

from utils.mini_claw_storage import _storage_get_text


def build_agent_tag_header(
    *,
    storage: Any,
    identity_key: str,
    identity_md: str | None,
    default_name: str = "Mini_Claw",
    default_emoji: str = "🤖",
) -> str:
    def pick_field(md: str, keys: list[str]) -> str:
        s = str(md or "")
        if not s:
            return ""
        for k in keys:
            rx = re.compile(
                rf"^\s*(?:-\s*)?\*\*\s*{re.escape(k)}\s*:\s*\*\*\s*(.+?)\s*$",
                flags=re.M | re.I,
            )
            m = rx.search(s)
            if m:
                return str(m.group(1) or "").strip()
        return ""

    identity_text = ""
    try:
        identity_text = _storage_get_text(storage, identity_key).strip()
    except Exception:
        identity_text = ""
    if not identity_text:
        try:
            identity_text = str(identity_md or "").strip()
        except Exception:
            identity_text = ""

    name = pick_field(identity_text, ["Name", "名字", "称呼"])
    emoji = pick_field(identity_text, ["Emoji", "表情", "签名", "签名Emoji"])
    name = re.sub(r"\s+", " ", name).strip() if name else ""
    emoji = re.sub(r"\s+", " ", emoji).strip() if emoji else ""
    if not name:
        name = default_name
    if not emoji:
        emoji = default_emoji
    return f"【{emoji}{name}】" if emoji else f"【{name}】"

