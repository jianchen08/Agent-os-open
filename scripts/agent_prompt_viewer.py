#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Agent 提示词拼接查看器（调真实接口版本）

通过 subprocess 隔离调用 `PromptBuildPlugin.execute(ctx)` 来生成与运行时一致的
SystemMessage，并把所有占位符/注入点按来源分组展示为可折叠 section。

行为约束（与实际运行时对齐）：
- folder 类型项目的拼接 base = state["ws_meta"]["path"]（**容器任务工作空间**），
  不是开发机项目目录。默认不传 --workspace 时 ws_meta.path 为空，folder 注入自然为空。
- path 类型项目的拼接 base = state["project_root"]（Agent OS 项目根目录）
- 压缩块（compression_messages）和动态变量（prompt.dynamic_vars）均以 XML 包裹

实现说明：
- 主进程不直接 import pipeline.plugin / plugins.*，避免污染 sys.modules / PYTHONPATH
- 用 subprocess 启动一个隔离 Python 进程（--worker 模式），sys.path 完全干净
- 所有从 `PromptBuildPlugin` 加载的代码都发生在隔离子进程内，确保使用**当前项目根目录**
  下的 plugin.py，不被其他 PYTHONPATH 路径干扰

用法:
    python scripts/agent_prompt_viewer.py
    python scripts/agent_prompt_viewer.py --workspace D:\\path\\to\\wt_xxx

输出:
    docs/agent_prompts_viewer.html
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# ─── 项目根目录定位 ───

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent

AGENTS_DIR = PROJECT_ROOT / "config" / "agents"
OUTPUT_PATH = PROJECT_ROOT / "docs" / "agent_prompts_viewer.html"

# 其他可能干扰的项目根（坚果云同步路径等），会在子进程中从 sys.path 剔除
EXTRA_PROJECT_PATHS = (
    r"D:\Jianguoyun\Agent os",
    r"D:\Jianguoyun\Agent_os",
)


# ─── 实际提示词拼接（通过子进程隔离调用 PromptBuildPlugin） ───


def _build_agent_state(agent_cfg: dict, workspace_path: str) -> dict:
    """根据 Agent YAML 构造与运行时一致的 state 字典。

    Args:
        agent_cfg: 解析后的 Agent YAML（dict）。
        workspace_path: 容器任务工作空间路径（来自 --workspace），用于 folder 注入。

    Returns:
        可直接传给 PromptBuildPlugin.execute 的 state 字典。
    """
    static_def = agent_cfg.get("static_vars") or {}
    dynamic_def = agent_cfg.get("dynamic_vars") or {}
    return {
        "context.system_prompt": agent_cfg.get("system_prompt", "") or "",
        "context.session_id": "viewer-test",
        "context.agent_name": agent_cfg.get("name", ""),
        "context.static_vars": static_def.get("items", []) if static_def.get("enabled", True) else [],
        "context.dynamic_vars": dynamic_def.get("items", []) if dynamic_def.get("enabled", True) else [],
        "constraints": {
            "hard": agent_cfg.get("hard_constraints", []) or [],
            "soft": agent_cfg.get("soft_constraints", []) or [],
        },
        "ws_meta": {"path": workspace_path or ""},
        "project_root": str(PROJECT_ROOT),
        "memory.retrieved": "",
        "messages": [],
        "llm_model": agent_cfg.get("model_name", ""),
        "context_window": 0,
    }


def _run_worker(agent_cfg: dict, workspace_path: str) -> dict:
    """在隔离子进程中调用 PromptBuildPlugin 组装单个 Agent 的提示词。

    用 subprocess 启动 `python -I scripts/agent_prompt_viewer.py --worker ...`，
    `-I` 标志让 Python 进入隔离模式（忽略所有 site-packages、PYTHONPATH、当前目录），
    启动后我们手动重建 sys.path：先加 stdlib 和必要的 site-packages，
    再加当前 PROJECT_ROOT，彻底避免 `agent-pipeline` 这类 `pip install -e` 引入的
    其他项目路径（如 D:\\Jianguoyun\\Agent os\\src）污染。

    Args:
        agent_cfg: 解析后的 Agent YAML（dict）。
        workspace_path: 容器任务工作空间路径。

    Returns:
        包含以下键的 dict：
        - system_message: SystemMessage 文本
        - system_message_raw: SystemMessage 原文
        - compression_messages: 列表，每条独立消息（XML 包裹）
        - dynamic_vars: 动态变量消息（XML 包裹或纯文本）
        - static_items: static_vars.items 列表
        - dynamic_items: dynamic_vars.items 列表
        - workspace_path: 实际使用的 ws_meta.path
    """
    import os
    state = _build_agent_state(agent_cfg, workspace_path)
    payload = {"state": state, "workspace_path": workspace_path or ""}

    # 用 -I 隔离模式：忽略 PYTHONPATH、PYTHONHOME、site、user site、当前目录
    # 启动后由 worker 端 _worker_main() 手动重建 sys.path
    cmd = [
        sys.executable,
        "-I",
        str(SCRIPT_DIR / "agent_prompt_viewer.py"),
        "--worker",
        "--workspace", workspace_path or "",
    ]
    # Agent YAML 内容通过 stdin 传递，避免命令行长度限制
    stdin_data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    # 完全不传 PYTHONPATH：让 -I 隔离模式生效
    env = {
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "TEMP": os.environ.get("TEMP", ""),
        "TMP": os.environ.get("TMP", ""),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    }

    proc = subprocess.run(
        cmd,
        input=stdin_data,
        capture_output=True,
        env=env,
        cwd=str(PROJECT_ROOT),
        timeout=120,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Worker 子进程退出码 {proc.returncode}。stderr: {err[:2000]}"
        )
    try:
        result = json.loads(proc.stdout.decode("utf-8"))
    except json.JSONDecodeError as e:
        out = proc.stdout.decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Worker 输出无法解析为 JSON: {e}\nstdout 前 500 字符: {out[:500]}"
        ) from e
    if not result.get("ok"):
        raise RuntimeError(f"Worker 返回错误: {result.get('error')}")
    return result["data"]


