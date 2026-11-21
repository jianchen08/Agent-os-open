#!/usr/bin/env python
"""async 内同步阻塞调用检查器（AST 静态扫描 + 逐项基线棘轮）。

背景（真源 docs/working/fs_tools同族阻塞_同类审计与修复方案_20261116.md）：
装机日志实证 cohost watchdog 停滞 ×4 + ``response channel closed`` ×13，
根因 = ``async def`` 函数体内做同步文件操作（resolve/realpath + 真实 I/O），
直接跑在合宿宿主事件循环线程。SDK ``_invoke_callable`` 只对**同步函数**下放
线程池，async-def-with-sync-body 是盲区——本检查器把该盲区焊成 CI 门禁。

机制（承接 mypy-baseline / file-length-baseline 同款纪律）：
- 扫描 ``plugins/shared`` 与 ``plugins/sdk/src`` 下所有 ``async def`` 函数体，
  检测同步阻塞调用（文件 I/O 原语 + shutil/subprocess/time.sleep/os.* 家族）；
- ``.github/async-blocking-baseline.txt`` 存放现存违规清单（逐项
  ``file::func::call::line``）；**不在基线内的新增违规 → exit 1 爆红**
  （stderr 列出新增项）；基线只减不增（存量清理后应同步收缩基线，
  ``--baseline-monotonic`` 对照 base 提交执法账本棘轮）；
- 输出 ``file::func::call::line`` 清单；test_*.py 与 tests/** 目录排除
  （pytest 独立进程，不在 cohost 事件循环）。

启发式收窄（与方案文档 §一 审计口径同源）：
- 文件 I/O 原语的**方法调用形态**只在接收者为路径形态时才计（路径形态
  变量名 / ``Path(...)`` 调用形态 / 函数内从上述形态传播的绑定）——
  ``dict.copy`` / ``str.replace`` / ``config.get`` 等同名噪声必须排除；
- 模块级调用形态（``os.listdir`` / ``shutil.*`` / ``subprocess.*`` /
  ``time.sleep`` / 内建 ``open``）无条件计——模块名即语义，无接收者歧义；
- ``await asyncio.to_thread(...)`` / ``await run_blocking(...)`` 的参数子树
  不下探（下放线程池是合法出口，SDK run_blocking 为推荐习语）；
- 嵌套**同步** def / lambda 不下探（本仓既有习语：扫描体提取同步函数 +
  to_thread 承载，如 ``_sync_move`` / ``_search_sync``）；嵌套 ``async def``
  照常下探（await 时仍在事件循环线程执行）。

用法：
    python scripts/check_async_blocking.py                 # 执法（对照基线）
    python scripts/check_async_blocking.py --generate      # 生成/重写基线
    python scripts/check_async_blocking.py --baseline-monotonic  # 账本棘轮 + 执法

门禁归属：run_gates.py 的 async-blocking gate（fast，CI 侧带
--baseline-monotonic，经 ci.yml traceability-gate job 调度）。
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE_FILE = ROOT / ".github" / "async-blocking-baseline.txt"

# 扫描面（现状契约，见模块 docstring）：async 工具/插件 handler 的两个生产根
SCAN_ROOTS = ("plugins/shared", "plugins/sdk/src")

# 排除目录：依赖/构建产物/缓存 + tests/**（pytest 独立进程不在事件循环）；
# .verify_scratch = plugin_verify staging 的临时验证产物树（untracked，非产品面）
EXCLUDED_DIRS = frozenset(
    {
        "__pycache__",
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "tests",
        "test",
        "build",
        "dist",
        "site-packages",
        ".zctmp",
        ".ai_workspaces",
        ".pytest_cache",
        ".mypy_cache",
        ".verify_scratch",
    }
)

# 排除文件名形态：测试文件（同 file-length 的 Python 测试判别口径）
TEST_FILE_SUFFIXES = ("_test.py",)


def is_test_file(name: str) -> bool:
    """测试文件判别：test_*.py / *_test.py / conftest.py。"""
    return name.startswith("test_") or name.endswith(TEST_FILE_SUFFIXES) or name == "conftest.py"


# ── 原语集 ────────────────────────────────────────────────────────────

# 内建阻塞调用（无条件计）
BUILTIN_PRIMITIVES = frozenset({"open"})

# os / os.path 模块级阻塞调用（点路径精确匹配，无条件计）
OS_PRIMITIVES = frozenset(
    {
        "os.listdir",
        "os.scandir",
        "os.walk",
        "os.stat",
        "os.lstat",
        "os.remove",
        "os.unlink",
        "os.mkdir",
        "os.makedirs",
        "os.rmdir",
        "os.removedirs",
        "os.rename",
        "os.replace",
        "os.truncate",
        "os.chmod",
        "os.chown",
        "os.link",
        "os.symlink",
    }
)
OS_PATH_PRIMITIVES = frozenset(
    {
        "os.path.exists",
        "os.path.lexists",
        "os.path.isfile",
        "os.path.isdir",
        "os.path.islink",
        "os.path.getsize",
        "os.path.getmtime",
        "os.path.getctime",
        "os.path.realpath",
        "os.path.samefile",
    }
)

# 模块级无条件计的调用根前缀：shutil.* / subprocess.* 整模块
MODULE_PREFIX_PRIMITIVES = ("shutil.", "subprocess.")
# 模块级单点：time.sleep / glob.glob
MODULE_EXACT_PRIMITIVES = frozenset({"time.sleep", "glob.glob", "glob.iglob"})

# 路径形态接收者上的阻塞方法（Path 及同形态对象；接收者必须路径形态）
PATH_METHOD_PRIMITIVES = frozenset(
    {
        "resolve",
        "exists",
        "is_file",
        "is_dir",
        "is_symlink",
        "is_junction",
        "stat",
        "lstat",
        "read_text",
        "write_text",
        "read_bytes",
        "write_bytes",
        "mkdir",
        "rmdir",
        "unlink",
        "iterdir",
        "glob",
        "rglob",
        "walk",
        "scandir",
        "touch",
        "chmod",
        "rename",
        "samefile",
        "open",
    }
)

# 文件句柄方法（接收者必须来自 with open(...) 绑定）：open 已由内建/方法原语
# 计一次，句柄上的读写是同一阻塞窗口的延续
HANDLE_METHOD_PRIMITIVES = frozenset({"read", "write", "seek", "truncate", "flush", "readline", "readlines"})

# 合法出口：参数子树不下探（下放线程池）。run_blocking = SDK 推荐习语
# （经 from agentos_plugin_sdk import run_blocking 等别名导入后按后缀识别）。
OFFLOAD_EXACT = frozenset({"asyncio.to_thread"})
OFFLOAD_SUFFIX = "run_blocking"

# 路径形态变量名启发：覆盖本仓既有命名（file_path/root/target/candidate/
# workspace_path/search_path/plugin_jsons/full_path/resolved/…）
PATH_NAME_RE = re.compile(
    r"path|file|dir|folder|root|target|candidate|entry|base|anchor|"
    r"workspace|worktree|mount|resolved|realpath|canonical|upload",
    re.IGNORECASE,
)

# 调用名返回路径形态值的启发（_resolve_target / _validate_path_in_workspace /
# resolve_uploads_url / _anchor 等）
CALL_PATH_NAME_RE = re.compile(r"resolve|path|anchor|abspath|realpath|canonical|normalize", re.IGNORECASE)

# 容器收纳路径形态值的绑定方法（plugin_jsons.append(Path(...)) → 容器路径形态）
CONTAINER_ADD_METHODS = frozenset({"append", "extend", "insert", "add"})


@dataclass(frozen=True)
class Violation:
    """一处 async 内同步阻塞调用。"""

    file: str  # 仓库相对 posix 路径
    func: str  # 函数限定名（类方法带类前缀）
    call: str  # 调用形态渲染（os.walk / file_path.resolve / open / via:helper）
    line: int
    # 调用图穿透命中（call 形如 "via:helper"）时的 helper 内原语展示面：
    # entry 呈现「async 函数 → helper → 原语」完整链路，普通命中两字段为空。
    via_call: str = ""
    via_line: int = 0

    @property
    def entry(self) -> str:
        """基线/输出条目：file::func::call::line（穿透 = file::func::via:helper::原语::helper内行号）。"""
        if self.call.startswith("via:"):
            return f"{self.file}::{self.func}::{self.call}::{self.via_call}::{self.via_line}"
        return f"{self.file}::{self.func}::{self.call}::{self.line}"

    @property
    def key(self) -> str:
        """基线匹配键（行号/helper 内原语形态不参与——随编辑漂移，键必须稳定）。"""
        return f"{self.file}::{self.func}::{self.call}"


# ── 模块级导入别名解析 ────────────────────────────────────────────────


class _AliasMap:
    """import 别名 → 完整点路径（import os as _os / from os.path import exists）。"""

    def __init__(self, tree: ast.Module) -> None:
        self._by_local: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    local = alias.asname or alias.name.split(".")[0]
                    self._by_local[local] = alias.name if alias.asname else alias.name.split(".")[0]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                for alias in node.names:
                    local = alias.asname or alias.name
                    self._by_local[local] = f"{node.module}.{alias.name}"

    def resolve(self, name: str) -> str:
        """本地名 → 导入源点路径（未知名原样返回）。"""
        return self._by_local.get(name, name)


def _module_dotted(node: ast.expr, aliases: _AliasMap) -> str | None:
    """若表达式是「模块名.属性…」纯链，返回完整点路径，否则 None。"""
    parts: list[str] = []
    cur: ast.expr = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if not isinstance(cur, ast.Name):
        return None
    parts.append(aliases.resolve(cur.id))
    return ".".join(reversed(parts))


def _call_name(func: ast.expr) -> str:
    """调用可执行体的名字（Name id 或 Attribute 末段；其余空串）。"""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _is_path_constructor(call: ast.Call, aliases: _AliasMap) -> bool:
    """Path(...) 构造调用（含 pathlib.Path / from pathlib import Path 别名）。"""
    if isinstance(call.func, ast.Name):
        return call.func.id == "Path" or aliases.resolve(call.func.id).endswith(".Path")
    if isinstance(call.func, ast.Attribute):
        dotted = _module_dotted(call.func, aliases)
        return dotted is not None and dotted.endswith(".Path")
    return False


def _is_offload(dotted: str | None) -> bool:
    """asyncio.to_thread / run_blocking（含模块路径别名形态）。"""
    if dotted is None:
        return False
    return dotted in OFFLOAD_EXACT or dotted == OFFLOAD_SUFFIX or dotted.endswith(f".{OFFLOAD_SUFFIX}")


def _is_open_call(node: ast.expr, aliases: _AliasMap) -> bool:
    """表达式是否文件 open(...) 调用（内建 open 或路径形态对象 .open 方法）。

    收窄意图：urllib opener.open 等**非文件** open 名同形调用不得进句柄
    传播（本检查器原语面 = 文件/进程 I/O，网络阻塞不在任务书原语集）。
    """
    if not isinstance(node, ast.Call):
        return False
    if isinstance(node.func, ast.Name):
        return node.func.id == "open" and aliases.resolve(node.func.id) == "open"
    if isinstance(node.func, ast.Attribute):
        return node.func.attr == "open" and _recv_pathish_static(node.func.value, aliases)
    return False


def _recv_pathish_static(node: ast.expr, aliases: _AliasMap) -> bool:
    """无作用域信息的接收者路径形态判断（Path 构造/路径形态名/属性名）。"""
    if isinstance(node, ast.Name):
        return bool(PATH_NAME_RE.search(node.id))
    if isinstance(node, ast.Attribute):
        return bool(PATH_NAME_RE.search(node.attr)) or _recv_pathish_static(node.value, aliases)
    if isinstance(node, ast.Call):
        return _is_path_constructor(node, aliases)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _recv_pathish_static(node.left, aliases) or _recv_pathish_static(node.right, aliases)
    return False


def _recv_text(node: ast.expr, aliases: _AliasMap) -> str:
    """接收者紧凑渲染（Name / 短属性链 / Path(...)；其余 <expr>）。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _recv_text(node.value, aliases)
        if parent == "<expr>":
            return "<expr>"
        return f"{parent}.{node.attr}"
    if isinstance(node, ast.Call) and _is_path_constructor(node, aliases):
        return "Path(...)"
    return "<expr>"


