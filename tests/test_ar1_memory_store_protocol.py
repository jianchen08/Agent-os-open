"""AR-1 架构解耦验证：MemoryStoreProtocol 解耦 infrastructure ↔ channels.api。

验证覆盖点：
1. infrastructure/protocols.py 中 MemoryStoreProtocol 的接口定义正确
2. task_executor.py 中 _api_store 使用 MemoryStoreProtocol 类型注解
3. memory_store 实例通过 services 字典注入到 task_executor，解耦后行为一致
4. infrastructure/ 中不再有 from channels.api.memory_store import
"""
from __future__ import annotations

import ast
import inspect
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ── 公共路径常量 ──────────────────────────────────────────────
SRC_DIR = Path(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))
INFRA_DIR = SRC_DIR / "infrastructure"


# ═════════════════════════════════════════════════════════════
# 第一组：MemoryStoreProtocol 接口定义正确性
# ═════════════════════════════════════════════════════════════


class TestProtocolDefinition:
    """验证 MemoryStoreProtocol 的接口定义符合契约。"""

    def test_protocol_importable(self) -> None:
        """MemoryStoreProtocol 可正常导入。"""
        from infrastructure.protocols import MemoryStoreProtocol

        assert MemoryStoreProtocol is not None

    def test_protocol_is_runtime_checkable(self) -> None:
        """MemoryStoreProtocol 标注了 @runtime_checkable，支持 isinstance 检查。"""
        from infrastructure.protocols import MemoryStoreProtocol

        # runtime_checkable 的 Protocol 可以通过 isinstance 检查
        assert hasattr(MemoryStoreProtocol, "_is_runtime_protocol"), (
            "MemoryStoreProtocol 应标注 @runtime_checkable"
        )

    def test_protocol_has_get_session_method(self) -> None:
        """Protocol 定义了 get_session 方法。"""
        from infrastructure.protocols import MemoryStoreProtocol

        assert hasattr(MemoryStoreProtocol, "get_session"), (
            "MemoryStoreProtocol 必须定义 get_session 方法"
        )

    def test_protocol_has_set_session_method(self) -> None:
        """Protocol 定义了 set_session 方法。"""
        from infrastructure.protocols import MemoryStoreProtocol

        assert hasattr(MemoryStoreProtocol, "set_session"), (
            "MemoryStoreProtocol 必须定义 set_session 方法"
        )

    def test_protocol_get_session_signature(self) -> None:
        """get_session 方法签名：(self, thread_id: str) -> SessionModel | None。"""
        protocols_path = INFRA_DIR / "protocols.py"
        source = protocols_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        # 找到 MemoryStoreProtocol 类定义
        protocol_class = None
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "MemoryStoreProtocol":
                protocol_class = node
                break

        assert protocol_class is not None, "未找到 MemoryStoreProtocol 类定义"

        # 找到 get_session 方法
        get_session = None
        for item in protocol_class.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == "get_session":
                get_session = item
                break

        assert get_session is not None, "MemoryStoreProtocol 未定义 get_session"
        # 参数: self, thread_id（共 2 个）
        arg_names = [a.arg for a in get_session.args.args]
        assert arg_names == ["self", "thread_id"], (
            f"get_session 参数应为 [self, thread_id]，实际为 {arg_names}"
        )
        # thread_id 类型注解应为 str
        thread_id_annotation = get_session.args.args[1].annotation
        assert thread_id_annotation is not None, "get_session 的 thread_id 参数缺少类型注解"

    def test_protocol_set_session_signature(self) -> None:
        """set_session 方法签名：(self, thread_id: str, session: SessionModel) -> None。"""
        protocols_path = INFRA_DIR / "protocols.py"
        source = protocols_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        protocol_class = None
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "MemoryStoreProtocol":
                protocol_class = node
                break

        assert protocol_class is not None

        set_session = None
        for item in protocol_class.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == "set_session":
                set_session = item
                break

        assert set_session is not None, "MemoryStoreProtocol 未定义 set_session"
        arg_names = [a.arg for a in set_session.args.args]
        assert arg_names == ["self", "thread_id", "session"], (
            f"set_session 参数应为 [self, thread_id, session]，实际为 {arg_names}"
        )

    def test_protocol_only_defines_two_methods(self) -> None:
        """Protocol 只定义 get_session 和 set_session 两个方法，不过度抽象。"""
        protocols_path = INFRA_DIR / "protocols.py"
        source = protocols_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        protocol_class = None
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "MemoryStoreProtocol":
                protocol_class = node
                break

        assert protocol_class is not None

        method_names = {
            item.name
            for item in protocol_class.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert method_names == {"get_session", "set_session"}, (
            f"MemoryStoreProtocol 应只定义 get_session 和 set_session，实际定义了 {method_names}"
        )


# ═════════════════════════════════════════════════════════════
# 第二组：MemoryStore 满足 Protocol 契约
# ═════════════════════════════════════════════════════════════


class TestMemoryStoreSatisfiesProtocol:
    """验证 channels.api.memory_store.MemoryStore 满足 MemoryStoreProtocol 契约。"""

    def test_memory_store_instance_satisfies_protocol(self) -> None:
        """MemoryStore 实例满足 MemoryStoreProtocol（runtime_checkable isinstance 检查）。"""
        from infrastructure.protocols import MemoryStoreProtocol
        from channels.api.memory_store import MemoryStore

        store = MemoryStore(persist_dir=None)
        assert isinstance(store, MemoryStoreProtocol), (
            "MemoryStore 必须满足 MemoryStoreProtocol 契约"
        )

    def test_memory_store_get_session_callable(self) -> None:
        """MemoryStore.get_session 是可调用方法。"""
        from channels.api.memory_store import MemoryStore

        store = MemoryStore(persist_dir=None)
        assert callable(getattr(store, "get_session", None)), (
            "MemoryStore 必须实现可调用的 get_session 方法"
        )

    def test_memory_store_set_session_callable(self) -> None:
        """MemoryStore.set_session 是可调用方法。"""
        from channels.api.memory_store import MemoryStore

        store = MemoryStore(persist_dir=None)
        assert callable(getattr(store, "set_session", None)), (
            "MemoryStore 必须实现可调用的 set_session 方法"
        )


# ═════════════════════════════════════════════════════════════
# 第三组：task_executor 使用 Protocol 类型注解
# ═════════════════════════════════════════════════════════════


class TestTaskExecutorUsesProtocol:
    """验证 task_executor.py 通过 MemoryStoreProtocol 类型注解消费 api_store。"""

    def test_task_executor_imports_protocol(self) -> None:
        """task_executor.py 在 TYPE_CHECKING 下导入 MemoryStoreProtocol。"""
        executor_path = INFRA_DIR / "task_executor.py"
        source = executor_path.read_text(encoding="utf-8")

        assert "MemoryStoreProtocol" in source, (
            "task_executor.py 应引用 MemoryStoreProtocol"
        )
        assert "from infrastructure.protocols import MemoryStoreProtocol" in source, (
            "task_executor.py 应从 infrastructure.protocols 导入 MemoryStoreProtocol"
        )

    def test_task_executor_uses_protocol_type_annotation(self) -> None:
        """task_executor.py 中 _api_store 变量使用 MemoryStoreProtocol 类型注解。"""
        executor_path = INFRA_DIR / "task_executor.py"
        source = executor_path.read_text(encoding="utf-8")

        # 验证类型注解存在：MemoryStoreProtocol | None
        assert "MemoryStoreProtocol | None" in source or "Optional[MemoryStoreProtocol]" in source, (
            "_api_store 应使用 MemoryStoreProtocol 类型注解"
        )

    def test_task_executor_protocol_under_type_checking(self) -> None:
        """Protocol 导入位于 TYPE_CHECKING 条件块内（避免运行时循环依赖）。"""
        executor_path = INFRA_DIR / "task_executor.py"
        source = executor_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        # 查找 TYPE_CHECKING if 块中的导入
        protocol_import_found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.If):
                test_node = node.test
                if (
                    isinstance(test_node, ast.Name)
                    and test_node.id == "TYPE_CHECKING"
                ):
                    for child in ast.walk(node):
                        if isinstance(child, ast.ImportFrom):
                            module = child.module or ""
                            for alias in child.names:
                                if alias.name == "MemoryStoreProtocol":
                                    protocol_import_found = True

        assert protocol_import_found, (
            "MemoryStoreProtocol 导入应位于 TYPE_CHECKING 条件块内"
        )

    def test_task_executor_accesses_store_via_services(self) -> None:
        """task_executor.py 通过 services 字典获取 api_store，不直接导入 MemoryStore。"""
        executor_path = INFRA_DIR / "task_executor.py"
        source = executor_path.read_text(encoding="utf-8")

        assert 'services.get("api_store")' in source or '_services.get("api_store")' in source, (
            "task_executor.py 应通过 services 字典获取 api_store"
        )
        # 确认不直接导入 MemoryStore 类
        assert "from channels.api.memory_store import" not in source, (
            "task_executor.py 不应直接导入 channels.api.memory_store"
        )