def _safe_static_item_summary(item: dict | str, state: dict) -> dict:
    """提取单个 static_var item 的可展示信息。

    Args:
        item: 单个 static_vars.items 元素（dict 或占位符字符串）。
        state: 完整 state（用于决定 path 解析 base）。

    Returns:
        含 name/type/path/content/resolved_path 的展示字典。
    """
    # 字符串占位符形式：解析 {{rules}} / {{path:xxx}} 等，显示实际注入内容
    if isinstance(item, str):
        file_content = ""
        placeholder = item.strip()
        if placeholder == "{{rules}}":
            # 与 PromptBuildPlugin 一致：拼成 hard/soft 约束列表
            constraints = state.get("constraints") or {}
            parts = []
            for c in constraints.get("hard", []):
                parts.append(f"- [必须] {c}")
            for c in constraints.get("soft", []):
                parts.append(f"- [建议] {c}")
            file_content = "\n".join(parts)
        else:
            m = re.match(r"\{\{path:([^}]+)\}\}", placeholder)
            if m:
                rel = m.group(1).strip()
                p = Path(rel)
                target = p if p.is_absolute() else PROJECT_ROOT / p
                if target.is_file():
                    try:
                        text = target.read_text(encoding="utf-8")
                        file_content = f"<{target.stem}>\n{text}\n</{target.stem}>"
                    except Exception as e:
                        file_content = f"[读取失败: {e}]"
                else:
                    file_content = f"[文件不存在: {target}]"
        return {
            "name": item,
            "type": "placeholder",
            "path": "",
            "resolved_path": "",
            "exists": False,
            "content": "",
            "file_content": file_content,
            "tags": [],
            "extensions": [],
            "route_key": "",
            "routes": {},
        }

    var_type = item.get("type", "")
    var_name = item.get("name", var_type or "未命名")
    raw_path = item.get("path", "")

    # 解析 path 实际指向（path 类型用 project_root，folder 类型用 ws_meta.path）
    resolved = ""
    full_path = ""
    if var_type == "path" and raw_path:
        # 与 PromptBuildPlugin._resolve_path_for_file 一致：相对路径拼 project_root
        p = Path(raw_path)
        full_path = str(p if p.is_absolute() else PROJECT_ROOT / p)
    elif var_type == "folder" and raw_path:
        # 与 PromptBuildPlugin._resolve_target_path 一致：相对路径拼 ws_meta.path
        ws_path = (state.get("ws_meta") or {}).get("path", "")
        p = Path(raw_path)
        if p.is_absolute():
            full_path = str(p)
        elif ws_path:
            full_path = str(Path(ws_path) / raw_path)
    elif var_type == "folder":
        # 留空 path → 整个 ws_meta.path 作为 folder
        ws_path = (state.get("ws_meta") or {}).get("path", "")
        full_path = ws_path

    # 推断文件/目录是否存在
    exists = bool(full_path) and Path(full_path).exists()
    resolved = full_path

    # path/folder 类型：读取实际注入内容（与 PromptBuildPlugin 运行时一致），
    # 让 viewer 直接显示注入的具体文本，而不是只显示元信息。
    # path 类型包裹成 <tag_name>内容</tag_name>，目录/folder 拼接多文件。
    file_content = ""
    if exists and full_path:
        target = Path(full_path)
        try:
            if target.is_file():
                text = target.read_text(encoding="utf-8")
                file_content = f"<{target.stem}>\n{text}\n</{target.stem}>"
            elif target.is_dir():
                # 与 PromptBuildPlugin._read_dir_entries 一致：遍历顶层文件拼接
                exts = tuple(item.get("extensions") or ())
                parts = []
                for child in sorted(target.iterdir()):
                    if not child.is_file():
                        continue
                    if exts and child.suffix not in exts:
                        continue
                    if child.suffix not in (".md", ".yaml", ".yml", ".txt"):
                        continue
                    try:
                        parts.append(f"<{child.stem}>\n{child.read_text(encoding='utf-8')}\n</{child.stem}>")
                    except Exception:
                        pass
                if parts:
                    file_content = f'<files dir="{raw_path}">\n' + "\n".join(parts) + "\n</files>"
        except Exception as e:
            file_content = f"[读取失败: {e}]"

    return {
        "name": var_name,
        "type": var_type or "inline",
        "path": raw_path,
        "resolved_path": resolved,
        "exists": exists,
        "content": item.get("content", ""),
        "file_content": file_content,
        "tags": item.get("tags", []),
        "extensions": item.get("extensions", []),
        "route_key": item.get("route_key", ""),
        "routes": item.get("routes", {}),
    }


# ─── Worker 子进程入口（隔离 sys.path 后调 PromptBuildPlugin） ───


def _worker_main() -> None:
    """Worker 子进程入口：sys.path 隔离后调 PromptBuildPlugin，从 stdin 读 payload。

    只在子进程内执行，stdout 输出 JSON：
        - ok=True:  {"ok": true, "data": {...}}
        - ok=False: {"ok": false, "error": "..."}

    关键：父进程用 `python -I` 启动本子进程，Python 进入隔离模式：
    - 忽略 PYTHONPATH
    - 忽略 site（不加载 site-packages）
    - 不把当前目录加到 sys.path
    - 不加载 user site-packages
    然后我们手动重建 sys.path：
    1. 标准库（基于 sys.base_prefix / sys.exec_prefix / sys.path 启动时的初始值）
    2. 当前 PROJECT_ROOT
    """
    # 标准库路径：基于 Python 安装目录重建
    import asyncio  # noqa: F401  (在 -I 隔离模式下，需要显式 import)
    import site
    import importlib
    import io

    # 强制 stdout/stderr 用 utf-8 编码（-I 隔离模式下 PYTHONIOENCODING 可能失效，
    # Windows 默认 GBK 写入 emoji/中文时会抛 UnicodeEncodeError）
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass

    # 收集 Python 启动时（-I 隔离前）的标准库路径
    # -I 模式启动时 sys.path 只剩 stdlib 路径，不含 site-packages
    stdlib_paths = list(sys.path)  # 隔离模式下这一段是 stdlib

    # 重建 sys.path：stdlib + PROJECT_ROOT
    sys.path[:] = stdlib_paths
    # 把 PROJECT_ROOT 放第一位
    if str(PROJECT_ROOT) in sys.path:
        sys.path.remove(str(PROJECT_ROOT))
    sys.path.insert(0, str(PROJECT_ROOT))

    # 如果代码放在 src/ 下面，把 src 也加入（让 `from pipeline.xxx` / `from plugins.xxx` 工作）
    src_dir = PROJECT_ROOT / "src"
    if src_dir.is_dir():
        if str(src_dir) in sys.path:
            sys.path.remove(str(src_dir))
        sys.path.insert(0, str(src_dir))

    importlib.invalidate_caches()

    try:
        from pipeline.plugin import PluginContext
        from plugins.input.prompt_build import PromptBuildPlugin

        # 断言：确认加载的是当前项目根的代码
        import inspect as _inspect
        loaded_file = _inspect.getfile(PromptBuildPlugin._build_dynamic_vars)
        if not Path(loaded_file).resolve().is_relative_to(PROJECT_ROOT.resolve()):
            raise RuntimeError(
                f"PromptBuildPlugin 加载路径错误: {loaded_file}\n"
                f"期望在 {PROJECT_ROOT} 下"
            )

        raw = sys.stdin.buffer.read()
        payload = json.loads(raw.decode("utf-8"))
        state = payload["state"]

        ctx = PluginContext(state=state)
        plugin = PromptBuildPlugin(config={"include_compressed_layers": True})
        result = asyncio.run(plugin.execute(ctx))
        updates = result.state_updates

        system_msg = updates.get("system_message") or {}
        system_content = system_msg.get("content", "") if isinstance(system_msg, dict) else str(system_msg)

        dynamic_vars_msg = updates.get("prompt.dynamic_vars") or {}
        if isinstance(dynamic_vars_msg, dict):
            dynamic_vars_content = dynamic_vars_msg.get("content", "") or json.dumps(dynamic_vars_msg, ensure_ascii=False)
        else:
            dynamic_vars_content = str(dynamic_vars_msg)

        data = {
            "system_message": system_content,
            "system_message_raw": system_content,
            "compression_messages": list(updates.get("compression_messages", []) or []),
            "dynamic_vars": dynamic_vars_content,
            "static_items": list(state.get("context.static_vars", []) or []),
            "dynamic_items": list(state.get("context.dynamic_vars", []) or []),
            "workspace_path": payload.get("workspace_path", ""),
        }
        sys.stdout.write(json.dumps({"ok": True, "data": data}, ensure_ascii=False))
    except Exception as e:
        import traceback
        sys.stdout.write(json.dumps({
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "traceback": traceback.format_exc(),
        }, ensure_ascii=False))


