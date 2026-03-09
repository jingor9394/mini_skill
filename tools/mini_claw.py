import re
import json
import os
import time
import uuid
import base64
import hashlib
from datetime import datetime, timedelta, timezone as _dt_timezone
from collections.abc import Generator
from typing import Any

from utils.tools import (
    _build_prompt_message_tools,
    _download_file_content,
    _extract_url_and_name,
    _guess_mime_type,
    _infer_ext_from_url,
    _is_allow_reply,
    _is_deny_reply,
    _list_dir,
    _parse_tool_call,
    _safe_filename,
    _safe_get,
    _safe_join,
    _shorten_text,
    _split_message_content,
 )

from utils.mini_claw_debug import _dbg, _model_brief
from utils.mini_claw_exec import _cleanup_old_temp_sessions, _detect_skills_root
from utils.mini_claw_runtime import _AgentRuntime
from utils.mini_claw_schemas import TOOL_SCHEMAS, _tool_call_retry_prompt, _validate_tool_arguments
from utils.mini_claw_storage import (
    _append_history_turn,
    _get_approval_storage_key,
    _get_conversation_approval_storage_key,
    _get_history_storage_key,
    _get_memory_storage_key,
    _get_persona_storage_key,
    _get_session_dir_storage_key,
    _storage_get_json,
    _storage_get_text,
    _storage_set_json,
    _storage_set_text,
)
from utils.mini_claw_uploads import _build_uploads_context

from dify_plugin import Tool
from dify_plugin.entities.model.message import (
    AssistantPromptMessage,
    PromptMessageTool,
    SystemPromptMessage,
    ToolPromptMessage,
    UserPromptMessage,
)
from dify_plugin.entities.tool import ToolInvokeMessage

class SkillAgentTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        model = tool_parameters.get("model")
        query = tool_parameters.get("query")
        timeout_seconds = int(tool_parameters.get("timeout_seconds") or 120)
        compaction_max_prompt_tokens = int(tool_parameters.get("compaction_max_prompt_tokens") or 12000)
        loop_detection = bool(tool_parameters.get("loop_detection") if tool_parameters.get("loop_detection") is not None else True)

        memory_turns = int(tool_parameters.get("memory_turns") or 12)
        system_prompt = tool_parameters.get("system_prompt") or "你是一个xxxx"
        skills_root = _detect_skills_root(tool_parameters.get("skills_root"))

        if not query or not isinstance(query, str):
            yield self.create_text_message("❌缺少 query 参数\n")
            return
        user_input = str(query)
        started_at = time.time()
        max_tool_turns = 50
        effective_query = str(query)

        def _parse_exec_approval_reply(text: str) -> str | None:
            s = str(text or "").strip()
            if not s:
                return None
            s2 = s.replace(" ", "").replace("\t", "").replace("\r", "").replace("\n", "")
            if any(x in s2 for x in ["拒绝", "不允许", "deny", "no"]):
                return "deny"
            if any(x in s2 for x in ["总是允许", "永远允许", "一直允许", "alwaysallow", "always"]):
                return "always"
            if any(x in s2 for x in ["允许本技能", "本技能允许"]):
                return "skill"
            if any(x in s2 for x in ["允许本会话", "本会话允许", "会话内允许", "session"]):
                return "session"
            if any(x in s2 for x in ["允许一次", "允许本次", "once"]):
                return "once"
            if _is_allow_reply(s):
                return "once"
            return None

        def _coerce_allow_entries(v: Any) -> list[dict[str, Any]]:
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

        def _extract_patterns(entries: Any) -> list[str]:
            out: list[str] = []
            for e in _coerce_allow_entries(entries):
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

        def _add_allow_entry(
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
            if scope == "skill" and skill_name:
                bucket = _ensure_path(root, ["allow_skill", str(skill_name)])
            else:
                bucket = _ensure_path(root, ["allow"])
            items = bucket.get(exe0)
            entries = _coerce_allow_entries(items)
            existing = next((e for e in entries if str(e.get("pattern") or "").strip() == pat0), None)
            if existing is None:
                existing = {"pattern": pat0, "created_at": now_ts}
                entries.append(existing)
            existing["last_used_at"] = now_ts
            existing["last_used_command"] = " ".join([str(x) for x in (command or [])])[:500]
            entries = entries[-200:]
            bucket[exe0] = entries
            return store

        def _build_exec_override_from_grants(
            *,
            tool_name: str,
            skill_name: str | None,
            requested_command: list[str],
            exe0: str,
        ) -> dict[str, Any] | None:
            if not exe0:
                return None
            patterns: list[str] = []
            for src, scope in [(session_grants, "session"), (grants, "app")]:
                if not isinstance(src, dict):
                    continue
                exec_cfg = src.get("exec") if isinstance(src.get("exec"), dict) else {}
                allow = exec_cfg.get("allow")
                patterns.extend(_extract_patterns(allow.get(exe0) if isinstance(allow, dict) else None))
                if tool_name == "run_skill_command" and skill_name:
                    allow_skill = exec_cfg.get("allow_skill")
                    if isinstance(allow_skill, dict) and isinstance(allow_skill.get(str(skill_name)), dict):
                        patterns.extend(_extract_patterns(allow_skill.get(str(skill_name), {}).get(exe0)))
            dedup: list[str] = []
            for p in patterns:
                if p and p not in dedup:
                    dedup.append(p)
            if not dedup:
                return None
            return {
                "exe": exe0,
                "allow_not_in_allowlist": True,
                "allow_untrusted_path": True,
                "path_allowlist": dedup,
            }

        def invoke_llm_text_simple(prompt_messages: list[Any]) -> str:
            try:
                try:
                    resp = self.session.model.llm.invoke(
                        model_config=model,
                        prompt_messages=prompt_messages,
                        stream=False,
                    )
                except TypeError:
                    resp = self.session.model.llm.invoke(
                        model_config=model,
                        prompt_messages=prompt_messages,
                    )
            except Exception:
                return ""

            if _safe_get(resp, "message") is not None:
                msg = _safe_get(resp, "message") or {}
                content = _safe_get(msg, "content")
                text, _parts = _split_message_content(content)
                return str(text or "").strip()

            if isinstance(resp, str):
                return resp.strip()

            chunks: list[str] = []
            try:
                for chunk in resp:
                    delta = _safe_get(chunk, "delta") or {}
                    msg = _safe_get(delta, "message") or {}
                    content = _safe_get(msg, "content")
                    t, _parts = _split_message_content(content)
                    if t:
                        chunks.append(str(t))
            except Exception:
                pass
            return "".join(chunks).strip()

        storage = self.session.storage
        history_key = _get_history_storage_key(self.session)
        session_dir_key = _get_session_dir_storage_key(self.session)
        approval_pending_key = _get_conversation_approval_storage_key(self.session, "pending")
        approval_grants_key = _get_approval_storage_key(self.session, "grants")
        approval_session_grants_key = _get_conversation_approval_storage_key(self.session, "exec_grants")
        approval_state = _storage_get_json(storage, approval_pending_key)
        grants = _storage_get_json(storage, approval_grants_key)
        session_grants = _storage_get_json(storage, approval_session_grants_key)
        approval_kind = str(approval_state.get("kind") or "").strip()
        approval_pending = bool(approval_state.get("pending"))
        approval_just_granted = False
        install_granted = bool(grants.get("install") is True)

        plugin_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        temp_root = os.path.join(plugin_root, "temp")
        os.makedirs(temp_root, exist_ok=True)
        persisted_session_dir = _storage_get_text(storage, session_dir_key).strip()
        if persisted_session_dir and os.path.isdir(persisted_session_dir):
            session_dir = persisted_session_dir
        else:
            session_dir = os.path.join(temp_root, f"dify-skill-{uuid.uuid4().hex[:8]}-")
        approval_context = ""
        exec_once_allowed: dict[str, Any] | None = None

        if approval_pending:
            if approval_kind == "install":
                if _is_deny_reply(user_input):
                    _storage_set_json(storage, approval_pending_key, None)
                    yield self.create_text_message("🤝已收到你的拒绝，本次不会执行需要审批的操作。\n")
                    return
                if _is_allow_reply(user_input):
                    next_grants = dict(grants)
                    next_grants["install"] = True
                    _storage_set_json(storage, approval_grants_key, next_grants)
                    _storage_set_json(storage, approval_pending_key, None)
                    approval_just_granted = True
                    install_granted = True
                    original_query = str(approval_state.get("original_query") or "").strip()
                    if original_query:
                        effective_query = original_query + "\n\n[用户已审批]\n允许执行需要审批的操作。"
                    approval_context = "\n\n[审批结果]\n用户已同意执行上一轮需要审批的操作，请继续推进原任务。\n"
                else:
                    prompt = "需要你确认后才能继续执行相关操作。请回复“允许”或“拒绝”。"
                    yield self.create_text_message(prompt)
                    return
            else:
                decision = _parse_exec_approval_reply(user_input)
                if decision == "deny":
                    _storage_set_json(storage, approval_pending_key, None)
                    yield self.create_text_message("🤝已收到你的拒绝，本次不会执行需要审批的操作。\n")
                    return
                if decision in {"once", "session", "skill", "always"}:
                    next_grants = dict(grants)
                    next_session_grants = dict(session_grants)
                    exe0 = str(approval_state.get("exe") or "").strip()
                    skill0 = str(approval_state.get("skill_name") or "").strip() or None
                    resolved_path = str(approval_state.get("resolved_exe") or approval_state.get("path") or "").strip()
                    cmd = approval_state.get("command") if isinstance(approval_state.get("command"), list) else []

                    if decision == "always":
                        _add_allow_entry(
                            store=next_grants,
                            scope="always",
                            exe=exe0,
                            pattern=resolved_path,
                            skill_name=None,
                            command=cmd,
                        )
                        _storage_set_json(storage, approval_grants_key, next_grants)
                    elif decision == "skill":
                        if skill0:
                            _add_allow_entry(
                                store=next_grants,
                                scope="skill",
                                exe=exe0,
                                pattern=resolved_path,
                                skill_name=skill0,
                                command=cmd,
                            )
                            _storage_set_json(storage, approval_grants_key, next_grants)
                        else:
                            decision = "session"
                            _add_allow_entry(
                                store=next_session_grants,
                                scope="session",
                                exe=exe0,
                                pattern=resolved_path,
                                skill_name=None,
                                command=cmd,
                            )
                            _storage_set_json(storage, approval_session_grants_key, next_session_grants)
                    elif decision == "session":
                        _add_allow_entry(
                            store=next_session_grants,
                            scope="session",
                            exe=exe0,
                            pattern=resolved_path,
                            skill_name=None,
                            command=cmd,
                        )
                        _storage_set_json(storage, approval_session_grants_key, next_session_grants)

                    _storage_set_json(storage, approval_pending_key, None)
                    approval_just_granted = True
                    original_query = str(approval_state.get("original_query") or "").strip()
                    if original_query:
                        effective_query = original_query + "\n\n[用户已审批]\n允许执行需要审批的操作。"
                    exec_once_allowed = approval_state if isinstance(approval_state, dict) else None

                    decision_label = {
                        "once": "允许一次",
                        "session": "允许本会话",
                        "skill": "允许本技能",
                        "always": "总是允许",
                    }.get(decision, "允许")
                    approval_context = (
                        "\n\n[审批结果]\n"
                        + f"用户选择：{decision_label}。\n"
                        + "请继续推进原任务并重试上一轮被拦截的命令。\n"
                        + "命令："
                        + json.dumps(cmd or [], ensure_ascii=False)
                        + ("\n可执行文件：" + resolved_path if resolved_path else "")
                        + "\n"
                    )
                else:
                    prompt = (
                        "需要你确认后才能继续执行相关命令。请回复以下任意一种：\n"
                        "- “允许一次”\n"
                        "- “允许本会话”\n"
                        "- “允许本技能”\n"
                        "- “总是允许”\n"
                        "- “拒绝”\n"
                    )
                    yield self.create_text_message(prompt)
                    return

        onboarding_key = _get_persona_storage_key(self.session, "onboarding")
        identity_key = _get_persona_storage_key(self.session, "IDENTITY.md")
        user_key = _get_persona_storage_key(self.session, "USER.md")
        soul_key = _get_persona_storage_key(self.session, "SOUL.md")
        memory_key = _get_memory_storage_key(self.session, "MEMORY.md")
        identity_md = _storage_get_text(storage, identity_key).strip()
        onboarding_state = _storage_get_json(storage, onboarding_key)
        onboarding_completed = bool(onboarding_state.get("completed")) and bool(identity_md)

        reset_words = ["重置身份", "重置设定", "重置角色", "重新初始化", "重新认识", "重做初始化", "换个设定", "改角色", "改设定"]
        if any(w in str(user_input or "") for w in reset_words):
            _storage_set_json(storage, onboarding_key, {"stage": 0, "completed": False, "reset_at": int(time.time())})
            _storage_set_text(storage, identity_key, "")
            _storage_set_text(storage, soul_key, "")
            _storage_set_text(storage, user_key, "")
            _storage_set_text(storage, memory_key, "")
            try:
                now_ts = time.time()
                for days_ago in range(0, 30):
                    day = time.strftime("%Y-%m-%d", time.localtime(now_ts - (days_ago * 86400)))
                    daily_key = _get_memory_storage_key(self.session, f"memory/{day}.md")
                    _storage_set_text(storage, daily_key, "")
            except Exception:
                pass
            yield self.create_text_message("🦞角色重置成功：已清除身份设定与记忆信息，你可通过发送消息再次建立你的专属AI助手。\n")
            return

        if not onboarding_completed:
            stage = int(onboarding_state.get("stage") or 0)
            if stage <= 0:
                _storage_set_json(
                    storage,
                    onboarding_key,
                    {
                        "stage": 1,
                        "completed": False,
                        "created_at": int(time.time()),
                    },
                )
                yield self.create_text_message(
                    "你好哇😘，我是你的 AI 助手，基于Dify的Mini Claw。\n"
                    "我们先来认识一下吧。\n\n"
                    "请用一段话回答下面问题（按你喜欢的方式写即可）：\n"
                    "1) 你希望我叫什么名字？\n"
                    "2) 我应该怎么称呼你？\n"
                    "3) 你希望我是什么设定/生物？（例如：AI 助手/小动物/程序精灵/赛博管家…）\n"
                    "4) 你希望我的风格是什么？（例如：克制直接/温暖幽默/严谨专业…）\n"
                    "5) 给我设定一个签名 Emoji（可选）\n"
                    "\n"
                    "回答完这一次后，我会把这些信息写入我的身份设定，并从下一轮开始按这个风格交流。"
                )
                return

            def _guess_first_emoji(text: str) -> str:
                s = str(text or "")
                if not s:
                    return ""
                m = re.search(
                    r"(?:签名|emoji|表情|标签|tag)\s*(?:是|为|=|:|：)?\s*([^\s])",
                    s,
                    flags=re.I,
                )
                if m:
                    return str(m.group(1) or "").strip()
                for ch in s:
                    if ord(ch) >= 0x1F300:
                        return ch
                return ""

            def _heuristic_extract(text: str) -> dict[str, dict[str, str]]:
                s = str(text or "")
                user_name = ""
                user_addressing = ""
                agent_name = ""
                agent_creature = ""
                agent_vibe = ""
                agent_emoji = _guess_first_emoji(s)

                m_user_addressing = re.search(r"(?:以后叫我|你可以叫我|请叫我|称呼我|叫我)\s*([^\s，。；;!\n]{1,20})", s)
                if m_user_addressing:
                    user_addressing = str(m_user_addressing.group(1) or "").strip()

                m_user_name = re.search(r"(?:我叫|我是)\s*([^\s，。；;!\n]{1,20})", s)
                if m_user_name:
                    user_name = str(m_user_name.group(1) or "").strip()

                m_agent = re.search(r"(?:你是|你叫)\s*([^\s，。；;!\n]{1,20})", s)
                if m_agent:
                    agent_name = str(m_agent.group(1) or "").strip()

                if "小动物" in s:
                    agent_creature = "小动物"
                elif "程序精灵" in s:
                    agent_creature = "程序精灵"
                elif "赛博管家" in s:
                    agent_creature = "赛博管家"
                elif "小孩" in s or "孩子" in s:
                    agent_creature = "小孩"
                elif "AI" in s and ("助手" in s or "助理" in s):
                    agent_creature = "AI 助手"

                vibe_hits: list[str] = []
                for w in ["温柔", "幽默", "小调皮", "克制", "直接", "严谨", "专业", "靠谱", "可爱", "天真", "天真无邪", "好奇", "童真"]:
                    if w in s:
                        vibe_hits.append(w)
                if vibe_hits:
                    agent_vibe = "、".join(dict.fromkeys(vibe_hits))

                return {
                    "user": {"name": user_name, "addressing": user_addressing},
                    "agent": {"name": agent_name, "creature": agent_creature, "vibe": agent_vibe, "emoji": agent_emoji},
                }

            extractor_system = SystemPromptMessage(
                content=(
                    "你是信息提取器。请从用户回答中提取身份与偏好，输出严格 JSON。\n"
                    "输出必须是 JSON 对象本体：不要 Markdown、不要代码块、不要解释、不要多余字符。\n"
                    "必须包含所有字段；缺失时用空字符串。\n"
                    "\n"
                    "JSON schema:\n"
                    "{\n"
                    '  "user": {"name": string, "addressing": string},\n'
                    '  "agent": {"name": string, "creature": string, "vibe": string, "emoji": string}\n'
                    "}\n"
                    "\n"
                    "字段映射规则：\n"
                    "- user.name：用户的姓名/名字（例如“我叫张三/我是张三”）。\n"
                    "- user.addressing：用户希望你怎么称呼TA（例如“叫我X/以后叫我X/称呼我X”）。\n"
                    "- agent.name：用户希望你叫什么名字（例如“你叫X/你是X”）。\n"
                    "- agent.creature：设定/生物/身份（例如“AI 助手/赛博管家/程序精灵/小孩/孩子”）。\n"
                    "- agent.vibe：性格/语气/风格/人设，用原话短句归纳（例如“严谨克制”“像孩子一样天真无邪、对一切充满好奇”）。\n"
                    "- agent.emoji：签名/表情/标签/emoji（优先取 1 个字符，如“❓/🤖/😘”；多个时取第一个）。\n"
                    "\n"
                    "示例（仅示例，不要照抄）：\n"
                    '输入："你叫小元，我是你哥哥，你要像孩子一样天真无邪，对一切充满好奇，你的标签是❓"\n'
                    '输出：{"user":{"name":"","addressing":"哥哥"},"agent":{"name":"小元","creature":"小孩","vibe":"像孩子一样天真无邪、对一切充满好奇","emoji":"❓"}}\n'
                    '输入："叫我老板；你叫小元；你是赛博管家；语气严谨克制；emoji=🤖"\n'
                    '输出：{"user":{"name":"","addressing":"老板"},"agent":{"name":"小元","creature":"赛博管家","vibe":"严谨克制","emoji":"🤖"}}\n'
                )
            )
            extractor_user = UserPromptMessage(content=user_input)
            extracted = invoke_llm_text_simple([extractor_system, extractor_user])
            json_text = ""
            if extracted:
                a = extracted.find("{")
                b = extracted.rfind("}")
                if a != -1 and b != -1 and b > a:
                    json_text = extracted[a : b + 1]
            parsed: dict[str, Any] = {}
            if json_text:
                try:
                    obj = json.loads(json_text)
                    parsed = obj if isinstance(obj, dict) else {}
                except Exception:
                    parsed = {}

            if not parsed:
                parsed = _heuristic_extract(user_input)

            user_obj = parsed.get("user") if isinstance(parsed.get("user"), dict) else {}
            agent_obj = parsed.get("agent") if isinstance(parsed.get("agent"), dict) else {}
            user_name = str(user_obj.get("name") or "").strip()
            user_addressing = str(user_obj.get("addressing") or "").strip()
            agent_name = str(agent_obj.get("name") or "").strip()
            agent_creature = str(agent_obj.get("creature") or "").strip()
            agent_vibe = str(agent_obj.get("vibe") or "").strip()
            agent_emoji = str(agent_obj.get("emoji") or "").strip()

            if not agent_name:
                agent_name = "小元"
            if not agent_creature:
                agent_creature = "可爱的 AI 助手"
            if not agent_vibe:
                agent_vibe = "温柔、幽默、靠谱（偶尔有点小调皮）"
            if not agent_emoji:
                agent_emoji = "🤖"

            identity_md_next = (
                "# IDENTITY.md - Who Am I?\n\n"
                f"- **Name:** {agent_name}\n"
                f"- **Creature:** {agent_creature}\n"
                f"- **Vibe:** {agent_vibe}\n"
                f"- **Emoji:** {agent_emoji}\n"
            )
            user_md_next = (
                "# USER.md - Who Are You?\n\n"
                f"- **Name:** {user_name}\n"
                f"- **Addressing:** {user_addressing}\n"
            )
            soul_md_next = (
                "# SOUL.md - Who You Are\n\n"
                "## Core\n"
                "- 说人话，少模板；可以小调皮，但不油腻。\n"
                "- 语气不要刻意迎合，但要给与用户足够的尊重。\n"
                "- 有主见，但不自作主张，遇到不确定的事情时，会向用户询问。\n"
                "- 幽默是你的底色，善良是你的天性，你会主动关心用户。\n"
                "## Vibe\n"
                f"{agent_vibe}\n"
            )

            _storage_set_text(storage, identity_key, identity_md_next)
            _storage_set_text(storage, user_key, user_md_next)
            _storage_set_text(storage, soul_key, soul_md_next)
            if not _storage_get_text(storage, memory_key).strip():
                _storage_set_text(storage, memory_key, "# MEMORY.md - Long-term Memory\n\n")
            _storage_set_json(
                storage,
                onboarding_key,
                {"stage": 2, "completed": True, "completed_at": int(time.time())},
            )
            yield self.create_text_message(
                f"✅身份设定已完成：我叫 {agent_name}，风格是「{agent_vibe}」。\n"
                "后续我会基于这个身份来和你交流并持续记忆\n"
                "高阶策略：可通过update_persona工具调整身份，设置灵魂（SOUL.md）"
            )
            return
        os.makedirs(session_dir, exist_ok=True)
        _storage_set_text(storage, session_dir_key, session_dir)
        _cleanup_old_temp_sessions(temp_root, keep=4, protect_dirs={session_dir})

        file_items: list[Any] = []
        files_param = tool_parameters.get("files")
        if isinstance(files_param, list):
            file_items = [x for x in files_param if x]
        elif files_param:
            file_items = [files_param]
        elif tool_parameters.get("file"):
            file_items = [tool_parameters.get("file")]

        uploads_context = ""
        if file_items:
            uploads_dir = _safe_join(session_dir, "uploads")
            os.makedirs(uploads_dir, exist_ok=True)
            uploaded: list[dict[str, Any]] = []
            for item in file_items:
                url, name = _extract_url_and_name(item)
                if not url:
                    yield self.create_text_message("❌未能获取上传文件 URL（files[i].url）。\n")
                    return
                try:
                    content = _download_file_content(str(url), timeout=45)
                except Exception as e:
                    yield self.create_text_message(f"❌文件下载失败：{str(e)}\n")
                    return
                ext = _infer_ext_from_url(str(url))
                filename = _safe_filename(str(name) if name else None, fallback_ext=ext)
                abs_path = os.path.join(uploads_dir, filename)
                try:
                    with open(abs_path, "wb") as f:
                        f.write(content)
                except Exception as e:
                    yield self.create_text_message(f"❌保存上传文件失败：{str(e)}\n")
                    return

                rel_path = f"uploads/{filename}"
                mime = None
                if isinstance(item, dict) and item.get("mime_type"):
                    mime = str(item.get("mime_type") or "").strip() or None
                if not mime:
                    try:
                        mime = _guess_mime_type(filename)
                    except Exception:
                        mime = None
                uploaded.append(
                    {
                        "relative_path": rel_path,
                        "bytes": len(content),
                        "mime_type": mime or "",
                        "filename": filename,
                        "source_url": str(url),
                    }
                )

            lines = ["\n\n[上传文件清单]", "以下路径均相对于本次会话的 session_dir："]
            for f in uploaded:
                lines.append(
                    f"- {f.get('relative_path')} | mime={f.get('mime_type') or ''} | bytes={f.get('bytes') or 0} | filename={f.get('filename') or ''}"
                )
            uploads_context = "\n".join(lines) + "\n"
        else:
            uploads_dir = _safe_join(session_dir, "uploads")
            os.makedirs(uploads_dir, exist_ok=True)

        uploads_context = _build_uploads_context(session_dir)

        runtime = _AgentRuntime(
            skills_root=skills_root,
            session_dir=session_dir,
            memory_turns=memory_turns,
            skills_snapshot_cache_path=os.path.join(temp_root, "skills_snapshot.json"),
        )

        skills_snapshot = runtime.load_skills_snapshot()
        skills_index = runtime.load_skills_index()
        try:
            skills_count = len(skills_index.get("skills") or []) if isinstance(skills_index, dict) else 0
        except Exception:
            skills_count = 0
        _dbg(
            "start "
            + _model_brief(model)
            + f" session_dir={session_dir} skills_root={skills_root!s} skills_count={skills_count} "
            + f"query_len={len(query)}"
        )
        def _xml_escape(text: Any) -> str:
            s = str(text or "")
            return (
                s.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;")
                .replace("'", "&apos;")
            )

        def _build_skills_xml(snapshot: dict[str, Any]) -> str:
            root = str(snapshot.get("root") or "").strip()
            platform_name = str(snapshot.get("platform") or "").strip()
            skills = snapshot.get("skills") if isinstance(snapshot.get("skills"), list) else []
            lines: list[str] = [f'<available_skills root="{_xml_escape(root)}" platform="{_xml_escape(platform_name)}">']
            for s in skills:
                if not isinstance(s, dict):
                    continue
                status = s.get("status") if isinstance(s.get("status"), dict) else {}
                skill_id = str(s.get("folder") or s.get("id") or "").strip()
                base_dir_abs = os.path.join(root, skill_id) if root and skill_id else ""
                skill_md_abs = os.path.join(base_dir_abs, "SKILL.md") if base_dir_abs else ""
                missing = status.get("missing") if isinstance(status, dict) else {}
                miss_bins = ",".join(missing.get("bins") or []) if isinstance(missing, dict) else ""
                miss_any = ",".join(missing.get("anyBins") or []) if isinstance(missing, dict) else ""
                miss_env = ",".join(missing.get("env") or []) if isinstance(missing, dict) else ""
                miss_py = ",".join(missing.get("py") or []) if isinstance(missing, dict) else ""
                miss_js = ",".join(missing.get("js") or []) if isinstance(missing, dict) else ""
                eligible = str(bool(status.get("eligible")) if isinstance(status, dict) else False).lower()
                visible = str(bool(status.get("visible")) if isinstance(status, dict) else False).lower()
                os_ok = str(bool(status.get("os_ok")) if isinstance(status, dict) else True).lower()
                openclaw = s.get("openclaw") if isinstance(s.get("openclaw"), dict) else {}
                os_allow = ",".join(openclaw.get("os") or []) if isinstance(openclaw, dict) else ""

                reason_parts: list[str] = []
                if os_ok == "false":
                    reason_parts.append(f"os_not_supported allow={os_allow or 'unspecified'}")
                if miss_bins:
                    reason_parts.append(f"missing_bins={miss_bins} (install_in=plugin_daemon)")
                if miss_any:
                    reason_parts.append(f"missing_any_bins={miss_any} (install_in=plugin_daemon)")
                if miss_env:
                    reason_parts.append(f"missing_env={miss_env} (configure_env)")
                if miss_py:
                    reason_parts.append(f"missing_py={miss_py} (run_TM_dependency_install)")
                if miss_js:
                    reason_parts.append(f"missing_js={miss_js} (run_TM_dependency_install)")
                not_eligible_reason = "; ".join(reason_parts)
                lines.extend(
                    [
                        "  <skill>",
                        f"    <name>{_xml_escape(skill_id)}</name>",
                        f"    <display_name>{_xml_escape(s.get('name') or '')}</display_name>",
                        f"    <description>{_xml_escape(s.get('description') or '')}</description>",
                        f"    <location>{_xml_escape(skill_md_abs)}</location>",
                        f"    <base_dir>{_xml_escape(base_dir_abs)}</base_dir>",
                        f"    <visible>{_xml_escape(visible)}</visible>",
                        f"    <eligible>{_xml_escape(eligible)}</eligible>",
                        f"    <os_ok>{_xml_escape(os_ok)}</os_ok>",
                        f"    <os_allow>{_xml_escape(os_allow)}</os_allow>",
                        f"    <missing_bins>{_xml_escape(miss_bins)}</missing_bins>",
                        f"    <missing_any_bins>{_xml_escape(miss_any)}</missing_any_bins>",
                        f"    <missing_env>{_xml_escape(miss_env)}</missing_env>",
                        f"    <missing_py>{_xml_escape(miss_py)}</missing_py>",
                        f"    <missing_js>{_xml_escape(miss_js)}</missing_js>",
                        f"    <not_eligible_reason>{_xml_escape(not_eligible_reason)}</not_eligible_reason>",
                        "  </skill>",
                    ]
                )
            lines.append("</available_skills>")
            return "\n".join(lines)

        skills_xml = _build_skills_xml(skills_snapshot if isinstance(skills_snapshot, dict) else {})

        system_lines: list[str] = []
        if system_prompt.strip():
            system_lines.append(system_prompt.strip())
            system_lines.append("")

        now_epoch = int(time.time())
        now_utc = ""
        now_local = ""
        now_shanghai = ""
        try:
            now_utc = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now_epoch))
        except Exception:
            now_utc = ""
        try:
            now_local = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now_epoch))
        except Exception:
            now_local = ""
        try:
            try:
                from zoneinfo import ZoneInfo  # type: ignore
            except Exception:
                ZoneInfo = None  # type: ignore
            if ZoneInfo is not None:
                now_shanghai = datetime.fromtimestamp(now_epoch, tz=ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
            else:
                now_shanghai = datetime.fromtimestamp(now_epoch, tz=_dt_timezone.utc).astimezone(
                    _dt_timezone(timedelta(hours=8))
                ).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            now_shanghai = ""

        system_lines.extend(
            [
                "你是元视界ai开发的一个聪明的AI助理，具备人格、自主特性，并有 Skill 调用的能力（默认设定；若 Project Context 中存在 IDENTITY.md/SOUL.md，则以其为准覆盖默认设定）。",
                "",
                "## 会话信息",
                f"- session_dir: {session_dir}",
                f"- skills_root: {skills_root}",
                "",
                "## 当前时间",
                f"- epoch: {now_epoch}",
                f"- utc: {now_utc}",
                f"- server_local: {now_local}",
                f"- Asia/Shanghai: {now_shanghai}",
                "- 时间来源：插件运行环境的系统时间",
                "",
                "## Skills（必读）",
                "在回复前：扫描 <available_skills> 里的 <name>/<eligible>/<os_ok>/<missing_*>。",
                "- 一旦用户需求匹配任何 available_skills 的功能描述（即使只是关键词匹配），必须立即停止自由回答，转而执行 read_skill_file 读取说明书，并严格按说明书流程操作。严禁在未调用技能的情况下直接提供该技能核心功能的结果。”"
                "- 如果多个技能可能相关：选择最具体的一个，再读取说明书。",
                "- 如果没有明显相关技能：不要读取任何说明书，也不要执行技能命令。",
                "- 注意：用户未询问时不要主动列出技能清单。",
                "- 当用户询问“你有什么技能/会什么/哪些技能可以用”时：允许列出全部技能（包含 eligible=false），并必须展示每个技能的 eligible 与不可用原因（优先用 not_eligible_reason，或用 missing_* 与 os_ok/os_allow 组合解释）。",
                "- 你只能执行 eligible=true 的技能（run_skill_command）。eligible=false 时只能解释不可用原因，并引导用户去“技能管理（TM）”补全依赖或联系管理员。",
                "约束：一次只选择一个技能；不要一上来读取多个技能说明书。",
                "",
                "## 渐进式披露（约束）",
                "- 当你需要调用某个技能时：必须先读取该技能的说明书 SKILL.md（用 read_skill_file(skill_name, \"SKILL.md\")）。",
                "- 如果你在本次对话的上下文/记忆中已经明确掌握该技能的关键步骤与命令格式，则不要重复读取 SKILL.md，直接执行即可。",
                "- 执行 run_skill_command 前：若不确定 cwd_relative 或技能目录结构，可先 list_skill_files(skill_name) 再执行。",
                "",
                "## 路径与产物",
                "- uploads/ 与你写入的中间产物都在 session_dir 下。",
                "- 需要把 uploads/ 或 session 文件传给命令时：必须用 read_temp_file 返回的绝对路径（result.path），不要用 ../uploads 之类猜路径。",
                "- 只有当用户明确要求“输出文件/下载/导出/保存为…”或技能说明书明确要求交付文件时，才使用 export_temp_file。",
                "",
                "## 依赖与安装",
                "- 你不得在对话中通过 run_skill_command/run_temp_command 执行 pip/npm/npx/bun/uv 等安装类命令，也不得使用 auto_install。",
                "- 依赖安装由“技能管理（TM）”侧统一处理：用户可通过“依赖安装”补全 Python 依赖；JavaScript/系统级依赖请联系管理员在 plugin_daemon 容器安装。",
                "- 如果你已读取某技能的 SKILL.md，且说明书明确要求额外初始化步骤（例如：xxx install / install --with-deps / download browser 等）：不要假设已完成，也不要继续执行技能命令。请明确告诉用户需要在 plugin_daemon 容器执行哪些命令，并要求用户回复“已完成”后再继续。",
                "",
                "## 交互规则",
                "- 需要追问用户时：本轮只输出问题与选项，并立刻结束；不得在同一轮继续读文件/执行命令/生成产物。",
                "- 禁止用“请用户回复允许/确认后再执行”来人为拆分流程。需要审批时由系统拦截并提示用户选项。",
                "- 当用户表达“以后怎么称呼/签名/语气风格/灵魂核心规则”等偏好设定变化时：请调用 update_persona 更新并持久化；其中“称呼方式”更新到 user.addressing，“姓名/名字”更新到 user.name；“SOUL.md Core”用 soul.core_rules 或 soul.core_text 改写；必要时可先 get_persona 查看当前设定。",
                "",
                "## 执行审批",
                "- 正常情况下：直接调用 run_skill_command/run_temp_command 执行步骤，不要让用户先“允许”。",
                "- 只有当系统输出“需要你确认后才能继续”时，才停止并等待用户回复其选项。",
                "",
                "## 写文件约束",
                "准备调用 write_temp_file 前，先输出一行“写入意图确认”：relative_path + 内容摘要(80字) + 大致长度；然后再发起工具调用。",
                "- export_temp_file 只能用于用户明确要求的交付文件；禁止导出 IDENTITY.md / USER.md / SOUL.md / MEMORY.md / memory/*.md。",
                "",
                "## 可用工具",
                "get_session_context, get_system_status, get_current_time, get_persona, update_persona, list_skill_files, read_skill_file, run_skill_command, write_temp_file, read_temp_file, list_temp_files, glob_temp_files, grep_temp_files, edit_temp_file, delete_temp_path, run_temp_command, export_temp_file, web_fetch",
                "",
                "",
                "## Skills 清单（XML）",
                skills_xml,
            ]
        )
        if uploads_context:
            system_lines.insert(system_lines.index("## 可用工具"), uploads_context.strip() + "\n")
        if approval_context:
            system_lines.append(approval_context)

        soul_md = _storage_get_text(storage, soul_key).strip()
        user_md = _storage_get_text(storage, user_key).strip()
        identity_md = _storage_get_text(storage, identity_key).strip()
        memory_md = _storage_get_text(storage, memory_key).strip()
        today = time.strftime("%Y-%m-%d", time.localtime())
        yesterday = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
        daily_today_key = _get_memory_storage_key(self.session, f"memory/{today}.md")
        daily_yesterday_key = _get_memory_storage_key(self.session, f"memory/{yesterday}.md")
        daily_today_md = _storage_get_text(storage, daily_today_key).strip()
        daily_yesterday_md = _storage_get_text(storage, daily_yesterday_key).strip()
        context_blocks: list[tuple[str, str]] = []
        if soul_md:
            context_blocks.append(("SOUL.md", soul_md))
        if identity_md:
            context_blocks.append(("IDENTITY.md", identity_md))
        if user_md:
            context_blocks.append(("USER.md", user_md))
        if memory_md:
            context_blocks.append(("MEMORY.md", memory_md))
        if daily_yesterday_md:
            context_blocks.append((f"memory/{yesterday}.md", daily_yesterday_md))
        if daily_today_md:
            context_blocks.append((f"memory/{today}.md", daily_today_md))

        if context_blocks:
            insert_at = len(system_lines)
            try:
                insert_at = system_lines.index("## Skills 清单（XML）")
            except Exception:
                insert_at = len(system_lines)
            ctx_lines: list[str] = [
                "# Project Context",
                "以下文件由系统自动注入：它们定义你的身份、灵魂、用户画像与长期记忆。",
                "优先级：当 IDENTITY.md/SOUL.md 与其它提示（包括 system_prompt/默认设定）冲突时，以 IDENTITY.md/SOUL.md 为准。",
            ]
            if soul_md:
                ctx_lines.append("如果 SOUL.md 存在：请体现其中的人格与语气，避免僵硬泛化回答。")
            ctx_lines.append("")
            for p, c in context_blocks:
                ctx_lines.extend([f"## {p}", "", c.strip(), ""])
            system_lines[insert_at:insert_at] = ctx_lines

        system_content = "\n".join([x for x in system_lines if x is not None])

        messages: list[Any] = [SystemPromptMessage(content=system_content)]
        messages.append(UserPromptMessage(content=effective_query))

        conversation_summary: str = ""

        def estimate_tokens(text: Any) -> int:
            s = str(text or "")
            return max(1, (len(s) // 4) + 1)

        def message_to_text(msg: Any) -> str:
            content = getattr(msg, "content", None)
            if content is None:
                content = _safe_get(msg, "content")
            if isinstance(content, list):
                acc: list[str] = []
                for item in content:
                    if isinstance(item, dict):
                        t = item.get("text") or item.get("content") or ""
                        if t:
                            acc.append(str(t))
                    elif item:
                        acc.append(str(item))
                return "\n".join(acc).strip()
            if isinstance(content, dict):
                return json.dumps(content, ensure_ascii=False)
            return str(content or "").strip()

        def estimate_prompt_tokens(msgs: list[Any]) -> int:
            total = 0
            for m in msgs:
                total += estimate_tokens(message_to_text(m))
            return total

        def invoke_llm_text(prompt_messages: list[Any]) -> str:
            try:
                try:
                    resp = self.session.model.llm.invoke(
                        model_config=model,
                        prompt_messages=prompt_messages,
                        stream=False,
                    )
                except TypeError:
                    resp = self.session.model.llm.invoke(
                        model_config=model,
                        prompt_messages=prompt_messages,
                    )
            except Exception:
                return ""

            if _safe_get(resp, "message") is not None:
                msg = _safe_get(resp, "message") or {}
                content = _safe_get(msg, "content")
                text, _parts = _split_message_content(content)
                return str(text or "").strip()

            if isinstance(resp, str):
                return resp.strip()

            chunks: list[str] = []
            try:
                for chunk in resp:
                    delta = _safe_get(chunk, "delta") or {}
                    msg = _safe_get(delta, "message") or {}
                    content = _safe_get(msg, "content")
                    t, _parts = _split_message_content(content)
                    if t:
                        chunks.append(str(t))
            except Exception:
                pass
            return "".join(chunks).strip()

        def _memory_extract_updates(*, existing_memory_md: str, text: str) -> dict[str, Any]:
            s = str(text or "").strip()
            if not s:
                return {}
            existing = str(existing_memory_md or "").strip()
            if len(existing) > 4000:
                existing = existing[:4000]
            if len(s) > 12000:
                s = s[-12000:]

            extractor_system = SystemPromptMessage(
                content=(
                    "你是记忆整理器。请从对话内容中提取“应写入长期记忆”的信息，并输出严格 JSON。\n"
                    "只提取稳定且可复用的信息：用户偏好（称呼/风格/禁忌）、项目事实（路径/命令/约束）、关键决定。\n"
                    "不要写流水账；不要写情绪安慰；不要编造。\n"
                    "输出 JSON schema:\n"
                    "{\n"
                    '  "user_preferences": {string: string},\n'
                    '  "project_facts": {string: string},\n'
                    '  "decisions": [string]\n'
                    "}\n"
                    "规则：\n"
                    "- key 尽量短（<= 24 字）；value <= 200 字。\n"
                    "- 没有就输出空对象/空数组。\n"
                    "- 只输出 JSON，不要输出任何其它文本。\n"
                )
            )
            extractor_user = UserPromptMessage(
                content=(
                    ("[已有 MEMORY.md（节选）]\n" + existing + "\n\n") if existing else ""
                    + "[待提炼的对话]\n"
                    + s
                )
            )
            out = invoke_llm_text([extractor_system, extractor_user]).strip()
            if not out:
                return {}
            a = out.find("{")
            b = out.rfind("}")
            if a == -1 or b == -1 or b <= a:
                return {}
            j = out[a : b + 1]
            try:
                obj = json.loads(j)
                return obj if isinstance(obj, dict) else {}
            except Exception:
                return {}

        def _memory_merge_managed_block(existing_memory_md: str, updates: dict[str, Any]) -> str:
            existing = str(existing_memory_md or "").strip()
            if not existing:
                existing = "# MEMORY.md - Long-term Memory\n\n"

            def norm_kv(d: Any) -> dict[str, str]:
                if not isinstance(d, dict):
                    return {}
                out: dict[str, str] = {}
                for k, v in d.items():
                    kk = str(k or "").strip()
                    vv = str(v or "").strip()
                    if not kk or not vv:
                        continue
                    if len(kk) > 24:
                        kk = kk[:24]
                    if len(vv) > 200:
                        vv = vv[:200]
                    out[kk] = vv
                return out

            def norm_list(xs: Any) -> list[str]:
                if not isinstance(xs, list):
                    return []
                out: list[str] = []
                for x in xs:
                    s = str(x or "").strip()
                    if not s:
                        continue
                    if len(s) > 240:
                        s = s[:240]
                    if s not in out:
                        out.append(s)
                return out

            user_prefs = norm_kv(updates.get("user_preferences"))
            project_facts = norm_kv(updates.get("project_facts"))
            decisions = norm_list(updates.get("decisions"))

            marker = "## Managed Memory (auto)"
            lines = existing.splitlines()
            start = -1
            for i, line in enumerate(lines):
                if line.strip() == marker:
                    start = i
                    break
            if start != -1:
                end = len(lines)
                for j in range(start + 1, len(lines)):
                    if lines[j].startswith("## ") and lines[j].strip() != marker:
                        end = j
                        break
                preserved = "\n".join(lines[:start]).rstrip() + "\n\n" + "\n".join(lines[end:]).lstrip()
                existing = preserved.strip() + "\n"

            def render_section(title: str, kv: dict[str, str]) -> list[str]:
                if not kv:
                    return [f"### {title}", "- (empty)"]
                out = [f"### {title}"]
                for k in sorted(kv.keys()):
                    out.append(f"- **{k}:** {kv[k]}")
                return out

            def render_list(title: str, items: list[str]) -> list[str]:
                if not items:
                    return [f"### {title}", "- (empty)"]
                out = [f"### {title}"]
                for it in items:
                    out.append(f"- {it}")
                return out

            managed: list[str] = [marker]
            managed.append(f"- updated_at: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}")
            managed.append("")
            managed.extend(render_section("User Preferences", user_prefs))
            managed.append("")
            managed.extend(render_section("Project Facts", project_facts))
            managed.append("")
            managed.extend(render_list("Decisions", decisions))
            managed.append("")

            merged = existing.rstrip() + "\n\n" + "\n".join(managed).rstrip() + "\n"
            return merged

        def _memory_flush_for_compaction(*, text: str) -> None:
            existing_md = _storage_get_text(storage, memory_key).strip()
            if not existing_md:
                existing_md = "# MEMORY.md - Long-term Memory\n\n"
            updates = _memory_extract_updates(existing_memory_md=existing_md, text=text)
            if not updates:
                if not _storage_get_text(storage, memory_key).strip():
                    _storage_set_text(storage, memory_key, existing_md)
                return
            merged = _memory_merge_managed_block(existing_md, updates)
            _storage_set_text(storage, memory_key, merged)

        def compact_if_needed() -> None:
            nonlocal conversation_summary
            max_tokens = max(2000, int(compaction_max_prompt_tokens or 0))
            current_tokens = estimate_prompt_tokens(messages)
            if current_tokens <= max_tokens:
                return

            keep_recent_tokens = 2500
            if isinstance(memory_turns, int) and memory_turns > 0:
                keep_recent_tokens = min(6000, max(1200, int(memory_turns) * 250))

            system_msg = messages[0]
            tail: list[Any] = []
            tail_tokens = 0
            for m in reversed(messages[1:]):
                t = estimate_tokens(message_to_text(m))
                if tail and tail_tokens + t > keep_recent_tokens:
                    break
                tail.append(m)
                tail_tokens += t
                if tail_tokens >= keep_recent_tokens:
                    break
            tail.reverse()
            prefix = messages[1 : (len(messages) - len(tail))]
            prefix_text_blocks: list[str] = []
            for m in prefix:
                mt = message_to_text(m)
                if mt:
                    prefix_text_blocks.append(mt)
                if sum(len(x) for x in prefix_text_blocks) > 12000:
                    break
            prefix_text = "\n\n".join(prefix_text_blocks).strip()
            if not prefix_text:
                messages[:] = [system_msg, *tail]
                return

            try:
                _memory_flush_for_compaction(text=prefix_text)
            except Exception:
                pass

            summarizer_system = SystemPromptMessage(
                content=(
                    "你是一个对话压缩器。请把给定的对话内容总结成可用于后续继续工作的摘要。\n"
                    "要求：\n"
                    "- 只保留能影响后续行动的事实：用户目标、关键约束、已完成进度、重要决定、文件名/路径/参数。\n"
                    "- 不要写寒暄，不要复述无关细节。\n"
                    "- 输出不超过 900 字。\n"
                )
            )
            summarizer_user = UserPromptMessage(
                content=(
                    ("[已有摘要]\n" + conversation_summary + "\n\n") if conversation_summary else ""
                )
                + "[需要压缩的历史]\n"
                + prefix_text
            )
            summary = invoke_llm_text([summarizer_system, summarizer_user]).strip()
            if not summary:
                messages[:] = [system_msg, *tail]
                return

            conversation_summary = summary
            summary_msg = SystemPromptMessage(content="## Conversation Summary\n" + summary.strip())
            messages[:] = [system_msg, summary_msg, *tail]

        final_text: str | None = None
        final_file_meta: dict[str, dict[str, str]] = {}
        empty_responses = 0
        saved_asset_fingerprints: set[str] = set()
        final_text_already_streamed = False

        def stream_text_to_user(text: str, chunk_size: int = 8) -> Generator[ToolInvokeMessage]:
            s = (text or "").strip()
            if not s:
                return
            step = max(1, int(chunk_size))
            for i in range(0, len(s), step):
                yield self.create_text_message(s[i : i + step])

        def redact_user_visible_text(text: str) -> str:
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

        def persist_llm_assets(parts: Any) -> list[str]:
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
                    dst = _safe_join(out_dir, f"{base}-{fp[:8] if 'fp' in locals() else uuid.uuid4().hex[:8]}{ext}")
                try:
                    with open(dst, "wb") as f:
                        f.write(raw)
                    saved.append(os.path.relpath(dst, session_dir))
                except Exception:
                    continue
            return saved

        def build_agent_tag_header() -> str:
            default_name = "Mini_Claw"
            default_emoji = "🤖"

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

        agent_tag_header = build_agent_tag_header()

        def invoke_llm_live(
            *, prompt_messages: list[Any], tools: list[Any] | None
        ) -> Generator[ToolInvokeMessage, None, tuple[str, list[Any], Any, int, bool]]:
            nontext_content: list[dict[str, Any]] = []
            tool_calls_all: list[Any] = []
            text_parts: list[str] = []
            chunks_count = 0
            streamed_any = False
            saw_tool_calls = False
            typing_chunk = 6
            emitted_prefix = False
            emitted_len = 0

            def emit_typing(text: str) -> Generator[ToolInvokeMessage, None, None]:
                nonlocal streamed_any
                if not text:
                    return
                tagged = "\n" + agent_tag_header + "\n" + text.strip() + "\n\n"
                step = max(1, int(typing_chunk))
                for i in range(0, len(tagged), step):
                    yield self.create_text_message(tagged[i : i + step])
                    streamed_any = True
            
            def should_emit_user_text(text: str) -> bool:
                if not text:
                    return False
                s = str(text)
                stripped = s.lstrip()
                if stripped.startswith("```") and stripped.count("```") < 2:
                    return False
                return True

            try:
                try:
                    response = self.session.model.llm.invoke(
                        model_config=model,
                        prompt_messages=prompt_messages,
                        tools=tools,
                        stream=True,
                    )
                except TypeError:
                    response = self.session.model.llm.invoke(
                        model_config=model,
                        prompt_messages=prompt_messages,
                        stream=True,
                    )

                if _safe_get(response, "message") is not None:
                    msg = _safe_get(response, "message") or {}
                    content = _safe_get(msg, "content")
                    text, parts = _split_message_content(content)
                    if parts:
                        nontext_content.extend(parts)
                    tool_calls = _safe_get(msg, "tool_calls") or []
                    if isinstance(tool_calls, list):
                        tool_calls_all.extend(tool_calls)
                        if tool_calls:
                            saw_tool_calls = True
                    if text:
                        text_parts.append(text)
                    combined_text = "".join(text_parts).strip()
                    if combined_text and not saw_tool_calls and should_emit_user_text(combined_text):
                        yield from emit_typing(combined_text)
                    return combined_text, tool_calls_all, nontext_content, chunks_count, streamed_any

                for chunk in response:
                    chunks_count += 1
                    delta = _safe_get(chunk, "delta") or {}
                    msg = _safe_get(delta, "message") or {}
                    content = _safe_get(msg, "content")
                    t, parts = _split_message_content(content)
                    if parts:
                        nontext_content.extend(parts)
                    tc = _safe_get(msg, "tool_calls") or []
                    if isinstance(tc, list) and tc:
                        tool_calls_all.extend(tc)
                        if not saw_tool_calls:
                            saw_tool_calls = True
                    if t:
                        text_parts.append(t)
                        combined_text_live = "".join(text_parts).strip()
                        if combined_text_live and not saw_tool_calls and should_emit_user_text(combined_text_live):
                            if not emitted_prefix:
                                yield self.create_text_message("\n" + agent_tag_header + "\n")
                                emitted_prefix = True
                            new = combined_text_live[emitted_len:]
                            if new:
                                step = max(1, int(typing_chunk))
                                for i in range(0, len(new), step):
                                    yield self.create_text_message(new[i : i + step])
                                    streamed_any = True
                                emitted_len = len(combined_text_live)
                combined_text = "".join(text_parts).strip()
                if emitted_prefix:
                    yield self.create_text_message("\n\n")
                elif combined_text and not saw_tool_calls and should_emit_user_text(combined_text):
                    yield from emit_typing(combined_text)
                return combined_text, tool_calls_all, nontext_content, chunks_count, streamed_any
            except Exception as e:
                return "", [], {"error": "stream_parse_failed", "exception": str(e)}, chunks_count, streamed_any

        loop_history_size = 30
        loop_warning_threshold = 10
        loop_critical_threshold = 20
        loop_global_no_progress_threshold = 30
        tool_call_sig_history: list[str] = []
        tool_call_sig_result_history: list[tuple[str, str]] = []
        loop_warned_sigs: set[str] = set()

        def _user_explicitly_requested_skill(text: str, *, skill_id: str, display_name: str) -> bool:
            s = str(text or "")
            if not s:
                return False
            s_lower = s.lower()
            sid = str(skill_id or "").strip().lower()
            if sid and sid in s_lower:
                return True
            dn = str(display_name or "").strip().lower()
            if dn and dn in s_lower:
                return True
            return False

        try:
            for step_idx in range(max_tool_turns):
                if timeout_seconds > 0 and (time.time() - started_at) > float(timeout_seconds):
                    final_text = f"❌超过超时时间 timeout_seconds={timeout_seconds}，已提前停止。"
                    break
                compact_if_needed()
                _dbg(
                    f"step={step_idx+1}/{max_tool_turns} messages={len(messages)} "
                    f"est_tokens={estimate_prompt_tokens(messages)}"
                )
                try:
                    res_text, tool_calls, nontext, chunks, streamed_any = yield from invoke_llm_live(
                        prompt_messages=messages,
                        tools=_build_prompt_message_tools(TOOL_SCHEMAS, PromptMessageTool),
                    )
                except Exception as e:
                    msg = str(e)
                    if "NameResolutionError" in msg or "Failed to resolve" in msg:
                        yield self.create_text_message(
                            "❌ LLM 调用失败：无法解析模型服务域名（DNS/网络问题）。\n"
                            "当前报错信息：\n"
                            + msg
                            + "\n\n请检查：\n"
                            + "1) 运行插件的环境是否能访问公网/是否需要代理\n"
                            + "2) DNS 是否可用（能否解析 dashscope.aliyuncs.com 等域名）\n"
                            + "3) Dify 的模型供应商（通义）网络出站是否被限制\n"
                        )
                    else:
                        yield self.create_text_message("❌ LLM 调用失败：\n" + msg)
                    return

                _dbg(
                    f"llm_return content_len={len(res_text)} tool_calls={len(tool_calls)} chunks={chunks} "
                    f"nontext={_shorten_text(nontext, 200) if nontext else ''}"
                )
                if nontext:
                    saved_assets = persist_llm_assets(nontext)
                    if saved_assets:
                        _dbg(f"nontext_assets_saved={len(saved_assets)} paths={_shorten_text(saved_assets, 300)}")
                if tool_calls:
                    empty_responses = 0
                    messages.append(AssistantPromptMessage(content=res_text or "", tool_calls=tool_calls))
                    forced_text: str | None = None
                    for tc in tool_calls:
                        call_id, name, arguments = _parse_tool_call(tc)
                        tool_name = str(name or "")
                        _dbg(f"tool_call name={tool_name} id={call_id!s} args={_shorten_text(arguments, 400)}")

                        args_norm = ""
                        try:
                            args_norm = json.dumps(arguments or {}, ensure_ascii=False, sort_keys=True)
                        except Exception:
                            args_norm = str(arguments or "")
                        tool_sig = f"{tool_name}|{args_norm}"
                        if loop_detection:
                            recent = tool_call_sig_history[-loop_history_size:] if loop_history_size > 0 else tool_call_sig_history
                            repeats = sum(1 for s in recent if s == tool_sig)
                            if repeats >= loop_critical_threshold:
                                forced_text = (
                                    "❌检测到工具调用循环，已提前熔断以避免无意义重试。\n"
                                    f"- tool: {tool_name}\n"
                                    f"- repeats_in_window: {repeats}\n"
                                    "建议：检查参数是否正确、是否缺少前置步骤（如 list/read），或改用更高层的策略（先读说明书/先列目录/先产出中间文件）。"
                                )
                                break
                            if repeats >= loop_warning_threshold and tool_sig not in loop_warned_sigs:
                                loop_warned_sigs.add(tool_sig)
                                result = {
                                    "error": "tool_loop_warning",
                                    "tool": tool_name,
                                    "detail": f"检测到疑似重复调用（最近窗口内重复 {repeats} 次）。请停止重复调用并调整策略。",
                                }
                                messages.append(
                                    ToolPromptMessage(
                                        tool_call_id=str(call_id or ""),
                                        name=tool_name,
                                        content=json.dumps(result, ensure_ascii=False),
                                    )
                                )
                                messages.append(
                                    UserPromptMessage(
                                        content=(
                                            f"你正在重复调用 `{tool_name}`，这通常意味着陷入死循环。"
                                            "请改为：先检查前置条件/路径/目录结构，或直接给出当前可交付产物与下一步建议。"
                                        )
                                    )
                                )
                                continue

                        ok_args, arg_detail = _validate_tool_arguments(tool_name, arguments)
                        if not ok_args:
                            result = {
                                "error": "invalid_tool_arguments",
                                "tool": tool_name,
                                "detail": arg_detail,
                                "got": arguments,
                            }
                            _dbg(f"tool_result name={tool_name} result={_shorten_text(result, 700)}")
                            messages.append(
                                ToolPromptMessage(
                                    tool_call_id=str(call_id or ""),
                                    name=tool_name,
                                    content=json.dumps(result, ensure_ascii=False),
                                )
                            )
                            messages.append(UserPromptMessage(content=_tool_call_retry_prompt(tool_name, arg_detail)))
                            continue

                        if tool_name in {"list_skill_files", "read_skill_file", "run_skill_command"}:
                            skill_name = str(arguments.get("skill_name") or "").strip()
                            entry = runtime.get_skill_entry(skill_name) if skill_name else None
                            if entry:
                                status = entry.get("status") if isinstance(entry.get("status"), dict) else {}
                                visible = bool(status.get("visible")) if isinstance(status, dict) else False
                                display_name = str(entry.get("name") or "").strip()
                                skill_id = str(entry.get("folder") or entry.get("id") or skill_name).strip()
                                if not visible and not _user_explicitly_requested_skill(
                                    user_input, skill_id=skill_id, display_name=display_name
                                ):
                                    missing = status.get("missing") if isinstance(status, dict) else {}
                                    result = {
                                        "error": "skill_not_callable",
                                        "skill_name": skill_name,
                                        "detail": "该技能当前不可由模型调用（visible=false 或 disable-model-invocation）。仅允许在用户明确点名该技能时继续。",
                                        "missing": missing,
                                    }
                                    _dbg(f"tool_result name={tool_name} result={_shorten_text(result, 700)}")
                                    messages.append(
                                        ToolPromptMessage(
                                            tool_call_id=str(call_id or ""),
                                            name=tool_name,
                                            content=json.dumps(result, ensure_ascii=False),
                                        )
                                    )
                                    messages.append(
                                        UserPromptMessage(
                                            content=(
                                                f"你尝试调用技能《{skill_name}》，但它当前标记为不可由模型调用。\n"
                                                "要求：只允许把该技能作为“说明型展示”出现在技能列表里；不要继续读取/执行它。\n"
                                                "如果用户明确说“就用这个技能”并点名技能名，才允许继续。"
                                            )
                                        )
                                    )
                                    continue

                        if tool_name == "list_skill_files":
                            yield self.create_text_message(
                                f"✅正在查看技能《{str(arguments.get('skill_name') or '')}》文件结构…\n"
                            )
                        elif tool_name == "read_skill_file":
                            yield self.create_text_message(
                                f"✅正在读取技能《{str(arguments.get('skill_name') or '')}》文件：{str(arguments.get('relative_path') or '')}…\n"
                            )
                        elif tool_name == "run_skill_command":
                            yield self.create_text_message(
                                f"✅正在执行技能《{str(arguments.get('skill_name') or '')}》命令…\n"
                            )
                        elif tool_name == "get_session_context":
                            yield self.create_text_message("✅正在获取会话目录信息…\n")
                        elif tool_name == "get_system_status":
                            yield self.create_text_message("✅正在获取运行环境状态…\n")
                        elif tool_name == "get_current_time":
                            yield self.create_text_message("✅正在获取当前时间…\n")
                        elif tool_name == "get_persona":
                            yield self.create_text_message("✅正在读取当前设定…\n")
                        elif tool_name == "update_persona":
                            yield self.create_text_message("✅正在更新设定…\n")
                        elif tool_name == "write_temp_file":
                            yield self.create_text_message(
                                f"✅正在按说明书写入临时文件：{str(arguments.get('relative_path') or '')}…\n"
                            )
                        elif tool_name == "read_temp_file":
                            yield self.create_text_message(
                                f"✅正在读取临时文件：{str(arguments.get('relative_path') or '')}…\n"
                            )
                        elif tool_name == "list_temp_files":
                            yield self.create_text_message("✅正在查看临时目录文件…\n")
                        elif tool_name == "glob_temp_files":
                            yield self.create_text_message("✅正在按模式匹配临时目录文件…\n")
                        elif tool_name == "grep_temp_files":
                            yield self.create_text_message("✅正在检索临时目录内容…\n")
                        elif tool_name == "edit_temp_file":
                            yield self.create_text_message(
                                f"✅正在编辑临时文件：{str(arguments.get('relative_path') or '')}…\n"
                            )
                        elif tool_name == "delete_temp_path":
                            yield self.create_text_message(
                                f"✅正在删除临时路径：{str(arguments.get('relative_path') or '')}…\n"
                            )
                        elif tool_name == "run_temp_command":
                            yield self.create_text_message("✅正在执行临时命令…\n")
                        elif tool_name == "export_temp_file":
                            yield self.create_text_message(
                                f"✅正在标记交付文件：{str(arguments.get('temp_relative_path') or '')}…\n"
                            )
                        elif tool_name == "web_fetch":
                            yield self.create_text_message("✅正在抓取网页内容…\n")

                        if tool_name == "list_skill_files":
                            result = runtime.list_skill_files(
                                str(arguments.get("skill_name") or ""),
                                int(arguments.get("max_depth") or 2),
                            )
                        elif tool_name == "read_skill_file":
                            result = runtime.read_skill_file(
                                str(arguments.get("skill_name") or ""),
                                str(arguments.get("relative_path") or ""),
                                int(arguments.get("max_chars") or 12000),
                            )
                        elif tool_name == "run_skill_command":
                            requested_command = (
                                arguments.get("command") if isinstance(arguments.get("command"), list) else []
                            )
                            requested_auto_install = bool(arguments.get("auto_install") or False)
                            exe0 = str(requested_command[0] or "") if requested_command else ""
                            install_like_bins = {"pip", "npm", "npx", "bun", "uv", "uvx"}
                            if requested_auto_install or exe0 in install_like_bins:
                                result = {
                                    "error": "install_commands_disabled",
                                    "detail": "对话执行阶段禁止安装/包管理命令与 auto_install。请在“技能管理（TM）”中执行“依赖安装”补全 Python 依赖；JS/系统级依赖请联系管理员在 plugin_daemon 容器安装。",
                                    "command": requested_command,
                                    "auto_install": requested_auto_install,
                                }
                                _dbg(f"tool_result name={tool_name} result={_shorten_text(result, 700)}")
                                messages.append(
                                    ToolPromptMessage(
                                        tool_call_id=str(call_id or ""),
                                        name=tool_name,
                                        content=json.dumps(result, ensure_ascii=False),
                                    )
                                )
                                messages.append(
                                    UserPromptMessage(
                                        content=(
                                            "你刚才尝试在对话中安装依赖/运行包管理命令，这是被禁止的。\n"
                                            "请改为：提示用户打开“技能管理（TM）”并执行“依赖安装”；如果是 JS/系统依赖（如 node/npm/agent-browser），提示联系管理员在 plugin_daemon 容器安装。\n"
                                            "然后继续使用已就绪的技能执行任务。"
                                        )
                                    )
                                )
                                continue

                            skill_for_check = str(arguments.get("skill_name") or "").strip()
                            entry = runtime.get_skill_entry(skill_for_check) if skill_for_check else None
                            if entry:
                                status = entry.get("status") if isinstance(entry.get("status"), dict) else {}
                                eligible = bool(status.get("eligible")) if isinstance(status, dict) else False
                                if not eligible:
                                    missing = status.get("missing") if isinstance(status, dict) else {}
                                    result = {
                                        "error": "skill_not_eligible",
                                        "skill_name": skill_for_check,
                                        "detail": "该技能当前不可执行（eligible=false）。请先补全依赖后再运行。",
                                        "missing": missing,
                                    }
                                    _dbg(f"tool_result name={tool_name} result={_shorten_text(result, 700)}")
                                    messages.append(
                                        ToolPromptMessage(
                                            tool_call_id=str(call_id or ""),
                                            name=tool_name,
                                            content=json.dumps(result, ensure_ascii=False),
                                        )
                                    )
                                    messages.append(
                                        UserPromptMessage(
                                            content=(
                                                f"技能《{skill_for_check}》当前不可执行（缺少依赖）。\n"
                                                "请提示用户到“技能管理（TM）”执行“依赖安装”补全 Python 依赖；JS/系统级依赖请联系管理员在 plugin_daemon 容器安装。\n"
                                                "不要继续尝试 run_skill_command。"
                                            )
                                        )
                                    )
                                    continue
                            exec_override = None
                            if (
                                isinstance(exec_once_allowed, dict)
                                and str(exec_once_allowed.get("kind") or "") in {"exec_once", "exec"}
                                and str(exec_once_allowed.get("tool") or "") == tool_name
                                and isinstance(exec_once_allowed.get("command"), list)
                                and exec_once_allowed.get("command") == requested_command
                            ):
                                exec_override = {
                                    "exe": str(exec_once_allowed.get("exe") or exe0),
                                    "allow_not_in_allowlist": bool(exec_once_allowed.get("allow_not_in_allowlist") is True),
                                    "allow_untrusted_path": bool(exec_once_allowed.get("allow_untrusted_path") is True),
                                    "path_allowlist": exec_once_allowed.get("path_allowlist") if isinstance(exec_once_allowed.get("path_allowlist"), list) else [],
                                }
                            if exec_override is None:
                                exec_override = _build_exec_override_from_grants(
                                    tool_name=tool_name,
                                    skill_name=str(arguments.get("skill_name") or "").strip() or None,
                                    requested_command=requested_command,
                                    exe0=exe0,
                                )
                            result = runtime.run_skill_command(
                                skill_name=str(arguments.get("skill_name") or ""),
                                command=requested_command,
                                cwd_relative=(
                                    str(arguments.get("cwd_relative")) if arguments.get("cwd_relative") else None
                                ),
                                auto_install=requested_auto_install,
                                exec_override=exec_override,
                            )
                            if (
                                isinstance(exec_once_allowed, dict)
                                and exec_override is not None
                                and isinstance(result, dict)
                                and result.get("error") is None
                                and result.get("returncode") is not None
                            ):
                                exec_once_allowed = None
                            if isinstance(result, dict):
                                err = str(result.get("error") or "").strip()
                                detail = str(result.get("detail") or "").strip()
                                if (err.startswith("command not allowed:") or (err == "exec_denied" and detail == "executable path is not trusted")) and exec_override is None:
                                    allow_not_in_allowlist = err.startswith("command not allowed:")
                                    allow_untrusted_path = err == "exec_denied" and detail == "executable path is not trusted"
                                    resolved_path = str(result.get("path") or result.get("resolved_exe") or "").strip()
                                    _storage_set_json(
                                        storage,
                                        approval_pending_key,
                                        {
                                            "pending": True,
                                            "kind": "exec",
                                            "tool": tool_name,
                                            "skill_name": str(arguments.get("skill_name") or "").strip(),
                                            "command": requested_command,
                                            "exe": exe0,
                                            "allow_not_in_allowlist": allow_not_in_allowlist,
                                            "allow_untrusted_path": allow_untrusted_path,
                                            "path": resolved_path,
                                            "resolved_exe": resolved_path,
                                            "path_allowlist": [resolved_path] if resolved_path else [],
                                            "original_query": effective_query,
                                            "created_at": int(time.time()),
                                        },
                                    )
                                    forced_text = (
                                        "该步骤需要执行一个未在允许列表或路径不受信任的命令。\n"
                                        f"- 命令：{_shorten_text(requested_command, 200)}\n"
                                        + (f"- 可执行文件：{resolved_path}\n" if resolved_path else "")
                                        + "为安全起见，需要你确认后才能继续：\n"
                                        "- 回复“允许一次”继续（仅本次）\n"
                                        "- 回复“允许本会话”继续（会话内不再询问）\n"
                                        "- 回复“允许本技能”继续（该技能不再询问）\n"
                                        "- 回复“总是允许”继续（后续不再询问）\n"
                                        "- 回复“拒绝”停止\n"
                                    )
                                    break
                            if (
                                isinstance(result, dict)
                                and result.get("returncode") is not None
                                and int(result.get("returncode") or 0) != 0
                            ):
                                stderr = str(result.get("stderr") or "").strip()
                                if stderr:
                                    yield self.create_text_message(
                                        "❌命令执行失败（stderr）：\n" + _shorten_text(redact_user_visible_text(stderr), 1200) + "\n"
                                    )
                            if isinstance(result, dict) and result.get("error") == "no_executable_found":
                                skill = str(result.get("skill") or arguments.get("skill_name") or "")
                                module = str(result.get("module") or "")
                                messages.append(
                                    UserPromptMessage(
                                        content=(
                                            f"技能“{skill}”缺少可执行入口（python -m {module} 在技能目录不存在）。\n"
                                            "不要再尝试调用 run_skill_command 生成最终文件。\n"
                                            "改用 temp 目录策略：用 write_temp_file 生成脚本/程序，在 session_dir 里运行 run_temp_command 产出最终文件；\n"
                                            "若需要安装依赖或运行包管理命令，先向用户发起审批。"
                                        )
                                    )
                                )
                        elif tool_name == "get_session_context":
                            result = runtime.get_session_context()
                        elif tool_name == "get_system_status":
                            result = runtime.get_system_status()
                        elif tool_name == "get_current_time":
                            result = runtime.get_current_time(
                                timezone=(str(arguments.get("timezone") or "").strip() if arguments.get("timezone") else None)
                            )
                        elif tool_name == "get_persona":
                            result = {
                                "identity_md": _storage_get_text(storage, identity_key).strip(),
                                "user_md": _storage_get_text(storage, user_key).strip(),
                                "soul_md": _storage_get_text(storage, soul_key).strip(),
                                "memory_md": _storage_get_text(storage, memory_key).strip(),
                            }
                        elif tool_name == "update_persona":
                            def _md_pick_field(md: str, key: str) -> str:
                                s = str(md or "")
                                if not s:
                                    return ""
                                m = re.search(
                                    rf"^\s*(?:-\s*)?\*\*\s*{re.escape(key)}\s*:\s*\*\*\s*(.+?)\s*$",
                                    s,
                                    flags=re.M | re.I,
                                )
                                return str(m.group(1) or "").strip() if m else ""

                            def _md_set_field(md: str, *, key: str, value: str, header: str) -> str:
                                s = str(md or "").strip()
                                if not s:
                                    s = header.strip() + "\n"
                                lines = s.splitlines()
                                rx = re.compile(rf"^\s*(?:-\s*)?\*\*\s*{re.escape(key)}\s*:\s*\*\*\s*(.*)\s*$", flags=re.I)
                                out: list[str] = []
                                replaced = False
                                for line in lines:
                                    if rx.match(line):
                                        out.append(f"- **{key}:** {value}".rstrip())
                                        replaced = True
                                    else:
                                        out.append(line)
                                if not replaced:
                                    if out and out[-1].strip():
                                        out.append("")
                                    out.append(f"- **{key}:** {value}".rstrip())
                                return "\n".join(out).strip() + "\n"

                            def _soul_set_vibe(md: str, vibe: str) -> str:
                                s = str(md or "").strip()
                                if not s:
                                    return (
                                        "# SOUL.md - Who You Are\n\n"
                                        "## Core\n"
                                        "- 说人话，少模板；可以小调皮，但不油腻。\n"
                                        "- 语气不要刻意迎合，但要给与用户足够的尊重。\n"
                                        "- 有主见，但不自作主张，遇到不确定的事情时，会向用户询问。\n"
                                        "- 幽默是你的底色，善良是你的天性，你会主动关心用户。\n"

                                        "## Vibe\n"
                                        f"{vibe}\n"
                                    )
                                lines = s.splitlines()
                                out: list[str] = []
                                i = 0
                                replaced = False
                                while i < len(lines):
                                    line = lines[i]
                                    if re.match(r"^\s*##\s+Vibe\s*$", line, flags=re.I):
                                        out.append(line)
                                        out.append(vibe)
                                        replaced = True
                                        i += 1
                                        while i < len(lines) and not re.match(r"^\s*##\s+", lines[i]):
                                            i += 1
                                        continue
                                    out.append(line)
                                    i += 1
                                if not replaced:
                                    if out and out[-1].strip():
                                        out.append("")
                                    out.extend(["## Vibe", vibe])
                                return "\n".join(out).strip() + "\n"

                            def _soul_set_core(md: str, core_rules: list[str]) -> str:
                                cleaned: list[str] = []
                                for raw in core_rules or []:
                                    s = str(raw or "").strip()
                                    if not s:
                                        continue
                                    s = re.sub(r"^\s*\d+\s*[\.\)、]\s*", "", s).strip()
                                    if not s:
                                        continue
                                    if s.startswith("-"):
                                        s = s.lstrip("-").strip()
                                    if not s:
                                        continue
                                    if len(s) > 240:
                                        s = s[:240]
                                    if s not in cleaned:
                                        cleaned.append(s)
                                if not cleaned:
                                    return str(md or "").strip() + ("\n" if str(md or "").strip() else "")

                                s = str(md or "").strip()
                                if not s:
                                    s = "# SOUL.md - Who You Are\n\n## Core\n\n"

                                lines = s.splitlines()
                                core_titles = {"Core", "Core Truths"}
                                start = -1
                                for i, line in enumerate(lines):
                                    m = re.match(r"^\s*##\s+(.+?)\s*$", line)
                                    if not m:
                                        continue
                                    title = str(m.group(1) or "").strip()
                                    if title in core_titles:
                                        start = i
                                        break

                                def render_core_block() -> list[str]:
                                    out: list[str] = ["## Core"]
                                    for rule in cleaned:
                                        out.append(f"- {rule}")
                                    return out

                                if start != -1:
                                    end = len(lines)
                                    for j in range(start + 1, len(lines)):
                                        if re.match(r"^\s*##\s+", lines[j]):
                                            end = j
                                            break
                                    next_lines = lines[:start] + render_core_block() + [""] + lines[end:]
                                    return "\n".join(next_lines).strip() + "\n"

                                insert_at = 0
                                if lines and lines[0].lstrip().startswith("#"):
                                    insert_at = 1
                                    while insert_at < len(lines) and lines[insert_at].strip():
                                        insert_at += 1
                                    while insert_at < len(lines) and not lines[insert_at].strip():
                                        insert_at += 1
                                next_lines = lines[:insert_at] + ([""] if insert_at and lines[insert_at - 1].strip() else []) + render_core_block() + [""] + lines[insert_at:]
                                return "\n".join(next_lines).strip() + "\n"

                            mode = str(arguments.get("mode") or "apply").strip().lower()
                            if mode not in {"apply", "preview"}:
                                mode = "apply"

                            agent_obj = arguments.get("agent") if isinstance(arguments.get("agent"), dict) else {}
                            user_obj = arguments.get("user") if isinstance(arguments.get("user"), dict) else {}
                            soul_obj = arguments.get("soul") if isinstance(arguments.get("soul"), dict) else {}
                            agent_name = str(agent_obj.get("name") or "").strip()
                            agent_creature = str(agent_obj.get("creature") or "").strip()
                            agent_vibe = str(agent_obj.get("vibe") or "").strip()
                            agent_emoji = str(agent_obj.get("emoji") or "").strip()
                            user_name = str(user_obj.get("name") or "").strip()
                            user_addressing = str(user_obj.get("addressing") or "").strip()
                            soul_core_rules = soul_obj.get("core_rules") if isinstance(soul_obj.get("core_rules"), list) else None
                            soul_core_text = str(soul_obj.get("core_text") or "").strip()

                            identity_before = _storage_get_text(storage, identity_key).strip()
                            user_before = _storage_get_text(storage, user_key).strip()
                            soul_before = _storage_get_text(storage, soul_key).strip()

                            identity_after = identity_before
                            user_after = user_before
                            soul_after = soul_before
                            changed: list[str] = []

                            if agent_name:
                                identity_after = _md_set_field(
                                    identity_after, key="Name", value=agent_name, header="# IDENTITY.md - Who Am I?\n"
                                )
                                changed.append("agent.name")
                            if agent_creature:
                                identity_after = _md_set_field(
                                    identity_after, key="Creature", value=agent_creature, header="# IDENTITY.md - Who Am I?\n"
                                )
                                changed.append("agent.creature")
                            if agent_vibe:
                                identity_after = _md_set_field(
                                    identity_after, key="Vibe", value=agent_vibe, header="# IDENTITY.md - Who Am I?\n"
                                )
                                soul_after = _soul_set_vibe(soul_after, agent_vibe)
                                changed.append("agent.vibe")
                            if agent_emoji:
                                identity_after = _md_set_field(
                                    identity_after, key="Emoji", value=agent_emoji, header="# IDENTITY.md - Who Am I?\n"
                                )
                                changed.append("agent.emoji")
                            if user_name:
                                user_after = _md_set_field(
                                    user_after, key="Name", value=user_name, header="# USER.md - Who Are You?\n"
                                )
                                changed.append("user.name")
                            if user_addressing:
                                user_after = _md_set_field(
                                    user_after, key="Addressing", value=user_addressing, header="# USER.md - Who Are You?\n"
                                )
                                changed.append("user.addressing")
                            if isinstance(soul_core_rules, list) and soul_core_rules:
                                soul_after = _soul_set_core(soul_after, [str(x) for x in soul_core_rules])
                                changed.append("soul.core")
                            elif soul_core_text:
                                soul_after = _soul_set_core(soul_after, soul_core_text.splitlines())
                                changed.append("soul.core")

                            if mode == "apply" and changed:
                                _storage_set_text(storage, identity_key, identity_after)
                                _storage_set_text(storage, user_key, user_after)
                                if soul_after.strip():
                                    _storage_set_text(storage, soul_key, soul_after)
                                _storage_set_json(
                                    storage,
                                    onboarding_key,
                                    {"stage": 2, "completed": True, "updated_at": int(time.time())},
                                )
                                agent_tag_header = build_agent_tag_header()

                            result = {
                                "ok": True,
                                "mode": mode,
                                "changed": changed,
                                "before": {
                                    "identity_md": identity_before,
                                    "user_md": user_before,
                                    "soul_md": soul_before,
                                },
                                "after": {
                                    "identity_md": identity_after,
                                    "user_md": user_after,
                                    "soul_md": soul_after,
                                },
                            }
                        elif tool_name == "write_temp_file":
                            result = runtime.write_temp_file(
                                str(arguments.get("relative_path") or ""),
                                str(arguments.get("content") or ""),
                            )
                        elif tool_name == "read_temp_file":
                            result = runtime.read_temp_file(
                                str(arguments.get("relative_path") or ""),
                                int(arguments.get("max_chars") or 12000),
                            )
                        elif tool_name == "list_temp_files":
                            result = runtime.list_temp_files(int(arguments.get("max_depth") or 4))
                        elif tool_name == "glob_temp_files":
                            result = runtime.glob_temp_files(
                                str(arguments.get("pattern") or ""),
                                int(arguments.get("max_results") or 200),
                            )
                        elif tool_name == "grep_temp_files":
                            result = runtime.grep_temp_files(
                                str(arguments.get("pattern") or ""),
                                str(arguments.get("glob") or "**/*"),
                                int(arguments.get("max_matches") or 200),
                            )
                        elif tool_name == "edit_temp_file":
                            result = runtime.edit_temp_file(
                                str(arguments.get("relative_path") or ""),
                                str(arguments.get("old_text") or ""),
                                str(arguments.get("new_text") or ""),
                                bool(arguments.get("replace_all") or False),
                            )
                        elif tool_name == "delete_temp_path":
                            result = runtime.delete_temp_path(
                                str(arguments.get("relative_path") or ""),
                                bool(arguments.get("recursive") or False),
                            )
                        elif tool_name == "run_temp_command":
                            requested_command = (
                                arguments.get("command") if isinstance(arguments.get("command"), list) else []
                            )
                            requested_auto_install = bool(arguments.get("auto_install") or False)
                            exe0 = str(requested_command[0] or "") if requested_command else ""
                            install_like_bins = {"pip", "npm", "npx", "bun", "uv", "uvx"}
                            if requested_auto_install or exe0 in install_like_bins:
                                result = {
                                    "error": "install_commands_disabled",
                                    "detail": "对话执行阶段禁止安装/包管理命令与 auto_install。请在“技能管理（TM）”中执行“依赖安装”补全 Python 依赖；JS/系统级依赖请联系管理员在 plugin_daemon 容器安装。",
                                    "command": requested_command,
                                    "auto_install": requested_auto_install,
                                }
                                _dbg(f"tool_result name={tool_name} result={_shorten_text(result, 700)}")
                                messages.append(
                                    ToolPromptMessage(
                                        tool_call_id=str(call_id or ""),
                                        name=tool_name,
                                        content=json.dumps(result, ensure_ascii=False),
                                    )
                                )
                                messages.append(
                                    UserPromptMessage(
                                        content=(
                                            "你刚才尝试在对话中安装依赖/运行包管理命令，这是被禁止的。\n"
                                            "请改为：提示用户打开“技能管理（TM）”并执行“依赖安装”；如果是 JS/系统依赖（如 node/npm/agent-browser），提示联系管理员在 plugin_daemon 容器安装。\n"
                                            "然后继续使用已就绪的技能执行任务。"
                                        )
                                    )
                                )
                                continue
                            exec_override = None
                            if (
                                isinstance(exec_once_allowed, dict)
                                and str(exec_once_allowed.get("kind") or "") in {"exec_once", "exec"}
                                and str(exec_once_allowed.get("tool") or "") == tool_name
                                and isinstance(exec_once_allowed.get("command"), list)
                                and exec_once_allowed.get("command") == requested_command
                            ):
                                exec_override = {
                                    "exe": str(exec_once_allowed.get("exe") or exe0),
                                    "allow_not_in_allowlist": bool(exec_once_allowed.get("allow_not_in_allowlist") is True),
                                    "allow_untrusted_path": bool(exec_once_allowed.get("allow_untrusted_path") is True),
                                    "path_allowlist": exec_once_allowed.get("path_allowlist") if isinstance(exec_once_allowed.get("path_allowlist"), list) else [],
                                }
                            if exec_override is None:
                                exec_override = _build_exec_override_from_grants(
                                    tool_name=tool_name,
                                    skill_name=None,
                                    requested_command=requested_command,
                                    exe0=exe0,
                                )
                            result = runtime.run_temp_command(
                                command=requested_command,
                                cwd_relative=(
                                    str(arguments.get("cwd_relative")) if arguments.get("cwd_relative") else None
                                ),
                                auto_install=requested_auto_install,
                                exec_override=exec_override,
                            )
                            if (
                                isinstance(exec_once_allowed, dict)
                                and exec_override is not None
                                and isinstance(result, dict)
                                and result.get("error") is None
                                and result.get("returncode") is not None
                            ):
                                exec_once_allowed = None
                            if isinstance(result, dict):
                                err = str(result.get("error") or "").strip()
                                detail = str(result.get("detail") or "").strip()
                                if (err.startswith("command not allowed:") or (err == "exec_denied" and detail == "executable path is not trusted")) and exec_override is None:
                                    allow_not_in_allowlist = err.startswith("command not allowed:")
                                    allow_untrusted_path = err == "exec_denied" and detail == "executable path is not trusted"
                                    resolved_path = str(result.get("path") or result.get("resolved_exe") or "").strip()
                                    _storage_set_json(
                                        storage,
                                        approval_pending_key,
                                        {
                                            "pending": True,
                                            "kind": "exec",
                                            "tool": tool_name,
                                            "command": requested_command,
                                            "exe": exe0,
                                            "allow_not_in_allowlist": allow_not_in_allowlist,
                                            "allow_untrusted_path": allow_untrusted_path,
                                            "path": resolved_path,
                                            "resolved_exe": resolved_path,
                                            "path_allowlist": [resolved_path] if resolved_path else [],
                                            "original_query": effective_query,
                                            "created_at": int(time.time()),
                                        },
                                    )
                                    forced_text = (
                                        "该步骤需要执行一个未在允许列表或路径不受信任的命令。\n"
                                        f"- 命令：{_shorten_text(requested_command, 200)}\n"
                                        + (f"- 可执行文件：{resolved_path}\n" if resolved_path else "")
                                        + "为安全起见，需要你确认后才能继续：\n"
                                        "- 回复“允许一次”继续（仅本次）\n"
                                        "- 回复“允许本会话”继续（会话内不再提示）\n"
                                        "- 回复“总是允许”继续（写入允许列表，后续不再提示）\n"
                                        "- 回复“拒绝”停止\n"
                                    )
                                    break
                            if (
                                isinstance(result, dict)
                                and result.get("returncode") is not None
                                and int(result.get("returncode") or 0) != 0
                            ):
                                stderr = str(result.get("stderr") or "").strip()
                                if stderr:
                                    yield self.create_text_message(
                                        "❌命令执行失败（stderr）：\n" + _shorten_text(redact_user_visible_text(stderr), 1200) + "\n"
                                    )
                        elif tool_name == "export_temp_file":
                            temp_rel = str(arguments.get("temp_relative_path") or "")
                            workspace_rel = str(arguments.get("workspace_relative_path") or "")
                            reserved = {"identity.md", "user.md", "soul.md", "memory.md"}
                            ws_norm = workspace_rel.replace("\\", "/").lstrip("/").strip()
                            ws_base = os.path.basename(ws_norm).lower() if ws_norm else ""
                            if ws_base in reserved or ws_norm.lower().startswith("memory/"):
                                result = {
                                    "error": "reserved_export_name",
                                    "detail": "不允许导出/交付系统人格与记忆文件（IDENTITY/USER/SOUL/MEMORY 或 memory/*.md）。请改用 update_persona 更新设定。",
                                    "requested_name": workspace_rel,
                                }
                                messages.append(
                                    ToolPromptMessage(
                                        tool_call_id=str(call_id or ""),
                                        name=tool_name,
                                        content=json.dumps(result, ensure_ascii=False),
                                    )
                                )
                                messages.append(
                                    UserPromptMessage(
                                        content=(
                                            "你刚才尝试把系统人格/记忆文件当作交付物导出，这是不允许的。\n"
                                            "规则：IDENTITY.md/USER.md/SOUL.md/MEMORY.md/memory/*.md 只能用 update_persona 更新，不能 export_temp_file。\n"
                                            "请改用 update_persona 精确更新相应字段，然后用文本确认即可。"
                                        )
                                    )
                                )
                                continue
                            result = runtime.export_temp_file(
                                temp_relative_path=temp_rel,
                                workspace_relative_path=workspace_rel,
                                overwrite=bool(arguments.get("overwrite") or False),
                            )
                            out_name = os.path.basename(workspace_rel) if workspace_rel else ""
                            if (
                                isinstance(result, dict)
                                and not result.get("error")
                                and temp_rel
                                and out_name
                            ):
                                final_file_meta[temp_rel] = {
                                    **(final_file_meta.get(temp_rel) or {}),
                                    "filename": out_name,
                                    "mime_type": _guess_mime_type(out_name),
                                }
                        elif tool_name == "web_fetch":
                            result = runtime.web_fetch(
                                str(arguments.get("url") or ""),
                                str(arguments.get("extract_mode") or "markdown"),
                                int(arguments.get("max_chars") or 50000),
                            )
                        else:
                            result = {"error": f"unknown tool: {tool_name}"}

                        _dbg(f"tool_result name={tool_name} result={_shorten_text(result, 700)}")
                        if loop_detection:
                            try:
                                rh = hashlib.sha1(
                                    json.dumps(result, ensure_ascii=False, sort_keys=True).encode("utf-8", errors="ignore")
                                ).hexdigest()
                            except Exception:
                                rh = str(len(str(result)))
                            tool_call_sig_history.append(tool_sig)
                            tool_call_sig_result_history.append((tool_sig, rh))
                            if len(tool_call_sig_history) > 200:
                                tool_call_sig_history = tool_call_sig_history[-200:]
                            if len(tool_call_sig_result_history) > 200:
                                tool_call_sig_result_history = tool_call_sig_result_history[-200:]
                            recent_pairs = tool_call_sig_result_history[-loop_global_no_progress_threshold:]
                            if (
                                loop_global_no_progress_threshold > 0
                                and len(recent_pairs) >= loop_global_no_progress_threshold
                                and len(set(recent_pairs)) == 1
                            ):
                                forced_text = (
                                    "❌检测到持续无进展的工具循环（同一调用与结果反复出现），已提前停止。\n"
                                    f"- tool: {tool_name}\n"
                                    "建议：变更参数/切换工具/增加读文件或列目录步骤，或先交付当前阶段产物。"
                                )
                                break
                        messages.append(
                            ToolPromptMessage(
                                tool_call_id=str(call_id or ""),
                                name=tool_name,
                                content=json.dumps(result, ensure_ascii=False),
                            )
                        )
                    if forced_text:
                        final_text = forced_text
                        break
                    if step_idx >= max_tool_turns - 1:
                        try:
                            has_files = any(
                                e.get("type") == "file"
                                for e in _list_dir(session_dir, max_depth=2)
                                if isinstance(e, dict)
                            )
                        except Exception:
                            has_files = False
                        if final_file_meta or has_files:
                            final_text = "已生成文件。"
                            break
                    continue

                if not res_text and not nontext:
                    empty_responses += 1
                    _dbg(f"empty_response_count={empty_responses}")
                    if empty_responses < 3:
                        messages.append(
                            UserPromptMessage(
                                content="你刚才没有输出任何内容。请继续完成任务：如果需要使用工具，请以 function call 发起工具调用；若无需工具，请直接给出最终答复。"
                            )
                        )
                        continue
                    final_text = "模型连续返回空响应，未生成任何结果。"
                    break

                if res_text:
                    final_text = res_text
                    _dbg(f"final_text content_len={len(final_text)}")
                    if streamed_any and final_text:
                        final_text_already_streamed = True
                    break

                if nontext:
                    final_text = "已接收到模型输出的非文本内容。"
                    break

                final_text = "未生成任何文本或文件输出。"
                break
            else:
                try:
                    has_files = any(
                        e.get("type") == "file" for e in _list_dir(session_dir, max_depth=2) if isinstance(e, dict)
                    )
                except Exception:
                    has_files = False
                if final_file_meta or has_files:
                    final_text = "已生成文件。"
                else:
                    final_text = (
                        f"❌超过最大执行轮数 max_tool_turns={max_tool_turns}，仍未得到最终结果。\n"
                        f"可尝试：提高 timeout_seconds（当前 {timeout_seconds} 秒）或检查是否陷入重复工具调用。"
                    )
        finally:
            temp_files_text = ""
            try:
                temp_entries = _list_dir(session_dir, max_depth=10)
                rel_paths = [
                    str(e.get("relative_path"))
                    for e in temp_entries
                    if e.get("type") == "file" and isinstance(e.get("relative_path"), str)
                ]
                if rel_paths:
                    temp_files_text = "\n\n[temp_files]\n" + "\n".join(rel_paths)
                _dbg(f"temp_files_count={len(rel_paths)}")
            except Exception:
                temp_files_text = ""

            files_to_send: list[tuple[str, str, str, str]] = []
            try:
                for rel, meta_override in (final_file_meta or {}).items():
                    if not rel or not isinstance(rel, str):
                        continue
                    rel_norm = rel.replace("\\", "/").lstrip("/")
                    if not rel_norm:
                        continue
                    try:
                        path = _safe_join(session_dir, rel_norm)
                    except Exception:
                        continue
                    if not os.path.isfile(path):
                        continue
                    filename = os.path.basename(rel_norm)
                    out_name = (meta_override.get("filename") if isinstance(meta_override, dict) else None) or filename
                    mime_type = (meta_override.get("mime_type") if isinstance(meta_override, dict) else None) or _guess_mime_type(out_name or filename)
                    files_to_send.append((rel_norm, path, mime_type, out_name))
            except Exception:
                files_to_send = []

            has_any_files = False
            try:
                temp_entries = _list_dir(session_dir, max_depth=10)
                has_any_files = any(e.get("type") == "file" for e in temp_entries if isinstance(e, dict))
            except Exception:
                has_any_files = False

            assistant_text_for_history = ""
            if final_text and final_text.strip():
                if not files_to_send and final_text.strip() == "已生成文件。":
                    final_text = "已生成中间文件，但未调用 export_temp_file 标记交付文件。"
                assistant_text_for_history = final_text.strip()
                _append_history_turn(
                    storage,
                    history_key=history_key,
                    user_text=user_input,
                    assistant_text=assistant_text_for_history,
                )
                if not final_text_already_streamed:
                    yield from stream_text_to_user(final_text)
            elif files_to_send:
                assistant_text_for_history = "已生成文件。"
                _append_history_turn(
                    storage,
                    history_key=history_key,
                    user_text=user_input,
                    assistant_text=assistant_text_for_history,
                )
                yield from stream_text_to_user("已生成文件。")
            elif has_any_files:
                assistant_text_for_history = "已生成中间文件，但未调用 export_temp_file 标记交付文件。"
                _append_history_turn(
                    storage,
                    history_key=history_key,
                    user_text=user_input,
                    assistant_text=assistant_text_for_history,
                )
                yield from stream_text_to_user("已生成中间文件，但未调用 export_temp_file 标记交付文件。")
            else:
                assistant_text_for_history = "未生成任何文本或文件输出。"
                _append_history_turn(
                    storage,
                    history_key=history_key,
                    user_text=user_input,
                    assistant_text=assistant_text_for_history,
                )
                yield from stream_text_to_user("未生成任何文本或文件输出。")

            try:
                today = time.strftime("%Y-%m-%d", time.localtime())
                daily_key = _get_memory_storage_key(self.session, f"memory/{today}.md")
                existing_daily = _storage_get_text(storage, daily_key)
                header = f"# {today}\n\n"
                if not existing_daily.strip():
                    existing_daily = header
                ts = time.strftime("%H:%M", time.localtime())
                u = _shorten_text(str(user_input or "").strip().replace("\r", "").replace("\n", " "), 500)
                a = _shorten_text(str(assistant_text_for_history or "").strip().replace("\r", "").replace("\n", " "), 800)
                entry = f"- {ts} USER: {u}\n- {ts} ASSISTANT: {a}\n\n"
                merged = existing_daily + entry
                if len(merged) > 20000:
                    merged = merged[-20000:]
                    if not merged.lstrip().startswith("#"):
                        merged = header + merged
                _storage_set_text(storage, daily_key, merged)
            except Exception:
                pass

            yielded: set[str] = set()
            yielded_fingerprints: set[str] = set()
            for rel, path, mime_type, out_name in files_to_send:
                if rel in yielded:
                    continue
                yielded.add(rel)
                try:
                    with open(path, "rb") as fp:
                        content = fp.read()
                    try:
                        content_fp = hashlib.sha1(content).hexdigest()
                    except Exception:
                        content_fp = str(len(content))
                    fingerprint_key = f"{out_name}|{mime_type}|{content_fp}"
                    if fingerprint_key in yielded_fingerprints:
                        continue
                    yielded_fingerprints.add(fingerprint_key)
                    yield self.create_blob_message(blob=content, meta={"mime_type": mime_type, "filename": out_name})
                except Exception:
                    continue
            _dbg(f"temp_retained session_dir={session_dir}")