def _target_names(target: ast.expr | None) -> list[str]:
    """赋值目标里的裸名（Name / Tuple 展开；带属性的目标忽略）。"""
    if target is None:
        return []
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        out: list[str] = []
        for elt in target.elts:
            out.extend(_target_names(elt))
        return out
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    return []


class _ScopeWalker(ast.NodeVisitor):
    """同步作用域内遍历（不进嵌套 def/lambda——路径名传播不跨函数边界）。"""

    def __init__(self) -> None:
        self.nodes: list[ast.AST] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802
        return

    def generic_visit(self, node: ast.AST) -> None:
        self.nodes.append(node)
        super().generic_visit(node)


def _iter_sync_scope(body: list[ast.stmt]) -> list[ast.AST]:
    """函数体同步作用域的全部节点（嵌套 def/lambda 边界截断）。"""
    walker = _ScopeWalker()
    for stmt in body:
        walker.visit(stmt)
    return walker.nodes


# ── async 函数体内的路径形态传播 + 命中扫描 ──────────────────────────


class _FuncScanner:
    """扫描单个 async def 函数体：路径形态传播 + 阻塞原语命中。

    两遍：①预收集路径形态绑定名（忽略顺序，过近似）；②顺序下探命中。
    """

    def __init__(
        self,
        aliases: _AliasMap,
        violations: list[Violation],
        file: str,
        qualname: str,
        sync_helper_hits: dict[str, list[tuple[str, int]]] | None = None,
    ) -> None:
        self._aliases = aliases
        self._violations = violations
        self._file = file
        self._qualname = qualname
        self._path_names: set[str] = set()
        self._handle_names: set[str] = set()
        # 调用图穿透面（1d）：同模块模块级同步函数名 → 体内原语命中 [(形态, 行号)]。
        # async 体直调此类 helper 时归因回 async 函数（本仓习语把扫描体提取到
        # 模块级同步函数、async 体只留调用——字面原语扫描对此完全隐身，
        # 2026-11-18 两个实锤卡点 _reload→_scan_plugin_dirs /
        # _gate_orchestration→_iter_plugin_manifests 即此形态）。
        self._sync_helpers = sync_helper_hits or {}
        self._via_reported: set[str] = set()

    def scan(self, func_node: ast.AsyncFunctionDef) -> None:
        self.scan_body(func_node.body)

    def scan_body(self, body: list[ast.stmt]) -> None:
        self._precollect(body)
        self._walk(body)

    # ── 第一遍：路径形态名预收集 ──────────────────────────────────

    def _precollect(self, body: list[ast.stmt]) -> None:
        for node in _iter_sync_scope(body):
            self._absorb_bindings(node)
            self._absorb_container_adds(node)

    def _absorb_bindings(self, node: ast.AST) -> None:
        """赋值/循环/with 绑定：值表达式产出路径形态或句柄 → 记名。"""
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if value is not None and self._value_pathish(value):
                for target in targets:
                    self._path_names.update(_target_names(target))
        elif isinstance(node, ast.AugAssign):
            if isinstance(node.op, ast.Div) or self._value_pathish(node.value):
                self._path_names.update(_target_names(node.target))
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            iter_pathish = self._value_pathish(node.iter)
            for name_node in _target_names(node.target):
                if iter_pathish or PATH_NAME_RE.search(name_node):
                    self._path_names.add(name_node)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if (self._value_pathish(item.context_expr) or _is_open_call(item.context_expr, self._aliases)) and item.optional_vars is not None:
                    for name_node in _target_names(item.optional_vars):
                        self._handle_names.add(name_node)
                        self._path_names.add(name_node)

    def _absorb_container_adds(self, node: ast.AST) -> None:
        """容器收纳路径形态值（plugin_jsons.append(Path(...))）→ 容器记名。"""
        if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)):
            return
        call = node.value
        if not isinstance(call.func, ast.Attribute) or call.func.attr not in CONTAINER_ADD_METHODS:
            return
        if any(self._value_pathish(a) for a in call.args):
            self._path_names.update(_target_names(call.func.value))

    def _value_pathish(self, value: ast.expr | None) -> bool:
        """值表达式是否产出路径形态对象（构造/推导/传播三源）。"""
        if value is None:
            return False
        if isinstance(value, ast.Call):
            return self._call_yields_pathish(value)
        if isinstance(value, ast.Name):
            return value.id in self._path_names or bool(PATH_NAME_RE.search(value.id))
        if isinstance(value, ast.Attribute):
            return bool(PATH_NAME_RE.search(value.attr)) or self._value_pathish(value.value)
        if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Div):
            return self._value_pathish(value.left) or self._value_pathish(value.right)
        if isinstance(value, ast.Subscript):
            return self._value_pathish(value.value)
        if isinstance(value, ast.Await):
            return self._value_pathish(value.value)
        if isinstance(value, ast.IfExp):
            return self._value_pathish(value.body) or self._value_pathish(value.orelse)
        if isinstance(value, ast.Starred):
            return self._value_pathish(value.value)
        if isinstance(value, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
            return self._value_pathish(value.elt) or any(self._value_pathish(gen.iter) for gen in value.generators)
        if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
            return any(self._value_pathish(elt) for elt in value.elts)
        return False

    def _call_yields_pathish(self, call: ast.Call) -> bool:
        """调用是否返回路径形态值。"""
        if _is_path_constructor(call, self._aliases):
            return True
        if isinstance(call.func, ast.Attribute):
            dotted = _module_dotted(call.func, self._aliases)
            if dotted is not None:
                return dotted in OS_PATH_PRIMITIVES or dotted.startswith("os.path.join")
        name = _call_name(call.func)
        if name and CALL_PATH_NAME_RE.search(name):
            return True
        # 任意参数路径形态 → 结果按路径形态计（sorted(iterdir) / str(path) 等）
        return any(self._value_pathish(a) for a in call.args) or any(
            self._value_pathish(kw.value) for kw in call.keywords
        )

    # ── 第二遍：命中扫描 ─────────────────────────────────────────

    def _walk(self, nodes: list[ast.stmt]) -> None:
        for stmt in nodes:
            self._visit(stmt)

    def _visit(self, node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.Lambda)):
            return  # 嵌套同步 def / lambda：体不下探（下放线程池的既有习语）
        if isinstance(node, ast.AsyncFunctionDef):
            # 嵌套 async def：await 时仍在事件循环执行，独立作用域下探
            nested = _FuncScanner(
                self._aliases,
                self._violations,
                self._file,
                f"{self._qualname}.{node.name}",
                self._sync_helpers,
            )
            nested.scan(node)
            return
        if isinstance(node, (ast.With, ast.AsyncWith)):
            # with open(...) 绑定名 → 句柄形态（其 .read/.write 同一阻塞窗口）
            for item in node.items:
                if _is_open_call(item.context_expr, self._aliases) and item.optional_vars is not None:
                    for name_node in _target_names(item.optional_vars):
                        self._handle_names.add(name_node)
        if isinstance(node, ast.Call):
            self._check_call(node)
        for child in ast.iter_child_nodes(node):
            self._visit(child)

    def _check_call(self, node: ast.Call) -> None:
        dotted: str | None = None
        if isinstance(node.func, (ast.Attribute, ast.Name)):
            dotted = _module_dotted(node.func, self._aliases)
            if dotted is None and isinstance(node.func, ast.Name):
                dotted = self._aliases.resolve(node.func.id)

        if _is_offload(dotted):
            return  # 合法出口：参数子树不下探（to_thread(helper) 形态不穿透）
        if dotted is not None and dotted not in BUILTIN_PRIMITIVES and self._is_module_primitive(dotted):
            self._hit(node, dotted)
            return
        # 内建 open（未被 import 遮蔽）
        if isinstance(node.func, ast.Name) and node.func.id == "open" and dotted == "open":
            self._hit(node, "open")
            return
        # 调用图穿透（一层，1d）：async 体直调同模块模块级同步函数——helper
        # 体内含 IO 原语 → 归因回本 async 函数。护栏：每 helper 只归因一次；
        # helper 内原语形态/行号进展示面（stderr 一眼能修），不进匹配键
        # （helper 内增减原语不产生基线漂移）。
        if self._sync_helpers and isinstance(node.func, ast.Name):
            helper = node.func.id
            if helper in self._sync_helpers and helper not in self._via_reported:
                self._via_reported.add(helper)
                via_call, via_line = self._sync_helpers[helper][0]
                self._violations.append(
                    Violation(self._file, self._qualname, f"via:{helper}", node.lineno, via_call=via_call, via_line=via_line)
                )
                return
        # 方法调用形态：接收者必须路径形态（排除 dict.copy/str.replace 噪声）
        if isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            recv = node.func.value
            if attr in PATH_METHOD_PRIMITIVES and self._recv_pathish(recv):
                self._hit(node, f"{_recv_text(recv, self._aliases)}.{attr}")
                return
            if attr in HANDLE_METHOD_PRIMITIVES and self._recv_handle(recv):
                self._hit(node, f"{_recv_text(recv, self._aliases)}.{attr}")
                return

    def _is_module_primitive(self, dotted: str) -> bool:
        if dotted in OS_PRIMITIVES or dotted in OS_PATH_PRIMITIVES or dotted in MODULE_EXACT_PRIMITIVES:
            return True
        return dotted.startswith(MODULE_PREFIX_PRIMITIVES)

    def _recv_pathish(self, node: ast.expr) -> bool:
        """接收者是否路径形态（Name 名单/名形、Path(...) 链、拼接、下标、await）。"""
        if isinstance(node, ast.Name):
            return node.id in self._path_names or bool(PATH_NAME_RE.search(node.id))
        if isinstance(node, ast.Attribute):
            return bool(PATH_NAME_RE.search(node.attr)) or self._recv_pathish(node.value)
        if isinstance(node, ast.Call):
            return self._call_yields_pathish(node)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            return self._recv_pathish(node.left) or self._recv_pathish(node.right)
        if isinstance(node, ast.Subscript):
            return self._recv_pathish(node.value)
        if isinstance(node, ast.Await):
            return self._recv_pathish(node.value)
        if isinstance(node, ast.Starred):
            return self._recv_pathish(node.value)
        return False

    def _recv_handle(self, node: ast.expr) -> bool:
        if isinstance(node, ast.Name):
            return node.id in self._handle_names
        if isinstance(node, ast.Attribute):
            return self._recv_handle(node.value)
        return False

    def _hit(self, node: ast.Call, call_text: str) -> None:
        self._violations.append(Violation(self._file, self._qualname, call_text, node.lineno))


