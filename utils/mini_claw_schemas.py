from __future__ import annotations

from typing import Any


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_skill_files",
            "description": "列出指定技能包内的文件结构",
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_name": {"type": "string"},
                    "max_depth": {"type": "integer", "default": 2},
                },
                "required": ["skill_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_skill_file",
            "description": "读取技能包内的文件内容",
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_name": {"type": "string"},
                    "relative_path": {"type": "string"},
                    "max_chars": {"type": "integer", "default": 12000},
                },
                "required": ["skill_name", "relative_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_skill_command",
            "description": "在技能包目录内执行命令（限定可执行程序）",
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_name": {"type": "string"},
                    "command": {"type": "array", "items": {"type": "string"}},
                    "cwd_relative": {"type": "string"},
                    "auto_install": {"type": "boolean", "default": False},
                },
                "required": ["skill_name", "command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_session_context",
            "description": "获取本次会话的技能目录与临时目录信息",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_system_status",
            "description": "获取运行环境的基础状态（CPU负载/内存/磁盘等，安全版）",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "获取当前时间（可指定时区，默认返回UTC与服务器本地时间）",
            "parameters": {
                "type": "object",
                "properties": {"timezone": {"type": "string"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_persona",
            "description": "获取当前人格与用户画像（IDENTITY/USER/SOUL/MEMORY）",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_persona",
            "description": "更新人格与用户画像（仅修改提供的字段）",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "creature": {"type": "string"},
                            "vibe": {"type": "string"},
                            "emoji": {"type": "string"},
                        },
                    },
                    "user": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "addressing": {"type": "string"},
                            "timezone": {"type": "string"},
                        },
                    },
                    "soul": {
                        "type": "object",
                        "properties": {
                            "core_rules": {"type": "array", "items": {"type": "string"}},
                            "core_text": {"type": "string"},
                        },
                    },
                    "mode": {"type": "string", "default": "apply"},
                    "reason": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_temp_file",
            "description": "将文本写入 temp 会话目录（相对路径）",
            "parameters": {
                "type": "object",
                "properties": {
                    "relative_path": {"type": "string", "minLength": 1},
                    "content": {"type": "string"},
                },
                "required": ["relative_path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_temp_file",
            "description": "读取 temp 会话目录文件内容（相对路径）",
            "parameters": {
                "type": "object",
                "properties": {
                    "relative_path": {"type": "string", "minLength": 1},
                    "max_chars": {"type": "integer", "default": 12000},
                },
                "required": ["relative_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_temp_files",
            "description": "列出 temp 会话目录文件结构",
            "parameters": {
                "type": "object",
                "properties": {"max_depth": {"type": "integer", "default": 4}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob_temp_files",
            "description": "按 glob 模式匹配 temp 会话目录文件路径",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "minLength": 1},
                    "max_results": {"type": "integer", "default": 200},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep_temp_files",
            "description": "在 temp 会话目录内按正则检索文本内容",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "minLength": 1},
                    "glob": {"type": "string", "default": "**/*"},
                    "max_matches": {"type": "integer", "default": 200},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_temp_file",
            "description": "精确编辑 temp 会话目录文件（基于 old_text→new_text）",
            "parameters": {
                "type": "object",
                "properties": {
                    "relative_path": {"type": "string", "minLength": 1},
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                    "replace_all": {"type": "boolean", "default": False},
                },
                "required": ["relative_path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_temp_path",
            "description": "删除 temp 会话目录下的文件或目录（默认不允许递归删目录）",
            "parameters": {
                "type": "object",
                "properties": {
                    "relative_path": {"type": "string", "minLength": 1},
                    "recursive": {"type": "boolean", "default": False},
                },
                "required": ["relative_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_temp_command",
            "description": "在 temp 会话目录内执行命令（限定可执行程序）",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "array", "items": {"type": "string"}},
                    "cwd_relative": {"type": "string"},
                    "auto_install": {"type": "boolean", "default": False},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "export_temp_file",
            "description": "标记 temp 会话文件为最终交付文件（不复制）",
            "parameters": {
                "type": "object",
                "properties": {
                    "temp_relative_path": {"type": "string", "minLength": 1},
                    "workspace_relative_path": {"type": "string", "minLength": 1},
                    "overwrite": {"type": "boolean", "default": False},
                },
                "required": ["temp_relative_path", "workspace_relative_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "抓取网页内容并提取为可读文本（HTTP GET，HTML→文本）",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "minLength": 1},
                    "extract_mode": {"type": "string", "default": "markdown"},
                    "max_chars": {"type": "integer", "default": 50000},
                },
                "required": ["url"],
            },
        },
    },
]


def _validate_tool_arguments(tool_name: str, arguments: Any) -> tuple[bool, str]:
    if not isinstance(arguments, dict):
        return False, "arguments 必须是对象(dict)"

    required: dict[str, list[str]] = {
        "list_skill_files": ["skill_name"],
        "read_skill_file": ["skill_name", "relative_path"],
        "run_skill_command": ["skill_name", "command"],
        "get_session_context": [],
        "get_system_status": [],
        "get_current_time": [],
        "get_persona": [],
        "update_persona": [],
        "write_temp_file": ["relative_path", "content"],
        "read_temp_file": ["relative_path"],
        "list_temp_files": [],
        "glob_temp_files": ["pattern"],
        "grep_temp_files": ["pattern"],
        "edit_temp_file": ["relative_path", "old_text", "new_text"],
        "delete_temp_path": ["relative_path"],
        "run_temp_command": ["command"],
        "export_temp_file": ["temp_relative_path", "workspace_relative_path"],
        "web_fetch": ["url"],
    }

    if tool_name not in required:
        return True, ""

    missing: list[str] = []
    for key in required[tool_name]:
        val = arguments.get(key)
        if val is None:
            missing.append(key)
            continue
        if isinstance(val, str) and not val.strip():
            missing.append(key)
            continue
        if key == "command" and (not isinstance(val, list) or not val):
            missing.append(key)
            continue

    if missing:
        return False, "缺少或为空的必填参数: " + ", ".join(missing)
    return True, ""


def _tool_call_retry_prompt(tool_name: str, detail: str) -> str:
    return (
        f"你刚才发起的工具调用 `{tool_name}` 参数不合法：{detail}。"
        "请严格按工具 schema 重新发起调用（arguments 必须包含必填字段且非空）。"
    )
