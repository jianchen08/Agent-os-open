"""BUG-FIX-fix_20260510_pipeline_recursion 回归测试。

验证 _safe_deepcopy 在各种边界条件下不会触发 RecursionError：
1. 正常管道 state 能正确复制
2. 包含循环引用的 state 不崩溃
3. 低递归限制下不崩溃
4. 模拟 child_task_guard wait 场景（实际崩溃场景）
5. 手动复制辅助函数正确性
"""
import sys

sys.path.insert(0, "src")

from pipeline.engine_state import (
    _safe_deepcopy,
    _manual_copy_dict,
    _manual_copy_list,
)


class TestSafeDeepcopy:
    """_safe_deepcopy 回归测试。"""

    def test_normal_state_copies_correctly(self):
        """正常管道 state 能正确深拷贝。"""
        state = {
            "messages": [
                {"role": "user", "content": "hello"},
                {
                    "role": "assistant",
                    "content": "world",
                    "tool_calls": [
                        {
                            "id": "call_abc",
                            "type": "function",
                            "function": {
                                "name": "task_submit",
                                "arguments": '{"goal": "test"}',
                            },
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_abc", "content": "ok"},
            ],
            "iteration": 3,
            "ended": False,
            "pipeline_id": "test123",
            "submitted_task_ids": ["task1", "task2"],
            "tool_results": [
                {
                    "tool_name": "task_submit",
                    "success": True,
                    "data": {"task_id": "abc", "nested": {"key": "val"}},
                }
            ],
        }
        result = _safe_deepcopy(state)

        assert result["iteration"] == 3
        assert result["ended"] is False
        assert result["messages"] is not state["messages"]
        assert result["messages"][0] is not state["messages"][0]
        assert result["messages"][0]["content"] == "hello"
        assert (
            result["messages"][1]["tool_calls"][0]["function"]["name"]
            == "task_submit"
        )
        assert result["tool_results"][0]["data"]["nested"]["key"] == "val"

    def test_on_chunk_skipped(self):
        """on_chunk 不可序列化，应被跳过。"""
        state = {
            "messages": [],
            "on_chunk": lambda chunk: None,
            "streaming": True,
        }
        result = _safe_deepcopy(state)

        assert "on_chunk" not in result
        assert result["streaming"] is True

    def test_circular_reference_no_crash(self):
        """包含循环引用的 state 不崩溃。"""
        state = {"messages": [], "iteration": 1}
        state["self_ref"] = state

        result = _safe_deepcopy(state)
        assert result["iteration"] == 1

    def test_low_recursion_limit_no_crash(self):
        """在低递归限制下 _safe_deepcopy 不崩溃（模拟生产环境 RecursionError）。"""
        old_limit = sys.getrecursionlimit()
        sys.setrecursionlimit(50)
        try:
            state = {
                "messages": [{"role": "user", "content": "test"}],
                "iteration": 1,
                "ended": False,
                "pipeline_id": "abc",
            }
            result = _safe_deepcopy(state)
            assert result["iteration"] == 1
        finally:
            sys.setrecursionlimit(old_limit)

    def test_deeply_nested_dict_handled(self):
        """深层嵌套的 dict 不会无限递归。"""
        deep = {}
        current = deep
        for i in range(30):
            current["child"] = {"level": i}
            current = current["child"]

        state = {"deep": deep, "simple": "val"}
        result = _safe_deepcopy(state)
        assert result["simple"] == "val"

    def test_real_world_wait_scenario(self):
        """模拟 child_task_guard wait 场景（实际崩溃场景）。

        重现 iter=3 时 child_task_guard 发出 wait 信号后 _safe_deepcopy 崩溃。
        """
        tool_results_data = {
            "status": "completed",
            "success": True,
            "output": {
                "task_id": "sub_task_001",
                "title": "test task",
                "status": "pending",
                "metadata": {
                    "acceptance_criteria": {
                        "semantic_check": {
                            "input_params": {
                                "criteria": {
                                    "required_sections": [
                                        "task_analysis",
                                        "execution",
                                    ]
                                }
                            }
                        }
                    },
                    "target_id": "code_writer_agent",
                    "ws_meta": {
                        "mode": "worktree",
                        "path": "D:\\workspace\\wt_abc",
                        "branch": "task/sub_task_001",
                    },
                },
            },
        }
        state = {
            "messages": [
                {"role": "system", "content": "You are a programming coordinator."},
                {"role": "user", "content": "implement MiniMap component"},
                {
                    "role": "assistant",
                    "content": "submitting task to code_writer_agent",
                    "tool_calls": [
                        {
                            "id": "call_001",
                            "type": "function",
                            "function": {
                                "name": "task_submit",
                                "arguments": '{"goal": {"title": "MiniMap"}}',
                            },
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_001", "content": str(tool_results_data)},
                {
                    "role": "assistant",
                    "content": "task submitted, waiting for result",
                },
            ],
            "iteration": 3,
            "ended": False,
            "core_type": "llm_call",
            "pipeline_id": "sub_pipe_001",
            "raw_result": "task submitted, waiting for result",
            "raw_tool_calls": [],
            "raw_error": None,
            "submitted_task_ids": ["sub_task_001"],
            "tool_results": [
                {
                    "tool_name": "task_submit",
                    "success": True,
                    "data": tool_results_data,
                }
            ],
            "llm_usage": {
                "input_tokens": 11323,
                "output_tokens": 35,
                "total_tokens": 11358,
            },
            "context_window": 128000,
            "llm_model": "glm-5.1",
            "streaming": True,
            "on_chunk": lambda chunk: None,
            "system_prompt": "You are a programming coordinator expert.",
            "tool_ids": ["task_submit", "task_manage", "task_evaluate"],
            "constraints": {"hard": ["rule1"], "soft": ["suggestion"]},
            "plugin_configs": {"child_task_guard": {"idle_remind_limit": 3}},
            "workspace": "D:\\workspace",
            "task_id": "parent_task_001",
        }

        result = _safe_deepcopy(state)

        assert "on_chunk" not in result
        assert result["iteration"] == 3
        assert result["messages"] is not state["messages"]
        assert result["submitted_task_ids"] == ["sub_task_001"]
        assert (
            result["tool_results"][0]["data"]["output"]["task_id"]
            == "sub_task_001"
        )


class TestManualCopyHelpers:
    """手动复制辅助函数测试。"""

    def test_copy_dict_with_primitives(self):
        d = {"a": "str", "b": 1, "c": 3.14, "d": True, "e": None}
        result = _manual_copy_dict(d, depth=0)
        assert result == d
        assert result is not d

    def test_copy_dict_with_nested(self):
        d = {"list": [1, 2, {"inner": "val"}], "dict": {"nested": True}}
        result = _manual_copy_dict(d, depth=0)
        assert result["list"][2]["inner"] == "val"
        assert result["list"] is not d["list"]

    def test_copy_list_with_mixed(self):
        lst = [1, "a", {"k": "v"}, [1, 2]]
        result = _manual_copy_list(lst, depth=0)
        assert result == lst
        assert result is not lst
        assert result[2] is not lst[2]

    def test_depth_limit_triggers_shallow(self):
        deep = {}
        current = deep
        for i in range(25):
            current["child"] = {"level": i}
            current = current["child"]

        result = _manual_copy_dict(deep, depth=0)
        assert "child" in result