# ── 文件/树扫描 ───────────────────────────────────────────────────────


def _collect_sync_helper_hits(tree: ast.Module, aliases: _AliasMap) -> dict[str, list[tuple[str, int]]]:
    """收集模块级同步函数体内的 IO 原语命中（调用图穿透的 helper 面，1d）。

    仅一层穿透：只收模块级 def（嵌套 def 不下探的既有规则由 _FuncScanner
    的作用域截断保证——helper 内对其他函数的调用不再二次展开）。护栏：
    helper 名 test_ 前缀跳过（测试面不归因）。
    """

    def _hits(body: list[ast.stmt], name: str) -> list[tuple[str, int]]:
        bucket: list[Violation] = []
        scanner = _FuncScanner(aliases, bucket, "<sync-helper>", name)
        scanner.scan_body(body)
        return [(v.call, v.line) for v in bucket]

    helpers: dict[str, list[tuple[str, int]]] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("test_"):
            hits = _hits(node.body, node.name)
            if hits:
                helpers[node.name] = hits
    return helpers


def _scan_file(path: Path, rel: str, violations: list[Violation]) -> None:
    source = path.read_bytes()
    try:
        tree = ast.parse(source.decode("utf-8", errors="replace"), filename=rel)
    except SyntaxError as exc:
        print(f"❌ {rel}: 语法解析失败（fail-closed）：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    aliases = _AliasMap(tree)
    parents = _parent_map(tree)
    sync_helper_hits = _collect_sync_helper_hits(tree, aliases)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            scanner = _FuncScanner(aliases, violations, rel, _qualname_of(parents, node), sync_helper_hits)
            scanner.scan(node)


def _parent_map(tree: ast.Module) -> dict[ast.AST, ast.AST]:
    """子→父映射（每文件一次，供限定名回溯；逐函数重建是二次方开销）。"""
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return parents


def _qualname_of(parents: dict[ast.AST, ast.AST], target: ast.AsyncFunctionDef) -> str:
    """函数限定名：类方法带类前缀，嵌套函数用点连接。"""
    chain: list[str] = []
    cur: ast.AST | None = target
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            chain.append(cur.name)
        elif isinstance(cur, ast.Lambda):
            chain.append("<lambda>")
        cur = parents.get(cur)
    return ".".join(reversed(chain))


def scan(root: Path) -> list[Violation]:
    """扫描 root 下 plugins/shared + plugins/sdk/src 的 async 阻塞调用。"""
    violations: list[Violation] = []
    for scan_root in SCAN_ROOTS:
        root_path = root / scan_root
        if not root_path.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root_path):
            dirnames[:] = [
                d
                for d in dirnames
                if d not in EXCLUDED_DIRS and not d.startswith(".venv") and not d.endswith(".egg-info")
            ]
            for name in sorted(filenames):
                if not name.endswith(".py") or is_test_file(name):
                    continue
                path = Path(dirpath) / name
                rel = path.relative_to(root).as_posix()
                _scan_file(path, rel, violations)
    return violations


