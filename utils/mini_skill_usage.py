from __future__ import annotations

from decimal import Decimal
from typing import Any

from utils.tools import _safe_get


class LLMUsageAccumulator:
    def __init__(self) -> None:
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0
        self.prompt_price = Decimal("0")
        self.completion_price = Decimal("0")
        self.total_price = Decimal("0")
        self.currency = ""
        self.latency = 0.0

    def _to_int(self, v: Any) -> int:
        try:
            return int(v or 0)
        except Exception:
            return 0

    def _to_float(self, v: Any) -> float:
        try:
            return float(v or 0.0)
        except Exception:
            return 0.0

    def _to_decimal(self, v: Any) -> Decimal:
        if v is None:
            return Decimal("0")
        if isinstance(v, Decimal):
            return v
        try:
            return Decimal(str(v))
        except Exception:
            return Decimal("0")

    def record_usage_obj(self, usage: Any) -> None:
        if not usage:
            return
        self.prompt_tokens += self._to_int(_safe_get(usage, "prompt_tokens"))
        self.completion_tokens += self._to_int(_safe_get(usage, "completion_tokens"))
        self.total_tokens += self._to_int(_safe_get(usage, "total_tokens"))
        self.prompt_price += self._to_decimal(_safe_get(usage, "prompt_price"))
        self.completion_price += self._to_decimal(_safe_get(usage, "completion_price"))
        self.total_price += self._to_decimal(_safe_get(usage, "total_price"))
        cur = str(_safe_get(usage, "currency") or "").strip()
        if cur:
            if not self.currency:
                self.currency = cur
            elif self.currency != cur:
                self.currency = "MIXED"
        self.latency += self._to_float(_safe_get(usage, "latency"))

    def record_response(self, resp: Any) -> None:
        if not resp:
            return
        self.record_usage_obj(_safe_get(resp, "usage"))

    def record_chunk(self, chunk: Any) -> None:
        if not chunk:
            return
        delta = _safe_get(chunk, "delta")
        self.record_usage_obj(_safe_get(delta, "usage") if delta is not None else None)

    def payload(self) -> dict[str, Any]:
        return {
            "prompt_tokens": int(self.prompt_tokens),
            "completion_tokens": int(self.completion_tokens),
            "total_tokens": int(self.total_tokens),
            "prompt_price": str(self.prompt_price),
            "completion_price": str(self.completion_price),
            "total_price": str(self.total_price),
            "currency": str(self.currency or ""),
            "latency": float(self.latency),
        }

    def format_text(self, payload: dict[str, Any] | None = None) -> str:
        p = payload or self.payload()
        prompt_tokens = int(p.get("prompt_tokens") or 0)
        completion_tokens = int(p.get("completion_tokens") or 0)
        total_tokens = int(p.get("total_tokens") or 0)
        prompt_price = str(p.get("prompt_price") or "0")
        completion_price = str(p.get("completion_price") or "0")
        total_price = str(p.get("total_price") or "0")
        currency = str(p.get("currency") or "")
        return (
            f"\n📊 Token/费用 消耗统计：\n"
            f"  ✒️输入：{prompt_tokens} tokens\n"
            f"  ✒️输出：{completion_tokens} tokens\n"
            f"  ✒️总计：{total_tokens} tokens\n"
            f"  💰输入费用：{prompt_price} 元\n"
            f"  💰输出费用：{completion_price} 元\n"
            f"  💰总费用：{total_price} 元\n"
            f"  💵币种：{currency} \n"
        )

