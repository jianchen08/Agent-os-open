"""TodoList CLI 管理器 - 单元测试

测试覆盖存储、增删改查、筛选、格式化等核心功能。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

# 把仓库根目录加入 sys.path，便于直接 import todo_manager
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import todo_manager as tm


# -------------------- Fixtures --------------------

@pytest.fixture
def tmp_todos_file(tmp_path: Path) -> Path:
    """为每个测试创建独立的 JSON 文件"""
    return tmp_path / "todos.json"


@pytest.fixture
def clean_store(monkeypatch, tmp_todos_file: Path):
    """让 todo_manager 默认指向临时文件"""
    monkeypatch.setattr(tm, "DEFAULT_FILE", str(tmp_todos_file))
    # 同时清理可能存在的全局缓存（如果实现中有）
    if hasattr(tm, "_next_id_cache"):
        tm._next_id_cache = None
    return tmp_todos_file


# -------------------- load / save --------------------

class TestStorage:
    def test_load_empty_file_returns_empty_list(self, tmp_todos_file: Path):
        """文件不存在或为空时，应返回空列表"""
        assert tm.load_todos(str(tmp_todos_file)) == []

    def test_load_corrupted_file_returns_empty(self, tmp_todos_file: Path):
        """JSON 损坏时不应抛出，应返回空列表（容错）"""
        tmp_todos_file.write_text("{not valid json", encoding="utf-8")
        assert tm.load_todos(str(tmp_todos_file)) == []

    def test_save_and_load_roundtrip(self, tmp_todos_file: Path):
        """保存后读取应保持数据一致"""
        todos = [
            {"id": 1, "title": "A", "created_at": "2024-01-01T00:00:00", "priority": "high", "done": False},
        ]
        tm.save_todos(str(tmp_todos_file), todos)
        loaded = tm.load_todos(str(tmp_todos_file))
        assert loaded == todos


# -------------------- add --------------------

class TestAdd:
    def test_add_creates_task_with_fields(self, clean_store):
        """添加任务应生成 id/created_at/priority/done 字段"""
        todo = tm.add_todo("买牛奶", "high")
        assert todo["title"] == "买牛奶"
        assert todo["priority"] == "high"
        assert todo["done"] is False
        assert isinstance(todo["id"], int)
        assert todo["id"] >= 1
        assert "created_at" in todo and todo["created_at"]

    def test_add_default_priority_is_medium(self, clean_store):
        """不指定 priority 时默认为 medium"""
        todo = tm.add_todo("随便看看")
        assert todo["priority"] == "medium"

    def test_add_increments_id(self, clean_store):
        """多次添加时 id 应自增"""
        t1 = tm.add_todo("A", "high")
        t2 = tm.add_todo("B", "low")
        t3 = tm.add_todo("C", "medium")
        assert t1["id"] == 1
        assert t2["id"] == 2
        assert t3["id"] == 3

    def test_add_persists_to_file(self, clean_store):
        """添加后应立即写入文件"""
        tm.add_todo("持久化测试", "low")
        # 直接读文件验证
        with open(str(clean_store), "r", encoding="utf-8") as f:
            data = json.load(f)
        assert len(data) == 1
        assert data[0]["title"] == "持久化测试"

    def test_add_invalid_priority_rejected(self, clean_store):
        """非法优先级应抛出 ValueError"""
        with pytest.raises(ValueError):
            tm.add_todo("非法", "urgent")

    def test_add_empty_title_rejected(self, clean_store):
        """空标题应抛出 ValueError"""
        with pytest.raises(ValueError):
            tm.add_todo("   ", "high")


# -------------------- done / delete --------------------

class TestDoneAndDelete:
    def test_done_marks_task_completed(self, clean_store):
        """done 后任务 done 字段变为 True"""
        tm.add_todo("任务1", "high")
        result = tm.mark_done(1)
        assert result is True
        todos = tm.load_todos(str(clean_store))
        assert todos[0]["done"] is True

    def test_done_nonexistent_returns_false(self, clean_store):
        """不存在的 id 应返回 False"""
        assert tm.mark_done(999) is False

    def test_delete_removes_task(self, clean_store):
        """delete 应从列表中移除任务"""
        tm.add_todo("A", "high")
        tm.add_todo("B", "low")
        assert tm.delete_todo(1) is True
        remaining = tm.load_todos(str(clean_store))
        assert len(remaining) == 1
        assert remaining[0]["title"] == "B"

    def test_delete_nonexistent_returns_false(self, clean_store):
        """删除不存在的 id 应返回 False"""
        assert tm.delete_todo(999) is False


# -------------------- clear --------------------

class TestClear:
    def test_clear_removes_only_done_tasks(self, clean_store):
        """clear 应只删除已完成任务，保留未完成"""
        tm.add_todo("A", "high")
        tm.add_todo("B", "low")
        tm.add_todo("C", "medium")
        tm.mark_done(2)
        removed = tm.clear_done()
        assert removed == 1
        remaining = tm.load_todos(str(clean_store))
        assert len(remaining) == 2
        titles = [t["title"] for t in remaining]
        assert "A" in titles and "C" in titles
        assert "B" not in titles

    def test_clear_on_empty_returns_zero(self, clean_store):
        """空列表时 clear 应返回 0"""
        assert tm.clear_done() == 0


# -------------------- filter / list --------------------

class TestFilter:
    def test_filter_by_status_pending(self, clean_store):
        """--filter pending 只返回未完成任务"""
        tm.add_todo("A", "high")
        tm.add_todo("B", "low")
        tm.mark_done(1)
        result = tm.list_todos(status_filter="pending", priority_filter=None)
        assert len(result) == 1
        assert result[0]["title"] == "B"

    def test_filter_by_status_done(self, clean_store):
        """--filter done 只返回已完成任务"""
        tm.add_todo("A", "high")
        tm.add_todo("B", "low")
        tm.mark_done(1)
        result = tm.list_todos(status_filter="done", priority_filter=None)
        assert len(result) == 1
        assert result[0]["title"] == "A"

    def test_filter_by_priority(self, clean_store):
        """--priority high 只返回高优先级"""
        tm.add_todo("A", "high")
        tm.add_todo("B", "low")
        tm.add_todo("C", "high")
        result = tm.list_todos(status_filter="all", priority_filter="high")
        assert len(result) == 2
        assert all(t["priority"] == "high" for t in result)

    def test_filter_combined(self, clean_store):
        """status + priority 组合筛选"""
        tm.add_todo("A", "high")
        tm.add_todo("B", "high")
        tm.add_todo("C", "low")
        tm.mark_done(1)
        result = tm.list_todos(status_filter="pending", priority_filter="high")
        assert len(result) == 1
        assert result[0]["title"] == "B"


# -------------------- format --------------------

class TestFormat:
    def test_format_includes_header_and_separator(self):
        """格式化输出应包含表头和分隔线"""
        todos = [
            {"id": 1, "title": "X", "created_at": "2024-01-01T00:00:00", "priority": "high", "done": False},
        ]
        out = tm.format_todos(todos, use_color=False)
        assert "ID" in out
        assert "Title" in out
        assert "Priority" in out
        assert "Status" in out
        assert "-" in out  # 分隔线

    def test_format_status_marks_done(self):
        """已完成任务在状态列显示 Done"""
        todos = [
            {"id": 1, "title": "X", "created_at": "2024-01-01T00:00:00", "priority": "high", "done": True},
            {"id": 2, "title": "Y", "created_at": "2024-01-01T00:00:00", "priority": "low", "done": False},
        ]
        out = tm.format_todos(todos, use_color=False)
        assert "Done" in out
        assert "Pending" in out

    def test_format_empty_list(self):
        """空列表应给出友好提示"""
        out = tm.format_todos([], use_color=False)
        assert "empty" in out.lower() or "暂无" in out or "0" in out


# -------------------- argparse / main --------------------

class TestCLI:
    def test_main_add_and_list(self, monkeypatch, capsys, clean_store):
        """通过 main() 走 add + list 流程"""
        monkeypatch.setattr(sys, "argv", ["todo_manager.py", "add", "测试任务", "--priority", "high"])
        rc = tm.main()
        assert rc == 0

        monkeypatch.setattr(sys, "argv", ["todo_manager.py", "list"])
        rc = tm.main()
        assert rc == 0
        captured = capsys.readouterr()
        assert "测试任务" in captured.out

    def test_main_done_invalid_id(self, monkeypatch, capsys, clean_store):
        """done 一个不存在的 id 不应崩溃"""
        monkeypatch.setattr(sys, "argv", ["todo_manager.py", "done", "999"])
        rc = tm.main()
        assert rc == 1  # 失败时返回非零

    def test_main_delete_invalid_id(self, monkeypatch, capsys, clean_store):
        """delete 一个不存在的 id 不应崩溃"""
        monkeypatch.setattr(sys, "argv", ["todo_manager.py", "delete", "999"])
        rc = tm.main()
        assert rc == 1

    def test_main_custom_file(self, monkeypatch, tmp_path: Path, capsys):
        """--file 参数应改变存储位置"""
        custom = tmp_path / "custom.json"
        monkeypatch.setattr(sys, "argv", ["todo_manager.py", "--file", str(custom), "add", "自定义文件"])
        rc = tm.main()
        assert rc == 0
        assert custom.exists()
        data = json.loads(custom.read_text(encoding="utf-8"))
        assert data[0]["title"] == "自定义文件"

    def test_main_filter_priority(self, monkeypatch, capsys, clean_store):
        """list --priority 过滤应工作"""
        tm.add_todo("高优", "high")
        tm.add_todo("低优", "low")
        monkeypatch.setattr(sys, "argv", ["todo_manager.py", "list", "--priority", "high"])
        rc = tm.main()
        assert rc == 0
        captured = capsys.readouterr()
        assert "高优" in captured.out
        assert "低优" not in captured.out
