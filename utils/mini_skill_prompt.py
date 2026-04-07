from __future__ import annotations

import os
import time
from datetime import timedelta
from typing import Any

from utils.mini_skill_memory import _dt_beijing
from utils.mini_skill_storage import _get_memory_storage_key, _storage_get_text
from utils.mini_skill_hooks import PromptBuildContext, build_prompt_layers


def _xml_escape(text: Any) -> str:
    s = str(text or "")
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def build_skills_xml(*, snapshot: dict[str, Any]) -> str:
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
        miniskill = s.get("miniskill") if isinstance(s.get("miniskill"), dict) else {}
        os_allow = ",".join(miniskill.get("os") or []) if isinstance(miniskill, dict) else ""

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


def build_system_prompt_content(
    *,
    system_prompt: str,
    session_dir: str,
    skills_root: str | None,
    skills_snapshot: dict[str, Any],
    storage: Any,
    session: Any,
    user_id: str,
    identity_key: str,
    user_key: str,
    soul_key: str,
    memory_key: str,
    uploads_context: str,
    approval_context: str,
) -> str:
    skills_xml = build_skills_xml(snapshot=skills_snapshot if isinstance(skills_snapshot, dict) else {})
    system_lines: list[str] = []
    if str(system_prompt or "").strip():
        system_lines.append(str(system_prompt or "").strip())
        system_lines.append("")

    now_epoch = int(time.time())
    now_beijing = ""
    try:
        now_beijing = _dt_beijing(now_epoch).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        now_beijing = ""

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
            f"- 北京时间(Asia/Shanghai): {now_beijing}",
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
            '- 当你需要调用某个技能时：必须先读取该技能的说明书 SKILL.md（用 read_skill_file(skill_name, "SKILL.md")）。',
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
            "- 用户可通过技能管理节点管理技能，并通过依赖安装指令自动安装依赖，一些系统级依赖请联系管理员在 plugin_daemon 容器安装。",
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
            "## 内置可用工具",
            "get_session_context, get_system_status, get_current_time, get_persona, update_persona, list_skill_files, read_skill_file, run_skill_command, write_temp_file, read_temp_file, list_temp_files, glob_temp_files, grep_temp_files, edit_temp_file, delete_temp_path, run_temp_command, export_temp_file, web_fetch",
            "",
            "",
            "## Skills 清单（XML）",
            skills_xml,
        ]
    )

    if uploads_context:
        try:
            system_lines.insert(system_lines.index("## 可用工具"), uploads_context.strip() + "\n")
        except Exception:
            system_lines.append(uploads_context.strip() + "\n")
    if approval_context:
        system_lines.append(approval_context)

    soul_md = _storage_get_text(storage, soul_key).strip()
    layers = build_prompt_layers(
        PromptBuildContext(
            storage=storage,
            session=session,
            user_id=str(user_id or "").strip() or "global_user",
            identity_key=identity_key,
            user_key=user_key,
            soul_key=soul_key,
            memory_key=memory_key,
        )
    )
    shared_blocks = layers.get("shared") if isinstance(layers.get("shared"), list) else []
    personal_blocks = layers.get("personal") if isinstance(layers.get("personal"), list) else []
    session_blocks = layers.get("session") if isinstance(layers.get("session"), list) else []
    context_blocks: list[tuple[str, str]] = []
    context_blocks.extend([(str(p), str(c)) for p, c in shared_blocks if str(c or "").strip()])
    context_blocks.extend([(str(p), str(c)) for p, c in personal_blocks if str(c or "").strip()])
    context_blocks.extend([(str(p), str(c)) for p, c in session_blocks if str(c or "").strip()])

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

    return "\n".join([x for x in system_lines if x is not None])
