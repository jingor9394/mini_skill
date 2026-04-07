from __future__ import annotations

import base64
import hashlib
import os
import re
import uuid
from typing import Any

from utils.tools import _safe_join


def redact_user_visible_text(*, text: str, session_dir: str, skills_root: str | None) -> str:
    s = str(text or "")
    if not s:
        return s
    for p in [session_dir, skills_root]:
        if p and isinstance(p, str):
            s = s.replace(p, "<REDACTED_PATH>")
            s = s.replace(p.replace("\\", "/"), "<REDACTED_PATH>")
    s = re.sub(r"[A-Za-z]:\\[^\s\r\n\t\"']+", "<REDACTED_PATH>", s)
    s = re.sub(r"/[^\s\r\n\t\"']+", "<REDACTED_PATH>", s)
    return s


def persist_llm_assets(
    *,
    parts: Any,
    session_dir: str,
    saved_asset_fingerprints: set[str],
) -> list[str]:
    if not parts or not isinstance(parts, list):
        return []
    saved: list[str] = []
    out_dir = _safe_join(session_dir, "llm_assets")
    os.makedirs(out_dir, exist_ok=True)
    for i, item in enumerate(parts):
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "")
        if item_type not in {"image", "document", "audio", "video"}:
            continue
        mime = str(item.get("mime_type") or "")
        filename = str(item.get("filename") or "").strip()
        url = str(item.get("url") or item.get("data") or "").strip()
        b64 = str(item.get("base64_data") or "").strip()
        raw: bytes | None = None
        if b64:
            try:
                raw = base64.b64decode(b64, validate=False)
            except Exception:
                raw = None
        if raw is None and url.startswith("data:") and ";base64," in url:
            try:
                header, payload = url.split(";base64,", 1)
                if not mime and header.startswith("data:"):
                    mime = header[5:]
                raw = base64.b64decode(payload, validate=False)
            except Exception:
                raw = None
        if raw is None:
            continue
        fp = ""
        try:
            fp = hashlib.sha1(raw).hexdigest()
            key = f"{item_type}|{mime}|{fp}"
        except Exception:
            key = f"{item_type}|{mime}|{len(raw)}"
        if key in saved_asset_fingerprints:
            continue
        saved_asset_fingerprints.add(key)
        if not filename:
            ext = ""
            if mime:
                if "png" in mime:
                    ext = ".png"
                elif "jpeg" in mime or "jpg" in mime:
                    ext = ".jpg"
                elif "pdf" in mime:
                    ext = ".pdf"
                elif "json" in mime:
                    ext = ".json"
                elif "text" in mime or "markdown" in mime:
                    ext = ".txt"
            filename = f"{item_type}-{i+1}{ext or ''}"
        dst = _safe_join(out_dir, filename)
        if os.path.exists(dst):
            base, ext = os.path.splitext(filename)
            suffix = fp[:8] if fp else uuid.uuid4().hex[:8]
            dst = _safe_join(out_dir, f"{base}-{suffix}{ext}")
        try:
            with open(dst, "wb") as f:
                f.write(raw)
            saved.append(os.path.relpath(dst, session_dir))
        except Exception:
            continue
    return saved

