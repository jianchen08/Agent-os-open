#!/usr/bin/env python3
"""插件合宿宿主（co-hosting host process）。

方案：docs/working/插件合宿进程模型优化方案_20260826.md §4.3/4.5/4.7。
spawn 契约（内核 invoker 注入）：::

    python host.py --group light --slot N --members a,b,c
    # cwd = plugins/shared/_host/

职责：
- 按 --members 的 plugin_id 列表在 plugins/shared/{system,tools,pipeline}
  目录树下定位成员插件目录（plugin.json id 优先，目录名兜底）；
- 逐个加载成员 server.py 并取得其 ``AgentOSPlugin`` 实例，经 SDK
  ``CohostServer`` 聚合为单个 MCP stdio server（工具带 ``{plugin_id}.``
  前缀，initialize/生命周期通知扇出，共享反向调用通道）；
- 事件循环 watchdog：独立线程监控主 asyncio 循环心跳打点，停滞超过
  ``AGENTOS_HOST_WATCHDOG_SECS``（默认 30s）即 ``os._exit(1)`` 自杀，
  交由内核既有 crash-respawn 自愈（把"事件循环被阻塞"转化为可自愈崩溃）；
- 成员加载失败 fail-fast：任一成员 import/init 失败立即退出非零码并
  打印明确错误，内核按崩溃处理重试。

本目录不是插件（无 plugin.json），是宿主进程基座；共享 venv 约定见
方案 §4.7（plugins/shared/_host/.venv，成员依赖并集）。

平铺裸名隔离（加载器核心约束）：0.2 插件以裸名平铺导入（每插件目录都有
``plugin.py``，server.py 顶部 ``from plugin import X``）。多成员同进程时
sys.modules 的 ``plugin`` 等裸名槽位会串扰，处理策略见 ``_MemberLoader``：
成员 server.py 以唯一模块名 exec，加载下一成员前摘除先前成员引入的裸名
模块、exec 后恢复（首个成员的裸名在静息态常驻 sys.modules）。约束：成员
运行期懒加载自身目录裸名模块（函数内 ``import plugin``）在合宿下不受支持
——这是 light 准入担保的一部分（方案 §4.1 白名单语义）。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib.util
import json
import logging
import os
import re
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import ModuleType

from agentos_plugin_sdk import AgentOSPlugin, CohostServer

logger = logging.getLogger(__name__)

# 成员发现扫描的分组目录（plugins/shared/ 下）
_GROUP_ROOTS: tuple[str, ...] = ("system", "tools", "pipeline")
# 扫描剪枝目录：每插件的 venv 与 node_modules 等重型目录不进索引（plugin.json
# 只存在于插件目录根）；junction/符号链接目录也不进入（避免跟随外部仓库循环遍历）。
_SCAN_PRUNE_DIRS: frozenset[str] = frozenset(
    {".venv", "node_modules", "__pycache__", ".git", "target", "dist", "build"}
)
# 成员 server.py 在 sys.modules 的唯一模块名前缀（避免成员间 "server" 槽位互踩）
_MEMBER_MODULE_PREFIX = "_cohost_member_"
# watchdog：心跳停滞判定阈值默认值，环境变量 AGENTOS_HOST_WATCHDOG_SECS 可覆盖
_WATCHDOG_STALL_SECS_DEFAULT = 30.0
_WATCHDOG_ENV = "AGENTOS_HOST_WATCHDOG_SECS"
# watchdog 检查间隔与主循环心跳打点间隔
_WATCHDOG_CHECK_INTERVAL_SECS = 5.0
_HEARTBEAT_INTERVAL_SECS = 5.0


class CohostError(Exception):
    """宿主启动失败（成员发现/加载失败等），fail-fast 退出载体。"""


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


def _scan_plugin_dirs(shared_root: Path) -> tuple[dict[str, Path], dict[str, Path]]:
    """扫描三个分组目录树，建立 manifest id / 目录名 → 插件目录索引。

    仅收录含 server.py 的 sidecar 形态目录。损坏的 plugin.json 跳过
    （该目录仍可经目录名索引命中——宿主只做定位，不消费 manifest 内容）。
    """
    by_manifest_id: dict[str, Path] = {}
    by_dir_name: dict[str, Path] = {}
    for group in _GROUP_ROOTS:
        group_root = shared_root / group
        if not group_root.is_dir():
            continue
        # os.walk(followlinks=False) + 目录剪枝：不跟随目录 junction/符号链接，
        # 跳过 .venv/node_modules 等重型目录——plugins/shared 下有指向外部仓库的
        # junction（dsh_adapter/runtime/extra-tools 的 node_modules peer 装载区，
        # rglob 曾实测卡死）且 97 插件各带 venv，全树扫描必须剪链+剪枝。
        for root_dir, dirs, filenames in os.walk(group_root, followlinks=False):
            dirs[:] = sorted(
                d
                for d in dirs
                if d not in _SCAN_PRUNE_DIRS
                and not os.path.islink(os.path.join(root_dir, d))
            )
            if "plugin.json" not in filenames:
                continue
            plugin_dir = Path(root_dir)
            if not (plugin_dir / "server.py").is_file():
                continue
            by_dir_name[plugin_dir.name] = plugin_dir
            try:
                manifest = json.loads((plugin_dir / "plugin.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            manifest_id = manifest.get("id") if isinstance(manifest, dict) else None
            if isinstance(manifest_id, str) and manifest_id:
                by_manifest_id[manifest_id] = plugin_dir
    return by_manifest_id, by_dir_name


def _resolve_member_dir(plugin_id: str, by_manifest_id: Mapping[str, Path], by_dir_name: Mapping[str, Path]) -> Path | None:
    """成员 id → 插件目录：plugin.json id 优先（内核以 manifest id 标识插件），目录名兜底。"""
    return by_manifest_id.get(plugin_id) or by_dir_name.get(plugin_id)


# ── 成员加载（平铺裸名隔离）──────────────────────────────


class _MemberLoader:
    """成员 server.py 加载器：唯一模块名 exec + 平铺裸名模块隔离。

    每次加载：摘除先前成员引入的裸名模块（连同 ``name.*`` 子模块）→
    成员目录插到 sys.path 最前 → 以 ``_cohost_member_<plugin_id>`` 唯一名
    exec server.py → 登记本次引入的裸名模块 → 恢复被摘除模块（首个成员
    的裸名静息态常驻 sys.modules）。成员目录留在 sys.path 上（成员
    server.py 自身也会插入，与单插件进程语义一致）。
    """

    def __init__(self) -> None:
        # plugin_id → 该成员 exec 期间引入、位于其目录下的裸名模块表
        self._owned: dict[str, dict[str, ModuleType]] = {}

    def load(self, plugin_id: str, plugin_dir: Path) -> AgentOSPlugin:
        """加载一个成员，返回其 server.py 暴露的 ``plugin``（AgentOSPlugin 实例）。

        Raises:
            CohostError: server.py 无法 exec、或未暴露合法 ``plugin`` 对象。
        """
        masked = self._pop_previous_member_modules()
        sys.path.insert(0, str(plugin_dir))
        try:
            module = self._exec_member_module(plugin_id, plugin_dir)
            owned = self._collect_local_modules(plugin_dir)
        finally:
            self._restore_modules(masked)
        self._owned[plugin_id] = owned
        plugin_obj = getattr(module, "plugin", None)
        if not isinstance(plugin_obj, AgentOSPlugin):
            raise CohostError(f"成员 {plugin_id}：server.py 未暴露 plugin（AgentOSPlugin 实例）")
        return plugin_obj

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

    def _restore_modules(self, popped: Mapping[str, ModuleType]) -> None:
        """恢复被摘除的裸名模块（首个成员的裸名在静息态常驻）。"""
        for name, module in popped.items():
            sys.modules[name] = module

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


def _load_members(shared_root: Path, member_ids: Sequence[str]) -> dict[str, AgentOSPlugin]:
    """按成员 id 列表发现并加载全部成员。

    Raises:
        CohostError: 成员列表为空、成员重复、成员未找到或加载失败。
    """
    if not member_ids:
        raise CohostError("--members 为空：合宿宿主至少需要一个成员")
    by_manifest_id, by_dir_name = _scan_plugin_dirs(shared_root)
    loader = _MemberLoader()
    members: dict[str, AgentOSPlugin] = {}
    for plugin_id in member_ids:
        if plugin_id in members:
            raise CohostError(f"成员重复：{plugin_id}")
        plugin_dir = _resolve_member_dir(plugin_id, by_manifest_id, by_dir_name)
        if plugin_dir is None:
            raise CohostError(
                f"成员 {plugin_id} 未找到：{shared_root}/{{{','.join(_GROUP_ROOTS)}}} 下"
                "无匹配 plugin.json id 或目录名（且含 server.py）"
            )
        members[plugin_id] = loader.load(plugin_id, plugin_dir)
    return members


# ── 事件循环 watchdog ────────────────────────────────────


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
    冻住事件循环，打点停滞，本线程到点 ``os._exit(1)``，进程退出由内核
    crash-respawn 自愈。``clock``/``exit_fn`` 可注入（测试用 fake clock）。
    """

    def __init__(
        self,
        heartbeat: _Heartbeat,
        stall_secs: float,
        check_interval_secs: float = _WATCHDOG_CHECK_INTERVAL_SECS,
        *,
        clock: Callable[[], float] = time.monotonic,
        exit_fn: Callable[[int], None] = os._exit,
    ) -> None:
        self._heartbeat = heartbeat
        self._stall_secs = stall_secs
        self._check_interval_secs = check_interval_secs
        self._clock = clock
        self._exit_fn = exit_fn
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
        """线程主体：周期检查心跳，停滞超阈值自杀。"""
        while not self._stop.wait(timeout=self._check_interval_secs):
            stalled = self._clock() - self._heartbeat.last_beat
            if stalled > self._stall_secs:
                logger.critical(
                    "[cohost] 事件循环心跳停滞 %.1fs（阈值 %.1fs），自杀退出交由内核 respawn",
                    stalled,
                    self._stall_secs,
                )
                self._exit_fn(1)
                return


