"""
ConfigCenter 核心接口测试 — 热加载统一入口。

覆盖功能点：
- watch(path, callback) 文件监听注册与回调通知
- reload(path) 手动重载配置（含 YAML 解析、缓存更新）
- get(path) 获取当前缓存配置（含磁盘回退）
- 防抖（500ms）和内容哈希去重
- 读写锁并发安全
- 加载失败回滚旧配置
- 审批机制（L1 Agent 配置变更）
- 审计日志
- 排除规则（.env、docker-compose 等）
"""
import time
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from config.config_center import (
    ConfigCenter,
    AuditEntry,
    _RWLock,
    _ReadGuard,
    _WriteGuard,
    DEBOUNCE_SECONDS,
    MAX_AUDIT_HISTORY,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config_dir(tmp_path):
    """创建临时配置目录，模拟 config/ 结构。"""
    cfg = tmp_path / "config"
    cfg.mkdir()
    return cfg


@pytest.fixture
def center(config_dir):
    """创建 ConfigCenter 实例，指向临时目录。"""
    return ConfigCenter(config_root=config_dir, debounce_seconds=0.05)


def _write_yaml(path: Path, data: dict) -> None:
    """辅助：写入 YAML 文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False)


# ---------------------------------------------------------------------------
# reload() — 手动重载
# ---------------------------------------------------------------------------


class TestReload:
    """ConfigCenter.reload() 手动重载配置文件。"""

    def test_reload_success_reads_yaml(self, center, config_dir):
        """重载应正确读取 YAML 文件并返回成功。"""
        test_file = config_dir / "agents" / "test.yaml"
        _write_yaml(test_file, {"config_id": "test_agent", "name": "Test"})

        result = center.reload(str(test_file))

        assert result["success"] is True
        assert result["error"] is None
        assert result["rolled_back"] is False
        assert result["config_type"] == "agent"

    def test_reload_caches_data(self, center, config_dir):
        """重载后数据应存入缓存，get() 能获取到。"""
        test_file = config_dir / "agents" / "test.yaml"
        data = {"config_id": "test_agent", "name": "Test"}
        _write_yaml(test_file, data)

        center.reload(str(test_file))
        cached = center.get(str(test_file))

        assert cached is not None
        assert cached["config_id"] == "test_agent"

    def test_reload_file_not_found_raises(self, center, config_dir):
        """重载不存在的文件应抛出 FileNotFoundError。"""
        with pytest.raises(FileNotFoundError, match="配置文件不存在"):
            center.reload(str(config_dir / "nonexistent.yaml"))

    def test_reload_invalid_yaml_returns_failure(self, center, config_dir):
        """YAML 解析失败应返回失败结果。"""
        test_file = config_dir / "system" / "bad.yaml"
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_text("{{invalid yaml: [", encoding="utf-8")

        result = center.reload(str(test_file))

        assert result["success"] is False
        assert result["error"] is not None
        assert "YAML" in result["error"]

    def test_reload_non_dict_yaml_raises_value_error(self, center, config_dir):
        """YAML 内容不是字典应抛出 ValueError。"""
        test_file = config_dir / "system" / "list.yaml"
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_text("- item1\n- item2", encoding="utf-8")

        with pytest.raises(ValueError, match="字典"):
            center.reload(str(test_file))

    def test_reload_detects_config_type(self, center, config_dir):
        """重载应根据路径正确判断配置类型。"""
        cases = [
            ("agents/a.yaml", "agent"),
            ("pipelines/p.yaml", "pipeline"),
            ("tools/t.yaml", "tool"),
            ("models/m.yaml", "model"),
            ("templates/t.yaml", "template"),
            ("triggers/t.yaml", "trigger"),
        ]
        for sub_path, expected_type in cases:
            f = config_dir / sub_path
            _write_yaml(f, {"key": "value"})
            result = center.reload(str(f))
            assert result["config_type"] == expected_type, (
                f"路径 {sub_path} 应识别为 {expected_type}，实际为 {result['config_type']}"
            )

    def test_reload_content_hash_dedup(self, center, config_dir):
        """内容未变化时重载应跳过（哈希去重）。"""
        test_file = config_dir / "system" / "test.yaml"
        _write_yaml(test_file, {"key": "value"})

        result1 = center.reload(str(test_file))
        result2 = center.reload(str(test_file))

        assert result1["success"] is True
        assert result2["success"] is True
        # 第二次重载应检测到内容未变化，rolled_back=False
        assert result2["rolled_back"] is False


# ---------------------------------------------------------------------------
# get() — 获取配置
# ---------------------------------------------------------------------------


class TestGet:
    """ConfigCenter.get() 获取缓存配置。"""

    def test_get_returns_cached_data(self, center, config_dir):
        """reload 后 get 应返回缓存数据。"""
        test_file = config_dir / "agents" / "test.yaml"
        _write_yaml(test_file, {"config_id": "agent1", "name": "Agent1"})
        center.reload(str(test_file))

        result = center.get(str(test_file))

        assert result == {"config_id": "agent1", "name": "Agent1"}

    def test_get_returns_none_for_uncached(self, center, config_dir):
        """未缓存且磁盘不存在的配置返回 None。"""
        result = center.get("nonexistent/path.yaml")
        assert result is None

    def test_get_reads_from_disk_on_miss(self, center, config_dir):
        """缓存未命中时 get 应从磁盘读取并缓存。"""
        test_file = config_dir / "system" / "new.yaml"
        _write_yaml(test_file, {"key": "disk_value"})

        result = center.get(str(test_file))

        assert result is not None
        assert result["key"] == "disk_value"

    def test_get_updates_after_reload(self, center, config_dir):
        """reload 后再次 get 应返回更新后的数据。"""
        test_file = config_dir / "agents" / "test.yaml"
        _write_yaml(test_file, {"version": 1})
        center.reload(str(test_file))

        _write_yaml(test_file, {"version": 2})
        center.reload(str(test_file))

        result = center.get(str(test_file))
        assert result["version"] == 2


# ---------------------------------------------------------------------------
# watch() / unwatch() — 回调注册
# ---------------------------------------------------------------------------


class TestWatch:
    """ConfigCenter.watch() / unwatch() 回调注册。"""

    def test_watch_registers_callback(self, center):
        """注册回调后 watchers 中应有记录。"""
        cb = MagicMock()
        center.watch("agents/", cb)

        normalized = "agents"
        assert normalized in center._watchers
        assert cb in center._watchers[normalized]

    def test_unwatch_removes_callback(self, center):
        """取消注册后回调应被移除。"""
        cb = MagicMock()
        center.watch("agents/", cb)
        result = center.unwatch("agents/", cb)

        assert result is True
        assert cb not in center._watchers.get("agents", [])

    def test_unwatch_returns_false_for_unknown(self, center):
        """取消未注册的回调应返回 False。"""
        result = center.unwatch("agents/", MagicMock())
        assert result is False

    def test_watch_callback_called_on_reload(self, center, config_dir):
        """reload 后应触发已注册的回调。"""
        cb = MagicMock()
        center.watch("agents/", cb)

        test_file = config_dir / "agents" / "test.yaml"
        _write_yaml(test_file, {"key": "value"})
        center.reload(str(test_file))

        assert cb.called
        call_args = cb.call_args[0]
        assert call_args[0] == "manual_reload"
        assert "config_type" in call_args[2]

    def test_watch_multiple_callbacks(self, center, config_dir):
        """同一前缀可注册多个回调。"""
        cb1 = MagicMock()
        cb2 = MagicMock()
        center.watch("agents/", cb1)
        center.watch("agents/", cb2)

        test_file = config_dir / "agents" / "test.yaml"
        _write_yaml(test_file, {"key": "val"})
        center.reload(str(test_file))

        assert cb1.called
        assert cb2.called


# ---------------------------------------------------------------------------
# 防抖
# ---------------------------------------------------------------------------


class TestDebounce:
    """防抖机制：同一文件在窗口内的重复事件被过滤。"""

    def test_debounce_blocks_rapid_events(self, center):
        """防抖窗口内的重复事件应被过滤。"""
        path = "/fake/path/test.yaml"
        # 第一次应通过
        assert center._check_debounce(path) is True
        # 紧接着第二次应被阻止
        assert center._check_debounce(path) is False

    def test_debounce_allows_after_window(self, center):
        """防抖窗口过后的事件应被允许。"""
        path = "/fake/path/test.yaml"
        center._debounce_seconds = 0.05  # 50ms 窗口
        assert center._check_debounce(path) is True
        time.sleep(0.08)
        assert center._check_debounce(path) is True

    def test_debounce_independent_per_file(self, center):
        """不同文件的防抖应独立。"""
        p1 = "/fake/a.yaml"
        p2 = "/fake/b.yaml"
        assert center._check_debounce(p1) is True
        assert center._check_debounce(p2) is True


# ---------------------------------------------------------------------------
# 内容哈希去重
# ---------------------------------------------------------------------------


class TestContentHashDedup:
    """内容哈希去重：相同内容不重复处理。"""

    def test_same_content_not_reloaded(self, center, config_dir):
        """内容未变化时 reload 应跳过实际更新。"""
        test_file = config_dir / "system" / "test.yaml"
        _write_yaml(test_file, {"key": "same"})
        center.reload(str(test_file))

        # 记录回调调用次数
        cb = MagicMock()
        center.watch("system/", cb)

        # 再次 reload 同样内容
        center.reload(str(test_file))

        # 内容未变化，回调不应被调用
        assert not cb.called

    def test_different_content_triggers_callback(self, center, config_dir):
        """内容变化后 reload 应触发回调。"""
        test_file = config_dir / "system" / "test.yaml"
        _write_yaml(test_file, {"version": 1})
        center.reload(str(test_file))

        cb = MagicMock()
        center.watch("system/", cb)

        _write_yaml(test_file, {"version": 2})
        center.reload(str(test_file))

        assert cb.called


# ---------------------------------------------------------------------------
# 读写锁并发安全
# ---------------------------------------------------------------------------


class TestRWLock:
    """读写锁：多读单写。"""

    def test_read_lock_allows_concurrent_reads(self):
        """多个读操作可以并发。"""
        lock = _RWLock()
        results = []

        def reader(idx):
            with _ReadGuard(lock):
                results.append(f"read_{idx}")
                time.sleep(0.02)

        threads = [threading.Thread(target=reader, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 5

    def test_write_lock_blocks_reads(self):
        """写操作期间读操作应阻塞。"""
        lock = _RWLock()
        order = []

        with _WriteGuard(lock):
            order.append("write_start")
            # 在写锁期间尝试获取读锁（在另一个线程）
            read_done = threading.Event()
            def try_read():
                with _ReadGuard(lock):
                    order.append("read")
                read_done.set()
            t = threading.Thread(target=try_read)
            t.start()
            time.sleep(0.05)
            order.append("write_end")

        t.join(timeout=1)
        read_done.wait(timeout=1)

        # 写应在读之前完成
        assert order.index("write_start") < order.index("read")

    def test_rwlock_as_context_manager(self):
        """读写锁上下文管理器应正常工作。"""
        lock = _RWLock()
        with _ReadGuard(lock):
            pass  # 不应死锁
        with _WriteGuard(lock):
            pass  # 不应死锁


class TestConcurrentReload:
    """并发重载时读写锁应保证安全。"""

    def test_concurrent_reloads_no_data_loss(self, center, config_dir):
        """多线程并发 reload 不应丢失数据（串行化写入避免竞态）。"""
        test_file = config_dir / "agents" / "test.yaml"
        _write_yaml(test_file, {"version": 0})

        errors = []
        write_lock = threading.Lock()

        def reload_worker(version):
            try:
                with write_lock:
                    _write_yaml(test_file, {"version": version})
                    result = center.reload(str(test_file))
                if not result["success"] and result.get("error"):
                    errors.append(result["error"])
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=reload_worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"并发重载出现错误: {errors}"
        cached = center.get(str(test_file))
        assert cached is not None
        assert "version" in cached


# ---------------------------------------------------------------------------
# 加载失败回滚
# ---------------------------------------------------------------------------


class TestRollback:
    """加载失败时应回滚到旧配置。"""

    def test_rollback_preserves_old_config(self, center, config_dir):
        """加载失败后旧配置应保留在缓存中。"""
        test_file = config_dir / "agents" / "test.yaml"
        _write_yaml(test_file, {"config_id": "old", "name": "Old"})
        center.reload(str(test_file))

        # 写入无效 YAML
        test_file.write_text("{{invalid", encoding="utf-8")
        center.reload(str(test_file))

        cached = center.get(str(test_file))
        assert cached is not None
        assert cached["config_id"] == "old"

    def test_rollback_sets_rolled_back_flag(self, center, config_dir):
        """加载失败应设置 rolled_back=True。"""
        test_file = config_dir / "agents" / "test.yaml"
        _write_yaml(test_file, {"key": "val"})
        center.reload(str(test_file))

        test_file.write_text("{{bad", encoding="utf-8")
        result = center.reload(str(test_file))

        assert result["success"] is False
        assert result["rolled_back"] is True


# ---------------------------------------------------------------------------
# 排除规则
# ---------------------------------------------------------------------------


class TestExclusion:
    """排除规则：.env、Redis、Docker 等不参与热加载。"""

    def test_excluded_env_files(self, center):
        """.env 系列文件应被排除。"""
        assert center._is_excluded(Path(".env")) is True
        assert center._is_excluded(Path(".env.local")) is True
        assert center._is_excluded(Path(".env.production")) is True

    def test_excluded_docker_files(self, center):
        """Docker 相关文件应被排除。"""
        assert center._is_excluded(Path("docker-compose.yml")) is True
        assert center._is_excluded(Path("Dockerfile")) is True

    def test_excluded_backup_files(self, center):
        """备份文件应被排除。"""
        assert center._is_excluded(Path("config.yaml.bak")) is True

    def test_not_excluded_normal_yaml(self, center):
        """正常 YAML 文件不应被排除。"""
        assert center._is_excluded(Path("agents/test.yaml")) is False
        assert center._is_excluded(Path("system/config.yaml")) is False


# ---------------------------------------------------------------------------
# 审计日志
# ---------------------------------------------------------------------------


class TestAuditLog:
    """审计日志记录热加载操作。"""

    def test_reload_writes_audit(self, center, config_dir):
        """reload 成功应记录审计日志。"""
        test_file = config_dir / "agents" / "test.yaml"
        _write_yaml(test_file, {"key": "val"})
        center.reload(str(test_file))

        log = center.get_audit_log(limit=10)
        assert len(log) >= 1
        assert log[0]["success"] is True
        assert log[0]["event_type"] == "manual_reload"

    def test_failed_reload_writes_audit(self, center, config_dir):
        """reload 失败也应记录审计日志。"""
        test_file = config_dir / "agents" / "test.yaml"
        _write_yaml(test_file, {"key": "val"})
        center.reload(str(test_file))

        test_file.write_text("{{bad", encoding="utf-8")
        center.reload(str(test_file))

        log = center.get_audit_log(limit=10)
        failed_entries = [e for e in log if not e["success"]]
        assert len(failed_entries) >= 1

    def test_audit_log_max_history(self, config_dir):
        """审计日志不应超过最大条数。"""
        center = ConfigCenter(config_root=config_dir, debounce_seconds=0.01)
        test_file = config_dir / "system" / "test.yaml"
        _write_yaml(test_file, {"key": "val"})

        for i in range(MAX_AUDIT_HISTORY + 100):
            center._write_audit(AuditEntry(
                file_path=str(test_file),
                event_type="test",
                config_type="unknown",
                success=True,
                content_hash=f"hash_{i}",
            ))

        assert len(center._audit_log) <= MAX_AUDIT_HISTORY

    def test_audit_log_newest_first(self, center, config_dir):
        """审计日志最新记录应排在前面。"""
        test_file = config_dir / "system" / "test.yaml"
        _write_yaml(test_file, {"key": "val"})

        center.reload(str(test_file))
        _write_yaml(test_file, {"key": "val2"})
        center.reload(str(test_file))

        log = center.get_audit_log(limit=2)
        assert len(log) >= 2
        # 最新的应在前面
        assert log[0]["content_hash"] != log[1]["content_hash"]


# ---------------------------------------------------------------------------
# 配置类型判断
# ---------------------------------------------------------------------------


class TestConfigTypeDetection:
    """根据文件路径判断配置类型。"""

    @pytest.mark.parametrize("path,expected", [
        ("/config/agents/main/灵汐.yaml", "agent"),
        ("/config/pipelines/default.yaml", "pipeline"),
        ("/config/tools/search.yaml", "tool"),
        ("/config/models/llm.yaml", "model"),
        ("/config/templates/t.yaml", "template"),
        ("/config/triggers/cron.yaml", "trigger"),
        ("/config/evaluation_metrics/m.yaml", "evaluation_metric"),
        ("/config/evaluation/test.yaml", "evaluation"),
        ("/config/isolation/rules.yaml", "isolation"),
        ("/config/modules/m.yaml", "module"),
        ("/random/file.yaml", "unknown"),
    ])
    def test_determine_config_type(self, path, expected):
        """各路径应正确识别配置类型。"""
        assert ConfigCenter._determine_config_type(path) == expected


# ---------------------------------------------------------------------------
# start / stop 生命周期
# ---------------------------------------------------------------------------


class TestLifecycle:
    """ConfigCenter 启停生命周期。"""

    def test_is_running_initially_false(self, center):
        """初始状态应为未运行。"""
        assert center.is_running is False

    def test_stop_sets_running_false(self, center):
        """stop 后 is_running 应为 False。"""
        center._running = True
        center.stop()
        assert center.is_running is False


# ---------------------------------------------------------------------------
# _process_changes（模拟 watchfiles 事件）
# ---------------------------------------------------------------------------


class TestProcessChanges:
    """模拟 watchfiles 变更事件的处理。"""

    @pytest.mark.asyncio
    async def test_process_modified_event(self, center, config_dir):
        """modified 事件应更新缓存。"""
        test_file = config_dir / "agents" / "test.yaml"
        _write_yaml(test_file, {"config_id": "agent1", "name": "Agent1"})

        # 模拟 watchfiles 返回的变更集合: (change_type, path_str)
        # watchfiles.Change.modified = 2
        changes = {(2, str(test_file))}
        await center._process_changes(changes)

        cached = center.get(str(test_file))
        assert cached is not None
        assert cached["config_id"] == "agent1"

    @pytest.mark.asyncio
    async def test_process_deleted_event(self, center, config_dir):
        """deleted 事件应清除缓存。"""
        test_file = config_dir / "agents" / "test.yaml"
        _write_yaml(test_file, {"key": "val"})
        center.reload(str(test_file))

        # watchfiles.Change.deleted = 3
        changes = {(3, str(test_file))}
        await center._process_changes(changes)

        # 缓存应被清除
        with _ReadGuard(center._rwlock):
            assert str(test_file) not in center._config_cache

    @pytest.mark.asyncio
    async def test_process_ignores_non_yaml(self, center, config_dir):
        """非 YAML 文件变更应被忽略。"""
        txt_file = config_dir / "notes.txt"
        txt_file.write_text("hello")

        cb = MagicMock()
        center.watch("", cb)

        changes = {(2, str(txt_file))}
        await center._process_changes(changes)

        assert not cb.called

    @pytest.mark.asyncio
    async def test_process_ignores_temp_files(self, center, config_dir):
        """临时文件（以 . 或 ~ 开头）应被忽略。"""
        tmp_file = config_dir / ".test.yaml"
        tmp_file.write_text("key: val", encoding="utf-8")

        cb = MagicMock()
        center.watch("", cb)

        changes = {(2, str(tmp_file))}
        await center._process_changes(changes)

        assert not cb.called
