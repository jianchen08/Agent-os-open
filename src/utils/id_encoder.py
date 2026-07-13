"""
ID 编码模块（简化版）

提供 Base36 编解码功能。
用于生成紧凑的数字编码。
"""

import string

# Base36 字符集（0-9, a-z）
BASE36_CHARS = string.digits + string.ascii_lowercase


def encode_base36(num: int, width: int = 5) -> str:
    """将数字编码为 Base36 字符串

    Args:
        num: 要编码的非负整数
        width: 输出字符串的最小宽度，不足时左侧补 '0'

    Returns:
        Base36 编码的字符串

    Raises:
        ValueError: 如果 num 为负数

    Examples:
        >>> encode_base36(0, 5)
        '00000'
        >>> encode_base36(35, 2)
        '0z'
        >>> encode_base36(36, 2)
        '10'
        >>> encode_base36(12345, 5)
        '09ix'
    """
    if num < 0:
        raise ValueError(f"num 必须为非负整数，收到：{num}")

    if num == 0:
        return "0" * width

    result = []
    n = num

    while n > 0:
        n, remainder = divmod(n, 36)
        result.append(BASE36_CHARS[remainder])

    # 反转并补零
    encoded = "".join(reversed(result))
    return encoded.zfill(width)


def decode_base36(s: str) -> int:
    """将 Base36 字符串解码为整数

    Args:
        s: Base36 编码的字符串

    Returns:
        解码后的整数

    Raises:
        ValueError: 如果 s 包含非法字符

    Examples:
        >>> decode_base36('00000')
        0
        >>> decode_base36('0z')
        35
        >>> decode_base36('10')
        36
        >>> decode_base36('09ix')
        12345
    """
    s = s.lower().lstrip("0") or "0"

    # 验证字符
    for char in s:
        if char not in BASE36_CHARS:
            raise ValueError(f"非法 Base36 字符：'{char}'")

    result = 0
    for char in s:
        result = result * 36 + BASE36_CHARS.index(char)

    return result


def generate_project_id(project_index: int) -> str:
    """生成项目 ID

    Args:
        project_index: 项目索引

    Returns:
        项目 ID 字符串，格式为 "p-{encoded_index}"

    Examples:
        >>> generate_project_id(0)
        'p-000000'
        >>> generate_project_id(100)
        'p-00002s'
    """
    encoded = encode_base36(project_index, 6)
    return f"p-{encoded}"


def generate_task_id(
    project_id: str | None = None,
    task_index: int = 0,
    parent_task_id: str | None = None,
) -> str:
    """生成任务 ID

    Args:
        project_id: 项目 ID（当没有 parent_task_id 时必需）
        task_index: 任务索引
        parent_task_id: 父任务 ID（如果为子任务）

    Returns:
        任务 ID 字符串

    Raises:
        ValueError: 如果缺少必要的参数

    Examples:
        >>> generate_task_id(project_id="p-abc123", task_index=0)
        'p-abc123-t-00000'
        >>> generate_task_id(parent_task_id="p-abc123-t-00001", task_index=5)
        'p-abc123-t-00001-00005'
    """
    if parent_task_id is None:
        if project_id is None:
            raise ValueError("project_id 在没有 parent_task_id 时是必需的")
        # 生成主任务 ID
        encoded_index = encode_base36(task_index, 5)
        return f"{project_id}-t-{encoded_index}"
    # 生成子任务 ID
    encoded_index = encode_base36(task_index, 5)
    return f"{parent_task_id}-{encoded_index}"


def generate_nested_id(parent_id: str | None = None, sequence: int = 0, prefix: str = "exec") -> str:
    """生成嵌套ID

    用于ExecutionRecord和Task的ID生成，支持嵌套层级。

    Args:
        parent_id: 父ID（如 "exec-00001"）
        sequence: 序列号（从0开始）
        prefix: ID前缀（exec/task）

    Returns:
        嵌套ID

    Examples:
        >>> generate_nested_id(prefix="exec", sequence=1)
        'exec-00001'
        >>> generate_nested_id(parent_id="exec-00001", sequence=2, prefix="exec")
        'exec-00001-00002'
        >>> generate_nested_id(parent_id="exec-00001-00002", sequence=3, prefix="exec")
        'exec-00001-00002-00003'
    """
    encoded_seq = encode_base36(sequence, 5)

    if parent_id is None:
        # 根ID
        return f"{prefix}-{encoded_seq}"
    # 子ID：继承父ID的所有层级
    return f"{parent_id}-{encoded_seq}"


