from __future__ import annotations

import os
from typing import Any

from utils.tools import _guess_mime_type, _list_dir, _safe_join


def _build_uploads_context(session_dir: str, *, max_files: int = 50) -> str:
    uploads_dir = _safe_join(session_dir, "uploads")
    if not os.path.isdir(uploads_dir):
        return ""
    entries = _list_dir(uploads_dir, max_depth=2)
    files: list[dict[str, Any]] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        if e.get("type") != "file":
            continue
        rel = str(e.get("relative_path") or "").replace("\\", "/").lstrip("/")
        path = str(e.get("path") or "")
        if not rel or not path:
            continue
        filename = os.path.basename(rel)
        mime = ""
        try:
            mime = _guess_mime_type(filename)
        except Exception:
            mime = ""
        size = 0
        try:
            size = os.path.getsize(path)
        except Exception:
            size = 0
        files.append({"relative_path": f"uploads/{rel}", "bytes": size, "mime_type": mime, "filename": filename})
    if not files:
        return ""
    files = sorted(files, key=lambda x: str(x.get("relative_path") or ""))
    files = files[: max(1, int(max_files or 50))]
    lines = [
        "\n\n[上传文件]",
        f"用户本次通过 files 参数上传的文件会保存到：{uploads_dir}",
        "请使用 read_temp_file(relative_path) 读取文件；需要把文件传给命令时，用 read_temp_file 返回的绝对路径（result.path）。",
        "",
        "[上传文件清单]",
        "以下路径均相对于本次会话的 session_dir：",
    ]
    for f in files:
        rel = str(f.get("relative_path") or "")
        abs_path = _safe_join(session_dir, rel) if rel else ""
        lines.append(
            f"- {rel} | abs={abs_path} | mime={f.get('mime_type') or ''} | bytes={f.get('bytes') or 0} | filename={f.get('filename') or ''}"
        )
    return "\n".join(lines) + "\n"
