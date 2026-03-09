from __future__ import annotations

import mimetypes
import json
import re
import shutil
import tempfile
import uuid
from collections.abc import Generator
from pathlib import Path
from typing import Any
import os
import sys
import subprocess
from importlib import metadata as _importlib_metadata
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from zipfile import ZipFile

from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage


_INSTALL_HINT_RX = re.compile(
    r"(?i)\b("
    r"python\s+-m\s+pip\s+install|"
    r"pip3?\s+install|"
    r"uv\s+pip\s+install|"
    r"npm\s+(?:i|install)\b|"
    r"pnpm\s+(?:i|install)\b|"
    r"yarn\s+add\b|"
    r"bun\s+add\b|"
    r"apt-get\s+install|apt\s+install|"
    r"apk\s+add\b|"
    r"brew\s+install\b|"
    r"choco\s+install\b|"
    r"conda\s+install\b"
    r")\b"
)

_EXEC_HINT_RX = re.compile(
    r"(?i)\b("
    r"run_skill_command|"
    r"python\s+|"
    r"node\s+|"
    r"ffmpeg\s+|"
    r"pdftotext\s+|"
    r"java\s+|"
    r"bash\s+|"
    r"sh\s+|"
    r"powershell\s+|"
    r"pwsh\s+"
    r")\b"
)


