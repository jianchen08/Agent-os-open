"""工具调用回环 E2E 测试。

验证 file_write 写入文件 → file_read 读回内容一致；bash_execute 执行命令输出正确。
对应 features.md 场景 3。

测试用例：
- test_file_write_read_roundtrip：file_write → file_read 回环
- test_file_write_search_replace：file_write search_replace 操作
- test_bash_execute_simple_command：bash_execute 简单命令
- test_bash_execute_output_correctness：bash_execute 输出正确性
- test_bash_write_file_then_read：bash 写文件 → file_read 读回
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# 工具实例化 fixture（同步，无需 async）
# ---------------------------------------------------------------------------

@pytest.fixture
def file_write_tool(tmp_path: Path) -> Any:
    """提供 FileWriteTool 实例，工作目录隔离到 tmp_path。

    Args:
        tmp_path: pytest 临时路径

    Returns:
        FileWriteTool 实例
    """
    from tools.builtin.file_write.tool import FileWriteTool

    return FileWriteTool(base_path=str(tmp_path))


@pytest.fixture
def file_read_tool(tmp_path: Path) -> Any:
    """提供 FileReadTool 实例，工作目录隔离到 tmp_path。

    Args:
        tmp_path: pytest 临时路径

    Returns:
        FileReadTool 实例
    """
    from tools.builtin.file_read.tool import FileReadTool

    return FileReadTool(base_path=str(tmp_path))


@pytest.fixture
def bash_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """提供 BashTool 实例，工作目录隔离到 tmp_path。

    BashTool 通过 WorkspaceAwareMixin 注入 workspace，不直接接受 base_path。
    这里使用 monkeypatch 将 cwd 指向 tmp_path 实现隔离。

    Args:
        tmp_path: pytest 临时路径
        monkeypatch: pytest monkeypatch

    Returns:
        BashTool 实例
    """
    from tools.builtin.bash.tool import BashTool

    monkeypatch.chdir(tmp_path)
    return BashTool()


# ---------------------------------------------------------------------------
# 内部辅助 — 提取工具输出中的文本内容
# ---------------------------------------------------------------------------

def _extract_content(result_output: Any) -> str:
    """从工具执行结果中提取文本内容。

    统一处理 dict / str 两种输出格式，消除各测试函数中的重复解析逻辑。

    Args:
        result_output: 工具 execute() 返回的 output 字段

    Returns:
        提取出的文本字符串
    """
    if isinstance(result_output, dict):
        return result_output.get("content", str(result_output))
    return str(result_output)


def _extract_stdout(result_output: Any) -> str:
    """从 bash_execute 结果中提取 stdout 文本。

    Args:
        result_output: 工具 execute() 返回的 output 字段

    Returns:
        stdout 文本字符串
    """
    if isinstance(result_output, dict):
        return result_output.get("stdout", "") or result_output.get("output", "")
    return str(result_output)


# ---------------------------------------------------------------------------
# file_write → file_read 回环
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_file_write_read_roundtrip(
    file_write_tool: Any,
    file_read_tool: Any,
    tmp_path: Path,
) -> None:
    """file_write 写入文件 → file_read 读回，内容一致。

    验证点：
    - file_write action=write 返回成功
    - 文件确实写入磁盘
    - file_read 读回内容与写入内容一致
    """
    test_file = str(tmp_path / "roundtrip_test.txt")
    test_content = "line 1: hello e2e\nline 2: 工具回环测试\n"

    write_result = await file_write_tool.execute({
        "action": "write",
        "path": test_file,
        "content": test_content,
    })
    assert write_result.success, f"file_write 失败: {write_result}"

    assert os.path.exists(test_file), "写入后文件应存在"

    read_result = await file_read_tool.execute({"path": test_file})
    assert read_result.success, f"file_read 失败: {read_result}"

    read_content = _extract_content(read_result.output)
    assert "hello e2e" in read_content, (
        f"读回内容应包含写入的关键文本:\n写入: {test_content!r}\n读回: {read_content!r}"
    )
    assert "工具回环测试" in read_content, (
        f"读回内容应包含写入的中文文本:\n写入: {test_content!r}\n读回: {read_content!r}"
    )


# ---------------------------------------------------------------------------
# file_write search_replace
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_file_write_search_replace(
    file_write_tool: Any,
    file_read_tool: Any,
    tmp_path: Path,
) -> None:
    """file_write search_replace 操作后读回验证。

    验证点：
    - 先 write 创建文件
    - search_replace 替换内容
    - file_read 读回内容包含替换后的文本
    """
    test_file = str(tmp_path / "replace_test.txt")

    await file_write_tool.execute({
        "action": "write",
        "path": test_file,
        "content": "old text here\nsecond line\n",
    })

    replace_result = await file_write_tool.execute({
        "action": "search_replace",
        "path": test_file,
        "old_str": "old text here",
        "new_str": "new text here",
    })
    assert replace_result.success, f"search_replace 失败: {replace_result}"

    read_result = await file_read_tool.execute({"path": test_file})
    assert read_result.success
    read_content = _extract_content(read_result.output)
    assert "new text here" in read_content, "替换后的文本应出现在文件中"
    assert "old text here" not in read_content, "旧文本不应再出现在文件中"


# ---------------------------------------------------------------------------
# bash_execute 简单命令
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bash_execute_simple_command(bash_tool: Any) -> None:
    """bash_execute 执行简单 echo 命令。

    验证点：
    - action=execute 返回成功
    - stdout 包含预期输出
    """
    result = await bash_tool.execute({
        "action": "execute",
        "command": "echo 'hello e2e bash'",
    })
    assert result.success, f"bash_execute 失败: {result}"

    stdout = _extract_stdout(result.output)
    assert "hello e2e bash" in stdout, f"stdout 应包含预期输出，得到: {stdout!r}"


@pytest.mark.asyncio
async def test_bash_execute_output_correctness(bash_tool: Any) -> None:
    """bash_execute 数学运算输出正确性。

    验证点：
    - 执行 `echo $((2+3))` 输出 5
    - exit_code 为 0
    """
    result = await bash_tool.execute({
        "action": "execute",
        "command": "echo $((2+3))",
    })
    assert result.success, f"bash_execute 失败: {result}"

    stdout = _extract_stdout(result.output).strip()
    assert "5" in stdout, f"计算结果应包含 5，得到: {stdout!r}"

    if isinstance(result.output, dict):
        exit_code = result.output.get("exit_code", result.output.get("returncode", 0))
    else:
        exit_code = 0
    assert exit_code == 0, f"exit_code 应为 0，得到 {exit_code}"


@pytest.mark.asyncio
async def test_bash_write_file_then_read(
    bash_tool: Any,
    file_read_tool: Any,
    tmp_path: Path,
) -> None:
    """bash 写文件 → file_read 读回，跨工具一致性。

    验证点：
    - bash_execute 用 echo 重定向写文件
    - file_read 读回内容一致
    """
    test_file = tmp_path / "bash_created.txt"
    result = await bash_tool.execute({
        "action": "execute",
        "command": f"echo 'created by bash' > '{test_file}'",
    })
    assert result.success, f"bash 写文件失败: {result}"

    read_result = await file_read_tool.execute({"path": str(test_file)})
    assert read_result.success, f"file_read 失败: {read_result}"

    read_content = _extract_content(read_result.output)
    assert "created by bash" in read_content, (
        f"读回内容应包含 'created by bash'，得到: {read_content!r}"
    )


# ---------------------------------------------------------------------------
# memory store → memory retrieve 回环（补充场景 3）
# ---------------------------------------------------------------------------

def test_memory_store_retrieve_roundtrip() -> None:
    """memory store → memory retrieve 数据一致性。

    通过 MemoryStore 单例（与 API 路由共用同一实例）存储一条记忆，
    再通过 search_memories 关键词检索验证命中。

    MemoryTool 的 retrieve 依赖注入的 retriever（向量/关键词），
    在 E2E 测试环境（无 DB）中不可用。MemoryStore 内置关键词搜索，
    且是 routes_memory 的实际后端，因此直接测试它更忠实于 E2E 语义。

    验证点：
    - store.create_memory 存储成功，返回包含 id 的 dict
    - store.search_memories 关键词检索命中刚存储的记忆
    - 检索结果 content 包含原始内容
    """
    from channels.api.memory_store import store

    unique_content = "e2e_memory_roundtrip_unique_marker_xyz789"

    # --- Arrange & Act: 存储语义记忆 ---
    memory = store.create_memory(
        content=f"Roundtrip verification: {unique_content}",
        memory_type="semantic",
        tags=["e2e_test", "roundtrip"],
    )

    # --- Assert: 存储成功 ---
    assert "id" in memory, f"create_memory 返回缺少 id: {memory}"
    assert memory["content"].endswith(unique_content)

    # --- Act: 关键词检索 ---
    results = store.search_memories(query=unique_content, top_k=5)

    # --- Assert: 检索命中 ---
    assert len(results) > 0, "关键词检索应命中刚存储的记忆"

    found = any(
        unique_content in item.get("content", "")
        for item in results
    )
    assert found, (
        f"检索结果应包含刚存储的内容 '{unique_content}'，"
        f"实际结果: {[item.get('content', '')[:50] for item in results]}"
    )


# ---------------------------------------------------------------------------
# enhanced_search 基本搜索（补充场景 3）
# ---------------------------------------------------------------------------

@pytest.fixture
def search_tool(tmp_path: Path) -> Any:
    """提供 EnhancedSearchTool 实例，工作目录隔离到 tmp_path。

    Args:
        tmp_path: pytest 临时路径

    Returns:
        EnhancedSearchTool 实例
    """
    from tools.builtin.enhanced_search.tool import EnhancedSearchTool

    return EnhancedSearchTool(base_path=str(tmp_path))


@pytest.mark.asyncio
async def test_enhanced_search_basic(
    search_tool: Any,
    file_write_tool: Any,
    tmp_path: Path,
) -> None:
    """enhanced_search 搜索结果包含关键词。

    先写入包含特定关键词的文件，再用 enhanced_search 搜索，
    验证搜索结果命中该文件。

    依赖 ripgrep，未安装时跳过。

    验证点：
    - 先 file_write 创建包含特定关键词的文件
    - enhanced_search 搜索该关键词返回成功
    - 搜索结果中包含目标文件路径或关键词内容
    """
    if not search_tool._check_ripgrep():
        pytest.skip("ripgrep 未安装，跳过 enhanced_search E2E 测试")

    search_keyword = "e2e_search_unique_marker_42"
    test_file = tmp_path / "search_target.txt"

    # --- Arrange: 写入包含关键词的文件 ---
    await file_write_tool.execute({
        "action": "write",
        "path": str(test_file),
        "content": f"Line 1: some content\nLine 2: {search_keyword}\nLine 3: more content\n",
    })

    # --- Act: 搜索关键词 ---
    result = await search_tool.execute({
        "query": search_keyword,
        "search_type": "text",
        "path": str(tmp_path),
        "file_pattern": "*.txt",
        "context_lines": 0,
        "max_results": 10,
    })

    # --- Assert: 搜索命中 ---
    assert result.success, f"enhanced_search 失败: {result}"

    output = result.output if hasattr(result, "output") else result.data
    if isinstance(output, dict):
        contents = output.get("contents", [])
        file_paths = output.get("file_paths", [])
    else:
        contents = []
        file_paths = []

    # 验证搜索结果包含关键词
    found_in_contents = any(
        search_keyword in line for line in contents
    )
    found_in_paths = any(
        "search_target.txt" in fp for fp in file_paths
    )
    assert found_in_contents or found_in_paths, (
        f"搜索结果应包含关键词 '{search_keyword}' 或目标文件，"
        f"contents: {contents}, file_paths: {file_paths}"
    )
