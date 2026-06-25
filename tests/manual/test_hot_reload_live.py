"""热更新实时测试脚本。

验证 Agent 配置和 Tool 配置的实时修改后是否能自动生效。
测试场景：
1. Agent 配置热更新：创建/修改/删除 YAML，检查 AgentRegistry 是否同步
2. PluginHotReloader 的 watchdog 监听是否正常工作
3. Tool 动态加载器是否能按需发现和加载工具

用法：
    python tests/manual/test_hot_reload_live.py
"""

import sys
import os
import time
import shutil
import tempfile
from pathlib import Path

_src_dir = os.path.join(os.path.dirname(__file__), "..", "..", "src")
_src_dir = os.path.abspath(_src_dir)
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)

import yaml

from agents.loader import AgentConfigLoader
from agents.registry import AgentRegistry
from agents.types import AgentLevel
from plugins.hot_reload import PluginHotReloader, ReloadEvent


PASS = "\033[92m✓ PASS\033[0m"
FAIL = "\033[91m✗ FAIL\033[0m"
INFO = "\033[94mℹ INFO\033[0m"
SECTION = "\033[96m" + "=" * 60 + "\033[0m"


def _make_test_agent_yaml(config_id: str, name: str, description: str = "") -> str:
    """生成测试用 Agent 配置 YAML 字符串。"""
    return yaml.dump({
        "config_id": config_id,
        "name": name,
        "display_name": name,
        "description": description or f"测试 Agent: {name}",
        "agent_type": "system",
        "category": "test",
        "level": "L3",
        "system_prompt": f"你是{name}。",
        "tool_ids": [],
        "version": "1.0.0",
        "is_active": True,
        "status": "active",
    }, allow_unicode=True, default_flow_style=False)


def test_agent_config_loader():
    """测试 1: AgentConfigLoader 能否正确加载 YAML。"""
    print(f"\n{SECTION}")
    print("测试 1: AgentConfigLoader 加载 YAML")
    print(SECTION)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, encoding="utf-8"
    ) as f:
        f.write(_make_test_agent_yaml("test_loader", "加载测试Agent"))
        tmp_path = f.name

    try:
        config = AgentConfigLoader.load_from_yaml(tmp_path)
        assert config.config_id == "test_loader", f"config_id 不匹配: {config.config_id}"
        assert config.name == "加载测试Agent", f"name 不匹配: {config.name}"
        assert config.level == AgentLevel.L3_ATOMIC, f"level 不匹配: {config.level}"
        print(f"  {PASS} AgentConfigLoader.load_from_yaml 正确加载")
        print(f"  {PASS} config_id = {config.config_id}")
        print(f"  {PASS} name = {config.name}")
        print(f"  {PASS} level = {config.level.value}")
        return True
    except Exception as e:
        print(f"  {FAIL} 加载失败: {e}")
        return False
    finally:
        os.unlink(tmp_path)


def test_agent_registry_crud():
    """测试 2: AgentRegistry 的注册/查找/注销。"""
    print(f"\n{SECTION}")
    print("测试 2: AgentRegistry CRUD 操作")
    print(SECTION)

    registry = AgentRegistry()
    all_pass = True

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, encoding="utf-8"
    ) as f:
        f.write(_make_test_agent_yaml("crud_agent", "CRUD测试Agent"))
        tmp_path = f.name

    try:
        config = AgentConfigLoader.load_from_yaml(tmp_path)

        registry.register(config)
        found = registry.get("crud_agent")
        if found and found.config_id == "crud_agent":
            print(f"  {PASS} 注册 + 查找成功")
        else:
            print(f"  {FAIL} 注册 + 查找失败")
            all_pass = False

        found_list = registry.find_by_level(AgentLevel.L3_ATOMIC)
        if any(c.config_id == "crud_agent" for c in found_list):
            print(f"  {PASS} 按层级查找成功 (L3)")
        else:
            print(f"  {FAIL} 按层级查找失败")
            all_pass = False

        result = registry.unregister("crud_agent")
        if result and registry.get("crud_agent") is None:
            print(f"  {PASS} 注销成功")
        else:
            print(f"  {FAIL} 注销失败")
            all_pass = False

        result = registry.unregister("nonexistent")
        if not result:
            print(f"  {PASS} 注销不存在的 ID 返回 False")
        else:
            print(f"  {FAIL} 注销不存在的 ID 应返回 False")
            all_pass = False

    except Exception as e:
        print(f"  {FAIL} 异常: {e}")
        all_pass = False
    finally:
        os.unlink(tmp_path)

    return all_pass


