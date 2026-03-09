from __future__ import annotations

import os
import json
import platform
import shutil
import time
import subprocess
import sys
import re
import glob
from datetime import datetime, timezone as _dt_timezone
from typing import Any
from importlib import metadata as _importlib_metadata

from utils.mini_claw_exec import (
    _ensure_python_module,
    _skill_contains_python_module,
)
from utils.mini_claw_exec_policy import resolve_and_validate_exec
from utils.mini_claw_constants import EXEC_ALLOWED_BINS
from utils.mini_claw_paths import (
    _normalize_relative_file_path,
    _rewrite_existing_session_files_to_abs,
    _rewrite_out_arg_to_session_dir,
    _rewrite_uploads_paths_to_session_dir,
)
from utils.mini_claw_web_fetch import web_fetch as _web_fetch
from utils.tools import _list_dir, _parse_frontmatter, _parse_frontmatter_rich, _read_text, _safe_join


def _normalize_platform_name() -> str:
    p = (platform.system() or "").strip().lower()
    if p.startswith("darwin") or p.startswith("mac"):
        return "darwin"
    if p.startswith("win"):
        return "win32"
    if p.startswith("linux"):
        return "linux"
    return p or (sys.platform or "").lower()


def _parse_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    s = str(value).strip().lower()
    if s in {"1", "true", "yes", "y", "on"}:
        return True
    if s in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _safe_str_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list) or isinstance(value, tuple):
        out: list[str] = []
        for x in value:
            s = str(x or "").strip()
            if s:
                out.append(s)
        return out
    s = str(value or "").strip()
    return [s] if s else []


def _normalize_os_token(raw: str) -> str:
    s = str(raw or "").strip().lower()
    if not s:
        return ""
    if s in {"darwin", "mac", "macos", "osx"}:
        return "darwin"
    if s in {"linux", "gnu/linux", "gnu"}:
        return "linux"
    if s in {"win32", "windows", "win", "win64", "mswin", "ms-windows", "amd64", "x64"}:
        return "win32"
    return s


def _parse_requirement_names(text: str) -> list[str]:
    names: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-"):
            continue
        if "://" in line or line.startswith("git+"):
            continue
        head = re.split(r"\s*[<>=!~]=?\s*|\s+;|\s+@\s+|\s+", line, maxsplit=1)[0].strip()
        if not head:
            continue
        head = head.split("[", 1)[0].strip()
        head = re.sub(r"[^A-Za-z0-9._-]+", "", head)
        if head:
            names.append(head)
    out: list[str] = []
    seen: set[str] = set()
    for n in names:
        k = n.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(n)
    return out


def _dedup_lower(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for x in items:
        s = str(x or "").strip()
        if not s:
            continue
        k = s.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
    return out


def _parse_package_json_dependencies(text: str) -> list[str]:
    try:
        obj = json.loads(text or "{}")
    except Exception:
        return []
    if not isinstance(obj, dict):
        return []
    deps: list[str] = []
    for key in ("dependencies", "optionalDependencies"):
        v = obj.get(key)
        if isinstance(v, dict):
            for name in v.keys():
                deps.append(str(name or "").strip())
    return _dedup_lower(deps)


def _parse_package_lock_dependencies(text: str) -> list[str]:
    try:
        obj = json.loads(text or "{}")
    except Exception:
        return []
    if not isinstance(obj, dict):
        return []

    deps: list[str] = []
    packages = obj.get("packages")
    if isinstance(packages, dict):
        root_pkg = packages.get("") if isinstance(packages.get(""), dict) else {}
        if isinstance(root_pkg, dict):
            for key in ("dependencies", "optionalDependencies"):
                v = root_pkg.get(key)
                if isinstance(v, dict):
                    for name in v.keys():
                        deps.append(str(name or "").strip())
        return _dedup_lower(deps)

    top = obj.get("dependencies")
    if isinstance(top, dict):
        for name in top.keys():
            deps.append(str(name or "").strip())
    return _dedup_lower(deps)


def _node_modules_has_package(*, node_modules_dir: str, pkg: str) -> bool:
    name = str(pkg or "").strip()
    if not name:
        return False
    rel = name.replace("/", os.sep)
    p = os.path.join(node_modules_dir, rel)
    return os.path.isdir(p)


def _find_node_project_dir(skill_dir: str, *, max_depth: int = 3) -> str | None:
    base = os.path.abspath(skill_dir)
    base_depth = base.rstrip(os.sep).count(os.sep)
    fallback_lock_dir: str | None = None

    skip_names = {
        "node_modules",
        "dist",
        "build",
        ".git",
        "__pycache__",
        ".venv",
        "venv",
        ".mypy_cache",
        ".pytest_cache",
        "temp",
        ".temp",
    }

    for root, dirs, files in os.walk(base):
        depth = os.path.abspath(root).rstrip(os.sep).count(os.sep) - base_depth
        if depth > max_depth:
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs if d not in skip_names]

        if "package.json" in files:
            return root
        if "package-lock.json" in files and fallback_lock_dir is None:
            fallback_lock_dir = root

    return fallback_lock_dir


