"""配置读写回环 E2E 测试。

验证 PUT 配置 → 文件已写入 → GET 读回内容一致。
对应 features.md 场景 4。

测试用例：
- test_concurrency_config_rw：并发配置 PUT→GET 回环
- test_config_file_actually_written：PUT 后文件确实写入磁盘
- test_cost_control_config_rw：成本控制配置 PUT→GET 回环
- test_api_config_rw：API 配置 PUT→GET 回环
- test_config_overwrite_idempotent：连续两次 PUT 相同配置幂等
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml


# ---------------------------------------------------------------------------
# 配置路径隔离 — 参数化 fixture factory（消除 3 个重复 fixture）
# ---------------------------------------------------------------------------

# 模块私有变量名到临时 YAML 文件名的映射
_CONFIG_ATTR_MAP = {
    "concurrency": ("_CONCURRENCY_YAML", "concurrency_config.yaml"),
    "cost-control": ("_COST_CONTROL_YAML", "cost_control.yaml"),
    "api": ("_API_YAML", "api_config.yaml"),
}


def _isolate_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    config_key: str,
) -> Path:
    """将指定配置类型的 YAML 路径隔离到临时目录。

    Args:
        monkeypatch: pytest monkeypatch
        tmp_path: pytest 临时路径
        config_key: 配置类型键（concurrency / cost-control / api）

    Returns:
        临时 YAML 文件路径
    """
    attr_name, file_name = _CONFIG_ATTR_MAP[config_key]
    tmp_yaml = tmp_path / file_name
    monkeypatch.setattr(
        f"channels.api.routes_config.{attr_name}",
        tmp_yaml,
    )
    return tmp_yaml


def _put_config(client: Any, url: str, data: dict[str, Any], headers: dict[str, str]) -> Any:
    """封装 PUT 配置请求，使用后端 GenericConfigUpdateRequest 的 {"data": ...} 包装格式。

    Args:
        client: FastAPI TestClient
        url: 配置端点 URL
        data: 配置数据（裸 dict）
        headers: 认证头

    Returns:
        PUT 响应
    """
    return client.put(url, json={"data": data}, headers=headers)


# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------

def test_concurrency_config_rw(
    test_client: Any,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """并发配置 PUT → GET 回环。

    验证点：
    - PUT /api/v1/config/concurrency 返回 200
    - PUT 响应内容与请求完全一致
    - GET /api/v1/config/concurrency 返回内容与写入一致
    """
    _isolate_config(monkeypatch, tmp_path, "concurrency")

    config_data = {
        "task": {
            "max_concurrent_tasks": 5,
            "task_max_workers": 8,
            "task_timeout": 600,
        },
        "agent": {
            "l1_max_concurrent": 3,
            "l2_max_concurrent": 6,
            "l3_max_concurrent": 12,
        },
    }

    put_resp = _put_config(
        test_client, "/api/v1/config/concurrency", config_data, auth_headers,
    )
    assert put_resp.status_code == 200, f"PUT 并发配置失败: {put_resp.text}"
    assert put_resp.json() == config_data, "PUT 响应内容与请求不一致"

    get_resp = test_client.get(
        "/api/v1/config/concurrency",
        headers=auth_headers,
    )
    assert get_resp.status_code == 200, f"GET 并发配置失败: {get_resp.text}"

    get_data = get_resp.json()
    assert get_data == config_data, (
        f"GET 返回内容与 PUT 不一致:\nPUT: {config_data}\nGET: {get_data}"
    )


def test_config_file_actually_written(
    test_client: Any,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """PUT 后验证配置文件确实写入磁盘。

    验证点：
    - PUT 后临时目录中的 YAML 文件存在
    - 文件内容包含写入的数据
    """
    isolated_yaml = _isolate_config(monkeypatch, tmp_path, "cost-control")

    config_data = {
        "enabled": True,
        "global_config": {
            "daily_token_limit": 500000,
            "monthly_token_limit": 15000000,
        },
    }

    _put_config(
        test_client, "/api/v1/config/cost-control", config_data, auth_headers,
    )

    assert isolated_yaml.exists(), "配置文件未写入磁盘"

    with open(isolated_yaml, encoding="utf-8") as f:
        file_data = yaml.safe_load(f)
    assert file_data is not None, "配置文件内容为空"
    assert file_data["enabled"] is True, "配置文件中 enabled 应为 True"
    assert file_data["global_config"]["daily_token_limit"] == 500000, (
        "配置文件中 daily_token_limit 应为 500000"
    )


def test_cost_control_config_rw(
    test_client: Any,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """成本控制配置 PUT → GET 回环。

    验证点：
    - PUT /api/v1/config/cost-control 返回 200
    - PUT 响应与请求完全一致
    - GET /api/v1/config/cost-control 返回内容与写入一致
    """
    _isolate_config(monkeypatch, tmp_path, "cost-control")

    config_data = {
        "enabled": False,
        "global_config": {
            "daily_token_limit": 100,
            "monthly_token_limit": 1000,
            "per_task_token_limit": 50,
            "per_session_token_limit": 200,
        },
        "alerts": {
            "warning_threshold": 60,
            "critical_threshold": 80,
        },
    }

    put_resp = _put_config(
        test_client, "/api/v1/config/cost-control", config_data, auth_headers,
    )
    assert put_resp.status_code == 200, f"PUT 成本控制配置失败: {put_resp.text}"
    assert put_resp.json() == config_data, "PUT 响应内容与请求不一致"

    get_resp = test_client.get(
        "/api/v1/config/cost-control",
        headers=auth_headers,
    )
    assert get_resp.status_code == 200, f"GET 成本控制配置失败: {get_resp.text}"

    get_data = get_resp.json()
    assert get_data == config_data, (
        f"GET 返回内容与 PUT 不一致:\nPUT: {config_data}\nGET: {get_data}"
    )


def test_api_config_rw(
    test_client: Any,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """API 配置 PUT → GET 回环。

    验证点：
    - PUT /api/v1/config/api 返回 200
    - PUT 响应与请求完全一致
    - GET /api/v1/config/api 返回内容与写入一致
    """
    _isolate_config(monkeypatch, tmp_path, "api")

    config_data = {
        "endpoint": {
            "base_url": "http://test:9999",
            "version": "v2",
            "timeout": 60,
        },
        "rate_limit": {
            "global_limit": "200/minute",
        },
    }

    put_resp = _put_config(
        test_client, "/api/v1/config/api", config_data, auth_headers,
    )
    assert put_resp.status_code == 200, f"PUT API 配置失败: {put_resp.text}"
    assert put_resp.json() == config_data, "PUT 响应内容与请求不一致"

    get_resp = test_client.get(
        "/api/v1/config/api",
        headers=auth_headers,
    )
    assert get_resp.status_code == 200, f"GET API 配置失败: {get_resp.text}"

    get_data = get_resp.json()
    assert get_data == config_data, (
        f"GET 返回内容与 PUT 不一致:\nPUT: {config_data}\nGET: {get_data}"
    )


def test_config_overwrite_idempotent(
    test_client: Any,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """连续两次 PUT 相同配置，GET 结果一致。

    验证点：
    - 第一次 PUT 后 GET 一致
    - 第二次 PUT 相同数据后 GET 仍然一致
    """
    _isolate_config(monkeypatch, tmp_path, "concurrency")

    config_data = {
        "task": {"max_concurrent_tasks": 3},
    }

    for _ in range(2):
        _put_config(
            test_client, "/api/v1/config/concurrency", config_data, auth_headers,
        )

    get_resp = test_client.get(
        "/api/v1/config/concurrency",
        headers=auth_headers,
    )
    assert get_resp.status_code == 200
    get_data = get_resp.json()
    assert get_data == config_data, (
        f"幂等写入后 GET 结果不一致:\n期望: {config_data}\n实际: {get_data}"
    )