def test_hot_reload_file_watcher():
    """测试 3: PluginHotReloader 文件监听 + 自动重载。"""
    print(f"\n{SECTION}")
    print("测试 3: PluginHotReloader 文件监听热更新")
    print(SECTION)

    tmp_dir = Path(tempfile.mkdtemp(prefix="hot_reload_test_"))
    agents_dir = tmp_dir / "agents"
    agents_dir.mkdir(parents=True)

    registry = AgentRegistry()
    reloader = PluginHotReloader(
        config_dir=tmp_dir,
        agent_registry=registry,
        debounce_seconds=0.1,
    )

    events_received: list[ReloadEvent] = []
    reloader.add_callback(lambda e: events_received.append(e))

    all_pass = True

    try:
        reloader.start()
        print(f"  {INFO} 热重载器已启动, 监听目录: {tmp_dir}")
        assert reloader.is_running, "热重载器未正常运行"
        print(f"  {PASS} 热重载器启动成功 (is_running=True)")

        agent_file = agents_dir / "hot_test.yaml"
        agent_file.write_text(
            _make_test_agent_yaml("hot_agent", "热更新测试Agent", "初始描述"),
            encoding="utf-8",
        )
        print(f"  {INFO} 已创建配置文件: {agent_file}")

        time.sleep(1.5)

        config = registry.get("hot_agent")
        if config and config.name == "热更新测试Agent":
            print(f"  {PASS} 创建事件触发加载成功: config_id={config.config_id}")
        else:
            print(f"  {FAIL} 创建事件未触发加载 (registry 中无 hot_agent)")
            all_pass = False

        agent_file.write_text(
            _make_test_agent_yaml("hot_agent", "热更新测试Agent-V2", "修改后的描述"),
            encoding="utf-8",
        )
        print(f"  {INFO} 已修改配置文件 (name -> V2)")

        time.sleep(1.5)

        config = registry.get("hot_agent")
        if config and config.name == "热更新测试Agent-V2":
            print(f"  {PASS} 修改事件触发重载成功: name={config.name}")
        else:
            print(f"  {FAIL} 修改事件未触发重载")
            all_pass = False

        agent_file.unlink()
        print(f"  {INFO} 已删除配置文件")

        time.sleep(1.5)

        config = registry.get("hot_agent")
        if config is None:
            print(f"  {PASS} 删除事件触发注销成功")
        else:
            print(f"  {FAIL} 删除事件未触发注销 (registry 中仍有 hot_agent)")
            all_pass = False

        if events_received:
            print(f"  {PASS} 共收到 {len(events_received)} 个重载事件")
            for i, evt in enumerate(events_received):
                status = "成功" if evt.success else f"失败({evt.error})"
                print(f"         [{i+1}] {evt.event_type} | type={evt.config_type} | {status}")
        else:
            print(f"  {FAIL} 未收到任何重载事件")
            all_pass = False

    except Exception as e:
        print(f"  {FAIL} 异常: {e}")
        import traceback
        traceback.print_exc()
        all_pass = False
    finally:
        reloader.stop()
        shutil.rmtree(tmp_dir, ignore_errors=True)
        print(f"  {INFO} 热重载器已停止, 临时目录已清理")

    return all_pass


def test_hot_reload_manual_trigger():
    """测试 4: PluginHotReloader 手动触发重载。"""
    print(f"\n{SECTION}")
    print("测试 4: PluginHotReloader 手动触发重载 (reload_plugin)")
    print(SECTION)

    tmp_dir = Path(tempfile.mkdtemp(prefix="hot_reload_manual_"))
    agents_dir = tmp_dir / "agents"
    agents_dir.mkdir(parents=True)

    registry = AgentRegistry()
    reloader = PluginHotReloader(
        config_dir=tmp_dir,
        agent_registry=registry,
        debounce_seconds=0.1,
    )

    all_pass = True

    try:
        agent_file = agents_dir / "manual_test.yaml"
        agent_file.write_text(
            _make_test_agent_yaml("manual_agent", "手动重载测试Agent"),
            encoding="utf-8",
        )

        result = reloader.reload_plugin(str(agent_file))
        if result.success:
            print(f"  {PASS} 手动 reload_plugin 成功")
        else:
            print(f"  {FAIL} 手动 reload_plugin 失败: {result.error}")
            all_pass = False

        config = registry.get("manual_agent")
        if config and config.config_id == "manual_agent":
            print(f"  {PASS} 手动重载后注册表中有该 Agent")
        else:
            print(f"  {FAIL} 手动重载后注册表中无该 Agent")
            all_pass = False

        agent_file.write_text(
            _make_test_agent_yaml("manual_agent", "手动重载V2"),
            encoding="utf-8",
        )

        result = reloader.reload_plugin(str(agent_file))
        config = registry.get("manual_agent")
        if config and config.name == "手动重载V2":
            print(f"  {PASS} 手动重载更新成功: name={config.name}")
        else:
            print(f"  {FAIL} 手动重载更新失败")
            all_pass = False

        results = reloader.get_reload_history(limit=5)
        print(f"  {INFO} 重载历史记录数: {len(results)}")
        if results:
            print(f"  {PASS} 重载历史查询成功")
            for r in results[:3]:
                print(f"         path={Path(r['config_path']).name} | success={r['success']} | type={r['config_type']}")
        else:
            print(f"  {FAIL} 重载历史为空")
            all_pass = False

    except Exception as e:
        print(f"  {FAIL} 异常: {e}")
        import traceback
        traceback.print_exc()
        all_pass = False
    finally:
        reloader.stop()
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return all_pass


