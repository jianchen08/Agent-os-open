"""共享辅助：清理平铺 import 的裸模块缓存，避免跨插件测试串扰。

背景：0.2 插件内部用平铺 import（``from plugin import X``、``from models import Y``），
多个插件的 server.py / plugin.py / models.py 同名。当 pytest 在同一会话里收集多个
插件的测试时，先加载的插件会把 ``plugin`` 缓存到 sys.modules，后一个插件的
``from plugin import ...`` 会命中缓存拿到错误模块
（ImportError: cannot import name 'StuckDetector' from 'plugin' (.../llm_core/plugin.py)）。

策略：tests/conftest.py 的 pytest_collect_file（收集期，每个测试文件导入前）
与 tests/plugins/conftest.py 的 pytest_runtest_setup（运行期，每个测试执行前），
根据该文件/测试所在目录的 conftest 声明的源目录（``_PLUGIN_SOURCE_DIRS``），
把源目录推到 sys.path 最前，并踢掉这些裸名（连同子模块）的缓存，
使测试内的 ``from plugin import ...`` 按本插件目录重新解析到正确文件。
"""

from __future__ import annotations

import os
import sys

# 已知的平铺模块名（跨插件同名冲突，逐出名单必须覆盖全部常见名）：
# - adapter：7 个插件同名（multimodal vs llm/llm_core 等）——逐出缺失时
#   multimodal 的 capabilities.py ``from adapter import ClaudeVisionAdapter``
#   会命中其他渠道的缓存模块而 ImportError；
# - tool：12 个插件同名（bash vs task_evaluate 等）；
# - workspace：特殊——system/workspace/ 是**无 __init__.py 的 namespace 包**，
#   而 tasks/workspace.py、isolation/workspace.py 是裸模块。PathFinder 规则：
#   命中 namespace portion 后仍继续扫描 sys.path，**后续普通模块优先**——
#   只要 tasks/isolation 目录还在 sys.path 上，``from workspace.models import``
#   就会把 'workspace' 槽位解析成 tasks/workspace.py（非包）→
#   ``No module named 'workspace.models'``。故除逐出缓存外还需配对
#   _PLUGIN_CONFLICT_DIRS 把含同名裸模块的目录从 sys.path 摘除
#   （与 tests/channels/conftest.py use_channel 同款纪律）。
_COLLIDING_NAMES = frozenset(
    {
        "plugin",
        "server",
        "models",
        "manager",
        "reversers",
        "decorators",
        "integration",
        "artifact_service",
        "annotation_service",
        "workspace",
        "adapter",
        "tool",
        "capabilities",
        "mm_types",
        "asr",
        # cost_control 平铺模块族：llm/exceptions.py 与 cost_control/exceptions.py
        # 同名（llm 测试先执行后残留 sys.modules，cost_control 模块级
        # from exceptions import 命中错误模块）。逐出必须成族——
        # exceptions 与 budget_manager/config/constants 一起删，否则
        # budget_manager 缓存仍绑定旧 exceptions 类，pytest.raises 捕获
        # 的新类与抛出类身份不一致（双实例）。
        "exceptions",
        "budget_manager",
        "constants",
        # cost_control 平铺 config.py 与仓根 config/ 包同名：裸名 `from config
        # import`（cost_control/server.py）驻留后，sdk isolation_policy 的
        # `from config.config_center import` 断裂（'config' is not a package）
        # → 安全规则注入链降级 → soft_block 误拦（2026-09-26 三扫实证）。
        "config",
        # 注：pipeline 不逐出——它是包（plugins/shared/pipeline/），不是平铺裸名
        # 模块；逐出会让已持引用的测试模块与插件重载拿到两份 PluginResult
        # 类对象，isinstance 恒假（level_guard/environment_lifecycle 共跑 31 红）。
        # mcp-bridge 网关的 policy.py 与其他平铺目录同名（security_check 测试
        # 置前 sys.path 后 from policy import 命中错误缓存）
        "policy",
    }
)


def find_conftest_declared_dirs(config: object, start_dir: str, attr: str) -> list[str]:
    """沿 start_dir 向上找最近一个声明了 ``attr`` 的 conftest，返回其目录列表。

    attr 取 ``_PLUGIN_SOURCE_DIRS``（插件源目录，按优先级排序）或
    ``_PLUGIN_CONFLICT_DIRS``（需从 sys.path 摘除的冲突目录）。

    从 pytest pluginmanager 注册表按 ``__file__`` 定位 conftest 模块：
    无 ``__init__.py`` 的测试目录下 pytest 把 conftest 以裸名 ``conftest``
    导入，多个此类目录互相顶掉 sys.modules['conftest'] 槽位（仅最后一个
    存活），注册表对每个 conftest 模块都有独立登记，不受该冲突影响。
    未找到声明返回空列表。
    """
    current = start_dir
    for _ in range(10):  # 最多向上查 10 层
        conftest_path = os.path.join(current, "conftest.py")
        if os.path.isfile(conftest_path):
            for plug in config.pluginmanager.get_plugins():
                plug_file = getattr(plug, "__file__", None)
                if plug_file and os.path.abspath(plug_file) == os.path.abspath(conftest_path):
                    dirs = getattr(plug, attr, None)
                    if dirs:
                        return list(dirs)
                    break
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return []


def evict_bare_modules() -> None:
    """踢掉可能冲突的裸模块缓存及其直接子模块。

    只清 _COLLIDING_NAMES 中的顶层名及其 ``top.sub`` 子模块，
    保留无关模块（pytest / _pytest / 测试自身包等）。
    """
    to_remove = []
    for name in list(sys.modules):
        top = name.split(".", 1)[0]
        if top in _COLLIDING_NAMES:
            to_remove.append(name)
    for name in to_remove:
        del sys.modules[name]


def promote_source_dirs(dirs: list[str]) -> None:
    """把给定源目录推到 sys.path 最前（保持参数顺序，第一个最优先）。

    已存在的条目先移除再插入到首位，确保本插件目录是 sys.path[0]。
    """
    # 先清掉已有的同名条目，避免重复
    for d in dirs:
        while d in sys.path:
            sys.path.remove(d)
    # 按「最后一个应在最前」的顺序插入头部
    for d in reversed(dirs):
        sys.path.insert(0, d)


def demote_conflict_dirs(dirs: list[str]) -> None:
    """把与目标插件冲突的目录从 sys.path 摘除（全部出现位置）。

    适用场景：目标插件目录是无 __init__.py 的 namespace 包（如
    system/workspace/），而冲突目录内有同名裸模块（tasks/workspace.py）——
    PathFinder 里普通模块优先于 namespace portion，只提升自身目录不够，
    必须把冲突目录摘掉才能让包形态胜出（与 channels use_channel 同款）。
    """
    for d in dirs:
        while d in sys.path:
            sys.path.remove(d)