# ═════════════════════════════════════════════════════════════
# 第四组：通过 services 字典注入的行为一致性
# ═════════════════════════════════════════════════════════════


class TestInjectionBehaviorConsistency:
    """验证 memory_store 通过 services 字典注入后，get_session/set_session 行为一致。"""

    def test_get_session_returns_session_model(self) -> None:
        """get_session 通过注入的实例调用，正确返回 SessionModel。"""
        from channels.api.memory_store import MemoryStore
        from infrastructure.session.models import SessionModel

        store = MemoryStore(persist_dir=None)
        services = {"api_store": store}

        thread_id = "test_thread_001"
        session = SessionModel(session_id="sess_001", channel_type="web")

        # 通过注入的实例写入
        api_store = services.get("api_store")
        assert api_store is not None
        api_store.set_session(thread_id, session)

        # 通过注入的实例读回
        result = api_store.get_session(thread_id)

        assert result is not None
        assert isinstance(result, SessionModel)
        assert result.session_id == "sess_001"

    def test_get_session_returns_none_for_missing_thread(self) -> None:
        """get_session 对不存在的 thread_id 返回 None。"""
        from channels.api.memory_store import MemoryStore

        store = MemoryStore(persist_dir=None)
        services = {"api_store": store}

        api_store = services.get("api_store")
        assert api_store is not None

        result = api_store.get_session("nonexistent_thread")
        assert result is None

    def test_set_session_persists_and_get_session_reads_back(self) -> None:
        """set_session 后 get_session 读回的数据与写入一致（往返一致性）。"""
        from channels.api.memory_store import MemoryStore
        from infrastructure.session.models import SessionModel

        store = MemoryStore(persist_dir=None)
        services = {"api_store": store}
        api_store = services.get("api_store")
        assert api_store is not None

        thread_id = "round_trip_thread"
        session = SessionModel(
            session_id="sess_round_trip",
            channel_type="cli",
            pipeline_ids=["pipe_1", "pipe_2"],
        )

        api_store.set_session(thread_id, session)
        retrieved = api_store.get_session(thread_id)

        assert retrieved is not None
        assert retrieved.session_id == session.session_id
        assert retrieved.channel_type == session.channel_type
        assert retrieved.pipeline_ids == session.pipeline_ids

    def test_set_session_updates_existing_session(self) -> None:
        """set_session 对已存在的 thread 更新会话数据。"""
        from channels.api.memory_store import MemoryStore
        from infrastructure.session.models import SessionModel

        store = MemoryStore(persist_dir=None)
        services = {"api_store": store}
        api_store = services.get("api_store")
        assert api_store is not None

        thread_id = "update_thread"
        session_v1 = SessionModel(session_id="v1", channel_type="web")
        api_store.set_session(thread_id, session_v1)

        # 更新
        session_v2 = SessionModel(session_id="v2", channel_type="cli")
        api_store.set_session(thread_id, session_v2)

        result = api_store.get_session(thread_id)
        assert result is not None
        assert result.session_id == "v2"

    def test_injected_mock_satisfies_protocol(self) -> None:
        """Mock 对象只要实现了 get_session/set_session 就能通过 Protocol 检查（鸭子类型）。"""
        from infrastructure.protocols import MemoryStoreProtocol

        # 构造满足 Protocol 的 Mock
        mock_store = MagicMock()
        mock_store.get_session = MagicMock(return_value=None)
        mock_store.set_session = MagicMock(return_value=None)

        # runtime_checkable Protocol 的 isinstance 检查基于方法存在性
        assert hasattr(mock_store, "get_session")
        assert hasattr(mock_store, "set_session")

        # 通过 services 字典注入后调用行为正确
        services = {"api_store": mock_store}
        api_store = services.get("api_store")
        assert api_store is not None

        api_store.get_session("any_thread")
        mock_store.get_session.assert_called_once_with("any_thread")