# ─── 扫描与解析 ───


def scan_agents() -> list[dict]:
    """扫描所有 Agent 配置文件并返回解析结果（仅 YAML 解析，不拼接）。

    Returns:
        包含 name/config_id/category/level/path/yaml_data 的 Agent 列表。
    """
    agents = []
    for yaml_file in AGENTS_DIR.rglob("*.yaml"):
        if ".bak" in yaml_file.name:
            continue
        try:
            with open(yaml_file, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)
        except Exception as e:
            print(f"  [跳过] 解析失败: {yaml_file} - {e}", file=sys.stderr)
            continue
        if not config or not isinstance(config, dict):
            continue

        config_id = config.get("config_id", yaml_file.stem)
        name = config.get("name", config_id)
        category = config.get("category", "uncategorized")
        level = config.get("level", "")
        agent_type = config.get("agent_type", "")
        description = (config.get("description") or "").strip().split("\n")[0][:120]
        rel_path = str(yaml_file.relative_to(PROJECT_ROOT))

        agents.append({
            "config_id": config_id,
            "name": name,
            "category": category,
            "level": level,
            "agent_type": agent_type,
            "description": description,
            "path": rel_path,
            "yaml_data": config,
        })
    agents.sort(key=lambda a: (a["category"], a["config_id"]))
    return agents


def build_all_prompts(agents: list[dict], workspace_path: str) -> list[dict]:
    """对所有 Agent 通过隔离子进程调用 PromptBuildPlugin 生成提示词。

    Args:
        agents: scan_agents() 返回的列表。
        workspace_path: 容器任务工作空间路径。

    Returns:
        每个 agent 附加了 'prompt_data' 字段的列表。
    """
    results = []
    for ag in agents:
        try:
            prompt_data = _run_worker(ag["yaml_data"], workspace_path)
        except Exception as e:
            logger.warning("[%s] PromptBuildPlugin 调用失败: %s", ag["config_id"], e)
            prompt_data = {
                "system_message": f"[生成失败: {e}]",
                "system_message_raw": f"[生成失败: {e}]",
                "compression_messages": [],
                "dynamic_vars": "",
                "static_items": ag["yaml_data"].get("static_vars", {}).get("items", []),
                "dynamic_items": ag["yaml_data"].get("dynamic_vars", {}).get("items", []),
                "workspace_path": workspace_path or "",
            }
        results.append({**ag, "prompt_data": prompt_data})
    return results


# ─── HTML 生成 ───


def _clean_surrogates(s):
    """Remove unpaired surrogate characters."""
    return s.encode('utf-8', errors='replace').decode('utf-8')

def _json_dump(data: object) -> str:
    r"""JSON 序列化 + 防止 <script>/</script> 等标签破坏 HTML。

    问题：JSON 字符串里如果含字面 `<script>` 或 `</script>`，浏览器会把它当作
    HTML 标签处理——`</script>` 提前闭合当前脚本块，`<script>` 开启新的脚本块。
    两者都会导致 viewer 的 JS 失效、点击 agent 无反应。某 agent 的 system_prompt
    里有 Vue 代码示例 `<script setup>...</script>`，历史版本因此完全点不开。

    解法：把 `<` 转义成 JSON unicode 转义 `\u003c`。JS JSON.parse 能正确还原成
    `<`（数据完全无损），而 HTML 解析器看到的是 `\u003cscript>`，绝不会当作标签。
    这是前后端通用、最彻底的防注入方式。
    """
    s = json.dumps(data, ensure_ascii=False, indent=None)
    # 把 < 转义成 \u003c，防止 <script>、</script>、<style> 等任何 HTML 标签
    s = s.replace("<", "\\u003c")
    return s


def _build_inject_map(system_message, yaml_data):
    inject_map = []
    sys_prompt = yaml_data.get("system_prompt", "")
    file_refs = set()
    for m in re.finditer(r'\{\{path:([^}]+)\}\}', sys_prompt):
        file_refs.add(m.group(1).strip())
    sv = yaml_data.get("static_vars", {})
    for item in (sv.get("items", []) if isinstance(sv, dict) else []):
        if isinstance(item, dict) and item.get("type") == "path":
            file_refs.add(item.get("path", ""))
        elif isinstance(item, str):
            for m in re.finditer(r'\{\{path:([^}]+)\}\}', item):
                file_refs.add(m.group(1).strip())
    for raw_path in sorted(file_refs):
        if raw_path.startswith("skills/"):
            continue
        resolved = PROJECT_ROOT / raw_path
        if not resolved.exists():
            continue
        try:
            if resolved.is_file():
                fc = resolved.read_text(encoding="utf-8")
                pos = system_message.find(fc)
                if pos >= 0:
                    inject_map.append({"name": resolved.name, "path": raw_path,
                        "start": pos, "end": pos + len(fc), "size": len(fc)})
            elif resolved.is_dir():
                total, sp = 0, -1
                for f2 in sorted(resolved.rglob("*")):
                    if f2.is_file() and f2.suffix in (".md",".yaml",".yml",".txt"):
                        try:
                            fc2 = f2.read_text(encoding="utf-8")
                            p = system_message.find(fc2)
                            if p >= 0:
                                total += len(fc2)
                                if sp < 0: sp = p
                        except: pass
                if sp >= 0:
                    inject_map.append({"name": str(raw_path).rstrip("/"), "path": raw_path,
                        "start": sp, "end": sp + total, "size": total})
        except: pass
    inject_map.sort(key=lambda x: x["start"])
    return inject_map


