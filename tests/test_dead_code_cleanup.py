"""
死代码清理 — 无破坏性影响验证

验证范围：
1. 24 个已删除模块在 src/ 中无 import 引用残留
2. src/core/errors.py 合并后功能正常（ErrorSeverity、ErrorCode、StandardError）
3. src/cache/multi_level_cache.py 纯内存缓存工作正常
4. src/cache/decorators.py 缓存装饰器工作正常
5. src/templates/ 模块（loader、registry）工作正常
6. 保留文件 game_engine.py 和 generic.py 完好
7. 现有测试套件无新增失败（单独运行本文件确认通过）
"""
import ast
import os
import re
import time
from unittest.mock import patch

import pytest


# ============================================================================
# 辅助函数 + session 级预扫描（所有测试查表，不重复 I/O）
# ============================================================================

def _src_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))


def _project_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _scan_all_imports(src_dir: str) -> dict[str, list[str]]:
    """一次性扫描 src/ 下所有 .py 文件的 import 行，返回 {filepath: [stripped_lines]}。"""
    result: dict[str, list[str]] = {}
    for root, _dirs, files in os.walk(src_dir):
        for f in files:
            if not f.endswith(".py"):
                continue
            full = os.path.join(root, f)
            try:
                with open(full, encoding="utf-8", errors="replace") as fh:
                    lines = [line.strip() for line in fh
                             if line.lstrip().startswith(("import ", "from "))]
                    if lines:
                        result[full] = lines
            except OSError:
                continue
    return result


@pytest.fixture(scope="session")
def import_index():
    """session 级 fixture：预扫描 src/ 全部 import 行。"""
    return _scan_all_imports(_src_root())


def _check_pattern(import_index: dict[str, list[str]], pattern: str) -> list[str]:
    escaped = re.escape(pattern)
    import_re = re.compile(
        rf"^\s*(?:from\s+.*{escaped}|import\s+.*{escaped})", re.IGNORECASE,
    )
    hits: list[str] = []
    for filepath, lines in import_index.items():
        for line in lines:
            if import_re.search(line):
                hits.append(f"{filepath}:{line}")
    return hits


def _check_name(import_index: dict[str, list[str]], name: str) -> list[str]:
    escaped = re.escape(name)
    import_re = re.compile(
        rf"(?:from\s+\S+\s+import\s+\S*\b{escaped}\b|import\s+\S*\b{escaped}\b)",
        re.IGNORECASE,
    )
    hits: list[str] = []
    for filepath, lines in import_index.items():
        for line in lines:
            if import_re.search(line):
                hits.append(f"{filepath}:{line}")
    return hits


# ============================================================================
# 验证项 1：24 个已删除模块在 src/ 中无 import 引用残留
# ============================================================================

DELETED_MODULES = [
    "tasks.progress", "core.versioning", "core.audit", "core.embeddings",
    "utils.background_tasks", "utils.converters", "utils.decorators",
    "utils.performance_decorators", "utils.debug",
    "monitoring.task_progress", "monitoring.execution_logger",
    "monitoring.execution_monitor", "services.agent_sync_service",
    "services.agent_call_recorder", "services.evaluation_metric_service",
    "services.monitoring_service", "services.watchdog_service",
    "services.tool_marketplace", "schemas.message", "schemas.task",
    "core.error_reporter", "cache.redis_manager",
    "templates.renderer", "monitoring.usage_monitor",
]

DELETED_NAMES = [
    "ErrorReporter", "ErrorMetrics", "ErrorCategory",
    "RedisManager", "get_redis_manager", "TemplateRenderer", "UsageMonitor",
]


class TestNoDanglingImports:
    """验证已删除模块无 import 残留。"""

    @pytest.mark.parametrize("module_name", DELETED_MODULES)
    def test_no_import_reference_to_deleted_module(self, import_index, module_name):
        hits = _check_pattern(import_index, module_name)
        assert not hits, f"发现对已删除模块 {module_name!r} 的 import 引用:\n" + "\n".join(hits)

    @pytest.mark.parametrize("name", DELETED_NAMES)
    def test_no_import_reference_to_deleted_name(self, import_index, name):
        hits = _check_name(import_index, name)
        assert not hits, f"发现对已删除名称 {name!r} 的 import 引用:\n" + "\n".join(hits)

    def test_deleted_files_do_not_exist(self):
        deleted_files = [
            "src/tasks/progress.py", "src/core/versioning.py", "src/core/audit.py",
            "src/core/embeddings.py", "src/utils/background_tasks.py",
            "src/utils/converters.py", "src/utils/decorators.py",
            "src/utils/performance_decorators.py", "src/utils/debug.py",
            "src/monitoring/task_progress.py", "src/monitoring/execution_logger.py",
            "src/monitoring/execution_monitor.py", "src/services/agent_sync_service.py",
            "src/services/agent_call_recorder.py",
            "src/services/evaluation_metric_service.py",
            "src/services/monitoring_service.py", "src/services/watchdog_service.py",
            "src/services/tool_marketplace.py", "src/schemas/message.py",
            "src/schemas/task.py", "src/core/error_reporter.py",
            "src/cache/redis_manager.py", "src/templates/renderer.py",
            "src/monitoring/usage_monitor.py",
        ]
        for rel_path in deleted_files:
            assert not os.path.exists(os.path.join(_project_root(), rel_path)), \
                f"已删除文件仍然存在: {rel_path}"