def build_skills_snapshot(
    *,
    skills_root: str,
    platform_name: str | None = None,
) -> dict[str, Any]:
    now = int(time.time())
    platform_norm = _normalize_os_token(platform_name or "") or _normalize_platform_name()
    skills: list[dict[str, Any]] = []
    source_mtime_max = 0.0

    for folder in sorted(os.listdir(skills_root)):
        skill_dir = os.path.join(skills_root, folder)
        if not os.path.isdir(skill_dir):
            continue
        skill_md = os.path.join(skill_dir, "SKILL.md")
        if not os.path.isfile(skill_md):
            continue
        try:
            mtime = float(os.path.getmtime(skill_md))
            if mtime > source_mtime_max:
                source_mtime_max = mtime
        except Exception:
            pass

        content = _read_text(skill_md, 20000)
        fm = _parse_frontmatter_rich(content)
        display_name = str(fm.get("name") or "").strip() or folder
        description = str(fm.get("description") or "").strip()

        invocation = {
            "user_invocable": _parse_bool(fm.get("user-invocable"), True),
            "disable_model_invocation": _parse_bool(fm.get("disable-model-invocation"), False),
        }

        def _parse_json_obj(raw: Any) -> dict[str, Any]:
            if isinstance(raw, dict):
                return raw
            if isinstance(raw, str):
                s = raw.strip()
                if not s:
                    return {}
                try:
                    obj = json.loads(s)
                    return obj if isinstance(obj, dict) else {}
                except Exception:
                    return {}
            return {}

        meta_obj = fm.get("metadata")
        meta_dict = _parse_json_obj(meta_obj)
        openclaw_meta: dict[str, Any] = {}
        for k in ("openclaw", "clawdbot"):
            v = meta_dict.get(k)
            if isinstance(v, dict):
                openclaw_meta = v
                break
        if not openclaw_meta and isinstance(meta_dict, dict) and isinstance(meta_dict.get("requires"), dict):
            openclaw_meta = meta_dict

        requires = openclaw_meta.get("requires") if isinstance(openclaw_meta, dict) else None
        requires_obj = requires if isinstance(requires, dict) else {}
        required_bins = _safe_str_list(requires_obj.get("bins"))
        any_bins = _safe_str_list(requires_obj.get("anyBins") or requires_obj.get("any_bins"))
        required_env = _safe_str_list(requires_obj.get("env"))
        os_allow = _safe_str_list(openclaw_meta.get("os"))
        always = bool(openclaw_meta.get("always") is True)
        install_specs = openclaw_meta.get("install") if isinstance(openclaw_meta, dict) else None
        install_list = install_specs if isinstance(install_specs, list) else []

        allowed_tools = str(fm.get("allowed-tools") or fm.get("allowed_tools") or "").strip()
        if allowed_tools:
            for m in re.findall(r"Bash\(([^)]+)\)", allowed_tools):
                for entry in str(m or "").split(","):
                    head = str(entry or "").strip()
                    if not head:
                        continue
                    cmd = head.split(":", 1)[0].strip()
                    if cmd and cmd not in required_bins:
                        required_bins.append(cmd)

        missing_py: list[str] = []
        req_txt = os.path.join(skill_dir, "requirements.txt")
        if os.path.isfile(req_txt):
            try:
                req_text = _read_text(req_txt, 100000)
                req_names = _parse_requirement_names(req_text)
                for pkg in req_names[:30]:
                    try:
                        _importlib_metadata.version(pkg)
                    except Exception:
                        missing_py.append(pkg)
            except Exception:
                missing_py = []
            if not any_bins:
                any_bins = ["python", "python3", "py"]

        node_project_dir = _find_node_project_dir(skill_dir)
        pkg_json = os.path.join(node_project_dir, "package.json") if node_project_dir else ""
        lock_path = os.path.join(node_project_dir, "package-lock.json") if node_project_dir else ""
        has_pkg_json = bool(pkg_json) and os.path.isfile(pkg_json)
        has_lock_json = bool(lock_path) and os.path.isfile(lock_path)
        if has_pkg_json or has_lock_json:
            for b in ("node", "npm"):
                if b not in required_bins:
                    required_bins.append(b)

        missing_js: list[str] = []
        if node_project_dir and (has_pkg_json or has_lock_json):
            node_modules_dir = os.path.join(node_project_dir, "node_modules")
            has_node_modules = os.path.isdir(node_modules_dir)
            has_dist = os.path.isdir(os.path.join(node_project_dir, "dist"))
            required_pkgs: list[str] = []
            if has_lock_json:
                required_pkgs = _parse_package_lock_dependencies(_read_text(lock_path, 500000))[:80]
            elif has_pkg_json:
                required_pkgs = _parse_package_json_dependencies(_read_text(pkg_json, 200000))[:80]
            if not has_node_modules and not has_dist:
                if has_lock_json and not has_pkg_json:
                    missing_js = ["<package.json>"]
                else:
                    missing_js = required_pkgs[:] if required_pkgs else ["<node_modules>"]
            elif required_pkgs and has_node_modules:
                for pkg in required_pkgs:
                    if not _node_modules_has_package(node_modules_dir=node_modules_dir, pkg=pkg):
                        missing_js.append(pkg)

        missing_bins = [b for b in required_bins if not shutil.which(b)]
        any_bins_ok = True
        missing_any_bins: list[str] = []
        if any_bins:
            any_bins_ok = any(bool(shutil.which(b)) for b in any_bins)
            if not any_bins_ok:
                missing_any_bins = any_bins[:]
        missing_env = [k for k in required_env if not os.environ.get(k)]

        os_ok = True
        if os_allow:
            allow_norm = {_normalize_os_token(o) for o in os_allow if str(o).strip()}
            os_ok = platform_norm in allow_norm

        eligible = os_ok and not missing_bins and not missing_env and any_bins_ok and not missing_py and not missing_js
        visible = eligible or always

        skills.append(
            {
                "id": folder,
                "folder": folder,
                "name": display_name,
                "description": description,
                "skill_md": f"{folder}/SKILL.md",
                "openclaw": {
                    "always": always,
                    "os": os_allow,
                    "requires": {
                        "bins": required_bins,
                        "anyBins": any_bins,
                        "env": required_env,
                    },
                    "install": install_list,
                },
                "invocation": invocation,
                "status": {
                    "platform": platform_norm,
                    "os_ok": os_ok,
                    "eligible": eligible,
                    "visible": visible and not invocation["disable_model_invocation"],
                    "missing": {
                        "bins": missing_bins,
                        "anyBins": missing_any_bins,
                        "env": missing_env,
                        "py": missing_py,
                        "js": missing_js,
                    },
                },
            }
        )

    return {
        "root": skills_root,
        "generated_at": now,
        "platform": platform_norm,
        "source_mtime_max": source_mtime_max,
        "skills": skills,
    }