async def _heartbeat_loop(heartbeat: _Heartbeat, interval_secs: float = _HEARTBEAT_INTERVAL_SECS) -> None:
    """主循环内周期打点（事件循环健康即持续更新心跳时间戳）。"""
    while True:
        heartbeat.beat()
        await asyncio.sleep(interval_secs)


# ── 编排与入口 ───────────────────────────────────────────


async def _serve(server: CohostServer, stall_secs: float = _WATCHDOG_STALL_SECS_DEFAULT) -> None:
    """主循环编排：心跳任务 + watchdog + 聚合 MCP 服务（至 stdin EOF）。"""
    heartbeat = _Heartbeat()
    beat_task = asyncio.create_task(_heartbeat_loop(heartbeat))
    watchdog = _LoopWatchdog(heartbeat, stall_secs)
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

    成员加载失败 fail-fast：返回 1 并打印明确错误（内核按崩溃处理重试）。
    """
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    root = shared_root if shared_root is not None else _default_shared_root()
    member_ids = [m.strip() for m in args.members.split(",") if m.strip()]
    stall_secs = _watchdog_stall_secs()
    try:
        members = _load_members(root, member_ids)
        server = CohostServer(members)
    except (CohostError, ValueError) as exc:
        print(f"[cohost] 启动失败（fail-fast）：{exc}", file=sys.stderr)
        return 1
    print(
        f"[cohost] group={args.group} slot={args.slot} members={len(members)} tools={len(server.tool_names)}",
        file=sys.stderr,
    )
    asyncio.run(_serve(server, stall_secs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