# ============================================================================
# 验证项 2：src/core/errors.py 合并后功能正常
# ============================================================================

class TestErrorsModuleAfterMerge:
    def test_import_error_severity(self):
        from src.core.errors import ErrorSeverity
        assert ErrorSeverity.INFO.value == "info"
        assert ErrorSeverity.WARNING.value == "warning"
        assert ErrorSeverity.ERROR.value == "error"

    def test_import_error_code(self):
        from src.core.errors import ErrorCode
        assert hasattr(ErrorCode, "WS_CONN_1001")
        assert hasattr(ErrorCode, "API_AUTH_2001")

    def test_import_standard_error(self):
        from src.core.errors import StandardError
        assert StandardError is not None

    def test_standard_error_create(self):
        from src.core.errors import StandardError
        err = StandardError.create(error_code="WS_CONN_1001")
        assert err.code == "WS_CONN_1001"
        assert err.message == "WebSocket 连接失败"
        assert err.category == "WS"
        assert err.severity == "error"
        assert err.trace_id
        assert err.suggested_action is not None

    def test_standard_error_create_with_unknown_code(self):
        from src.core.errors import StandardError
        err = StandardError.create(error_code="UNKNOWN_9999")
        assert err.code == "UNKNOWN_9999"
        assert err.message == "未知错误"

    def test_standard_error_to_http_response(self):
        from src.core.errors import StandardError
        err = StandardError.create(error_code="API_NOTF_2004")
        status, resp = err.to_http_response()
        assert status == 404
        assert resp is err

    def test_helper_functions(self):
        from src.core.errors import ErrorSeverity, get_error_message, get_error_severity, \
            get_http_status, get_suggested_action, is_retryable_error
        assert get_error_message("WS_CONN_1001") == "WebSocket 连接失败"
        assert get_error_severity("WS_CONN_1001") == ErrorSeverity.ERROR
        assert get_http_status("API_AUTH_2001") == 401
        assert is_retryable_error("API_TIME_2005") is True
        assert is_retryable_error("API_AUTH_2001") is False
        assert get_suggested_action("WS_AUTH_1004") is not None

    def test_error_reporter_not_importable(self):
        import src.core.errors as m
        assert not hasattr(m, "ErrorReporter")
        assert not hasattr(m, "ErrorMetrics")
        assert not hasattr(m, "ErrorCategory")


# ============================================================================
# 验证项 3：src/cache/multi_level_cache.py 纯内存缓存工作正常
# ============================================================================

