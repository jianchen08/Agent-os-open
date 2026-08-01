#!/usr/bin/env python3
"""0.1 (Python 栈) 插件热加载实测脚本。

验证 0.1 的配置级热加载（对照 0.2 的代码级热加载）：
  - /api/v1/plugins/reload 端点（重载 YAML 配置）
  - /api/v1/plugins/reload-all
  - /api/v1/plugins/status（查询插件状态）
  - /api/v1/plugins/history（重载历史）

注意：0.1 的热加载是**配置级**（重新加载 agent/tool 的 YAML 定义），
不是代码级（改 .py 需重启进程）。这与 0.2 sidecar（改代码 kill+respawn）不同。

依赖：
  - 0.1 :8988 运行，凭证 admin/admin123

用法：
  python tests/manual/test_0_1_hot_reload.py

[来源: 阶段2.2 0.1 热加载参照]
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

API_01 = os.environ.get("AGENTOS_API_URL", "http://127.0.0.1:8988")
CRED = ("admin", "admin123")

_results: list[tuple[str, bool, str]] = []


def record(name: str, passed: bool, detail: str = "") -> None:
    _results.append((name, passed, detail))
    tag = "PASS" if passed else "FAIL"
    print(f"  [{tag}] {name}: {detail}")


def http(url: str, method: str = "GET", body=None, token: str | None = None, timeout: int = 30):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read())


def login() -> str:
    _, resp = http(
        f"{API_01}/api/v1/auth/login",
        method="POST",
        body={"username": CRED[0], "password": CRED[1]},
    )
    return resp["access_token"]


def test_login() -> str | None:
    print("\n=== 测试1: 0.1 登录 ===")
    try:
        token = login()
        record("登录", True, "获取 token 成功")
        return token
    except Exception as e:
        record("登录", False, f"失败: {e}")
        return None


def test_plugin_status(token: str) -> None:
    print("\n=== 测试2: 插件状态查询 ===")
    try:
        _, resp = http(f"{API_01}/api/v1/plugins/status", token=token)
        # resp 可能是列表或 dict
        items = resp if isinstance(resp, list) else resp.get("items", resp.get("plugins", []))
        record("插件状态", isinstance(items, list) and len(items) > 0, f"共 {len(items)} 个插件")
    except Exception as e:
        record("插件状态", False, f"失败: {e}")


def test_reload_all(token: str) -> None:
    print("\n=== 测试3: reload-all 全量重载 ===")
    try:
        status, resp = http(
            f"{API_01}/api/v1/plugins/reload-all", method="POST", token=token, timeout=60
        )
        # reload-all 返回重载结果列表
        ok = status == 200
        detail = f"HTTP {status}"
        if isinstance(resp, list):
            detail += f", 重载 {len(resp)} 个"
        elif isinstance(resp, dict):
            detail += f", {json.dumps(resp, ensure_ascii=False)[:80]}"
        record("reload-all", ok, detail)
    except urllib.error.HTTPError as e:
        record("reload-all", False, f"HTTP {e.code}: {e.read().decode()[:100]}")
    except Exception as e:
        record("reload-all", False, f"失败: {e}")


def test_reload_single(token: str) -> None:
    """重载单个 agent 配置文件（用 agentos.yaml）。"""
    print("\n=== 测试4: reload 单个配置 ===")
    config_path = "config/agents/main/agentos.yaml"
    try:
        status, resp = http(
            f"{API_01}/api/v1/plugins/reload?config_path={config_path}",
            method="POST", token=token, timeout=30,
        )
        ok = status == 200 and (resp.get("success") if isinstance(resp, dict) else True)
        record(
            "reload 单个", ok,
            f"HTTP {status} {json.dumps(resp, ensure_ascii=False)[:80] if isinstance(resp, dict) else ''}",
        )
    except urllib.error.HTTPError as e:
        record("reload 单个", False, f"HTTP {e.code}: {e.read().decode()[:100]}")
    except Exception as e:
        record("reload 单个", False, f"失败: {e}")


def test_history(token: str) -> None:
    print("\n=== 测试5: 重载历史 ===")
    try:
        _, resp = http(f"{API_01}/api/v1/plugins/history?limit=10", token=token)
        items = resp if isinstance(resp, list) else resp.get("items", resp.get("history", []))
        record("重载历史", isinstance(items, list), f"共 {len(items)} 条")
    except Exception as e:
        record("重载历史", False, f"失败: {e}")


def main() -> int:
    print("=" * 70)
    print("0.1 (Python 栈) 插件热加载实测")
    print(f"API: {API_01}")
    print("=" * 70)

    token = test_login()
    if not token:
        print("\n0.1 不可用，跳过其余测试")
        return 1

    test_plugin_status(token)
    test_reload_all(token)
    test_reload_single(token)
    test_history(token)

    total = len(_results)
    passed = sum(1 for _, p, _ in _results if p)
    failed = total - passed
    print("\n" + "=" * 70)
    print(f"汇总: {passed}/{total} 通过, {failed} 失败")
    print("=" * 70)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