# ── 基线机制 ──────────────────────────────────────────────────────────


def parse_baseline(path: Path) -> set[str]:
    """解析基线 → 匹配键集（file::func::call）；格式非法即红。

    两种行形态：普通命中 4 段（file::func::call::line）；调用图穿透命中
    5 段（file::func::via:helper::原语::helper内行号）——键均取前 3 段。
    """
    if not path.exists():
        return set()
    return _parse_baseline_lines(path.read_text(encoding="utf-8").splitlines(), strict=True)


def parse_baseline_text(text: str) -> set[str]:
    """从文本解析基线键集（--baseline-monotonic 对照 base 版本用）。"""
    return _parse_baseline_lines(text.splitlines(), strict=False)


def _parse_baseline_lines(lines: list[str], strict: bool) -> set[str]:
    keys: set[str] = set()
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("::")
        is_plain = len(parts) == 4
        is_via = len(parts) == 5 and parts[2].startswith("via:")
        if is_plain or is_via:
            keys.add("::".join(parts[:3]))
        elif strict:
            raise SystemExit(f"❌ 基线格式非法（须为 file::func::call::line 或穿透 5 段形态）: {line}")
    return keys


def generate(root: Path, baseline: Path) -> int:
    """以当前扫描结果重写基线（存量准入；P0/P1 面随修复提交收缩）。"""
    violations = sorted(scan(root), key=lambda v: (v.file, v.func, v.call, v.line))
    header = (
        "# async 内同步阻塞基线（逐项棘轮，只减不增）——scripts/check_async_blocking.py 消费\n"
        "# 格式：「file::func::call::line」每处一行；# 开头为注释；匹配键 = 前 3 段（行号不参与）。\n"
        "# P0/P1 面（builtin_tools/fs_tools.py、builtin_tools/search_tool.py、search/tool.py、\n"
        "# prompt_build/plugin.py、workspace/server.py）随修复任务同步收缩，不得作为常驻债留存。\n"
    )
    body = "".join(f"{v.entry}\n" for v in violations)
    baseline.write_text(header + body, encoding="utf-8")
    print(f"✅ 基线已写入 {baseline}：{len(violations)} 处")
    return 0