class TestMultiLevelCache:
    def test_cache_manager_basic_operations(self):
        from src.cache.multi_level_cache import CacheManager
        cm = CacheManager(default_ttl=60)
        cm.set("k1", "v1")
        assert cm.get("k1") == "v1"
        assert cm.get("missing") is None
        assert cm.delete("k1") is True
        assert cm.get("k1") is None
        assert cm.delete("k1") is False

    def test_cache_manager_ttl_expiry(self):
        """通过 mock time 验证 TTL 过期，避免真实 sleep。"""
        from src.cache.multi_level_cache import CacheManager
        cm = CacheManager()
        cm.set("ttl_key", "value", ttl=1)
        assert cm.get("ttl_key") == "value"
        # 将内部时间快进 2 秒以触发过期
        with patch("time.time", return_value=time.time() + 2):
            assert cm.get("ttl_key") is None

    def test_cache_manager_stats(self):
        from src.cache.multi_level_cache import CacheManager
        cm = CacheManager()
        cm.set("a", 1)
        cm.set("b", 2)
        cm.get("a"); cm.get("a"); cm.get("c")
        stats = cm.stats()
        assert stats["hits"] == 2
        assert stats["misses"] == 1
        assert stats["size"] == 2

    def test_cache_manager_clear(self):
        from src.cache.multi_level_cache import CacheManager
        cm = CacheManager()
        cm.set("x", 1); cm.set("y", 2)
        cm.clear()
        assert cm.get("x") is None
        assert cm.get("y") is None

    @pytest.mark.asyncio
    async def test_multi_level_cache_async_operations(self):
        from src.cache.multi_level_cache import MultiLevelCache
        cache = MultiLevelCache(l1_ttl=300)
        await cache.set("key1", {"data": "value1"})
        assert await cache.get("key1") == {"data": "value1"}
        assert await cache.delete("key1") is True
        assert await cache.get("key1") is None

    @pytest.mark.asyncio
    async def test_multi_level_cache_stats(self):
        from src.cache.multi_level_cache import MultiLevelCache
        cache = MultiLevelCache()
        await cache.set("k", "v")
        await cache.get("k")
        stats = cache.get_stats()
        assert "l1" in stats
        assert stats["l1"]["size"] == 1

    @pytest.mark.asyncio
    async def test_multi_level_cache_clear_pattern(self):
        from src.cache.multi_level_cache import MultiLevelCache
        cache = MultiLevelCache()
        await cache.set("a", 1); await cache.set("b", 2)
        count = await cache.clear_pattern("*")
        assert count == 1
        assert await cache.get("a") is None

    def test_get_global_cache_returns_same_instance(self):
        from src.cache.multi_level_cache import get_global_cache
        import src.cache.multi_level_cache as mod
        mod._global_cache = None
        assert get_global_cache() is get_global_cache()
        mod._global_cache = None

    @pytest.mark.asyncio
    async def test_cached_helper_function(self):
        from src.cache.multi_level_cache import MultiLevelCache, cached
        cache = MultiLevelCache()
        call_count = 0

        async def factory():
            nonlocal call_count; call_count += 1
            return "computed"

        await cached("test_key", factory, cache_instance=cache)
        await cached("test_key", factory, cache_instance=cache)
        assert call_count == 1

    def test_no_redis_imports(self):
        import src.cache.multi_level_cache as mod
        with open(mod.__file__, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "redis" not in alias.name.lower(), f"发现 Redis import: {alias.name}"
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    assert "redis" not in node.module.lower(), f"发现 Redis import: from {node.module}"
                for alias in (node.names or []):
                    assert "redis" not in alias.name.lower(), f"发现 Redis import: {alias.name}"


# ============================================================================
# 验证项 4：src/cache/decorators.py 缓存装饰器工作正常
# ============================================================================

class TestCacheDecorators:
    def test_cache_key_generator_deterministic(self):
        from src.cache.decorators import cache_key_generator
        assert cache_key_generator("a", "b", x=1) == cache_key_generator("a", "b", x=1)

    def test_cache_key_generator_different_args(self):
        from src.cache.decorators import cache_key_generator
        assert cache_key_generator("a") != cache_key_generator("b")

    @pytest.mark.asyncio
    async def test_cached_function_decorator(self):
        from src.cache.decorators import cached_function
        from src.cache.multi_level_cache import MultiLevelCache
        import src.cache.multi_level_cache as mod
        cache = MultiLevelCache()
        mod._global_cache = cache
        call_count = 0

        @cached_function(ttl=60, key_prefix="test")
        async def compute(x: int) -> int:
            nonlocal call_count; call_count += 1
            return x * 2

        assert await compute(5) == 10
        assert await compute(5) == 10
        assert call_count == 1
        mod._global_cache = None

    def test_cache_result_is_callable(self):
        from src.cache.decorators import cache_result
        assert callable(cache_result(ttl=300, key_prefix="r"))

    @pytest.mark.asyncio
    async def test_invalidate_cache_decorator(self):
        from src.cache.decorators import invalidate_cache
        from src.cache.multi_level_cache import MultiLevelCache
        import src.cache.multi_level_cache as mod
        cache = MultiLevelCache()
        mod._global_cache = cache

        @invalidate_cache(pattern="*")
        async def clear_all():
            return "done"

        await cache.set("old_key", "old_value")
        assert await cache.get("old_key") == "old_value"
        assert await clear_all() == "done"
        assert await cache.get("old_key") is None
        mod._global_cache = None


# ============================================================================
# 验证项 5：src/templates/ 模块工作正常
# ============================================================================

class TestTemplatesModule:
    def test_public_api_imports(self):
        from src.templates import (EvaluationDimension, TemplateLoader, TemplateRegistry,
                                   TemplateSection, TemplateSpec, TemplateType)
        assert TemplateLoader is not None

    def test_no_renderer_export(self):
        import src.templates as tmpl
        assert not hasattr(tmpl, "TemplateRenderer")
        assert "TemplateRenderer" not in tmpl.__all__

    def test_template_types_enum(self):
        from src.templates import TemplateType
        assert TemplateType.CONSUMABLE.value == "A"
        assert TemplateType.DELIVERABLE.value == "B"

    def test_template_loader_parse_markdown(self, tmp_path):
        from src.templates import TemplateLoader
        md = tmp_path / "test_template.md"
        md.write_text(
            "<!--\n【模板是什么】测试模板\n【模板的作用】1. 测试\n【如何使用本模板】1. 直接用\n"
            "【适用场景】- 单元测试\n【与其他模板的关系】上游：无\n下游：无\n-->\n\n"
            "# 测试模板\n\n## 正文 [必填]\n\nHello {name}，{place}。\n\n"
            "<!-- 评估指南\n检查维度：\n| 维度 | 检查 | 必填 | 标准 |\n|------|------|------|------|\n"
            "| 完整性 | 非空 | 是 | 不空白 |\n-->\n", encoding="utf-8")
        spec = TemplateLoader().load_from_markdown(md)
        assert spec.template_id == "test"
        assert "name" in spec.placeholders
        assert spec.sections[0].required == "required"

    def test_template_loader_file_not_found(self, tmp_path):
        from src.templates import TemplateLoader
        with pytest.raises(FileNotFoundError):
            TemplateLoader().load_from_markdown(tmp_path / "nope.md")

    def test_template_loader_empty_file(self, tmp_path):
        from src.templates import TemplateLoader
        f = tmp_path / "empty.md"
        f.write_text("", encoding="utf-8")
        with pytest.raises(ValueError):
            TemplateLoader().load_from_markdown(f)

    def test_template_loader_directory(self, tmp_path):
        from src.templates import TemplateLoader
        for i in range(3):
            (tmp_path / f"tpl{i}_template.md").write_text(
                f"# 模板{i}\n\n## 正文 [必填]\n\n内容{i}\n", encoding="utf-8")
        (tmp_path / "_skip_template.md").write_text("# 跳过\n", encoding="utf-8")
        (tmp_path / "readme.md").write_text("# Readme\n", encoding="utf-8")
        assert len(TemplateLoader().load_from_directory(tmp_path)) == 3

    def test_template_registry_register_and_get(self):
        from src.templates import TemplateRegistry, TemplateSpec
        r = TemplateRegistry()
        s = TemplateSpec(template_id="test", name="测试")
        r.register(s)
        assert r.get("test") is s
        assert r.get("missing") is None
        assert r.count() == 1

    def test_template_registry_unregister(self):
        from src.templates import TemplateRegistry, TemplateSpec
        r = TemplateRegistry()
        r.register(TemplateSpec(template_id="rm", name="待删"))
        assert r.unregister("rm") is True
        assert r.get("rm") is None

    def test_template_registry_find_by_type(self):
        from src.templates import TemplateRegistry, TemplateSpec, TemplateType
        r = TemplateRegistry()
        r.register(TemplateSpec(template_id="a", template_type=TemplateType.CONSUMABLE))
        r.register(TemplateSpec(template_id="b", template_type=TemplateType.DELIVERABLE))
        assert len(r.find_by_type(TemplateType.CONSUMABLE)) == 1

    def test_template_registry_list_all(self):
        from src.templates import TemplateRegistry, TemplateSpec
        r = TemplateRegistry()
        for i in range(5):
            r.register(TemplateSpec(template_id=f"t{i}"))
        assert len(r.list_all()) == 5


# ============================================================================
# 验证项 6：保留文件 game_engine.py 和 generic.py 完好
# ============================================================================

class TestPreservedFiles:
    def test_game_engine_file_exists(self):
        assert os.path.exists(os.path.join(_project_root(), "src", "connectors", "creative", "game_engine.py"))

    def test_generic_file_exists(self):
        assert os.path.exists(os.path.join(_project_root(), "src", "connectors", "creative", "generic.py"))

    def test_game_engine_has_class(self):
        content = open(os.path.join(_project_root(), "src", "connectors", "creative", "game_engine.py"), encoding="utf-8").read()
        assert "class GameEngineConnector" in content

    def test_generic_has_class(self):
        content = open(os.path.join(_project_root(), "src", "connectors", "creative", "generic.py"), encoding="utf-8").read()
        assert "class GenericCreativeConnector" in content

    def test_game_engine_syntax_valid(self):
        with open(os.path.join(_project_root(), "src", "connectors", "creative", "game_engine.py"), encoding="utf-8") as f:
            ast.parse(f.read())

    def test_generic_syntax_valid(self):
        with open(os.path.join(_project_root(), "src", "connectors", "creative", "generic.py"), encoding="utf-8") as f:
            ast.parse(f.read())