class _AgentRuntime:
    def __init__(
        self,
        *,
        skills_root: str | None,
        session_dir: str,
        memory_turns: int,
        skills_snapshot_cache_path: str | None = None,
    ) -> None:
        self.skills_root = skills_root
        self.session_dir = session_dir
        self.memory_turns = memory_turns
        self._skill_files_listed: set[str] = set()
        self._skills_snapshot_cache_path = skills_snapshot_cache_path
        self._skills_snapshot: dict[str, Any] | None = None
        self._skill_name_to_folder: dict[str, str] = {}

    def _resolve_skill_folder(self, skill_name: str) -> tuple[str | None, str | None]:
        raw = str(skill_name or "").strip()
        if not raw:
            return None, "skill_name is empty"
        if not self.skills_root:
            return None, "skills_root not found"
        if os.path.isdir(os.path.join(self.skills_root, raw)):
            return raw, None
        key = raw.lower()
        folder = self._skill_name_to_folder.get(key)
        if folder:
            return folder, None
        self.load_skills_snapshot()
        folder = self._skill_name_to_folder.get(key)
        if folder:
            return folder, None
        return None, "skill not found"

    def load_skills_snapshot(self) -> dict[str, Any]:
        if self._skills_snapshot is not None:
            return self._skills_snapshot
        if not self.skills_root:
            self._skills_snapshot = {"root": None, "skills": []}
            return self._skills_snapshot

        cache_path = str(self._skills_snapshot_cache_path or "").strip() or None
        snapshot = build_skills_snapshot(skills_root=self.skills_root)
        if cache_path:
            try:
                os.makedirs(os.path.dirname(cache_path), exist_ok=True)
                with open(cache_path, "w", encoding="utf-8", newline="\n") as f:
                    f.write(json.dumps(snapshot, ensure_ascii=False))
            except Exception:
                pass

        mapping: dict[str, str] = {}
        skills_list = snapshot.get("skills")
        if isinstance(skills_list, list):
            for s in skills_list:
                if not isinstance(s, dict):
                    continue
                folder = str(s.get("folder") or s.get("id") or "").strip()
                if not folder:
                    continue
                mapping[folder.lower()] = folder
                name = str(s.get("name") or "").strip()
                if name and name.lower() not in mapping:
                    mapping[name.lower()] = folder

        self._skill_name_to_folder = mapping
        self._skills_snapshot = snapshot
        return snapshot

    def load_skills_index(self) -> dict[str, Any]:
        snapshot = self.load_skills_snapshot()
        skills_list = snapshot.get("skills")
        if not isinstance(skills_list, list):
            return {"root": self.skills_root, "skills": []}
        skills: list[dict[str, Any]] = []
        for s in skills_list:
            if not isinstance(s, dict):
                continue
            status = s.get("status") if isinstance(s.get("status"), dict) else {}
            if isinstance(status, dict) and status.get("visible") is False:
                continue
            skills.append(
                {
                    "name": s.get("name") or s.get("folder") or "",
                    "folder": s.get("folder") or s.get("id") or "",
                    "description": s.get("description") or "",
                    "eligible": bool(status.get("eligible")) if isinstance(status, dict) else False,
                    "missing": status.get("missing") if isinstance(status, dict) else {},
                }
            )
        return {"root": self.skills_root, "skills": skills}

    def get_skill_entry(self, skill_name: str) -> dict[str, Any] | None:
        resolved, _ = self._resolve_skill_folder(skill_name)
        if not resolved:
            return None
        snapshot = self.load_skills_snapshot()
        skills_list = snapshot.get("skills")
        if not isinstance(skills_list, list):
            return None
        for s in skills_list:
            if not isinstance(s, dict):
                continue
            folder = str(s.get("folder") or s.get("id") or "").strip()
            if folder == resolved:
                return s
        return None

    def list_skill_files(self, skill_name: str, max_depth: int = 2) -> dict[str, Any]:
        if not self.skills_root:
            return {"error": "skills_root not found"}
        resolved, err = self._resolve_skill_folder(skill_name)
        if not resolved:
            return {"error": err or "skill not found", "skill": skill_name}
        skill_path = _safe_join(self.skills_root, resolved)
        self._skill_files_listed.add(resolved)
        return {"skill": resolved, "entries": _list_dir(skill_path, max_depth=max_depth)}

    def has_listed_skill_files(self, skill_name: str) -> bool:
        resolved, _ = self._resolve_skill_folder(skill_name)
        if not resolved:
            return False
        return resolved in self._skill_files_listed

    def read_skill_file(self, skill_name: str, relative_path: str, max_chars: int = 12000) -> dict[str, Any]:
        if not self.skills_root:
            return {"error": "skills_root not found"}
        resolved, err = self._resolve_skill_folder(skill_name)
        if not resolved:
            return {"error": err or "skill not found", "skill": skill_name}
        skill_path = _safe_join(self.skills_root, resolved)
        file_path = _safe_join(skill_path, relative_path)
        if not os.path.isfile(file_path):
            return {"error": "file not found", "path": relative_path}
        return {"path": file_path, "content": _read_text(file_path, max_chars)}

    def write_temp_file(self, relative_path: str, content: str) -> dict[str, Any]:
        os.makedirs(self.session_dir, exist_ok=True)
        rp = _normalize_relative_file_path(relative_path)
        if not rp:
            return {"error": "invalid relative_path", "relative_path": relative_path}
        try:
            path = _safe_join(self.session_dir, rp)
        except Exception as e:
            return {"error": "invalid relative_path", "relative_path": relative_path, "exception": str(e)}
        if os.path.isdir(path):
            return {"error": "path is a directory", "relative_path": relative_path, "path": path}
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(content or "")
        except Exception as e:
            return {"error": "write failed", "relative_path": relative_path, "path": path, "exception": str(e)}
        return {"path": path, "bytes": len((content or "").encode("utf-8"))}

    def read_temp_file(self, relative_path: str, max_chars: int = 12000) -> dict[str, Any]:
        os.makedirs(self.session_dir, exist_ok=True)
        rp = _normalize_relative_file_path(relative_path)
        if not rp:
            return {"error": "invalid relative_path", "relative_path": relative_path}
        try:
            path = _safe_join(self.session_dir, rp)
        except Exception as e:
            return {"error": "invalid relative_path", "relative_path": relative_path, "exception": str(e)}
        if os.path.isdir(path):
            return {"error": "path is a directory", "relative_path": relative_path, "path": path}
        if not os.path.isfile(path):
            return {"error": "file not found", "relative_path": relative_path}
        try:
            return {"path": path, "content": _read_text(path, max_chars)}
        except Exception as e:
            return {"error": "read failed", "relative_path": relative_path, "path": path, "exception": str(e)}

    def list_temp_files(self, max_depth: int = 4) -> dict[str, Any]:
        os.makedirs(self.session_dir, exist_ok=True)
        return {"session_dir": self.session_dir, "entries": _list_dir(self.session_dir, max_depth=max_depth)}

    def delete_temp_path(self, relative_path: str, recursive: bool = False) -> dict[str, Any]:
        os.makedirs(self.session_dir, exist_ok=True)
        rp = _normalize_relative_file_path(relative_path)
        if not rp:
            return {"error": "invalid relative_path", "relative_path": relative_path}
        try:
            path = _safe_join(self.session_dir, rp)
        except Exception as e:
            return {"error": "invalid relative_path", "relative_path": relative_path, "exception": str(e)}
        if not os.path.exists(path):
            return {"error": "not found", "relative_path": relative_path}
        try:
            if os.path.isdir(path):
                if not recursive:
                    return {"error": "path is a directory", "relative_path": relative_path, "path": path}
                shutil.rmtree(path, ignore_errors=True)
                return {"deleted": True, "path": path, "type": "directory"}
            os.remove(path)
            return {"deleted": True, "path": path, "type": "file"}
        except Exception as e:
            return {"error": "delete failed", "relative_path": relative_path, "path": path, "exception": str(e)}

    def edit_temp_file(
        self,
        relative_path: str,
        old_text: str,
        new_text: str,
        replace_all: bool = False,
        max_bytes: int = 1_000_000,
    ) -> dict[str, Any]:
        os.makedirs(self.session_dir, exist_ok=True)
        rp = _normalize_relative_file_path(relative_path)
        if not rp:
            return {"error": "invalid relative_path", "relative_path": relative_path}
        try:
            path = _safe_join(self.session_dir, rp)
        except Exception as e:
            return {"error": "invalid relative_path", "relative_path": relative_path, "exception": str(e)}
        if not os.path.isfile(path):
            return {"error": "file not found", "relative_path": relative_path}
        try:
            size = os.path.getsize(path)
        except Exception:
            size = 0
        if size > int(max_bytes or 0):
            return {"error": "file too large", "relative_path": relative_path, "bytes": size}
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                original = f.read()
        except Exception as e:
            return {"error": "read failed", "relative_path": relative_path, "path": path, "exception": str(e)}
        if old_text not in original:
            return {"error": "old_text not found", "relative_path": relative_path}
        if replace_all:
            updated = original.replace(old_text, new_text)
        else:
            updated = original.replace(old_text, new_text, 1)
        if updated == original:
            return {"error": "no change", "relative_path": relative_path}
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(updated)
        except Exception as e:
            return {"error": "write failed", "relative_path": relative_path, "path": path, "exception": str(e)}
        return {"path": path, "bytes_before": len(original.encode("utf-8")), "bytes_after": len(updated.encode("utf-8"))}

    def glob_temp_files(self, pattern: str, max_results: int = 200) -> dict[str, Any]:
        os.makedirs(self.session_dir, exist_ok=True)
        p = str(pattern or "").strip()
        if not p:
            return {"error": "invalid pattern"}
        if p.startswith(("/", "\\")):
            return {"error": "absolute patterns are not allowed"}
        base = self.session_dir.replace("\\", "/").rstrip("/")
        glob_pat = f"{base}/{p}"
        results = glob.glob(glob_pat, recursive=True)
        rels: list[str] = []
        for r in results:
            try:
                rel = os.path.relpath(r, self.session_dir).replace("\\", "/")
            except Exception:
                continue
            if rel.startswith(".."):
                continue
            rels.append(rel)
        rels = sorted(list(dict.fromkeys(rels)))
        max_results = int(max_results or 0)
        if max_results < 1:
            max_results = 1
        if max_results > 1000:
            max_results = 1000
        return {"session_dir": self.session_dir, "pattern": p, "matches": rels[:max_results], "count": len(rels)}

    def grep_temp_files(
        self,
        pattern: str,
        glob_pattern: str | None = None,
        max_matches: int = 200,
        max_file_bytes: int = 1_000_000,
    ) -> dict[str, Any]:
        os.makedirs(self.session_dir, exist_ok=True)
        pat = str(pattern or "")
        if not pat:
            return {"error": "invalid pattern"}
        try:
            rx = re.compile(pat)
        except Exception as e:
            return {"error": "invalid regex", "detail": str(e)}
        g = str(glob_pattern or "").strip() or "**/*"
        glob_res = self.glob_temp_files(g, max_results=5000)
        if glob_res.get("error"):
            return glob_res
        matches: list[dict[str, Any]] = []
        for rel in glob_res.get("matches") if isinstance(glob_res.get("matches"), list) else []:
            if not isinstance(rel, str) or not rel:
                continue
            try:
                abs_path = _safe_join(self.session_dir, rel)
            except Exception:
                continue
            if not os.path.isfile(abs_path):
                continue
            try:
                size = os.path.getsize(abs_path)
            except Exception:
                size = 0
            if size > int(max_file_bytes or 0):
                continue
            try:
                with open(abs_path, "r", encoding="utf-8", errors="ignore") as f:
                    for idx, line in enumerate(f, start=1):
                        if rx.search(line):
                            matches.append({"relative_path": rel, "line": idx, "text": line.rstrip("\n")})
                            if len(matches) >= int(max_matches or 0):
                                return {"pattern": pat, "glob": g, "matches": matches, "truncated": True}
            except Exception:
                continue
        return {"pattern": pat, "glob": g, "matches": matches, "truncated": False}

    def web_fetch(
        self,
        url: str,
        extract_mode: str = "markdown",
        max_chars: int = 50000,
    ) -> dict[str, Any]:
        return _web_fetch(url=str(url or ""), extract_mode=str(extract_mode or "markdown"), max_chars=int(max_chars or 0))

    def get_system_status(self) -> dict[str, Any]:
        status: dict[str, Any] = {}
        status["platform"] = platform.platform()
        status["python"] = {"executable": sys.executable, "version": sys.version.splitlines()[0]}
        venv = os.environ.get("VIRTUAL_ENV") or ""
        if venv:
            status["python"]["virtual_env"] = venv
        status["session_dir"] = self.session_dir
        status["skills_root"] = self.skills_root

        try:
            status["time"] = {
                "epoch": int(time.time()),
                "local": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                "utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
            }
        except Exception:
            status["time"] = {}

        try:
            if hasattr(os, "getloadavg"):
                la = os.getloadavg()
                status["loadavg"] = {"1m": float(la[0]), "5m": float(la[1]), "15m": float(la[2])}
        except Exception:
            status["loadavg"] = {}

        mem: dict[str, Any] = {}
        try:
            if os.path.isfile("/proc/meminfo"):
                with open("/proc/meminfo", "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        if ":" not in line:
                            continue
                        k, v = line.split(":", 1)
                        key = k.strip()
                        val = v.strip()
                        if not key or not val:
                            continue
                        m = re.search(r"(\d+)", val)
                        if not m:
                            continue
                        mem[key] = int(m.group(1)) * 1024
        except Exception:
            mem = {}
        if mem:
            total = int(mem.get("MemTotal") or 0)
            avail = int(mem.get("MemAvailable") or 0)
            if total:
                status["memory"] = {
                    "total_bytes": total,
                    "available_bytes": avail,
                    "used_bytes_est": max(0, total - avail) if avail else None,
                }
        else:
            status["memory"] = {}

        disks: dict[str, Any] = {}
        for label, p in [("root", "/"), ("session_dir", self.session_dir)]:
            try:
                du = shutil.disk_usage(p)
                disks[label] = {"path": p, "total": du.total, "used": du.used, "free": du.free}
            except Exception:
                continue
        status["disk"] = disks

        try:
            status["pid"] = os.getpid()
        except Exception:
            status["pid"] = None
        try:
            status["is_docker"] = bool(os.path.exists("/.dockerenv"))
        except Exception:
            status["is_docker"] = None
        return status

    def get_current_time(self, timezone: str | None = None) -> dict[str, Any]:
        epoch = int(time.time())
        server_local = ""
        utc = ""
        try:
            server_local = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        except Exception:
            server_local = ""
        try:
            utc = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        except Exception:
            utc = ""

        tz_raw = str(timezone or "").strip()
        tz_norm = tz_raw
        if tz_norm in {"上海", "北京", "中国", "北京时间"}:
            tz_norm = "Asia/Shanghai"
        if tz_norm.upper() in {"UTC", "GMT"}:
            tz_norm = "UTC"

        tz_time = ""
        tz_offset_minutes: int | None = None
        tz_valid = False
        if tz_norm:
            try:
                try:
                    from zoneinfo import ZoneInfo  # type: ignore
                except Exception:
                    ZoneInfo = None  # type: ignore
                if ZoneInfo is not None:
                    z = ZoneInfo(tz_norm)
                    dt = datetime.now(tz=z)
                    tz_time = dt.strftime("%Y-%m-%d %H:%M:%S")
                    off = dt.utcoffset()
                    if off is not None:
                        tz_offset_minutes = int(off.total_seconds() // 60)
                    tz_valid = True
            except Exception:
                tz_valid = False

        if not tz_time:
            try:
                dt_utc = datetime.fromtimestamp(epoch, tz=_dt_timezone.utc)
                tz_time = dt_utc.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                tz_time = utc

        return {
            "epoch": epoch,
            "server_local": server_local,
            "utc": utc,
            "timezone": tz_norm or "",
            "tz_time": tz_time,
            "tz_offset_minutes": tz_offset_minutes,
            "tz_valid": tz_valid,
        }

    def get_session_context(self) -> dict[str, Any]:
        return {
            "skills_root": self.skills_root,
            "session_dir": self.session_dir,
        }

    def run_skill_command(
        self,
        *,
        skill_name: str,
        command: list[str],
        cwd_relative: str | None = None,
        auto_install: bool = False,
        exec_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.skills_root:
            return {"error": "skills_root not found"}
        if not command:
            return {"error": "command must be a non-empty list"}
        resolved, err = self._resolve_skill_folder(skill_name)
        if not resolved:
            return {"error": err or "skill not found", "skill": skill_name}
        skill_path = _safe_join(self.skills_root, resolved)
        exe = command[0]
        if exe == "python":
            if "-m" in command:
                module_index = command.index("-m") + 1
                if module_index < len(command):
                    module_name = command[module_index]
                    if not _skill_contains_python_module(skill_path, str(module_name)):
                        return {
                            "error": "no_executable_found",
                            "skill": resolved,
                            "reason": "python -m module not found in skill folder",
                            "module": str(module_name),
                        }
                    module_check = _ensure_python_module(str(module_name), auto_install=auto_install, cwd=self.session_dir)
                    if not module_check.get("ok"):
                        return module_check
        allow_bins = set(EXEC_ALLOWED_BINS)
        try:
            skill_md = os.path.join(skill_path, "SKILL.md")
            if os.path.isfile(skill_md):
                fm = _parse_frontmatter_rich(_read_text(skill_md, 12000))
                allowed_tools = str(fm.get("allowed-tools") or fm.get("allowed_tools") or "").strip()
                if allowed_tools:
                    for m in re.findall(r"Bash\(([^)]+)\)", allowed_tools):
                        for entry in str(m or "").split(","):
                            head = str(entry or "").strip()
                            if not head:
                                continue
                            cmd = head.split(":", 1)[0].strip()
                            if cmd:
                                allow_bins.add(cmd)
        except Exception:
            pass

        exec_check = resolve_and_validate_exec(
            command=command,
            session_dir=self.session_dir,
            skills_root=self.skills_root,
            allow_bins=allow_bins,
            exec_override=exec_override,
        )
        if not exec_check.get("ok"):
            return exec_check
        command = exec_check.get("argv") if isinstance(exec_check.get("argv"), list) else command
        command = _rewrite_uploads_paths_to_session_dir(command, session_dir=self.session_dir)
        command = _rewrite_existing_session_files_to_abs(command, session_dir=self.session_dir)
        command = _rewrite_out_arg_to_session_dir(command, session_dir=self.session_dir)
        try:
            cwd = skill_path if not cwd_relative else _safe_join(skill_path, cwd_relative)
        except Exception as e:
            return {"error": "invalid cwd_relative", "cwd_relative": cwd_relative, "exception": str(e)}
        if not os.path.isdir(cwd):
            return {"error": "cwd_not_found", "cwd": cwd, "cwd_relative": cwd_relative}
        try:
            env = os.environ.copy()
            env["SKILL_AGENT_SESSION_DIR"] = self.session_dir
            env["SKILL_AGENT_UPLOADS_DIR"] = os.path.join(self.session_dir, "uploads")
            env["SKILL_AGENT_SKILLS_ROOT"] = self.skills_root
            env["SKILL_AGENT_SKILL_ID"] = resolved
            env["SKILL_AGENT_SKILL_DIR"] = skill_path
            env["SKILL_AGENT_CWD"] = cwd
            if exe == "python":
                prev = env.get("PYTHONPATH") or ""
                extra = skill_path
                env["PYTHONPATH"] = (extra + (os.pathsep + prev if prev else "")).strip()
            result = subprocess.run(
                command,
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                env=env,
            )
            return {"returncode": result.returncode, "stdout": result.stdout.strip(), "stderr": result.stderr.strip()}
        except FileNotFoundError as e:
            return {"error": "executable_not_found", "exe": str(command[0] or exe), "exception": str(e)}
        except Exception as e:
            return {"error": "subprocess_failed", "exe": str(command[0] or exe), "exception": str(e)}

    def run_temp_command(
        self,
        *,
        command: list[str],
        cwd_relative: str | None = None,
        auto_install: bool = False,
        exec_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not command:
            return {"error": "command must be a non-empty list"}
        exe = command[0]
        if exe == "python":
            if "-m" in command:
                module_index = command.index("-m") + 1
                if module_index < len(command):
                    module_name = command[module_index]
                    module_check = _ensure_python_module(str(module_name), auto_install=auto_install, cwd=self.session_dir)
                    if not module_check.get("ok"):
                        return module_check
        exec_check = resolve_and_validate_exec(
            command=command,
            session_dir=self.session_dir,
            skills_root=self.skills_root,
            exec_override=exec_override,
        )
        if not exec_check.get("ok"):
            return exec_check
        command = exec_check.get("argv") if isinstance(exec_check.get("argv"), list) else command
        command = _rewrite_uploads_paths_to_session_dir(command, session_dir=self.session_dir)
        command = _rewrite_existing_session_files_to_abs(command, session_dir=self.session_dir)
        os.makedirs(self.session_dir, exist_ok=True)
        try:
            cwd = self.session_dir if not cwd_relative else _safe_join(self.session_dir, cwd_relative)
        except Exception as e:
            return {"error": "invalid cwd_relative", "cwd_relative": cwd_relative, "exception": str(e)}
        if not os.path.isdir(cwd):
            return {"error": "cwd_not_found", "cwd": cwd, "cwd_relative": cwd_relative}
        try:
            env = os.environ.copy()
            env["SKILL_AGENT_SESSION_DIR"] = self.session_dir
            env["SKILL_AGENT_UPLOADS_DIR"] = os.path.join(self.session_dir, "uploads")
            env["SKILL_AGENT_CWD"] = cwd
            result = subprocess.run(
                command,
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                env=env,
            )
            return {"returncode": result.returncode, "stdout": result.stdout.strip(), "stderr": result.stderr.strip()}
        except FileNotFoundError as e:
            return {"error": "executable_not_found", "exe": str(command[0] or exe), "exception": str(e)}
        except Exception as e:
            return {"error": "subprocess_failed", "exe": str(command[0] or exe), "exception": str(e)}

    def export_temp_file(
        self,
        *,
        temp_relative_path: str,
        workspace_relative_path: str,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        os.makedirs(self.session_dir, exist_ok=True)
        rp = _normalize_relative_file_path(temp_relative_path)
        if not rp:
            return {"error": "invalid temp_relative_path", "temp_relative_path": temp_relative_path}
        try:
            src = _safe_join(self.session_dir, rp)
        except Exception as e:
            return {"error": "invalid temp_relative_path", "temp_relative_path": temp_relative_path, "exception": str(e)}
        if os.path.isdir(src):
            return {"error": "source path is a directory", "temp_relative_path": temp_relative_path, "source": src}
        if not os.path.isfile(src):
            return {"error": "source file not found", "temp_relative_path": temp_relative_path}
        return {
            "source": src,
            "relative_path": temp_relative_path,
            "bytes": os.path.getsize(src),
            "note": "export_temp_file does not copy files; tool marks final output only",
            "requested_name": workspace_relative_path,
            "overwrite": overwrite,
        }