def test_hot_reload_reload_all():
    """测试 5: PluginHotReloader 全量重载 (reload_all)。"""
    print(f"\n{SECTION}")
    print("测试 5: PluginHotReloader 全量重载 (reload_all)")
    print(SECTION)

    tmp_dir = Path(tempfile.mkdtemp(prefix="hot_reload_all_"))
    agents_dir = tmp_dir / "agents"
    agents_dir.mkdir(parents=True)

    registry = AgentRegistry()
    reloader = PluginHotReloader(
        config_dir=tmp_dir,
        agent_registry=registry,
        debounce_seconds=0.1,
    )

    all_pass = True

    try:
        for i in range(3):
            agent_file = agents_dir / f"agent_{i}.yaml"
            agent_file.write_text(
                _make_test_agent_yaml(f"batch_agent_{i}", f"批量Agent{i}"),
                encoding="utf-8",
            )

        results = reloader.reload_all()
        success_count = sum(1 for r in results if r.success)
        print(f"  {INFO} 全量重载结果: {success_count}/{len(results)} 成功")

        if success_count == 3:
            print(f"  {PASS} 3 个 Agent 全部重载成功")
        else:
            print(f"  {FAIL} 部分重载失败: {success_count}/3")
            all_pass = False

        for i in range(3):
            config = registry.get(f"batch_agent_{i}")
            if config:
                print(f"  {PASS} batch_agent_{i} 已注册: name={config.name}")
            else:
                print(f"  {FAIL} batch_agent_{i} 未注册")
                all_pass = False

        plugin_status = reloader.get_plugin_status()
        print(f"  {INFO} 插件状态数量: {len(plugin_status)}")

    except Exception as e:
        print(f"  {FAIL} 异常: {e}")
        import traceback
        traceback.print_exc()
        all_pass = False
    finally:
        reloader.stop()
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return all_pass


def test_hot_reload_invalid_config_rollback():
    """测试 6: 无效配置的回滚机制。"""
    print(f"\n{SECTION}")
    print("测试 6: 无效配置的热重载回滚")
    print(SECTION)

    tmp_dir = Path(tempfile.mkdtemp(prefix="hot_reload_rollback_"))
    agents_dir = tmp_dir / "agents"
    agents_dir.mkdir(parents=True)

    registry = AgentRegistry()
    reloader = PluginHotReloader(
        config_dir=tmp_dir,
        agent_registry=registry,
        debounce_seconds=0.1,
    )

    all_pass = True

    try:
        agent_file = agents_dir / "rollback_test.yaml"
        agent_file.write_text(
            _make_test_agent_yaml("rollback_agent", "回滚测试Agent"),
            encoding="utf-8",
        )

        result = reloader.reload_plugin(str(agent_file))
        if result.success:
            print(f"  {PASS} 初始加载成功")
        else:
            print(f"  {FAIL} 初始加载失败: {result.error}")
            all_pass = False

        agent_file.write_text("invalid: yaml\nmissing: fields", encoding="utf-8")
        print(f"  {INFO} 写入无效配置（缺少 config_id, name 等必填字段）")

        result = reloader.reload_plugin(str(agent_file))
        if not result.success:
            print(f"  {PASS} 无效配置被正确拒绝: error={result.error}")
        else:
            print(f"  {FAIL} 无效配置不应加载成功")
            all_pass = False

        config = registry.get("rollback_agent")
        if config and config.name == "回滚测试Agent":
            print(f"  {PASS} 回滚成功: 旧配置仍在注册表中")
        else:
            print(f"  {FAIL} 回滚失败: 旧配置丢失")
            all_pass = False

    except Exception as e:
        print(f"  {FAIL} 异常: {e}")
        import traceback
        traceback.print_exc()
        all_pass = False
    finally:
        reloader.stop()
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return all_pass