def baseline_monotonic(root: Path, baseline: Path, base: str) -> int:
    """账本棘轮：对照 base 提交的基线——新增键 = 红（只许减不许增）。"""
    rev = subprocess.run(
        ["git", "rev-parse", "--verify", f"{base}^{{commit}}"],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=False,
    )
    if rev.returncode != 0:
        print(f"❌ base 引用不可解析: {base}（须完整历史或显式 --base）")
        return 1
    rel_baseline = baseline.relative_to(root).as_posix()
    probe = subprocess.run(
        ["git", "cat-file", "-e", f"{base}:{rel_baseline}"],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        print(
            f"ℹ️ base {base} 处基线文件尚不存在（闸门 adoption 前区间）——"
            "账本棘轮无对照面，本次跳过（下一起点起正常对照）"
        )
        return 0
    proc = subprocess.run(
        ["git", "show", f"{base}:{rel_baseline}"],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    old_keys = parse_baseline_text(proc.stdout) if proc.returncode == 0 else set()
    current_keys = {v.key for v in scan(root)}
    regressions = sorted(key for key in current_keys if key not in old_keys)
    if regressions:
        for item in regressions:
            print(f"❌ {item}")
        print(f"\n❌ 基线账本相对 {base} 出现 {len(regressions)} 处新增（棘轮不可逆）")
        return 1
    print(f"✅ 基线账本相对 {base} 无放宽（{len(current_keys)} 键）")
    return 0


def check(root: Path, baseline: Path) -> int:
    """执法：不在基线内的新增违规 → 红（stderr 列新增项）；存量消失 → 提示收缩。"""
    violations = scan(root)
    known = parse_baseline(baseline)
    fresh = [v for v in violations if v.key not in known]
    if fresh:
        print(f"\n❌ async 内同步阻塞：{len(fresh)} 处新增违规（不在基线内）：", file=sys.stderr)
        for v in fresh:
            print(f"  ❌ {v.entry}", file=sys.stderr)
        print(
            "\nasync 工具 handler 内一切同步文件/进程 I/O 必经 asyncio.to_thread / run_blocking 下放；"
            "确属存量清理待办的可收敛项不得新增（基线棘轮只减不增）。",
            file=sys.stderr,
        )
        return 1
    current_keys = {v.key for v in violations}
    stale = sorted(key for key in known if key not in current_keys)
    print(f"扫描违规 {len(violations)} 处（基线 {len(known)} 键）")
    if stale:
        print(f"\n⚠️ 基线中 {len(stale)} 键已无对应违规（应同步收缩基线，不阻断）：")
        for item in stale:
            print(f"  ↘ {item}")
    print("✅ async-blocking 无新增违规")
    return 0


def resolve_base(explicit: str | None) -> str:
    """账本对照参照：--base > GITHUB_BASE_REF > HEAD~1。"""
    if explicit:
        return explicit
    env = os.environ.get("GITHUB_BASE_REF", "").strip()
    if env:
        return env
    return "HEAD~1"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="async 内同步阻塞调用检查器（AST 扫描 + 基线棘轮）")
    parser.add_argument("--generate", action="store_true", help="以当前扫描结果重写基线")
    parser.add_argument("--baseline-monotonic", action="store_true", help="对照 base 提交执法账本棘轮（CI 用）+ 常规执法")
    parser.add_argument("--base", metavar="REF", help="账本对照参照（默认 GITHUB_BASE_REF > HEAD~1）")
    parser.add_argument("--root", metavar="DIR", help="仓库根覆盖（单测构造样本树用）")
    parser.add_argument("--baseline", metavar="FILE", help="基线文件路径覆盖（默认 .github/async-blocking-baseline.txt）")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve() if args.root else ROOT
    baseline = Path(args.baseline).resolve() if args.baseline else BASELINE_FILE

    if args.generate:
        return generate(root, baseline)
    if args.baseline_monotonic:
        rc = baseline_monotonic(root, baseline, resolve_base(args.base))
        if rc != 0:
            return rc
    return check(root, baseline)


if __name__ == "__main__":
    sys.exit(main())
