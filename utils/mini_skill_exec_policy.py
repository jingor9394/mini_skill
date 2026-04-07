from __future__ import annotations

import fnmatch
import os
import sys
from typing import Any

from utils.mini_skill_constants import EXEC_ALLOWED_BINS, EXEC_TRUSTED_DIR_PREFIXES
from utils.mini_skill_exec import _missing_executable_hint, _resolve_executable


def _is_under_prefixes(path: str, prefixes: tuple[str, ...]) -> bool:
    p = str(path or "")
    if not p:
        return False
    norm = os.path.abspath(p)
    for pref in prefixes:
        if norm.startswith(pref):
            return True
    return False


def _is_under_dir(path: str, base_dir: str | None) -> bool:
    if not path or not base_dir:
        return False
    try:
        base = os.path.abspath(base_dir)
        p = os.path.abspath(path)
        return os.path.commonpath([base, p]) == base
    except Exception:
        return False


def _resolve_venv_bin_dirs() -> list[str]:
    candidates: list[str] = []
    for v in [os.environ.get("VIRTUAL_ENV"), os.environ.get("CONDA_PREFIX"), sys.prefix, sys.exec_prefix]:
        p = str(v or "").strip()
        if not p:
            continue
        candidates.append(os.path.abspath(os.path.join(p, "bin")))
        candidates.append(os.path.abspath(os.path.join(p, "Scripts")))
        candidates.append(os.path.abspath(os.path.join(p, "Library", "bin")))
        candidates.append(os.path.abspath(os.path.join(p, "Library", "usr", "bin")))
        candidates.append(os.path.abspath(os.path.join(p, "Library", "mingw-w64", "bin")))
        candidates.append(os.path.abspath(os.path.join(p, "Library", "ucrt64", "bin")))
        candidates.append(os.path.abspath(os.path.join(p, "Library", "clang64", "bin")))
        candidates.append(os.path.abspath(os.path.join(p, "Library", "msys64", "usr", "bin")))
        candidates.append(os.path.abspath(os.path.join(p, "Library", "msys64", "mingw64", "bin")))
        candidates.append(os.path.abspath(os.path.join(p, "Library", "msys64", "ucrt64", "bin")))
        candidates.append(os.path.abspath(os.path.join(p, "Library", "msys64", "clang64", "bin")))

    for v in [os.environ.get("APPDATA"), os.environ.get("LOCALAPPDATA")]:
        p = str(v or "").strip()
        if not p:
            continue
        candidates.append(os.path.abspath(os.path.join(p, "npm")))
    out: list[str] = []
    for c in candidates:
        if c and c not in out:
            out.append(c)
    return out


def _is_trusted_exec_path(path: str, trusted_dir_prefixes: tuple[str, ...]) -> bool:
    resolved_norm = os.path.abspath(str(path or ""))
    if not resolved_norm:
        return False
    if resolved_norm == os.path.abspath(sys.executable):
        return True
    if _is_under_prefixes(resolved_norm, trusted_dir_prefixes):
        return True
    for b in _resolve_venv_bin_dirs():
        if _is_under_dir(resolved_norm, b):
            return True
    return False


def _deny_by_args(exe_name: str, argv: list[str]) -> str | None:
    name = (exe_name or "").strip().lower()
    args = [str(a) for a in (argv or [])]
    if name in {"bash", "sh", "zsh"}:
        if "-c" in args or "--command" in args:
            return "shell -c is not allowed"
        if "-i" in args:
            return "interactive shell is not allowed"
    if name.startswith("python") or name == os.path.basename(sys.executable).lower():
        if "-c" in args:
            return "python -c is not allowed"
    if name == "node":
        if "-e" in args or "--eval" in args or "-p" in args or "--print" in args:
            return "node eval/print mode is not allowed"
    return None


def _normalize_match_path(path: str) -> str:
    p = os.path.abspath(str(path or ""))
    p = p.replace("\\", "/")
    return p.lower()


def _match_any_path_pattern(path: str, patterns: list[str]) -> bool:
    p = _normalize_match_path(path)
    for raw in patterns:
        pat = str(raw or "").strip()
        if not pat:
            continue
        pat_norm = os.path.abspath(pat).replace("\\", "/").lower()
        if fnmatch.fnmatchcase(p, pat_norm):
            return True
    return False


def resolve_and_validate_exec(
    *,
    command: list[str],
    session_dir: str,
    skills_root: str | None = None,
    allow_bins: set[str] | None = None,
    trusted_dir_prefixes: tuple[str, ...] | None = None,
    exec_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not command:
        return {"ok": False, "error": "command must be a non-empty list"}

    allow_bins = allow_bins or set(EXEC_ALLOWED_BINS)
    trusted_dir_prefixes = trusted_dir_prefixes or EXEC_TRUSTED_DIR_PREFIXES

    raw_exe = str(command[0] or "").strip()
    if not raw_exe:
        return {"ok": False, "error": "command must be a non-empty list"}

    if "/" in raw_exe or "\\" in raw_exe:
        return {"ok": False, "error": "path-scoped executables are not allowed"}

    exe = raw_exe
    argv = [str(x) for x in command]
    resolved_exe_path: str | None = None
    override_exe = str((exec_override or {}).get("exe") or "").strip()
    allow_not_in_allowlist = bool((exec_override or {}).get("allow_not_in_allowlist") is True)

    if exe == "python":
        resolved_exe_path = os.path.abspath(sys.executable)
        argv = [resolved_exe_path] + argv[1:]
    else:
        resolved_exe_path = _resolve_executable(exe)
        resolved_norm_for_err = os.path.abspath(str(resolved_exe_path)) if resolved_exe_path else None
        if exe not in allow_bins:
            if not (allow_not_in_allowlist and override_exe and exe == override_exe):
                hint = ""
                if exe in {"top", "df", "free", "ps", "uptime", "vmstat", "iostat"}:
                    hint = "该类系统命令默认被禁用；请改用 get_system_status 获取安全版的负载/内存/磁盘信息。"
                out: dict[str, Any] = {"ok": False, "error": f"command not allowed: {exe}", "hint": hint}
                if resolved_norm_for_err:
                    out["path"] = resolved_norm_for_err
                    out["resolved_exe"] = resolved_norm_for_err
                return out
        if not resolved_exe_path:
            return {
                "ok": False,
                "error": "executable_not_found",
                "exe": exe,
                "hint": _missing_executable_hint(exe),
            }
        argv = [resolved_exe_path] + argv[1:]

    exe_name = os.path.basename(str(resolved_exe_path or "")).lower()
    deny_reason = _deny_by_args(exe_name, argv[1:])
    if deny_reason:
        return {"ok": False, "error": "exec_denied", "detail": deny_reason, "exe": exe}

    resolved_norm = os.path.abspath(str(resolved_exe_path))
    if _is_under_dir(resolved_norm, session_dir):
        return {"ok": False, "error": "exec_denied", "detail": "executable inside session_dir is not allowed"}
    if skills_root and _is_under_dir(resolved_norm, skills_root):
        return {"ok": False, "error": "exec_denied", "detail": "executable inside skills_root is not allowed"}

    return {"ok": True, "argv": argv, "resolved_exe": resolved_norm, "exe": exe}