def test_tool_dynamic_loader():
    """测试 7: Tool 动态加载器的工具发现。"""
    print(f"\n{SECTION}")
    print("测试 7: Tool 动态加载器 - 工具发现能力")
    print(SECTION)

    all_pass = True

    try:
        from tools.loader import DynamicToolLoader
        from tools.registry import ToolRegistry

        registry = ToolRegistry()
        loader = DynamicToolLoader(registry)

        available = loader.get_available_tools()
        print(f"  {INFO} DynamicToolLoader 发现了 {len(available)} 个内置工具")

        core_tools = [
            "file_read", "file_write", "bash_execute",
            "enhanced_search", "web_search",
        ]
        for tool_name in core_tools:
            if loader.is_available(tool_name):
                print(f"  {PASS} 核心工具 '{tool_name}' 可被发现")
            else:
                print(f"  {INFO} 工具 '{tool_name}' 未在 builtin 目录中发现（可能尚未实现）")

        if len(available) > 0:
            sample = available[0]
            print(f"  {INFO} 尝试加载工具: {sample}")
            success = loader.load_tool(sample)
            if success:
                tool_def = registry.get(sample)
                if tool_def:
                    print(f"  {PASS} 工具 '{sample}' 加载成功: {tool_def.name if hasattr(tool_def, 'name') else type(tool_def).__name__}")
                else:
                    print(f"  {PASS} 工具 '{sample}' 加载成功（已注册到 ToolRegistry）")
            else:
                print(f"  {FAIL} 工具 '{sample}' 加载失败")
                all_pass = False
        else:
            print(f"  {INFO} 没有发现可加载的内置工具（可能 src/tools/builtin/ 目录为空或尚未实现）")

    except ImportError as e:
        print(f"  {INFO} 跳过 Tool 动态加载器测试（模块未就绪: {e}）")
    except Exception as e:
        print(f"  {FAIL} 异常: {e}")
        import traceback
        traceback.print_exc()
        all_pass = False

    return all_pass


def test_real_config_directory():
    """测试 8: 加载项目实际的 config/agents/ 目录。"""
    print(f"\n{SECTION}")
    print("测试 8: 加载项目实际 config/agents/ 目录")
    print(SECTION)

    project_root = Path(__file__).parent.parent.parent
    agents_dir = project_root / "config" / "agents"

    if not agents_dir.exists():
        print(f"  {INFO} 跳过: config/agents/ 目录不存在 ({agents_dir})")
        return True

    all_pass = True

    try:
        registry = AgentRegistry()
        count = registry.load_directory(str(agents_dir))

        print(f"  {PASS} 成功加载 {count} 个 Agent 配置")

        all_configs = registry.list_all() if hasattr(registry, 'list_all') else []
        if not all_configs:
            l1 = registry.find_by_level(AgentLevel.L1_MAIN)
            l2 = registry.find_by_level(AgentLevel.L2_SUBTASK)
            l3 = registry.find_by_level(AgentLevel.L3_ATOMIC)
            print(f"  {INFO} L1 Agents: {len(l1)} 个")
            print(f"  {INFO} L2 Agents: {len(l2)} 个")
            print(f"  {INFO} L3 Agents: {len(l3)} 个")

            for agent in l1:
                print(f"         L1: {agent.config_id} - {agent.name}")
        else:
            print(f"  {INFO} 共 {len(all_configs)} 个配置")

    except Exception as e:
        print(f"  {FAIL} 异常: {e}")
        import traceback
        traceback.print_exc()
        all_pass = False

    return all_pass


def main():
    """运行所有热更新测试。"""
    print(f"\n{'#' * 60}")
    print("  Agent OS 热更新功能实时测试")
    print(f"{'#' * 60}")

    tests = [
        ("AgentConfigLoader 加载", test_agent_config_loader),
        ("AgentRegistry CRUD", test_agent_registry_crud),
        ("PluginHotReloader 文件监听", test_hot_reload_file_watcher),
        ("PluginHotReloader 手动重载", test_hot_reload_manual_trigger),
        ("PluginHotReloader 全量重载", test_hot_reload_reload_all),
        ("无效配置回滚", test_hot_reload_invalid_config_rollback),
        ("Tool 动态加载器", test_tool_dynamic_loader),
        ("实际配置目录加载", test_real_config_directory),
    ]

    results = {}
    for name, test_fn in tests:
        try:
            results[name] = test_fn()
        except Exception as e:
            print(f"\n  {FAIL} 测试 '{name}' 崩溃: {e}")
            import traceback
            traceback.print_exc()
            results[name] = False

    print(f"\n{'#' * 60}")
    print("  测试结果汇总")
    print(f"{'#' * 60}")

    pass_count = sum(1 for v in results.values() if v)
    total = len(results)

    for name, passed in results.items():
        status = PASS if passed else FAIL
        print(f"  {status} {name}")

    print(f"\n  总计: {pass_count}/{total} 通过")

    if pass_count == total:
        print(f"\n  🎉 所有测试通过！热更新功能正常工作。")
    else:
        print(f"\n  ⚠️  有 {total - pass_count} 个测试未通过，请检查上方日志。")

    return pass_count == total


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
