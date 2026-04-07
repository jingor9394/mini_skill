from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Callable

from utils.mini_skill_memory import _dt_beijing
from utils.mini_skill_storage import _get_memory_storage_key, _storage_get_text


@dataclass(frozen=True)
class DailyWriteContext:
    user_id: str
    user_text: str
    assistant_text: str
    approval_pending: bool
    approval_context: str


DailyWriteFilter = Callable[[DailyWriteContext], bool | None]


@dataclass(frozen=True)
class MemoryWriteContext:
    user_id: str
    text: str


MemoryWriteFilter = Callable[[MemoryWriteContext], str | None]


@dataclass(frozen=True)
class ExecPolicyContext:
    tool: str
    skill_name: str | None
    command: list[str]
    session_dir: str
    skills_root: str | None


ExecPolicy = Callable[[ExecPolicyContext], dict[str, Any] | None]


@dataclass(frozen=True)
class PromptBuildContext:
    storage: Any
    session: Any
    user_id: str
    identity_key: str
    user_key: str
    soul_key: str
    memory_key: str


PromptHook = Callable[[PromptBuildContext], list[tuple[str, str]] | None]


_DAILY_WRITE_FILTERS: list[DailyWriteFilter] = []
_MEMORY_WRITE_FILTERS: list[MemoryWriteFilter] = []
_EXEC_POLICIES: list[ExecPolicy] = []
_PROMPT_SHARED_HOOKS: list[PromptHook] = []
_PROMPT_PERSONAL_HOOKS: list[PromptHook] = []
_PROMPT_SESSION_HOOKS: list[PromptHook] = []


def register_daily_write_filter(hook: DailyWriteFilter) -> None:
    _DAILY_WRITE_FILTERS.append(hook)


def register_memory_write_filter(hook: MemoryWriteFilter) -> None:
    _MEMORY_WRITE_FILTERS.append(hook)


def register_exec_policy(hook: ExecPolicy) -> None:
    _EXEC_POLICIES.append(hook)


def register_prompt_shared_hook(hook: PromptHook) -> None:
    _PROMPT_SHARED_HOOKS.append(hook)


def register_prompt_personal_hook(hook: PromptHook) -> None:
    _PROMPT_PERSONAL_HOOKS.append(hook)


def register_prompt_session_hook(hook: PromptHook) -> None:
    _PROMPT_SESSION_HOOKS.append(hook)


def should_write_daily(ctx: DailyWriteContext) -> bool:
    for h in _DAILY_WRITE_FILTERS:
        try:
            r = h(ctx)
            if r is False:
                return False
        except Exception:
            continue
    return True


def filter_memory_write(ctx: MemoryWriteContext) -> str:
    cur = str(ctx.text or "")
    for h in _MEMORY_WRITE_FILTERS:
        try:
            nxt = h(MemoryWriteContext(user_id=ctx.user_id, text=cur))
            if nxt is None:
                continue
            cur = str(nxt)
        except Exception:
            continue
    return cur


def apply_exec_policies(ctx: ExecPolicyContext) -> dict[str, Any]:
    cur = ExecPolicyContext(
        tool=ctx.tool,
        skill_name=ctx.skill_name,
        command=[str(x) for x in (ctx.command or [])],
        session_dir=str(ctx.session_dir or ""),
        skills_root=str(ctx.skills_root) if ctx.skills_root else None,
    )
    for h in _EXEC_POLICIES:
        try:
            out = h(cur)
        except Exception:
            continue
        if not out:
            continue
        if out.get("ok") is False:
            return out
        nxt = out.get("command")
        if isinstance(nxt, list) and nxt:
            cur = ExecPolicyContext(
                tool=cur.tool,
                skill_name=cur.skill_name,
                command=[str(x) for x in nxt],
                session_dir=cur.session_dir,
                skills_root=cur.skills_root,
            )
    return {"ok": True, "command": cur.command}


def build_prompt_layers(ctx: PromptBuildContext) -> dict[str, list[tuple[str, str]]]:
    shared: list[tuple[str, str]] = []
    personal: list[tuple[str, str]] = []
    session: list[tuple[str, str]] = []

    for h in _PROMPT_SHARED_HOOKS:
        try:
            blocks = h(ctx)
            if blocks:
                shared.extend([(str(p), str(c)) for p, c in blocks if str(c or "").strip()])
        except Exception:
            continue
    for h in _PROMPT_PERSONAL_HOOKS:
        try:
            blocks = h(ctx)
            if blocks:
                personal.extend([(str(p), str(c)) for p, c in blocks if str(c or "").strip()])
        except Exception:
            continue
    for h in _PROMPT_SESSION_HOOKS:
        try:
            blocks = h(ctx)
            if blocks:
                session.extend([(str(p), str(c)) for p, c in blocks if str(c or "").strip()])
        except Exception:
            continue

    return {"shared": shared, "personal": personal, "session": session}


