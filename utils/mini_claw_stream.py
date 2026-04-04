from __future__ import annotations

from collections.abc import Callable, Generator

from dify_plugin.entities.tool import ToolInvokeMessage


def stream_text_to_user(
    *,
    create_text_message: Callable[[str], ToolInvokeMessage],
    text: str,
    chunk_size: int = 8,
) -> Generator[ToolInvokeMessage, None, None]:
    s = (text or "").strip()
    if not s:
        return
    step = max(1, int(chunk_size))
    for i in range(0, len(s), step):
        yield create_text_message(s[i : i + step])