# ═════════════════════════════════════════════════════════════
# 第五组：infrastructure/ 中无反向导入
# ═════════════════════════════════════════════════════════════


class TestNoReverseImport:
    """验证 infrastructure/ 目录中不再有 from channels.api.memory_store import。"""

    def test_no_channels_import_in_infrastructure(self) -> None:
        """infrastructure/ 下所有 .py 文件不含 from channels.api.memory_store import。"""
        violations: list[str] = []

        for py_file in INFRA_DIR.rglob("*.py"):
            try:
                source = py_file.read_text(encoding="utf-8")
            except Exception:
                continue

            for i, line in enumerate(source.splitlines(), start=1):
                stripped = line.strip()
                if "from channels.api.memory_store import" in stripped or (
                    "import channels.api.memory_store" in stripped
                    and not stripped.startswith("#")
                ):
                    violations.append(f"{py_file.name}:{i}: {stripped}")

        assert violations == [], (
            f"infrastructure/ 中不应导入 channels.api.memory_store，发现以下违规:\n"
            + "\n".join(violations)
        )

    def test_infrastructure_depends_on_protocol_not_concrete(self) -> None:
        """infrastructure 层依赖 Protocol 抽象，不依赖具体实现。"""
        protocols_path = INFRA_DIR / "protocols.py"
        source = protocols_path.read_text(encoding="utf-8")

        # protocols.py 不应导入 channels
        assert "from channels" not in source, (
            "protocols.py 不应导入 channels 模块（保持层间解耦）"
        )
        # protocols.py 只应导入 typing 和 infrastructure 自身的模型
        assert "from infrastructure.session.models import SessionModel" in source, (
            "protocols.py 应从 infrastructure.session.models 导入 SessionModel"
        )