def _default_daily_write_filter_approval(ctx: DailyWriteContext) -> bool | None:
    s_a = str(ctx.assistant_text or "")
    if not s_a:
        return None
    keys = [
        "需要你确认后才能继续",
        "需要你确认后才能继续执行",
        "该步骤需要执行一个未在允许列表的命令",
        "执行审批",
        "审批结果",
        "用户已审批",
        "已收到你的拒绝",
        "允许一次",
        "总是允许",
        "拒绝",
    ]
    if any(k in s_a for k in keys):
        return False
    if ctx.approval_pending or str(ctx.approval_context or "").strip():
        s_u = str(ctx.user_text or "").strip()
        if s_u.isdigit() and 1 <= len(s_u) <= 3:
            return False
    return None


def _default_exec_policy_strip_backticks(ctx: ExecPolicyContext) -> dict[str, Any] | None:
    if not ctx.command:
        return None
    out: list[str] = []
    for i, a in enumerate(ctx.command):
        s = str(a or "").strip()
        if i > 0:
            if s.startswith("`") and s.endswith("`") and len(s) >= 2:
                s = s[1:-1].strip()
            if s.startswith("`"):
                s = s[1:].strip()
            if s.endswith("`"):
                s = s[:-1].strip()
        out.append(s)
    out = [x for x in out if x != ""]
    if not out:
        return {"ok": False, "error": "command must be a non-empty list"}
    return {"ok": True, "command": out}


def _default_memory_write_filter_approval(ctx: MemoryWriteContext) -> str | None:
    s = str(ctx.text or "")
    if not s:
        return None
    keys = [
        "需要你确认后才能继续",
        "需要你确认后才能继续执行",
        "该步骤需要执行一个未在允许列表的命令",
        "执行审批",
        "审批结果",
        "用户已审批",
        "已收到你的拒绝",
        "允许一次",
        "总是允许",
        "拒绝",
    ]
    blocks = [b for b in s.split("\n\n") if b.strip()]
    kept: list[str] = []
    for b in blocks:
        if any(k in b for k in keys):
            continue
        kept.append(b)
    return "\n\n".join(kept).strip()


def _default_prompt_shared(ctx: PromptBuildContext) -> list[tuple[str, str]] | None:
    soul_md = _storage_get_text(ctx.storage, ctx.soul_key).strip()
    identity_md = _storage_get_text(ctx.storage, ctx.identity_key).strip()
    blocks: list[tuple[str, str]] = []
    if soul_md:
        blocks.append(("SOUL.md", soul_md))
    if identity_md:
        blocks.append(("IDENTITY.md", identity_md))
    return blocks


def _default_prompt_personal(ctx: PromptBuildContext) -> list[tuple[str, str]] | None:
    user_md = _storage_get_text(ctx.storage, ctx.user_key).strip()
    memory_md = _storage_get_text(ctx.storage, ctx.memory_key).strip()
    dt_now_bj = _dt_beijing()
    today = dt_now_bj.strftime("%Y-%m-%d")
    yesterday = (dt_now_bj - timedelta(days=1)).strftime("%Y-%m-%d")
    uid = str(ctx.user_id or "").strip() or "global_user"
    daily_today_key = _get_memory_storage_key(ctx.session, f"memory/{uid}/{today}.md")
    daily_yesterday_key = _get_memory_storage_key(ctx.session, f"memory/{uid}/{yesterday}.md")
    daily_today_md = _storage_get_text(ctx.storage, daily_today_key).strip()
    daily_yesterday_md = _storage_get_text(ctx.storage, daily_yesterday_key).strip()
    blocks: list[tuple[str, str]] = []
    if user_md:
        blocks.append(("USER.md", user_md))
    if memory_md:
        blocks.append(("MEMORY.md", memory_md))
    if daily_yesterday_md:
        blocks.append((f"memory/{uid}/{yesterday}.md", daily_yesterday_md))
    if daily_today_md:
        blocks.append((f"memory/{uid}/{today}.md", daily_today_md))
    return blocks


register_daily_write_filter(_default_daily_write_filter_approval)
register_memory_write_filter(_default_memory_write_filter_approval)
register_exec_policy(_default_exec_policy_strip_backticks)
register_prompt_shared_hook(_default_prompt_shared)
register_prompt_personal_hook(_default_prompt_personal)