def generate_html(agents: list[dict], workspace_path: str) -> str:
    """生成 HTML 查看页面。

    Args:
        agents: build_all_prompts() 返回的列表。
        workspace_path: 实际使用的容器任务工作空间路径。

    Returns:
        完整 HTML 字符串。
    """
    grouped: dict[str, list[dict]] = defaultdict(list)
    for agent in agents:
        grouped[agent["category"]].append(agent)

    # 左侧导航
    nav_items = []
    for category in sorted(grouped.keys()):
        cat_agents = grouped[category]
        cat_label = category.replace("_", " ").title()
        cat_id = f"cat-{category}"
        nav_items.append(
            f"""
            <div class="nav-group">
                <div class="nav-group-header" onclick="toggleGroup('{cat_id}')">
                    <span class="arrow" id="arrow-{cat_id}">▼</span>
                    <span>{cat_label}</span>
                    <span class="count">{len(cat_agents)}</span>
                </div>
                <div class="nav-group-items" id="{cat_id}">
                    {" ".join(
                        f'<div class="nav-item" data-agent="{a["config_id"]}" '
                        f'onclick="showAgent(\'{a["config_id"]}\')">'
                        f'{a["name"]} <span class="level">{a["level"]}</span></div>'
                        for a in cat_agents
                    )}
                </div>
            </div>
        """
        )

    # 转换 agents 为 JSON（去掉 yaml_data 减少体积；静态/动态 items 用 list 形式）
    agents_for_json = {}
    for a in agents:
        pd = a["prompt_data"]
        state_for_view = _build_agent_state(a["yaml_data"], workspace_path)
        static_view = [_safe_static_item_summary(it, state_for_view) for it in pd["static_items"]]
        agents_for_json[a["config_id"]] = {
            "config_id": a["config_id"],
            "name": a["name"],
            "category": a["category"],
            "level": a["level"],
            "agent_type": a["agent_type"],
            "description": a["description"],
            "path": a["path"],
            "system_message": _clean_surrogates(pd["system_message"]),
            "system_message_raw": _clean_surrogates(pd["system_message_raw"]),
            "system_message_length": len(pd["system_message"]),
            "compression_messages": pd["compression_messages"],
            "dynamic_vars": pd["dynamic_vars"],
            "static_items_view": static_view,
            "dynamic_items_view": pd["dynamic_items"],
            "workspace_path": pd["workspace_path"],
            "inject_map": _build_inject_map(pd["system_message"], a["yaml_data"]),
        }
    agents_json = _json_dump(agents_for_json)

    total_agents = len(agents)
    total_prompt_size = sum(len(a["prompt_data"]["system_message"]) for a in agents)
    categories_count = len(grouped)

    workspace_info = (
        f"容器任务工作空间: <code>{workspace_path}</code>"
        if workspace_path
        else "容器任务工作空间: <em>（未指定 — 项目文件注入为空，符合运行时无任务工作空间的行为）</em>"
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Agent 提示词查看器（调真实接口）</title>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
  :root {{
    --bg-primary: #1a1a2e;
    --bg-secondary: #16213e;
    --bg-tertiary: #0f3460;
    --bg-card: #1e2a4a;
    --text-primary: #e0e0e0;
    --text-secondary: #a0a0b0;
    --text-muted: #6c6c80;
    --accent: #4fc3f7;
    --accent-hover: #81d4fa;
    --border: #2a3a5c;
    --nav-hover: #1e3a5f;
    --tag-bg: #2a4a6a;
    --tag-text: #8ac4ff;
    --scrollbar-track: #16213e;
    --scrollbar-thumb: #3a5a8c;
    --warn: #ffb74d;
    --ok: #81c784;
  }}

  * {{ margin: 0; padding: 0; box-sizing: border-box; }}

  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Noto Sans SC', sans-serif;
    background: var(--bg-primary);
    color: var(--text-primary);
    height: 100vh;
    overflow: hidden;
  }}

  .container {{ display: flex; height: 100vh; }}

  .sidebar {{
    width: 280px;
    min-width: 280px;
    background: var(--bg-secondary);
    border-right: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }}

  .sidebar-header {{
    padding: 16px 20px;
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
  }}
  .sidebar-header h1 {{
    font-size: 16px;
    font-weight: 600;
    color: var(--accent);
    margin-bottom: 4px;
  }}
  .sidebar-header .stats {{ font-size: 12px; color: var(--text-muted); }}
  .sidebar-header .ws-info {{
    font-size: 11px;
    color: var(--text-secondary);
    margin-top: 6px;
    line-height: 1.4;
    word-break: break-all;
  }}
  .sidebar-header .ws-info code {{
    background: var(--bg-tertiary);
    padding: 1px 4px;
    border-radius: 3px;
    font-size: 11px;
  }}
  .sidebar-header .ws-info em {{ color: var(--warn); font-style: normal; }}

  .search-box {{
    padding: 8px 12px;
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
  }}
  .search-box input {{
    width: 100%;
    padding: 8px 12px;
    border: 1px solid var(--border);
    border-radius: 6px;
    background: var(--bg-primary);
    color: var(--text-primary);
    font-size: 13px;
    outline: none;
  }}
  .search-box input:focus {{ border-color: var(--accent); }}

  .nav-scroll {{ flex: 1; overflow-y: auto; padding: 4px 0; }}

  .nav-group-header {{
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 8px 16px;
    cursor: pointer;
    font-size: 12px;
    font-weight: 600;
    color: var(--text-secondary);
    text-transform: uppercase;
    letter-spacing: 0.5px;
    user-select: none;
  }}
  .nav-group-header:hover {{ color: var(--text-primary); }}
  .nav-group-header .arrow {{ font-size: 10px; transition: transform 0.2s; }}
  .nav-group-header .arrow.collapsed {{ transform: rotate(-90deg); }}
  .nav-group-header .count {{
    margin-left: auto;
    font-size: 11px;
    background: var(--tag-bg);
    color: var(--tag-text);
    padding: 1px 6px;
    border-radius: 8px;
  }}
  .nav-group-items {{ overflow: hidden; transition: max-height 0.3s ease; }}
  .nav-group-items.collapsed {{ max-height: 0 !important; }}

  .nav-item {{
    padding: 6px 16px 6px 28px;
    cursor: pointer;
    font-size: 13px;
    color: var(--text-secondary);
    transition: all 0.15s;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .nav-item:hover {{ background: var(--nav-hover); color: var(--text-primary); }}
  .nav-item.active {{
    background: var(--bg-tertiary);
    color: var(--accent);
    border-right: 2px solid var(--accent);
  }}
  .nav-item .level {{ font-size: 11px; color: var(--text-muted); margin-left: 6px; }}

  .content {{ flex: 1; display: flex; flex-direction: column; overflow: hidden; }}

  .content-header {{
    padding: 16px 24px;
    border-bottom: 1px solid var(--border);
    background: var(--bg-secondary);
    flex-shrink: 0;
  }}
  .agent-meta {{ display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }}
  .agent-meta .agent-name {{ font-size: 20px; font-weight: 700; color: var(--accent); }}
  .agent-meta .tag {{
    font-size: 11px;
    padding: 3px 10px;
    border-radius: 12px;
    background: var(--tag-bg);
    color: var(--tag-text);
  }}
  .agent-meta .path {{
    font-size: 12px;
    color: var(--text-muted);
    font-family: 'Fira Code', 'Consolas', monospace;
  }}
  .agent-description {{
    margin-top: 8px;
    font-size: 13px;
    color: var(--text-secondary);
    line-height: 1.5;
  }}
  .content-toolbar {{
    display: flex;
    align-items: center;
    gap: 10px;
    margin-top: 8px;
  }}
  .content-toolbar button {{
    padding: 4px 12px;
    border: 1px solid var(--border);
    border-radius: 4px;
    background: var(--bg-card);
    color: var(--text-secondary);
    font-size: 12px;
    cursor: pointer;
  }}
  .content-toolbar button:hover {{ border-color: var(--accent); color: var(--accent); }}
  .content-toolbar button.active {{ background: var(--accent); color: var(--bg-primary); }}
  .content-toolbar .size-info {{
    margin-left: auto;
    font-size: 12px;
    color: var(--text-muted);
  }}

  .content-body {{ flex: 1; overflow-y: auto; padding: 24px; }}

  /* SystemMessage 渲染区 */
  .prompt-content {{
    max-width: 900px;
    margin: 0 auto 24px;
    line-height: 1.7;
    font-size: 14px;
    padding: 16px 20px;
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 6px;
  }}
  .prompt-content.raw {{
    white-space: pre-wrap;
    word-break: break-word;
    font-family: 'Fira Code', 'Consolas', 'Monaco', monospace;
    font-size: 13px;
    line-height: 1.6;
    color: var(--text-secondary);
  }}
  .prompt-content h1 {{
    font-size: 22px; color: var(--accent);
    border-bottom: 1px solid var(--border); padding-bottom: 8px; margin: 24px 0 16px;
  }}
  .prompt-content h2 {{
    font-size: 18px; color: var(--text-primary);
    border-bottom: 1px solid var(--border); padding-bottom: 6px; margin: 20px 0 12px;
  }}
  .prompt-content h3 {{ font-size: 15px; color: var(--text-primary); margin: 16px 0 8px; }}
  .prompt-content h4 {{ font-size: 14px; color: var(--accent-hover); margin: 12px 0 6px; }}
  .prompt-content p {{ margin: 8px 0; }}
  .prompt-content ul, .prompt-content ol {{ margin: 8px 0; padding-left: 24px; }}
  .prompt-content li {{ margin: 4px 0; }}
  .prompt-content code {{
    background: var(--bg-tertiary);
    padding: 2px 6px;
    border-radius: 3px;
    font-size: 13px;
    font-family: 'Fira Code', 'Consolas', monospace;
  }}
  .prompt-content pre {{
    background: var(--bg-primary);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 16px;
    overflow-x: auto;
    margin: 12px 0;
  }}
  .prompt-content pre code {{ background: none; padding: 0; }}
  .prompt-content table {{
    width: 100%; border-collapse: collapse; margin: 12px 0; font-size: 13px;
  }}
  .prompt-content th {{
    background: var(--bg-tertiary); padding: 8px 12px;
    text-align: left; border: 1px solid var(--border); font-weight: 600;
  }}
  .prompt-content td {{ padding: 8px 12px; border: 1px solid var(--border); }}
  .prompt-content blockquote {{
    border-left: 3px solid var(--accent);
    padding-left: 16px;
    margin: 12px 0;
    color: var(--text-secondary);
  }}
  .prompt-content hr {{
    border: none; border-top: 1px solid var(--border); margin: 20px 0;
  }}
  .prompt-content strong {{ color: var(--text-primary); }}
  .prompt-content em {{ color: var(--accent-hover); }}

  /* 折叠区：注入点详情 */
  .inject-section {{
    max-width: 900px;
    margin: 0 auto 16px;
    background: var(--bg-secondary);
    border: 1px solid var(--border);
    border-radius: 6px;
    overflow: hidden;
  }}
  .inject-section-header {{
    padding: 10px 16px;
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 10px;
    user-select: none;
    font-size: 13px;
    font-weight: 600;
    color: var(--accent);
    background: var(--bg-tertiary);
    border-bottom: 1px solid var(--border);
  }}
  .inject-section-header:hover {{ background: var(--nav-hover); }}
  .inject-section-header .arrow {{
    font-size: 10px;
    transition: transform 0.2s;
  }}
  .inject-section-header .arrow.collapsed {{ transform: rotate(-90deg); }}
  .inject-section-header .title {{ flex: 1; }}
  .inject-section-header .count {{
    font-size: 11px;
    color: var(--text-muted);
    font-weight: 400;
  }}
  .inject-section-body {{
    padding: 0;
    max-height: 9999px;
    overflow: hidden;
    transition: max-height 0.3s ease;
  }}
  .inject-section-body.collapsed {{ max-height: 0 !important; }}

  .inject-item {{
    padding: 10px 16px;
    border-top: 1px solid var(--border);
  }}
  .inject-item:first-child {{ border-top: none; }}
  .inject-item-head {{
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 6px;
    font-size: 13px;
  }}
  .inject-item-head .name {{ color: var(--text-primary); font-weight: 600; }}
  .inject-item-head .type-tag {{
    font-size: 10px;
    padding: 1px 6px;
    background: var(--tag-bg);
    color: var(--tag-text);
    border-radius: 8px;
    font-family: 'Fira Code', 'Consolas', monospace;
  }}
  .inject-item-head .path-tag {{
    font-size: 11px;
    color: var(--text-muted);
    font-family: 'Fira Code', 'Consolas', monospace;
  }}
  .inject-item-head .exists-ok {{ color: var(--ok); font-size: 11px; }}
  .inject-item-head .exists-warn {{ color: var(--warn); font-size: 11px; }}

  .inject-item-body {{
    font-size: 12px;
    color: var(--text-secondary);
    line-height: 1.6;
  }}
  .inject-item-body pre {{
    background: var(--bg-primary);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 10px;
    overflow-x: auto;
    font-size: 12px;
    font-family: 'Fira Code', 'Consolas', monospace;
    color: var(--text-secondary);
    white-space: pre-wrap;
    word-break: break-word;
    max-height: 400px;
    overflow-y: auto;
  }}
  .inject-item-body .empty {{
    color: var(--warn);
    font-style: italic;
  }}
  .inject-item-body ul {{
    padding-left: 20px;
    margin: 4px 0;
  }}

  .welcome {{
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    height: 100%;
    color: var(--text-muted);
    text-align: center;
  }}
  .welcome .icon {{ font-size: 64px; margin-bottom: 16px; }}
  .welcome h2 {{ font-size: 20px; color: var(--text-secondary); margin-bottom: 8px; }}
  .welcome p {{ font-size: 14px; }}

  ::-webkit-scrollbar {{ width: 6px; height: 6px; }}
  ::-webkit-scrollbar-track {{ background: var(--scrollbar-track); }}
  ::-webkit-scrollbar-thumb {{ background: var(--scrollbar-thumb); border-radius: 3px; }}
  ::-webkit-scrollbar-thumb:hover {{ background: var(--accent); }}
</style>
</head>
<body>

<div class="container">
  <div class="sidebar">
    <div class="sidebar-header">
      <h1>Agent 提示词查看器</h1>
      <div class="stats">{total_agents} 个 Agent · {categories_count} 个分类 · {total_prompt_size:,} 字符</div>
      <div class="ws-info">{workspace_info}</div>
    </div>
    <div class="search-box">
      <input type="text" id="searchInput" placeholder="搜索 Agent 名称或 ID..." oninput="filterAgents()">
    </div>
    <div class="nav-scroll" id="navScroll">
      {"".join(nav_items)}
    </div>
  </div>

  <div class="content">
    <div id="welcomePage" class="welcome">
      <div class="icon">📋</div>
      <h2>选择一个 Agent 查看提示词</h2>
      <p>从左侧导航栏选择 Agent，查看其通过 PromptBuildPlugin 实际拼接后的提示词</p>
    </div>
    <div id="agentPage" style="display:none; flex-direction:column; height:100%;">
      <div class="content-header">
        <div class="agent-meta">
          <span class="agent-name" id="agentName"></span>
          <span class="tag" id="agentLevel"></span>
          <span class="tag" id="agentType"></span>
          <span class="tag" id="agentCategory"></span>
        </div>
        <div class="path" id="agentPath"></div>
        <div class="agent-description" id="agentDesc"></div>
        <div class="content-toolbar">
          <button id="btnMarkdown" class="active" onclick="setViewMode('markdown')">Markdown 渲染</button>
          <button id="btnRaw" onclick="setViewMode('raw')">原文</button>
          <button onclick="copyPrompt()">复制 SystemMessage</button>
          <span class="size-info" id="sizeInfo"></span>
        </div>
      </div>
      <div class="content-body">
        <div class="prompt-content" id="promptContent"></div>
        <div id="injectSections"></div>
      </div>
    </div>
  </div>
</div>

<script>
const agentsData = {agents_json};

let currentMode = 'markdown';
let currentAgentId = null;

function escapeHtml(s) {{
  return String(s || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}}

function showAgent(agentId) {{
  const agent = agentsData[agentId];
  if (!agent) return;
  currentAgentId = agentId;

  document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
  document.querySelector(`.nav-item[data-agent="${{agentId}}"]`)?.classList.add('active');

  document.getElementById('agentName').textContent = agent.name;
  document.getElementById('agentLevel').textContent = agent.level;
  document.getElementById('agentType').textContent = agent.agent_type;
  document.getElementById('agentCategory').textContent = agent.category;
  document.getElementById('agentPath').textContent = agent.path;
  document.getElementById('agentDesc').textContent = agent.description;
  document.getElementById('sizeInfo').textContent = `${{agent.system_message_length.toLocaleString()}} 字符`;

  renderPrompt(agent.system_message);
  renderInjectSections(agent);

  document.getElementById('welcomePage').style.display = 'none';
  document.getElementById('agentPage').style.display = 'flex';
}}

function renderPrompt(text) {{
  const container = document.getElementById('promptContent');
  const agent = agentsData[currentAgentId];
  const injectMap = (agent && agent.inject_map) ? agent.inject_map : [];

  if (injectMap.length === 0) {{
    if (currentMode === 'markdown') {{
      container.classList.remove('raw');
      container.innerHTML = marked.parse(text || '(空)');
    }} else {{
      container.classList.add('raw');
      container.textContent = text || '(空)';
    }}
    return;
  }}

  var parts = [];
  var lastEnd = 0;
  var sorted = injectMap.slice().sort(function(a,b) {{ return a.start - b.start; }});
  for (var i = 0; i < sorted.length; i++) {{
    var inj = sorted[i];
    if (inj.start > lastEnd) parts.push({{ t:'text', c: text.slice(lastEnd, inj.start) }});
    parts.push({{ t:'inj', n: inj.name, p: inj.path, s: inj.size, c: text.slice(inj.start, inj.end) }});
    lastEnd = inj.end;
  }}
  if (lastEnd < text.length) parts.push({{ t:'text', c: text.slice(lastEnd) }});

  var html = '';
  for (var j = 0; j < parts.length; j++) {{
    var sec = parts[j];
    if (sec.t === 'inj') {{
      html += '<details style="margin:4px 0;border:1px solid var(--border);border-radius:4px">';
      html += '<summary style="padding:4px 10px;cursor:pointer;background:var(--bg-tertiary);font-size:12px;color:var(--accent)">\ud83d\udcc4 ' + escapeHtml(sec.n) + ' <span style="font-size:10px;color:var(--text-secondary)">' + sec.p + ' (' + sec.s.toLocaleString() + ' 字符)</span></summary>';
      html += '<pre style="margin:0;padding:8px;white-space:pre-wrap;font-size:12px;line-height:1.5;border-top:1px solid var(--border)">' + escapeHtml(sec.c) + '</pre>';
      html += '</details>';
    }} else {{
      if (currentMode === 'markdown') html += marked.parse(sec.c);
      else html += '<pre style="margin:0;white-space:pre-wrap;font-size:13px;line-height:1.6">' + escapeHtml(sec.c) + '</pre>';
    }}
  }}
  container.innerHTML = html;
  if (currentMode === 'raw') container.classList.add('raw');
  else container.classList.remove('raw');
}}

function setViewMode(mode) {{
  currentMode = mode;
  document.getElementById('btnMarkdown').classList.toggle('active', mode === 'markdown');
  document.getElementById('btnRaw').classList.toggle('active', mode === 'raw');
  if (currentAgentId) renderPrompt(agentsData[currentAgentId].system_message);
}}

function copyPrompt() {{
  if (!currentAgentId) return;
  const text = agentsData[currentAgentId].system_message;
  navigator.clipboard.writeText(text).then(() => {{
    const btn = event.target;
    const orig = btn.textContent;
    btn.textContent = '已复制 ✓';
    setTimeout(() => btn.textContent = orig, 1500);
  }});
}}

function toggleGroup(groupId) {{
  const items = document.getElementById(groupId);
  const arrow = document.getElementById('arrow-' + groupId);
  if (items.classList.contains('collapsed')) {{
    items.classList.remove('collapsed');
    arrow.classList.remove('collapsed');
  }} else {{
    items.classList.add('collapsed');
    arrow.classList.add('collapsed');
  }}
}}

function toggleSection(secId) {{
  const body = document.getElementById('body-' + secId);
  const arrow = document.getElementById('arrow-' + secId);
  if (body.classList.contains('collapsed')) {{
    body.classList.remove('collapsed');
    arrow.classList.remove('collapsed');
  }} else {{
    body.classList.add('collapsed');
    arrow.classList.add('collapsed');
  }}
}}

function filterAgents() {{
  const query = document.getElementById('searchInput').value.toLowerCase().trim();
  document.querySelectorAll('.nav-item').forEach(item => {{
    const agentId = item.dataset.agent;
    const agent = agentsData[agentId];
    if (!agent) return;
    const match = !query
      || agent.name.toLowerCase().includes(query)
      || agent.config_id.toLowerCase().includes(query)
      || agent.description.toLowerCase().includes(query);
    item.style.display = match ? '' : 'none';
  }});
  document.querySelectorAll('.nav-group').forEach(group => {{
    const visibleItems = group.querySelectorAll('.nav-item:not([style*="display: none"])');
    group.style.display = visibleItems.length > 0 ? '' : 'none';
  }});
}}

function renderStaticItem(item) {{
  const ws = item.resolved_path ? `<span class="path-tag">${{escapeHtml(item.resolved_path)}}</span>` : '';
  const existsBadge = item.resolved_path
    ? (item.exists
        ? '<span class="exists-ok">✓ 存在</span>'
        : '<span class="exists-warn">⚠ 不存在（运行时该注入项为空）</span>')
    : '';
  let body = '';
  if (item.type === 'rules') {{
    body = '<div class="empty">rules 类型 — 内容由 hard_constraints / soft_constraints 拼接，已合并到 system_message 中。</div>';
  }} else if (item.type === 'reference' || item.type === 'content' || item.type === '' || item.type === 'inline') {{
    const content = item.content || '(空)';
    body = `<pre>${{escapeHtml(content)}}</pre>`;
  }} else if (item.type === 'placeholder') {{
    // 字符串占位符（{{rules}} / {{path:xxx}} 等）：显示解析后的注入内容
    if (item.file_content) {{
      body = `<pre>${{escapeHtml(item.file_content)}}</pre>`;
    }} else {{
      body = `<div class="empty">占位符，运行时由 PromptBuildPlugin 解析（当前无内容）</div>`;
    }}
  }} else if (item.type === 'path') {{
    if (!item.resolved_path) {{
      body = '<div class="empty">path 解析失败（project_root 未设置或路径无效）</div>';
    }} else if (!item.exists) {{
      body = `<div class="empty">文件不存在：${{escapeHtml(item.resolved_path)}}</div>`;
    }} else if (item.file_content) {{
      body = `<pre>${{escapeHtml(item.file_content)}}</pre>`;
    }} else {{
      body = `<div class="empty">文件存在但内容为空</div>`;
    }}
  }} else if (item.type === 'folder') {{
    if (!item.resolved_path) {{
      body = '<div class="empty">ws_meta.path 为空 — 容器任务工作空间未指定，folder 注入为空（符合运行时无任务工作空间的行为）</div>';
    }} else if (!item.exists) {{
      body = `<div class="empty">目录不存在：${{escapeHtml(item.resolved_path)}}</div>`;
    }} else if (item.file_content) {{
      body = `<pre>${{escapeHtml(item.file_content)}}</pre>`;
    }} else {{
      body = `<div class="empty">目录存在但无 .md/.yaml/.yml/.txt 文件可注入</div>`;
    }}
  }} else if (item.type === 'tags' || item.type === 'retrieval') {{
    body = `<div>tags: <code>${{escapeHtml((item.tags || []).join(', '))}}</code></div>
            <div class="empty">检索结果在运行时由 memory_service 填充（viewer 离线环境无数据）</div>`;
  }} else if (item.type === 'routed') {{
    body = `<div>route_key: <code>${{escapeHtml(item.route_key)}}</code></div>
            <pre>${{escapeHtml(JSON.stringify(item.routes, null, 2))}}</pre>
            <div class="empty">路由值在运行时由 state["${{escapeHtml(item.route_key)}}"] 决定</div>`;
  }} else {{
    body = `<pre>${{escapeHtml(JSON.stringify(item, null, 2))}}</pre>`;
  }}
  return `
    <div class="inject-item">
      <div class="inject-item-head">
        <span class="name">${{escapeHtml(item.name)}}</span>
        <span class="type-tag">${{escapeHtml(item.type)}}</span>
        ${{ws}}
        ${{existsBadge}}
      </div>
      <div class="inject-item-body">${{body}}</div>
    </div>
  `;
}}

function renderDynamicItem(item) {{
  const type = item.type || 'inline';
  let body = '';
  if (item.content) {{
    body = `<pre>${{escapeHtml(item.content)}}</pre>`;
  }} else if (type === 'timestamp') {{
    body = '<div class="empty">timestamp 类型 — 运行时生成当前时间</div>';
  }} else if (type === 'session') {{
    body = '<div class="empty">session 类型 — 运行时取 context.session_id</div>';
  }} else if (type === 'agent') {{
    body = '<div class="empty">agent 类型 — 运行时取 context.agent_name</div>';
  }} else if (type === 'model') {{
    body = '<div class="empty">model 类型 — 运行时取 llm_model</div>';
  }} else {{
    body = `<pre>${{escapeHtml(JSON.stringify(item, null, 2))}}</pre>`;
  }}
  return `
    <div class="inject-item">
      <div class="inject-item-head">
        <span class="name">${{escapeHtml(item.name || '未命名')}}</span>
        <span class="type-tag">${{escapeHtml(type)}}</span>
      </div>
      <div class="inject-item-body">${{body}}</div>
    </div>
  `;
}}

function renderInjectSections(agent) {{
  const root = document.getElementById('injectSections');
  const sections = [];

  // 静态变量
  if (agent.static_items_view && agent.static_items_view.length) {{
    const secId = `static-${{agent.config_id}}`;
    const items = agent.static_items_view.map(renderStaticItem).join('');
    sections.push(`
      <div class="inject-section">
        <div class="inject-section-header" onclick="toggleSection('${{secId}}')">
          <span class="arrow" id="arrow-${{secId}}">▼</span>
          <span class="title">📦 静态变量注入点（${{agent.static_items_view.length}} 项）</span>
          <span class="count">点击展开</span>
        </div>
        <div class="inject-section-body collapsed" id="body-${{secId}}">
          ${{items}}
        </div>
      </div>
    `);
  }}

  // 动态变量
  if (agent.dynamic_items_view && agent.dynamic_items_view.length) {{
    const secId = `dynamic-${{agent.config_id}}`;
    const items = agent.dynamic_items_view.map(renderDynamicItem).join('');
    sections.push(`
      <div class="inject-section">
        <div class="inject-section-header" onclick="toggleSection('${{secId}}')">
          <span class="arrow" id="arrow-${{secId}}">▼</span>
          <span class="title">🔄 动态变量注入点（${{agent.dynamic_items_view.length}} 项）</span>
          <span class="count">点击展开</span>
        </div>
        <div class="inject-section-body collapsed" id="body-${{secId}}">
          ${{items}}
        </div>
      </div>
    `);
  }}

  // 压缩块
  if (agent.compression_messages && agent.compression_messages.length) {{
    const secId = `comp-${{agent.config_id}}`;
    const items = agent.compression_messages.map((m, i) => `
      <div class="inject-item">
        <div class="inject-item-head">
          <span class="name">压缩块 #${{i + 1}}</span>
          <span class="type-tag">${{escapeHtml(m.name || 'compressed')}}</span>
        </div>
        <div class="inject-item-body"><pre>${{escapeHtml(m.content)}}</pre></div>
      </div>
    `).join('');
    sections.push(`
      <div class="inject-section">
        <div class="inject-section-header" onclick="toggleSection('${{secId}}')">
          <span class="arrow" id="arrow-${{secId}}">▼</span>
          <span class="title">🗜 压缩块（XML 格式，${{agent.compression_messages.length}} 条）</span>
          <span class="count">运行时由 LLMCore._build_messages 拼到 system_message 之后</span>
        </div>
        <div class="inject-section-body collapsed" id="body-${{secId}}">
          ${{items}}
        </div>
      </div>
    `);
  }} else {{
    const secId = `comp-${{agent.config_id}}`;
    sections.push(`
      <div class="inject-section">
        <div class="inject-section-header">
          <span class="arrow">▼</span>
          <span class="title">🗜 压缩块（0 条）</span>
          <span class="count">viewer 离线环境无 pipeline_run_id，无压缩块；运行时由 ChunkService 按预算加载</span>
        </div>
        <div class="inject-section-body"></div>
      </div>
    `);
  }}

  // 动态变量消息
  if (agent.dynamic_vars) {{
    const secId = `dynmsg-${{agent.config_id}}`;
    sections.push(`
      <div class="inject-section">
        <div class="inject-section-header" onclick="toggleSection('${{secId}}')">
          <span class="arrow" id="arrow-${{secId}}">▼</span>
          <span class="title">⚡ 动态变量消息（XML 格式，独立 system 消息）</span>
          <span class="count">运行时由 LLMCore._build_messages 追加到消息列表末尾</span>
        </div>
        <div class="inject-section-body collapsed" id="body-${{secId}}">
          <div class="inject-item-body"><pre>${{escapeHtml(agent.dynamic_vars)}}</pre></div>
        </div>
      </div>
    `);
  }}

  root.innerHTML = sections.join('');
}}

document.addEventListener('DOMContentLoaded', () => {{
  document.querySelectorAll('.nav-group-items').forEach(el => el.classList.remove('collapsed'));
}});
</script>
</body>
</html>"""


# ─── 主流程 ───


def main() -> None:
    """主流程：扫描 Agent → 调用 PromptBuildPlugin → 生成 HTML。"""
    parser = argparse.ArgumentParser(description="Agent 提示词拼接查看器（调真实接口）")
    parser.add_argument(
        "--workspace",
        type=str,
        default="",
        help="容器任务工作空间路径（用于 folder 类型项目文件注入）。不传则为空，folder 注入自然为空。",
    )
    parser.add_argument(
        "--worker",
        action="store_true",
        help="内部标志：作为子进程 Worker 模式运行（从 stdin 读 payload，stdout 输出 JSON）",
    )
    args = parser.parse_args()

    if args.worker:
        # 子进程模式：清 sys.path 后调 PromptBuildPlugin，把结果写到 stdout
        _worker_main()
        return

    # 主进程模式
    import asyncio  # only needed for worker; lazy import keeps main mode clean
    workspace_path = args.workspace.strip()

    print("Agent 提示词拼接查看器（调真实接口）")
    print("=" * 60)
    print(f"项目根目录: {PROJECT_ROOT}")
    print(f"Agent 配置目录: {AGENTS_DIR}")
    print(f"输出文件: {OUTPUT_PATH}")
    print(f"容器任务工作空间: {workspace_path or '(空)'}")
    print()

    print("正在扫描 Agent 配置...")
    agents = scan_agents()
    print(f"  找到 {len(agents)} 个 Agent")

    print("正在通过子进程隔离调用 PromptBuildPlugin 组装提示词...")
    agents = build_all_prompts(agents, workspace_path)
    for agent in agents:
        sys_len = len(agent["prompt_data"]["system_message"])
        comp_n = len(agent["prompt_data"]["compression_messages"])
        dyn = "有" if agent["prompt_data"]["dynamic_vars"] else "无"
        print(
            f"  - [{agent['category']}] {agent['name']} ({agent['config_id']}) "
            f"system={sys_len:,} 字符 | 压缩块={comp_n} | 动态变量={dyn}"
        )

    print()
    print("正在生成 HTML...")
    html = generate_html(agents, workspace_path)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    html_bytes = html.encode("utf-8", errors="replace")
    with open(OUTPUT_PATH, "wb") as f:
        f.write(html_bytes)

    output_size = len(html_bytes)
    print(f"  输出文件: {OUTPUT_PATH}")
    print(f"  文件大小: {output_size:,} 字节")
    print()
    print("完成！")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
    main()