def parse_nested_id(nested_id: str) -> dict[str, any]:
    """解析嵌套ID

    Args:
        nested_id: 嵌套ID字符串（如 "exec-00001-00002-00003"）

    Returns:
        包含解析结果的字典：
        - prefix: ID前缀（exec/task）
        - sequences: 序列号列表
        - depth: 嵌套深度
        - parent_id: 父ID（如果存在）

    Examples:
        >>> parse_nested_id("exec-00001")
        {'prefix': 'exec', 'sequences': [1], 'depth': 1, 'parent_id': None}
        >>> parse_nested_id("exec-00001-00002")
        {'prefix': 'exec', 'sequences': [1, 2], 'depth': 2, 'parent_id': 'exec-00001'}
        >>> parse_nested_id("task-00001-00002-00003")
        {'prefix': 'task', 'sequences': [1, 2, 3], 'depth': 3, 'parent_id': 'task-00001-00002'}
    """
    parts = nested_id.split("-")

    if len(parts) < 2:
        raise ValueError(f"无效的嵌套ID格式: {nested_id}")

    prefix = parts[0]
    sequences = []

    # 解析所有序列号
    for i in range(1, len(parts)):
        seq = decode_base36(parts[i])
        sequences.append(seq)

    depth = len(sequences)

    # 构建父ID
    parent_id = None
    if depth > 1:
        parent_parts = parts[:-1]
        parent_id = "-".join(parent_parts)

    return {
        "prefix": prefix,
        "sequences": sequences,
        "depth": depth,
        "parent_id": parent_id,
    }


def exec_id_to_task_id(exec_id: str) -> str:
    """将ExecutionRecord ID转换为Task ID

    Args:
        exec_id: ExecutionRecord ID（如 "exec-00001-00002"）

    Returns:
        Task ID（如 "task-00001-00002"）

    Examples:
        >>> exec_id_to_task_id("exec-00001")
        'task-00001'
        >>> exec_id_to_task_id("exec-00001-00002-00003")
        'task-00001-00002-00003'
    """
    if not exec_id.startswith("exec-"):
        raise ValueError(f"无效的ExecutionRecord ID: {exec_id}，必须以'exec-'开头")

    return exec_id.replace("exec-", "task-", 1)


def task_id_to_exec_id(task_id: str) -> str:
    """将Task ID转换为ExecutionRecord ID

    Args:
        task_id: Task ID（如 "task-00001-00002"）

    Returns:
        ExecutionRecord ID（如 "exec-00001-00002"）

    Examples:
        >>> task_id_to_exec_id("task-00001")
        'exec-00001'
        >>> task_id_to_exec_id("task-00001-00002-00003")
        'exec-00001-00002-00003'
    """
    if not task_id.startswith("task-"):
        raise ValueError(f"无效的Task ID: {task_id}，必须以'task-'开头")

    return task_id.replace("task-", "exec-", 1)


def parse_task_id(task_id: str) -> dict[str, any]:
    """解析任务 ID

    Args:
        task_id: 任务 ID 字符串

    Returns:
        包含解析结果的字典：
        - project_id: 项目 ID
        - task_index: 任务索引（如果存在）
        - parent_task_id: 父任务 ID（如果存在）
        - depth: 任务层级深度

    Examples:
        >>> parse_task_id("p-abc123")
        {'project_id': 'p-abc123', 'task_index': None, 'parent_task_id': None, 'depth': 0}
        >>> parse_task_id("p-abc123-t-00001")
        {'project_id': 'p-abc123', 'task_index': 1, 'parent_task_id': None, 'depth': 1}
        >>> parse_task_id("p-abc123-t-00001-00002")
        {'project_id': 'p-abc123', 'task_index': 2, 'parent_task_id': 'p-abc123-t-00001', 'depth': 2}
    """
    parts = task_id.split("-")

    result = {
        "project_id": None,
        "task_index": None,
        "parent_task_id": None,
        "depth": 0,
    }

    if len(parts) < 2:
        raise ValueError(f"无效的任务 ID 格式: {task_id}")

    # 提取项目 ID（格式：p-{encoded}）
    result["project_id"] = f"{parts[0]}-{parts[1]}"

    # 如果只有项目 ID
    if len(parts) == 2:
        return result

    # 解析任务层级
    i = 2
    current_id = result["project_id"]

    while i < len(parts):
        if parts[i] == "t" and i + 1 < len(parts):
            # 主任务
            encoded_index = parts[i + 1]
            task_index = decode_base36(encoded_index)

            # 保存当前 ID 作为父任务 ID（如果是子任务）
            if result["depth"] > 0:
                result["parent_task_id"] = current_id

            # 更新 current_id 为当前任务 ID
            current_id = f"{current_id}-t-{encoded_index}"

            # 更新结果
            result["task_index"] = task_index
            result["depth"] += 1

            i += 2
        elif i >= 4 and i < len(parts):
            # 子任务（格式：p-project-t-parent-child 或 p-project-t-parent-child-grandchild）
            # 此时 parts[i] 是子任务索引
            encoded_index = parts[i]
            task_index = decode_base36(encoded_index)

            # 设置父任务 ID 为当前 ID
            result["parent_task_id"] = current_id

            # 更新 current_id 为当前任务 ID
            current_id = f"{current_id}-{encoded_index}"

            # 更新结果
            result["task_index"] = task_index
            result["depth"] += 1
            i += 1
        else:
            # 未知格式，退出
            break

    return result
