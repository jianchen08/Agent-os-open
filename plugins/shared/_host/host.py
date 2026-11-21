#!/usr/bin/env python3
"""插件合宿宿主（co-hosting host process）。

方案：docs/working/插件合宿进程模型优化方案_20260826.md §4.3/4.5/4.7。
spawn 契约（内核 invoker 注入）：::

    python host.py --group light --slot N --members a,b,c
    # cwd = plugins/shared/_host/

职责：
- 按 --members 的 plugin_id 列表在内核插件发现面（出厂根 plugins/shared +
  用户插件根，任意域任意深度，同 id 双源按 BuiltinFirst 版本裁决：用户副本
  semver 严格大于内置才接管，对齐内核 ADR 2026-09-20）定位成员插件目录
  （plugin.json id 优先，目录名兜底）；
- 逐个加载成员 server.py 并取得其 ``AgentOSPlugin`` 实例，经 SDK
  ``CohostServer`` 聚合为单个 MCP stdio server（工具带 ``{plugin_id}.``
  前缀，initialize/生命周期通知扇出，共享反向调用通道）；
- 事件循环 watchdog：独立线程监控主 asyncio 循环心跳打点，停滞超过
  ``AGENTOS_HOST_WATCHDOG_SECS``（默认 30s）先把全部线程 Python 栈落盘取证
  （``_dump_stall_stacks``，目录 ``AGENTOS_STALL_DUMP_DIR`` > 用户空间 logs），
  再有界（≤2s）尽力杀掉成员登记的活跃子进程（防 os._exit 留无主孤儿），
  最后 ``os._exit(1)``，交由内核既有 crash-respawn 自愈（把"事件循环被阻塞"
  转化为可自愈崩溃）；
- 成员加载失败 fail-soft：单成员失败（未找到 / import・init 抛错）跳过该
  成员并打 ``[cohost][member-offline]`` 结构性行上报，其余成员照常服务；
  仅成员表为空/重复或全部成员失败才退出非零码（内核按崩溃处理重试）。

本目录不是插件（无 plugin.json），是宿主进程基座；共享 venv 约定见
方案 §4.7（plugins/shared/_host/.venv，成员依赖并集）。

平铺裸名隔离（加载器核心约束）：0.2 插件以裸名平铺导入（每插件目录都有
``plugin.py``，server.py 顶部 ``from plugin import X``）。多成员同进程时
sys.modules 的 ``plugin`` 等裸名槽位会串扰，处理策略见 ``_MemberLoader``：
成员 server.py 以唯一模块名 exec，加载下一成员前摘除先前成员引入的裸名
模块、exec 后恢复（首个成员的裸名在静息态常驻 sys.modules）。约束：多属主
裸名禁运行期解析（light 准入担保，方案 §4.1 白名单语义）；单属主运行期
懒加载自身目录裸名模块（函数内 ``import 裸名``）可，reload 按文件归属摘除
未登记成员模块，懒加载点随成员热重载刷新（不必整组重启）。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import faulthandler
import importlib.util
import json
import logging
import os
import re
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, NoReturn

from agentos_plugin_sdk import AgentOSPlugin, CohostServer

logger = logging.getLogger(__name__)

# 扫描剪枝目录：每插件的 venv 与 node_modules 等重型目录不进索引（plugin.json
# 只存在于插件目录根）；junction/符号链接目录也不进入（避免跟随外部仓库循环遍历）。
_SCAN_PRUNE_DIRS: frozenset[str] = frozenset(
    {".venv", "node_modules", "__pycache__", ".git", "target", "dist", "build"}
)
# semver 2.0.0 文法：主.次.补三段数字（前置零拒绝）+ 可选预发布/构建元数据。
# 与内核 semver::Version::parse 同规（双源裁决口径两侧一致的前提）。
_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
# 双源裁决候选：（层级，版本原文，插件目录）。层级 0=内置（roots 首个根），
# 1=用户层；版本 None = manifest 缺失 version 字段或非字符串。
_Candidate = tuple[int, str | None, Path]
# 成员 server.py 在 sys.modules 的唯一模块名前缀（避免成员间 "server" 槽位互踩）
_MEMBER_MODULE_PREFIX = "_cohost_member_"
# watchdog：心跳停滞判定阈值默认值，环境变量 AGENTOS_HOST_WATCHDOG_SECS 可覆盖
_WATCHDOG_STALL_SECS_DEFAULT = 30.0
_WATCHDOG_ENV = "AGENTOS_HOST_WATCHDOG_SECS"
# watchdog 检查间隔与主循环心跳打点间隔
_WATCHDOG_CHECK_INTERVAL_SECS = 5.0
_HEARTBEAT_INTERVAL_SECS = 5.0
# 自杀退出前的子进程清理预算（秒）：os._exit 跳过 atexit（bash server.py 的
# shutdown_all 挂在那里），退出前必须以同步方式有界尽力杀掉活跃子进程
_EXIT_CLEANUP_BUDGET_SECS = 2.0
# 停滞取证栈落盘：目录优先级 AGENTOS_STALL_DUMP_DIR > <用户空间>/logs（Windows
# 即 %APPDATA%\agentos\logs）> ~/.agentos/logs；文件名 host_stall_<UTC>_<pid>.log
_STALL_DUMP_DIR_ENV = "AGENTOS_STALL_DUMP_DIR"
_STALL_DUMP_PREFIX = "host_stall_"


class CohostError(Exception):
    """宿主启动失败（成员表为空/重复、全部成员失败），fail-fast 退出载体。"""


@dataclass(frozen=True)
class MemberLoadFailure:
    """单成员加载失败记录（跳过该成员、组内其余成员照常服务的上报单元）。

    经 ``[cohost][member-offline]`` 结构性行落 stderr——内核侧以此行
    为组内成员下线的观测源（通知/降级判定消费方）。
    """

    plugin_id: str
    stage: str  # "not_found"（发现面无此成员） / "load"（import・init 抛错）
    detail: str


def _default_shared_root() -> Path:
    """默认成员发现根：本文件位于 plugins/shared/_host/，其上级即 plugins/shared。"""
    return Path(__file__).resolve().parents[1]


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """解析内核注入的 spawn 参数（--group/--slot/--members）。"""
    parser = argparse.ArgumentParser(
        prog="host.py",
        description="插件合宿宿主：单进程聚合多个轻插件成员的 MCP stdio 服务",
    )
    parser.add_argument("--group", required=True, help="宿主分组（如 light）")
    parser.add_argument("--slot", required=True, type=int, help="宿主槽位序号")
    parser.add_argument("--members", required=True, help="成员 plugin_id 逗号分隔列表（内核 invoker 注入）")
    return parser.parse_args(argv)


def _watchdog_stall_secs(env: Mapping[str, str] | None = None) -> float:
    """读取 watchdog 心跳停滞阈值（AGENTOS_HOST_WATCHDOG_SECS）。

    非法/非正值回退默认 30s（watchdog 是兜底机制，配置错误降级不阻塞启动，
    但给出降级提示）。
    """
    raw = (os.environ if env is None else env).get(_WATCHDOG_ENV, "")
    if not raw:
        return _WATCHDOG_STALL_SECS_DEFAULT
    try:
        value = float(raw)
    except ValueError:
        logger.warning("[cohost] %s=%r 非数值，回退默认 %.0fs", _WATCHDOG_ENV, raw, _WATCHDOG_STALL_SECS_DEFAULT)
        return _WATCHDOG_STALL_SECS_DEFAULT
    if value <= 0:
        logger.warning("[cohost] %s=%r 非正数，回退默认 %.0fs", _WATCHDOG_ENV, raw, _WATCHDOG_STALL_SECS_DEFAULT)
        return _WATCHDOG_STALL_SECS_DEFAULT
    return value


# ── 成员发现 ─────────────────────────────────────────────


def _user_plugins_root() -> Path | None:
    """用户插件根（``<USER_ROOT>/plugins``）——成员发现的双根之一。

    经共享根裸模块 ``user_space`` 解析（内核 ``agentos_core::user_space`` 的
    Python 镜像，两侧同规则；插件侧先例见 agent_manager/mode_keys），不本地
    重拼路径。用户层不可解析（共享根缺失/损坏、无 OS data dir）→ None：
    只有出厂根是合法降级形态（存量部署无用户空间），用户层成员届时以
    「未找到」显式报出，不静默换源。
    """
    shared_root = str(_default_shared_root())
    if shared_root not in sys.path:
        sys.path.insert(0, shared_root)
    try:
        from _root_api import user_space

        return user_space.user_plugins_dir()
    except Exception as exc:  # noqa: BLE001 — 用户层不可解析：降级仅出厂根并告警，不阻断宿主
        logger.warning("[cohost] 用户插件层不可解析，仅扫出厂插件根 | err=%s", exc)
        return None


def _member_roots(shared_root: Path) -> list[Path]:
    """成员发现根集合：出厂根 + 用户插件根（后者按 BuiltinFirst 版本裁决覆盖前者）。

    与内核一致：插件面 = 出厂根与用户插件根并集，域/深度不设白名单
    （``plugins/shared/modes/``、用户空间 ``modes/`` 等任意域同规则可达）。
    """
    roots = [shared_root]
    user_root = _user_plugins_root()
    if user_root is not None and user_root not in roots:
        roots.append(user_root)
    return roots


def _parse_version(version: str | None) -> tuple[int, int, int, tuple] | None:
    """semver 2.0.0 解析 → 可比较排序键；不可解析返回 ``None``。

    对齐内核 ``semver::Version::parse``（``loader.rs`` ``strictly_newer`` 同源）：
    主.次.补三段数字必需（前置零拒绝），预发布/构建元数据可选，构建元数据
    不参与比较。预发布段排序键：正式版 > 预发布；标识符数字段按数值比较且
    低于字母数字段；位段多者大。数字判定限 ASCII（``isdigit`` 会放行非拉丁数字，
    内核侧不可解析，两侧必须同判）。
    """
    if version is None:
        return None
    match = _SEMVER_RE.match(version.strip())
    if match is None:
        return None
    if match[4] is None:
        prerelease: tuple = (1,)
    else:
        identifiers = []
        for identifier in match[4].split("."):
            if identifier.isdigit() and identifier.isascii():
                identifiers.append((0, int(identifier), ""))
            else:
                identifiers.append((1, 0, identifier))
        prerelease = (0, tuple(identifiers))
    return int(match[1]), int(match[2]), int(match[3]), prerelease


def _strictly_newer(a_version: str | None, b_version: str | None) -> bool:
    """``a`` 是否按 semver 严格大于 ``b``（对齐内核 ``loader.rs`` ``strictly_newer``）。

    任一侧缺失/不可解析 → ``False``：不可判定时保守判「挑战者不新」——
    装机口径下内置为权威分发物（fail-closed）。
    """
    a = _parse_version(a_version)
    b = _parse_version(b_version)
    return a is not None and b is not None and a > b


def _adjudicate(key: str, incumbent: _Candidate | None, challenger: _Candidate) -> _Candidate:
    """同键候选裁决（BuiltinFirst，与内核 ``loader.rs`` ``should_replace`` 同规）。

    用户层候选须 semver **严格大于**内置层候选才接管；持平、更低或任一侧
    版本缺失/不可解析 → 内置赢（消灭「宿主装了内核没装的代码」分裂——
    2026-10-15 装机审批 502 事故根因）；同层候选严格更新才换、持平保留
    在位者（多源逐对归约等价于取全候选中版本最高者）。用户层候选因版本
    缺失/不可解析落败时告警（该分裂此前完全静默），按设计完成的版本裁决
    不告警。
    """
    if incumbent is None:
        return challenger
    inc_layer, inc_version, _ = incumbent
    chal_layer, chal_version, chal_dir = challenger
    if chal_layer > inc_layer:
        replace = _strictly_newer(chal_version, inc_version)
    elif chal_layer < inc_layer:
        replace = not _strictly_newer(inc_version, chal_version)
    else:
        replace = _strictly_newer(chal_version, inc_version)
    if replace:
        return challenger
    if chal_layer != inc_layer:
        user_version, user_dir = (
            (chal_version, chal_dir) if chal_layer > inc_layer else (inc_version, incumbent[2])
        )
        if _parse_version(user_version) is None:
            logger.warning(
                "[cohost] 插件 %s 用户副本版本缺失/不可解析，内置副本接管 | dir=%s",
                key,
                user_dir,
            )
    return incumbent


def _scan_plugin_dirs(roots: Sequence[Path]) -> tuple[dict[str, Path], dict[str, Path]]:
    """递归扫描插件根集合，建立 manifest id / 目录名 → 插件目录索引。

    仅收录含 server.py 的 sidecar 形态目录。损坏的 plugin.json 跳过
    （该目录仍可经目录名索引命中——宿主只做定位，不消费 manifest 其余内容）。
    同键多候选按 BuiltinFirst 版本裁决（见 ``_adjudicate``）：roots 首个根为
    内置层，其后均为用户层（调用方按「出厂 → 用户」排序），用户副本版本
    严格大于内置才覆盖。
    """
    by_manifest_id: dict[str, _Candidate] = {}
    by_dir_name: dict[str, _Candidate] = {}
    for layer, root in enumerate(roots):
        if not root.is_dir():
            continue
        # os.walk(followlinks=False) + 目录剪枝：不跟随目录 junction/符号链接，
        # 跳过 .venv/node_modules 等重型目录——plugins/shared 下有指向外部仓库的
        # junction（dsh_adapter/runtime/extra-tools 的 node_modules peer 装载区，
        # 不可剪枝的 rglob 会跟随进外部仓库）且 97 插件各带 venv，必须剪链+剪枝。
        for root_dir, dirs, filenames in os.walk(root, followlinks=False):
            dirs[:] = sorted(
                d
                for d in dirs
                if d not in _SCAN_PRUNE_DIRS
                and not (Path(root_dir) / d).is_symlink()
            )
            if "plugin.json" not in filenames:
                continue
            plugin_dir = Path(root_dir)
            if not (plugin_dir / "server.py").is_file():
                continue
            manifest_id: str | None = None
            version: str | None = None
            try:
                manifest = json.loads((plugin_dir / "plugin.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
            else:
                if isinstance(manifest, dict):
                    raw_id = manifest.get("id")
                    if isinstance(raw_id, str) and raw_id:
                        manifest_id = raw_id
                    raw_version = manifest.get("version")
                    if isinstance(raw_version, str):
                        version = raw_version
            by_dir_name[plugin_dir.name] = _adjudicate(
                plugin_dir.name, by_dir_name.get(plugin_dir.name), (layer, version, plugin_dir)
            )
            if manifest_id is not None:
                by_manifest_id[manifest_id] = _adjudicate(
                    manifest_id,
                    by_manifest_id.get(manifest_id),
                    (layer, version, plugin_dir),
                )
    return (
        {key: candidate[2] for key, candidate in by_manifest_id.items()},
        {key: candidate[2] for key, candidate in by_dir_name.items()},
    )


def _resolve_member_dir(plugin_id: str, by_manifest_id: Mapping[str, Path], by_dir_name: Mapping[str, Path]) -> Path | None:
    """成员 id → 插件目录：plugin.json id 优先（内核以 manifest id 标识插件），目录名兜底。"""
    return by_manifest_id.get(plugin_id) or by_dir_name.get(plugin_id)


# ── 成员加载（平铺裸名隔离）──────────────────────────────


class _MemberLoader:
    """成员 server.py 加载器：唯一模块名 exec + 平铺裸名模块隔离。

    每次加载：摘除先前成员引入的裸名模块（连同 ``name.*`` 子模块）→
    reload 场景再摘除文件归属本目录、不在 owned 表的运行期懒载模块 →
    成员目录插到 sys.path 最前 → 以 ``_cohost_member_<plugin_id>`` 唯一名
    exec server.py → 登记本次引入的裸名模块 → 恢复被摘除模块（首个成员
    的裸名静息态常驻 sys.modules；被重载成员目录下的旧模块排除在外）。
    成员目录留在 sys.path 上（成员 server.py 自身也会插入，与单插件进程
    语义一致）。
    """

    def __init__(self) -> None:
        # plugin_id → 该成员 exec 期间引入、位于其目录下的裸名模块表
        self._owned: dict[str, dict[str, ModuleType]] = {}
        # plugin_id → 成员插件目录（drop_member 按文件归属排除恢复用）
        self._dirs: dict[str, Path] = {}

    def load(self, plugin_id: str, plugin_dir: Path) -> AgentOSPlugin:
        """加载一个成员，返回其 server.py 暴露的 ``plugin``（AgentOSPlugin 实例）。

        Raises:
            CohostError: server.py 无法 exec、或未暴露合法 ``plugin`` 对象。
        """
        masked = self._pop_previous_member_modules()
        reloading = plugin_id in self._owned
        if reloading:
            # 运行期懒载模块不在 owned 表（owned 只在 exec 期间收集），reload
            # 须一并摘除：否则函数内 ``import 裸名`` 命中 sys.modules 旧缓存，
            # 懒加载点持续跑旧代码（须整组重启才刷新）。并入 masked 后经
            # exclude_dir 排除恢复——静息态不回流。
            masked.update(self._pop_unregistered_member_modules(plugin_dir))
        sys.path.insert(0, str(plugin_dir))
        # 重载语义：仅当该成员已有 owned 登记（reload 而非首载）时，恢复阶段
        # 排除其目录下的旧模块（静息态不残留旧代码对象）；首载无排除。
        exclude_dir = plugin_dir if reloading else None
        try:
            module = self._exec_member_module(plugin_id, plugin_dir)
            owned = self._collect_local_modules(plugin_dir)
        finally:
            self._restore_modules(masked, exclude_dir=exclude_dir)
        self._owned[plugin_id] = owned
        self._dirs[plugin_id] = plugin_dir
        plugin_obj = getattr(module, "plugin", None)
        if not isinstance(plugin_obj, AgentOSPlugin):
            raise CohostError(f"成员 {plugin_id}：server.py 未暴露 plugin（AgentOSPlugin 实例）")
        return plugin_obj

    def reload(self, plugin_id: str, plugin_dir: Path) -> AgentOSPlugin:
        """单成员热重载：同 ``load`` 流程重 exec 成员代码（成员粒度热重载）。

        遮蔽纪律与首载共用（同一 loader，owned 表连续）：摘除全部已登记
        裸名模块与文件归属本目录的运行期懒载模块 → 唯一名重 exec → 收集
        新 owned → 恢复他人模块。恢复按 **文件归属**排除该成员目录下的旧
        模块（含运行期懒载模块）——静息态裸名槽位不残留旧代码对象：首成员
        reload 后驻留槽位刷新为新模块；非首成员的碰撞名由他人模块恢复原驻
        留。必须与首载同一 loader 实例：owned 表是被重载成员模块边界的
        事实记录。
        """
        return self.load(plugin_id, plugin_dir)

    def drop_member(self, plugin_id: str) -> int:
        """成员卸载后的模块缓存摘除（``agentos/unload_member`` 的宿主侧收尾）。

        只摘「当前驻留者属于被卸成员」的槽位：唯一名模块（含 ``name.*``
        懒载子模块）与其 owned 裸名表中文件归属本成员目录的驻留模块。裸名
        槽位被他人模块驻留（载入次序的遮蔽恢复所致）时不摘——那是别人的
        活模块。Python 无强制卸载——已被闭包/线程/他人模块持有引用的对象
        不受影响，本方法只保证「下次重载该成员必从磁盘新 exec」与静息态
        槽位不残留旧代码对象，不承诺内存立即回收。必须与首载同一 loader
        实例（owned/_dirs 表是成员模块边界的事实记录）。返回摘除的模块数。
        """
        owned = self._owned.pop(plugin_id, None)
        if owned is None:
            return 0
        plugin_dir = self._dirs.pop(plugin_id, None)
        prefix = (
            os.path.normcase(os.path.abspath(plugin_dir)) + os.sep
            if plugin_dir is not None
            else None
        )
        module_name = _MEMBER_MODULE_PREFIX + re.sub(r"\W", "_", plugin_id)
        candidates = (
            set(owned)
            | {name for name in sys.modules if name.startswith(module_name)}
        )
        removed = 0
        for name in sorted(candidates):
            module = sys.modules.get(name)
            if module is None:
                continue
            file = getattr(module, "__file__", None)
            under_dropped = (
                prefix is not None
                and file
                and os.path.normcase(os.path.abspath(file)).startswith(prefix)
            )
            if name.startswith(module_name) or under_dropped:
                if sys.modules.pop(name, None) is not None:
                    removed += 1
        return removed

    def _restore_modules(
        self, popped: Mapping[str, ModuleType], exclude_dir: Path | None = None
    ) -> None:
        """恢复被摘除的裸名模块（首个成员的裸名在静息态常驻）。

        ``exclude_dir``：按文件归属跳过恢复——用于成员重载，把该成员目录下
        的旧模块（含 ``name.*`` 懒载子模块）挡在 sys.modules 之外，新 exec 的
        模块驻留、旧代码对象不再占槽；他人模块（即使裸名与被重载成员碰撞）
        原样恢复。无 ``__file__`` 的模块（内置/命名空间）无从归属，一律恢复。
        """
        prefix = (
            os.path.normcase(os.path.abspath(exclude_dir)) + os.sep
            if exclude_dir is not None
            else None
        )
        for name, module in popped.items():
            file = getattr(module, "__file__", None)
            if (
                prefix is not None
                and file
                and os.path.normcase(os.path.abspath(file)).startswith(prefix)
            ):
                continue
            sys.modules[name] = module

    def _exec_member_module(self, plugin_id: str, plugin_dir: Path) -> ModuleType:
        """以唯一模块名 exec 成员 server.py（不占 ``server`` 槽位）。"""
        module_name = _MEMBER_MODULE_PREFIX + re.sub(r"\W", "_", plugin_id)
        server_py = plugin_dir / "server.py"
        spec = importlib.util.spec_from_file_location(module_name, server_py)
        if spec is None or spec.loader is None:
            raise CohostError(f"成员 {plugin_id}：无法从 {server_py} 创建模块 spec")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            del sys.modules[module_name]
            raise CohostError(f"成员 {plugin_id} 加载失败：{exc!r}") from exc
        return module

    def _pop_previous_member_modules(self) -> dict[str, ModuleType]:
        """摘除先前成员引入的全部裸名模块（连同 ``name.*`` 子模块），返回待恢复表。"""
        owned_names = {name for owned in self._owned.values() for name in owned}
        targets = {
            module_name
            for module_name in sys.modules
            if any(module_name == name or module_name.startswith(name + ".") for name in owned_names)
        } | owned_names
        popped: dict[str, ModuleType] = {}
        for candidate in sorted(targets):
            module = sys.modules.pop(candidate, None)
            if module is not None:
                popped[candidate] = module
        return popped

    def _pop_unregistered_member_modules(self, plugin_dir: Path) -> dict[str, ModuleType]:
        """摘除文件归属成员目录、但不在任何 owned 登记表的模块（运行期懒载残留）。

        owned 表只在成员 exec 期间收集；函数级懒加载（运行期 ``import 裸名``）
        引入的模块晚于 exec 驻留，reload 若不摘除，sys.modules 旧缓存会让
        懒加载点持续跑旧代码（须整组重启才刷新）。按 ``__file__`` 文件归属
        判定（与 ``_restore_modules``/``drop_member`` 同款 normcase+abspath），
        不按模块名枚举——本目录现在与未来的全部运行期懒载模块一并覆盖。
        唯一名成员模块（``_MEMBER_MODULE_PREFIX``）不在此列（exec 时整槽替换）。
        """
        registered = {name for owned in self._owned.values() for name in owned}
        prefix = os.path.normcase(os.path.abspath(plugin_dir)) + os.sep
        popped: dict[str, ModuleType] = {}
        for name, module in list(sys.modules.items()):
            if name in registered or name.startswith(_MEMBER_MODULE_PREFIX):
                continue
            file = getattr(module, "__file__", None)
            if not file:
                continue
            if os.path.normcase(os.path.abspath(file)).startswith(prefix):
                if sys.modules.pop(name, None) is not None:
                    popped[name] = module
        return popped

    def _collect_local_modules(self, plugin_dir: Path) -> dict[str, ModuleType]:
        """收集 exec 后位于成员目录下的裸名模块（本成员的隔离登记表）。"""
        prefix = os.path.normcase(str(plugin_dir.resolve())) + os.sep
        owned: dict[str, ModuleType] = {}
        for name, module in list(sys.modules.items()):
            if name.startswith(_MEMBER_MODULE_PREFIX):
                continue
            file = getattr(module, "__file__", None)
            if file and os.path.normcase(os.path.abspath(file)).startswith(prefix):
                owned[name] = module
        return owned


def _load_members(
    shared_root: Path,
    member_ids: Sequence[str],
    loader: _MemberLoader | None = None,
) -> tuple[dict[str, AgentOSPlugin], list[MemberLoadFailure]]:
    """按成员 id 列表发现并加载成员；单成员失败跳过并记录，不拖垮整组。

    单成员失败（目录未找到 / import・init 抛错）只记入返回的 failures，
    其余成员照常加载——一个坏成员不得令全组工具下线（ADR
    2026-10-17-cohost-member-fail-soft）。仅整组级配置错误（成员表为空、
    成员重复）与「全部成员失败」仍抛 ``CohostError``：无任何可服务成员的
    宿主进程没有存在意义。

    ``loader``：外部注入的成员加载器（main 注入并在 reload 编排中复用同一
    实例——owned 表连续是裸名遮蔽判定的前提）；缺省内部自建（单测直调形态）。

    Raises:
        CohostError: 成员列表为空、成员重复，或全部成员加载失败。
    """
    if not member_ids:
        raise CohostError("--members 为空：合宿宿主至少需要一个成员")
    roots = _member_roots(shared_root)
    by_manifest_id, by_dir_name = _scan_plugin_dirs(roots)
    loader = loader if loader is not None else _MemberLoader()
    members: dict[str, AgentOSPlugin] = {}
    failures: list[MemberLoadFailure] = []
    seen: set[str] = set()
    for plugin_id in member_ids:
        if plugin_id in seen:
            raise CohostError(f"成员重复：{plugin_id}")
        seen.add(plugin_id)
        plugin_dir = _resolve_member_dir(plugin_id, by_manifest_id, by_dir_name)
        if plugin_dir is None:
            failures.append(
                MemberLoadFailure(
                    plugin_id,
                    "not_found",
                    f"插件根 {[str(r) for r in roots]} 下无匹配 plugin.json id "
                    "或目录名（且含 server.py）",
                )
            )
            continue
        try:
            members[plugin_id] = loader.load(plugin_id, plugin_dir)
        except Exception as exc:  # noqa: BLE001 — 成员加载可抛任意插件错误；
            # 本边界正是「单成员失败不拖垮全组」的落点，记录后继续
            failures.append(
                MemberLoadFailure(plugin_id, "load", f"{type(exc).__name__}: {exc}")
            )
    if not members and failures:
        raise CohostError(
            "全部成员加载失败："
            + "; ".join(f"{f.plugin_id}[{f.stage}] {f.detail}" for f in failures)
        )
    return members, failures


def _build_reload_handler(
    shared_root: Path, loader: _MemberLoader
) -> Callable[[str], Any]:
    """构造成员粒度热重载的加载回调（``agentos/reload_member`` 的加载侧）。

    按成员 id 重新定位插件目录（成员目录可能新增/移动，不缓存首载索引），
    经同一 loader 按遮蔽纪律重 exec（owned 表连续）。定位失败抛
    ``CohostError`` → SDK 侧以协议错误应答 → 内核回退 force_unload。
    """

    async def _reload(plugin_id: str) -> AgentOSPlugin:
        # 清单扫描卸线程（2026-11-18 装机事故 1b）：os.walk 全量走 100+ 插件
        # 目录（慢盘实测停滞 30.5s），本回调直跑在合宿事件循环线程上——同步
        # 扫描会堵死全部并发请求；watcher 热重载风暴期每波 load_request 都
        # 触发全量重扫，卸载后循环仅 await。不走缓存面：每次扫描即最新视图，
        # 插件热发现（新增/移动）天然被下一轮定位感知（与「不缓存首载索引」
        # 的既有注释口径一致）。
        by_manifest_id, by_dir_name = await asyncio.to_thread(
            _scan_plugin_dirs, _member_roots(shared_root)
        )
        plugin_dir = _resolve_member_dir(plugin_id, by_manifest_id, by_dir_name)
        if plugin_dir is None:
            raise CohostError(f"成员 {plugin_id} 未找到（reload 定位失败）")
        return loader.reload(plugin_id, plugin_dir)

    return _reload


# ── 事件循环 watchdog ────────────────────────────────────


def _kill_registered_process(info: Any) -> bool:
    """同步杀死单个活跃进程登记（watchdog 线程内运行，事件循环已停滞）。

    优先复用进程所属 backend 的同步整树杀（LocalProcessBackend 的
    psutil 叶子→根，防孙子进程变孤儿）；无该能力的 backend（容器路径，
    宿主侧句柄是 docker exec 客户端进程）退化为单进程 kill。

    Returns:
        True 表示发起了杀进程调用；False 表示无可杀句柄（process 缺失）。
    """
    proc = getattr(info, "process", None)
    if proc is None:
        return False
    kill_tree = getattr(getattr(info, "backend", None), "_kill_tree_sync", None)
    if callable(kill_tree):
        kill_tree(proc.pid, force=True)
        return True
    proc.kill()
    return True


def _kill_tracked_children(budget_secs: float = _EXIT_CLEANUP_BUDGET_SECS) -> int:
    """自杀退出前的有界尽力清理：杀掉成员插件登记的活跃子进程。

    扫描合宿成员模块（``_MEMBER_MODULE_PREFIX`` 前缀）暴露的进程管理器
    活跃表（ProcessManager 形状：``active_processes`` 字典，BashTool 经
    ``process_manager`` 属性持有），逐个同步杀。事件循环已停滞（watchdog
    正因停滞触发），async terminate/kill 不可用，只能同步杀。``budget_secs``
    内尽力多杀，超预算立即放弃；任何异常就地吞并——清理失败不阻断自杀，
    这是 best-effort 的合法点。

    Returns:
        尝试杀掉的进程数（诊断用）
    """
    deadline = time.monotonic() + budget_secs
    killed = 0
    for name, module in list(sys.modules.items()):
        if not name.startswith(_MEMBER_MODULE_PREFIX):
            continue
        for attr in vars(module).values():
            for holder in (attr, getattr(attr, "process_manager", None)):
                active = getattr(holder, "active_processes", None)
                if not isinstance(active, dict):
                    continue
                for info in list(active.values()):
                    if time.monotonic() > deadline:
                        return killed
                    try:
                        if _kill_registered_process(info):
                            killed += 1
                    except Exception:  # noqa: BLE001 — best-effort 清理，失败不阻断自杀
                        continue
    return killed


def _stall_dump_dir(env: Mapping[str, str] | None = None) -> Path:
    """停滞取证栈落盘目录：``AGENTOS_STALL_DUMP_DIR`` > 用户空间 logs > ``~/.agentos/logs``。

    用户空间 logs：Windows 取 ``%APPDATA%\\agentos\\logs``（与
    ``user_space.user_root`` 同源规则；APPDATA 缺失时回落 home）；其余平台
    回退 ``~/.agentos/logs``。``env`` 缺省读 ``os.environ``（注入面测试用）。
    """
    source = os.environ if env is None else env
    raw = source.get(_STALL_DUMP_DIR_ENV, "")
    if raw.strip():
        return Path(raw.strip())
    if sys.platform == "win32":
        appdata = source.get("APPDATA", "")
        if appdata.strip():
            return Path(appdata.strip()) / "agentos" / "logs"
    return Path.home() / ".agentos" / "logs"


class _Heartbeat:
    """主事件循环心跳打点（watchdog 判活依据）。"""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self.last_beat: float = clock()

    def beat(self) -> None:
        """打点（由事件循环内的周期任务调用；循环被阻塞即停滞）。"""
        self.last_beat = self._clock()


class _LoopWatchdog:
    """独立线程 watchdog：主事件循环心跳停滞超阈值即自杀退出。

    心跳由 ``_heartbeat_loop`` 在主循环内打点；若成员工具的同步阻塞调用
    冻住事件循环，打点停滞，本线程到点先把全部线程 Python 栈落盘取证
    （``_dump_stall_stacks``），再有界清理成员活跃子进程（防 ``os._exit``
    留无主孤儿），最后 ``os._exit(1)``，进程退出由内核 crash-respawn 自愈。
    ``clock``/``exit_fn``/``cleanup_fn``/``dump_dir``/``host_name`` 可注入
    （测试用）。
    """

    def __init__(
        self,
        heartbeat: _Heartbeat,
        stall_secs: float,
        check_interval_secs: float = _WATCHDOG_CHECK_INTERVAL_SECS,
        *,
        clock: Callable[[], float] = time.monotonic,
        exit_fn: Callable[[int], None] = os._exit,
        cleanup_fn: Callable[[], int] = _kill_tracked_children,
        dump_dir: Path | None = None,
        host_name: str | None = None,
    ) -> None:
        self._heartbeat = heartbeat
        self._stall_secs = stall_secs
        self._check_interval_secs = check_interval_secs
        self._clock = clock
        self._exit_fn = exit_fn
        self._cleanup_fn = cleanup_fn
        self._dump_dir = dump_dir
        self._host_name = host_name
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """启动 watchdog 线程（幂等）。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="cohost-loop-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """停止 watchdog（服务正常退出时调用，防止误杀）。"""
        self._stop.set()

    def _run(self) -> None:
        """线程主体：周期检查心跳，停滞超阈值取证落盘、清理子进程后自杀。"""
        while not self._stop.wait(timeout=self._check_interval_secs):
            stalled = self._clock() - self._heartbeat.last_beat
            if stalled > self._stall_secs:
                logger.critical(
                    "[cohost] 事件循环心跳停滞 %.1fs（阈值 %.1fs），自杀退出交由内核 respawn",
                    stalled,
                    self._stall_secs,
                )
                self._dump_stall_stacks(stalled)
                self._cleanup_fn()
                self._exit_fn(1)
                return

    def _dump_stall_stacks(self, stalled_secs: float) -> Path | None:
        """自杀前取证：全部线程（含事件循环线程）Python 栈落盘，定位阻塞帧。

        本方法在 watchdog 线程内运行且事件循环已停滞——只调 ``faulthandler``
        （直接遍历线程帧，不经事件循环调度），不触碰任何 asyncio API。文件头
        记录停滞量/阈值/pid/宿主名/UTC 时间/心跳最后打点，其后是
        ``faulthandler.dump_traceback(all_threads=True)`` 的线程栈。

        best-effort：落盘失败只告警，不改变自杀语义（返回 None）。

        Returns:
            落盘文件路径；失败返回 None。
        """
        directory: Path | None = None
        path: Path | None = None
        try:
            directory = self._dump_dir if self._dump_dir is not None else _stall_dump_dir()
            directory.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            path = directory / f"{_STALL_DUMP_PREFIX}{stamp}_{os.getpid()}.log"
            header = (
                "[cohost][stall-dump] 事件循环心跳停滞取证\n"
                f"time_utc={stamp}\n"
                f"stalled_secs={stalled_secs:.3f}\n"
                f"stall_threshold_secs={self._stall_secs:.3f}\n"
                f"pid={os.getpid()}\n"
                f"host={self._host_name or 'unknown'}\n"
                f"heartbeat_last_beat={self._heartbeat.last_beat:.6f}\n"
                "\n── 全部线程 Python 栈（faulthandler all_threads）──\n"
            )
            with path.open("wb") as handle:
                handle.write(header.encode("utf-8"))
                # faulthandler 直接写 fd，绕过文件对象缓冲：先 flush 保证头在栈前
                handle.flush()
                faulthandler.dump_traceback(file=handle, all_threads=True)
                handle.flush()
        except Exception as exc:  # noqa: BLE001 — 取证 best-effort，失败不阻断自杀
            logger.warning(
                "[cohost] 停滞取证栈落盘失败（继续自杀） | dir=%s err=%r", directory, exc
            )
            return None
        logger.error("[cohost] 停滞取证栈已落盘 | path=%s", path)
        return path


async def _heartbeat_loop(heartbeat: _Heartbeat, interval_secs: float = _HEARTBEAT_INTERVAL_SECS) -> None:
    """主循环内周期打点（事件循环健康即持续更新心跳时间戳）。"""
    while True:
        heartbeat.beat()
        await asyncio.sleep(interval_secs)


# ── 编排与入口 ───────────────────────────────────────────


def _graceful_hard_exit(
    cleanup_fn: Callable[[], int] = _kill_tracked_children,
    exit_fn: Callable[[int], NoReturn] = os._exit,
) -> NoReturn:
    """stdin EOF 正常退出路径的硬退出兜底（与 watchdog 自杀同一机制族）。

    解释器收尾的 concurrent.futures atexit 钩子会无超时 join 默认执行器线程：
    任一 in-flight 任务卡住（如插件的目录长遍历）宿主即成僵尸——不再服务也
    退不出，内存与 CPU 持续流失（2026-10-16 workspace_service 全量扫描卡退
    出实证）。asyncio.run 返回即服务终局，in-flight 结果已无处投递，故与
    watchdog 自杀同口径：有界清理登记子进程（os._exit 跳过 atexit，bash 类
    shutdown_all 挂点由同步清理补位）后 os._exit(0)。
    """
    cleanup_fn()
    exit_fn(0)


# ── 共享 venv 身份自检 ────────────────────────────────────

# 合宿成员依赖装在 plugins/shared/_host/.venv，内核以该 venv 的解释器 spawn
# host.py。venv 缺 pyvenv.cfg（或启动器解析不到基座）时，解释器会静默退回外部
# Python——venv 内 site-packages 全部失效，成员依赖退化成「导入得到但没属性」的
# 残缺模块（2026-11-10 装机实证：aiohttp 命中外部环境的命名空间包 →
# sync_agent 上行永久失败 → outbox 触顶 → 事件循环被同步阻塞 → watchdog 判
# 心跳停滞自杀 → light_stable 组崩溃循环 17 次）。故引导期显式自检：venv
# site-packages 在位却不在导入路径上时补挂并告警，不再静默降级。
_VENV_CFG_NAME = "pyvenv.cfg"


def _shared_venv_site_packages(venv_dir: Path) -> Path | None:
    """venv 的 site-packages 目录（Windows ``Lib/`` 与 POSIX ``lib/pythonX.Y/`` 双形态）。"""
    candidates = [venv_dir / "Lib" / "site-packages"]
    candidates.extend(sorted((venv_dir / "lib").glob("python*/site-packages")))
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def _declared_venv_version(venv_dir: Path) -> tuple[int, int] | None:
    """pyvenv.cfg 声明的目标 Python 版本（缺文件/缺字段/格式异常返回 None）。"""
    cfg = venv_dir / _VENV_CFG_NAME
    if not cfg.is_file():
        return None
    try:
        lines = cfg.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for raw in lines:
        key, _, value = raw.strip().partition("=")
        if key.strip() != "version_info":
            continue
        parts = value.strip().split(".")
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            return int(parts[0]), int(parts[1])
    return None


def _ensure_shared_venv_active(venv_dir: Path | None = None, *, path: list[str] | None = None) -> bool:
    """保证合宿共享 venv 的 site-packages 在导入路径上（返回是否补挂）。

    venv 完好时无操作（site-packages 已在 path 上）；缺 pyvenv.cfg 导致解释器
    退回外部 Python 时补挂并告警——只在声明版本与运行解释器一致时补挂，版本
    不一致（二进制扩展 ABI 不匹配）拒绝补挂并报错，避免把静默降级换成更难查
    的导入崩溃。
    """
    venv = venv_dir if venv_dir is not None else Path(__file__).resolve().parent / ".venv"
    if not venv.is_dir():
        return False
    site_packages = _shared_venv_site_packages(venv)
    if site_packages is None:
        return False
    search = sys.path if path is None else path
    target = site_packages.resolve()
    for entry in search:
        if not entry:
            continue
        try:
            if Path(entry).resolve() == target:
                return False
        except OSError:  # 非法路径条目：不视为命中
            continue
    declared = _declared_venv_version(venv)
    if declared is not None and declared != sys.version_info[:2]:
        logger.error(
            "[cohost] 共享 venv 版本不匹配：venv=%s（声明 %s.%s）解释器=%s.%s，拒绝补挂 %s",
            venv,
            declared[0],
            declared[1],
            sys.version_info[0],
            sys.version_info[1],
            site_packages,
        )
        return False
    search.insert(0, str(target))
    logger.warning(
        "[cohost] 共享 venv 未生效（解释器 %s 未落在 venv %s 内），已补挂 site-packages=%s——"
        "请修 venv 本体（缺 pyvenv.cfg 时重建：uv sync --project plugins/shared/_host）",
        sys.executable,
        venv,
        site_packages,
    )
    return True


async def _serve(
    server: CohostServer,
    stall_secs: float = _WATCHDOG_STALL_SECS_DEFAULT,
    *,
    host_name: str | None = None,
) -> None:
    """主循环编排：心跳任务 + watchdog + 聚合 MCP 服务（至 stdin EOF）。"""
    heartbeat = _Heartbeat()
    beat_task = asyncio.create_task(_heartbeat_loop(heartbeat))
    watchdog = _LoopWatchdog(heartbeat, stall_secs, host_name=host_name)
    watchdog.start()
    try:
        await server.serve()
    finally:
        watchdog.stop()
        beat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beat_task


def main(argv: Sequence[str] | None = None, *, shared_root: Path | None = None) -> int:
    """宿主入口：加载成员 → 聚合 MCP 服务 + watchdog。返回进程退出码。

    单成员失败跳过并打 ``[cohost][member-offline]`` 结构性行，其余成员照常
    服务；仅成员表为空/重复或全部成员失败时返回 1（内核按崩溃处理重试）。
    正常服务终局（stdin EOF）经 _graceful_hard_exit 有界清理后 os._exit(0)，
    不走解释器收尾（atexit 对默认执行器线程的无超时 join 是僵尸进程挂点）。
    """
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    root = shared_root if shared_root is not None else _default_shared_root()
    _ensure_shared_venv_active()
    member_ids = [m.strip() for m in args.members.split(",") if m.strip()]
    stall_secs = _watchdog_stall_secs()
    loader = _MemberLoader()
    try:
        members, failures = _load_members(root, member_ids, loader=loader)
        server = CohostServer(
            members,
            reload_handler=_build_reload_handler(root, loader),
            unload_handler=loader.drop_member,
        )
    except (CohostError, ValueError) as exc:
        print(f"[cohost] 启动失败（fail-fast）：{exc}", file=sys.stderr)
        return 1
    for failure in failures:
        print(
            f"[cohost][member-offline] group={args.group} slot={args.slot} "
            f"plugin={failure.plugin_id} stage={failure.stage} error={failure.detail}",
            file=sys.stderr,
            flush=True,
        )
    print(
        f"[cohost] group={args.group} slot={args.slot} members={len(members)} "
        f"offline={len(failures)} tools={len(server.tool_names)}",
        file=sys.stderr,
    )
    asyncio.run(_serve(server, stall_secs, host_name=f"{args.group}:{args.slot}"))
    _graceful_hard_exit()


if __name__ == "__main__":
    sys.exit(main())