def _python_pip_available() -> bool:
    try:
        res = subprocess.run(
            [sys.executable, "-m", "pip", "-V"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
        return res.returncode == 0
    except Exception:
        return False


def _read_text_safe(path: Path, max_chars: int = 20000) -> str:
    try:
        if not path.is_file():
            return ""
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            return (f.read(max_chars) or "").strip()
    except Exception:
        return ""


def _find_node_project_dir(skill_dir: Path, *, max_depth: int = 3) -> Path | None:
    base = skill_dir.resolve()
    base_depth = len(base.parts)
    fallback_lock_dir: Path | None = None

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

    try:
        for root, dirs, files in os.walk(str(base)):
            root_p = Path(root)
            depth = len(root_p.resolve().parts) - base_depth
            if depth > max_depth:
                dirs[:] = []
                continue
            dirs[:] = [d for d in dirs if d not in skip_names]

            if "package.json" in files:
                return root_p
            if "package-lock.json" in files and fallback_lock_dir is None:
                fallback_lock_dir = root_p
    except Exception:
        return None

    return fallback_lock_dir


def _is_skill_metadata_uncertain(*, folder: Path, entry: dict[str, Any]) -> bool:
    openclaw = entry.get("openclaw") if isinstance(entry.get("openclaw"), dict) else {}
    requires = openclaw.get("requires") if isinstance(openclaw.get("requires"), dict) else {}
    req_bins = requires.get("bins") if isinstance(requires.get("bins"), list) else []
    req_any_bins = requires.get("anyBins") if isinstance(requires.get("anyBins"), list) else []
    req_env = requires.get("env") if isinstance(requires.get("env"), list) else []
    install_list = openclaw.get("install") if isinstance(openclaw.get("install"), list) else []
    has_declared_requires = bool(req_bins or req_any_bins or req_env)

    req_txt = folder / "requirements.txt"
    has_requirements_txt = req_txt.is_file()
    node_proj = _find_node_project_dir(folder)
    has_package_json = bool(node_proj and (node_proj / "package.json").is_file())
    has_package_lock = bool(node_proj and (node_proj / "package-lock.json").is_file())
    has_node_modules = bool(node_proj and (node_proj / "node_modules").is_dir())
    has_dist = bool(node_proj and (node_proj / "dist").is_dir())

    skill_md_text = _read_text_safe(folder / "SKILL.md")
    if not skill_md_text:
        return True
    if _INSTALL_HINT_RX.search(skill_md_text) and not install_list:
        return True
    if (has_package_json or has_package_lock) and not has_node_modules and not has_dist:
        return True
    if has_package_json or has_package_lock:
        return False
    if has_declared_requires or has_requirements_txt or install_list:
        return False
    return bool(_EXEC_HINT_RX.search(skill_md_text))


def get_file_content(url: str, timeout: int = 30) -> bytes:
    try:
        req = Request(url, headers={"User-Agent": "dify-plugin-skill/1.0"})
        with urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as e:
        raise RuntimeError(f"文件下载失败: {str(e)}") from e


def get_skills_dir() -> Path:
    root = Path(__file__).resolve().parent.parent
    skills_dir = root / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    return skills_dir


def invalidate_skills_snapshot() -> None:
    root = Path(__file__).resolve().parent.parent
    cache_path = root / "temp" / "skills_snapshot.json"
    try:
        cache_path.unlink()
    except Exception:
        return


def list_skills_sorted() -> list[Path]:
    skills_dir = get_skills_dir()
    folders = [p for p in skills_dir.iterdir() if p.is_dir()]
    folders.sort(key=lambda p: p.stat().st_ctime)
    return folders


def _load_skills_snapshot(skills_dir: Path) -> dict[str, Any]:
    from utils.mini_claw_runtime import build_skills_snapshot

    snapshot = build_skills_snapshot(skills_root=str(skills_dir))
    try:
        root = Path(__file__).resolve().parent.parent
        cache_path = root / "temp" / "skills_snapshot.json"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return snapshot


def _format_skill_line(*, idx: int, folder: Path, entry: dict[str, Any] | None) -> str:
    if not entry:
        return f"{idx}. {folder.name}\n🔴不可用【原因：无法读取技能元数据】"
    status = entry.get("status") if isinstance(entry.get("status"), dict) else {}
    eligible = bool(status.get("eligible")) if isinstance(status, dict) else False
    os_ok = bool(status.get("os_ok")) if isinstance(status, dict) else True
    missing = status.get("missing") if isinstance(status.get("missing"), dict) else {}
    miss_bins = missing.get("bins") if isinstance(missing.get("bins"), list) else []
    miss_any = missing.get("anyBins") if isinstance(missing.get("anyBins"), list) else []
    miss_env = missing.get("env") if isinstance(missing.get("env"), list) else []
    miss_py = missing.get("py") if isinstance(missing.get("py"), list) else []
    miss_js = missing.get("js") if isinstance(missing.get("js"), list) else []
    parts: list[str] = []
    if not os_ok:
        openclaw = entry.get("openclaw") if isinstance(entry.get("openclaw"), dict) else {}
        os_allow = openclaw.get("os") if isinstance(openclaw.get("os"), list) else []
        os_allow_str = ",".join([str(x) for x in os_allow if str(x).strip()])
        parts.append(f"不支持当前系统（仅支持：{os_allow_str or '未声明'}）")
    if miss_bins:
        bins = ",".join([str(x) for x in miss_bins if str(x).strip()])
        parts.append(f"缺少命令：{bins}；需前往 plugin_daemon 安装：{bins}")
    if miss_any:
        any_bins = ",".join([str(x) for x in miss_any if str(x).strip()])
        parts.append(f"缺少任一命令：{any_bins}；需前往 plugin_daemon 安装其中任意一个")
    if miss_env:
        envs = ",".join([str(x) for x in miss_env if str(x).strip()])
        parts.append(f"缺少环境变量：{envs}；请在 Dify 插件/容器环境变量中配置")
    if miss_py:
        libs = ",".join([str(x) for x in miss_py if str(x).strip()])
        parts.append(f"缺少 Python 库：{libs}；可执行“依赖安装”补齐")
    if miss_js:
        tokens = [str(x) for x in miss_js if str(x).strip()]
        if "<package.json>" in tokens:
            parts.append("缺少 package.json；请补齐后再执行“依赖安装”")
        elif "<node_modules>" in tokens and len(tokens) == 1:
            parts.append("缺少 node_modules；可执行“依赖安装”补齐")
        else:
            pkgs = ",".join(tokens)
            parts.append(f"缺少 Node 依赖；可执行“依赖安装”补齐")

    if eligible:
        if _is_skill_metadata_uncertain(folder=folder, entry=entry):
            return f"{idx}. {folder.name}\n🟡不确定【SKILL.md 依赖标准不规范，无法可靠判定依赖是否齐全】"
        return f"{idx}. {folder.name}\n🟢可用"
    if not parts:
        return f"{idx}. {folder.name}\n🔴不可用\n【未满足运行条件；可尝试执行“依赖安装”或联系管理员】"
    blocks = "\n".join([f"【{p}】" for p in parts if p])
    return f"{idx}. {folder.name}\n🔴不可用\n{blocks}"


def _skills_status_text() -> str:
    skills_dir = get_skills_dir()
    folders = list_skills_sorted()
    if not folders:
        return "❌当前没有已存入的技能包。\n"
    snapshot = _load_skills_snapshot(skills_dir)
    skills_list = snapshot.get("skills") if isinstance(snapshot.get("skills"), list) else []
    by_folder: dict[str, dict[str, Any]] = {}
    for s in skills_list:
        if not isinstance(s, dict):
            continue
        folder = str(s.get("folder") or s.get("id") or "").strip()
        if folder:
            by_folder[folder] = s
    lines: list[str] = ["👓当前技能列表："]
    for idx, p in enumerate(folders, start=1):
        lines.append(_format_skill_line(idx=idx, folder=p, entry=by_folder.get(p.name)))
    lines.extend(
        [
            "",
            "说明：",
            "- 🟢可用：依赖已满足，可以直接执行。",
            "- 🟡不确定：依赖声明不规范，无法可靠判断；建议按规范补齐依赖声明。",
            "- Python 依赖：可在本工具中执行“依赖安装”补全（安装到插件自身虚拟环境）。",
            "- Node 依赖：可在本工具中执行“依赖安装”补全（安装到技能目录的 node_modules）。",
            "- 系统级依赖（如 node/npm/ffmpeg 等命令）：请联系管理员在 plugin_daemon 容器中安装后再使用。",
        ]
    )
    return "\n".join(lines) + "\n"


def extract_url_and_name(file_item: Any) -> tuple[str | None, str | None]:
    url = None
    name = None
    if hasattr(file_item, "url"):
        url = getattr(file_item, "url", None)
    if hasattr(file_item, "filename"):
        name = getattr(file_item, "filename", None)
    if hasattr(file_item, "name") and not name:
        name = getattr(file_item, "name", None)
    if isinstance(file_item, dict):
        url = file_item.get("url", url)
        name = file_item.get("filename", name) or file_item.get("name", name)
    return url, name


def infer_ext_from_url(url: str) -> str:
    path = urlparse(url).path
    ext = Path(path).suffix
    return ext if ext else ".zip"


def safe_filename(preferred_name: str | None, fallback_ext: str = ".zip") -> str:
    if preferred_name:
        base = Path(preferred_name).name
        base = re.sub(r"[<>:\"/\\\\|?*]+", "_", base).strip()
        if base:
            return base
    return f"{uuid.uuid4().hex}{fallback_ext}"


def _safe_skill_folder_name(name: str) -> str:
    s = str(name or "").strip()
    s = re.sub(r"[<>:\"/\\\\|?*]+", "-", s)
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-._")
    return s or "skill"


def _is_within_dir(base: Path, target: Path) -> bool:
    try:
        base_resolved = base.resolve()
        target_resolved = target.resolve()
        return base_resolved == target_resolved or base_resolved in target_resolved.parents
    except Exception:
        return False


def _safe_extract_zip(zip_path: Path, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    with ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = info.filename
            if not name:
                continue
            if name.startswith("/") or name.startswith("\\"):
                raise RuntimeError("压缩包包含非法路径")
            target_path = (dest_dir / name).resolve()
            if not _is_within_dir(dest_dir, target_path):
                raise RuntimeError("压缩包包含越权路径")
            if info.is_dir():
                target_path.mkdir(parents=True, exist_ok=True)
                continue
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target_path, "wb") as dst:
                shutil.copyfileobj(src, dst)


def _find_skill_folders(extracted_root: Path) -> list[Path]:
    candidates: list[Path] = []
    for p in extracted_root.iterdir():
        if p.is_dir():
            candidates.append(p)
    if candidates:
        with_skill_md = [p for p in candidates if (p / "SKILL.md").is_file()]
        if with_skill_md:
            return with_skill_md
        if len(candidates) == 1:
            return candidates
        return candidates
    if (extracted_root / "SKILL.md").is_file():
        return [extracted_root]
    return []


class TMTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        command = str(tool_parameters.get("command", "")).strip()
        files_param = tool_parameters.get("files")

        if command in ("查看技能", "查看 技能", "查看"):
            yield self.create_text_message(_skills_status_text())
            return

        if command in ("依赖安装", "安装依赖", "依赖补全", "补全依赖"):
            skills_dir = get_skills_dir()
            snapshot = _load_skills_snapshot(skills_dir)
            skills_list = snapshot.get("skills") if isinstance(snapshot.get("skills"), list) else []

            py_tasks: list[tuple[str, list[str]]] = []
            node_tasks: list[tuple[str, list[str], str]] = []
            for s in skills_list:
                if not isinstance(s, dict):
                    continue
                folder = str(s.get("folder") or s.get("id") or "").strip()
                if not folder:
                    continue
                skill_path = skills_dir / folder
                status = s.get("status") if isinstance(s.get("status"), dict) else {}
                missing = status.get("missing") if isinstance(status.get("missing"), dict) else {}
                os_ok = bool(status.get("os_ok")) if isinstance(status, dict) else True
                miss_bins = missing.get("bins") if isinstance(missing.get("bins"), list) else []
                miss_any = missing.get("anyBins") if isinstance(missing.get("anyBins"), list) else []
                miss_env = missing.get("env") if isinstance(missing.get("env"), list) else []
                miss_py = missing.get("py") if isinstance(missing.get("py"), list) else []
                miss_js = missing.get("js") if isinstance(missing.get("js"), list) else []

                requires_ok = os_ok and (not miss_bins) and (not miss_any) and (not miss_env)
                if not requires_ok:
                    continue

                req = skill_path / "requirements.txt"
                if req.is_file() and miss_py:
                    py_tasks.append((folder, [sys.executable, "-m", "pip", "install", "-r", str(req)]))
                openclaw = s.get("openclaw") if isinstance(s.get("openclaw"), dict) else {}
                install_list = openclaw.get("install") if isinstance(openclaw.get("install"), list) else []
                for spec in install_list:
                    if not isinstance(spec, dict):
                        continue
                    kind = str(spec.get("kind") or "").strip().lower()
                    pkg = str(spec.get("package") or "").strip()
                    if kind == "uv" and pkg:
                        name = re.split(r"\s*[<>=!~]=?\s*|\s+;|\s+@\s+|\s+", pkg, maxsplit=1)[0].strip()
                        name = name.split("[", 1)[0].strip()
                        name = re.sub(r"[^A-Za-z0-9._-]+", "", name)
                        if not name:
                            continue
                        try:
                            _importlib_metadata.version(name)
                            continue
                        except Exception:
                            py_tasks.append((folder, [sys.executable, "-m", "pip", "install", pkg]))

                node_proj = _find_node_project_dir(skill_path)
                if node_proj and (node_proj / "package.json").is_file() and miss_js:
                    lock = node_proj / "package-lock.json"
                    if lock.is_file():
                        node_tasks.append(
                            (folder, ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"], str(node_proj))
                        )
                    else:
                        node_tasks.append(
                            (folder, ["npm", "install", "--ignore-scripts", "--no-audit", "--no-fund"], str(node_proj))
                        )

            install_tasks: list[tuple[str, list[str], str | None]] = []
            if py_tasks and (not _python_pip_available()):
                install_tasks.append(("（Python环境）", [sys.executable, "-m", "ensurepip", "--upgrade"], None))
            for skill, argv in py_tasks:
                install_tasks.append((skill, argv, None))
            for skill, argv, cwd_dir in node_tasks:
                install_tasks.append((skill, argv, cwd_dir))

            if not install_tasks:
                yield self.create_text_message(
                    "😑未发现需要安装的依赖（仅对“缺失 Python/Node 依赖”且已满足 metadata.openclaw.requires 的技能执行）。\n"
                )
                yield self.create_text_message(_skills_status_text())
                return

            ops: list[str] = []
            for skill, argv, _ in install_tasks:
                if (
                    isinstance(argv, list)
                    and len(argv) >= 3
                    and str(argv[1]) == "-m"
                    and str(argv[2]) == "ensurepip"
                ):
                    ops.append("python环境：pip工具安装")
                else:
                    ops.append(f"{skill}：依赖安装")

            yield self.create_text_message(
                "🧩开始安装依赖。\n"
                + "- Python：安装到插件 venv（必要时自动补齐 pip）\n"
                + "- Node：安装到技能目录下的 node_modules\n"
                + "正在进行以下操作：\n"
                + "\n".join([f"- {x}" for x in ops])
                + "\n"
            )

            ok_count = 0
            failed: list[str] = []
            for skill, argv, cwd in install_tasks:
                try:
                    res = subprocess.run(
                        argv,
                        cwd=cwd,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="ignore",
                    )
                except Exception as e:
                    failed.append(f"{skill}: 执行失败 ({e})")
                    continue
                if res.returncode == 0:
                    ok_count += 1
                else:
                    err = (res.stderr or res.stdout or "").strip()
                    failed.append(f"{skill}: 失败 code={res.returncode} {err[:500]}")

            invalidate_skills_snapshot()
            yield self.create_text_message(f"✅依赖安装完成：成功 {ok_count}/{len(install_tasks)}\n")
            if failed:
                yield self.create_text_message("❌失败明细（节选）：\n" + "\n".join(failed) + "\n")
            yield self.create_text_message(_skills_status_text())
            return

        if command in ("新增技能", "存入技能", "保存技能"):
            file_items: list[Any] = []
            if isinstance(files_param, list):
                file_items = [x for x in files_param if x]
            elif files_param:
                file_items = [files_param]
            elif "file" in tool_parameters and tool_parameters["file"]:
                file_items = [tool_parameters["file"]]

            if not file_items:
                yield self.create_text_message("❌未检测到上传的 zip 文件，请提供 files 参数。\n")
                return

            skills_dir = get_skills_dir()
            installed: list[str] = []

            for file_item in file_items:
                url, preferred_name = extract_url_and_name(file_item)
                if not url:
                    yield self.create_text_message("❌无法获取文件URL，请检查入参（files[i].url）。\n")
                    return

                filename_attr = None
                try:
                    filename_attr = getattr(file_item, "filename", None)
                except Exception:
                    filename_attr = None
                if isinstance(file_item, dict):
                    filename_attr = file_item.get("filename", filename_attr)

                try:
                    content = get_file_content(url)
                except Exception as e:
                    yield self.create_text_message(str(e))
                    return

                if filename_attr:
                    filename = Path(filename_attr).name
                else:
                    ext = infer_ext_from_url(url)
                    filename = safe_filename(preferred_name, fallback_ext=ext if ext else ".zip")

                with tempfile.TemporaryDirectory(prefix="skill-upload-") as td:
                    tmp_dir = Path(td)
                    zip_path = tmp_dir / filename
                    try:
                        zip_path.write_bytes(content)
                    except Exception as e:
                        yield self.create_text_message(f"❌保存临时文件失败：{e}\n")
                        return

                    extract_dir = tmp_dir / "extracted"
                    try:
                        _safe_extract_zip(zip_path, extract_dir)
                    except Exception as e:
                        yield self.create_text_message(f"❌解压失败：{e}\n")
                        return

                    skill_folders = _find_skill_folders(extract_dir)
                    if not skill_folders:
                        yield self.create_text_message("❌压缩包内未找到技能目录（应包含 SKILL.md）。\n")
                        return

                    for folder in skill_folders:
                        target_name = folder.name
                        if folder.resolve() == extract_dir.resolve() and (extract_dir / "SKILL.md").is_file():
                            target_name = _safe_skill_folder_name(Path(filename).stem)
                        target = skills_dir / target_name
                        if target.exists():
                            yield self.create_text_message(f"❌技能已存在：{target.name}（请先删除同名技能）\n")
                            return
                        try:
                            shutil.move(str(folder), str(target))
                            installed.append(target.name)
                        except Exception as e:
                            yield self.create_text_message(f"❌安装技能失败：{e}\n")
                            return

            invalidate_skills_snapshot()
            yield self.create_text_message("✅技能已安装：\n" + "\n".join(installed) + "\n")
            yield self.create_text_message(_skills_status_text())
            return

        m_del = re.match(r"^删除技能(\d+)$", command)
        if m_del:
            idx = int(m_del.group(1))
            skills = list_skills_sorted()
            if idx < 1 or idx > len(skills):
                yield self.create_text_message("❌技能序号无效或超出范围。请先使用“查看技能”确认序号。\n")
                return
            target = skills[idx - 1]
            try:
                shutil.rmtree(target, ignore_errors=False)
            except Exception as e:
                yield self.create_text_message(f"❌删除失败：{e}\n")
                return
            invalidate_skills_snapshot()
            yield self.create_text_message(f"✅已删除技能{idx}：{target.name}\n")
            skills = list_skills_sorted()
            if not skills:
                yield self.create_text_message("😑当前技能列表为空。\n")
            else:
                lines = [f"{i + 1}. {p.name}" for i, p in enumerate(skills)]
                yield self.create_text_message("👓当前技能列表：\n" + "\n".join(lines))
            return

        m_dl = re.match(r"^下载技能(\d+)$", command)
        if m_dl:
            idx = int(m_dl.group(1))
            skills = list_skills_sorted()
            if idx < 1 or idx > len(skills):
                yield self.create_text_message("❌技能序号无效或超出范围。请先使用“查看技能”确认序号。\n")
                return
            target = skills[idx - 1]

            try:
                with tempfile.TemporaryDirectory(prefix="skill-zip-") as td:
                    tmp_dir = Path(td)
                    zip_path = tmp_dir / f"{target.name}.zip"
                    shutil.make_archive(str(zip_path.with_suffix("")), "zip", root_dir=target.parent, base_dir=target.name)
                    blob = zip_path.read_bytes()
            except Exception as e:
                yield self.create_text_message(f"❌读取文件失败：{e}\n")
                return

            mime_type, _ = mimetypes.guess_type(f"{target.name}.zip")
            if not mime_type:
                mime_type = "application/zip"

            yield self.create_text_message(f"⬇️开始下载技能{idx}：{target.name}.zip\n")
            yield self.create_blob_message(
                blob=blob,
                meta={
                    "mime_type": mime_type,
                    "filename": f"{target.name}.zip",
                },
            )
            return

        yield self.create_text_message(
            "😑未识别的技能管理命令。支持：查看技能、新增技能、删除技能N、下载技能N、依赖安装。\n"
        )
        return
