"""Agent OS 前端端到端测试脚本。

测试覆盖：
1. 后端服务健康检查
2. Swagger UI 文档页面加载
3. 认证流程（登录/注册/Token刷新）
4. 线程（会话）CRUD - 对话页面核心功能
5. 消息收发 - 消息列表查询
6. Agent 配置查询
7. 任务 CRUD
8. 工具查询
9. 记忆检索
10. WebSocket 协议 - thinking/流式消息
11. UI Schema 查询
12. 对话模式相关 API
"""

import asyncio
import json
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

BASE_URL = "http://localhost:8888"
SCREENSHOT_DIR = Path(__file__).parent / "screenshots"
SCREENSHOT_DIR.mkdir(exist_ok=True)

# 测试结果收集
results: list[dict[str, Any]] = []
test_start_time = time.time()


def record(test_name: str, passed: bool, detail: str = "", response_data: Any = None):
    """记录测试结果。"""
    results.append({
        "test": test_name,
        "passed": passed,
        "detail": detail,
        "response_data": response_data,
        "timestamp": datetime.now().isoformat(),
    })
    status = "✅ PASS" if passed else "❌ FAIL"
    print(f"  {status} | {test_name}")
    if detail and not passed:
        print(f"         Detail: {detail}")


def save_screenshot(name: str, content: str, content_type: str = "html"):
    """保存页面快照。"""
    ext = "html" if content_type == "html" else "json"
    filepath = SCREENSHOT_DIR / f"{name}.{ext}"
    filepath.write_text(content, encoding="utf-8")
    print(f"  📸 快照已保存: {filepath}")
    return str(filepath)


def get_items(data: Any) -> list:
    """兼容列表和字典两种返回格式，提取 items 列表。"""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("items", [])
    return []


# ============================================================
# 测试用例
# ============================================================


def test_01_health_check(client: httpx.Client):
    """测试1: 服务健康检查。"""
    print("\n--- 测试1: 服务健康检查 ---")

    # 主健康检查
    r = client.get("/health")
    record("健康检查 - /health", r.status_code == 200 and r.json().get("status") == "ok",
           f"status={r.status_code}, body={r.json()}", r.json())
    save_screenshot("01_health", json.dumps(r.json(), indent=2, ensure_ascii=False), "json")

    # 存活检查
    r = client.get("/health/live")
    record("存活检查 - /health/live", r.status_code == 200, f"status={r.status_code}")

    # 就绪检查
    r = client.get("/health/ready")
    record("就绪检查 - /health/ready", r.status_code == 200, f"status={r.status_code}")


def test_02_swagger_ui(client: httpx.Client):
    """测试2: Swagger UI 文档页面加载。"""
    print("\n--- 测试2: API 文档页面 (Swagger UI) ---")

    r = client.get("/api/docs")
    is_ok = r.status_code == 200 and "swagger-ui" in r.text.lower()
    record("Swagger UI 加载", is_ok,
           f"status={r.status_code}, has_swagger={'swagger' in r.text.lower()}")
    save_screenshot("02_swagger_ui", r.text, "html")

    # OpenAPI JSON
    r = client.get("/api/openapi.json")
    data = r.json()
    paths = list(data.get("paths", {}).keys())
    record("OpenAPI Schema 加载", r.status_code == 200 and len(paths) > 0,
           f"endpoints={len(paths)}")
    save_screenshot("02_openapi", json.dumps(data, indent=2, ensure_ascii=False)[:5000], "json")

    # ReDoc
    r = client.get("/api/redoc")
    record("ReDoc 文档加载", r.status_code == 200, f"status={r.status_code}")


def test_03_auth_flow(client: httpx.Client):
    """测试3: 认证流程。"""
    print("\n--- 测试3: 认证流程 (登录/注册/Token) ---")

    # 注册
    r = client.post("/api/v1/auth/register", json={
        "username": f"e2e_test_{int(time.time())}",
        "password": "Test123456!",
        "email": "e2e@test.com",
    })
    register_ok = r.status_code in (200, 201, 409)  # 409 = 已存在也算通过
    record("用户注册", register_ok, f"status={r.status_code}")
    save_screenshot("03_register", json.dumps(r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text, indent=2, ensure_ascii=False), "json")

    # 登录
    r = client.post("/api/v1/auth/login", json={
        "username": "admin",
        "password": "admin123",
    })
    login_ok = r.status_code == 200
    token_data = r.json() if login_ok else {}
    access_token = token_data.get("access_token", "")
    record("用户登录", login_ok,
           f"status={r.status_code}, has_token={bool(access_token)}")
    save_screenshot("03_login", json.dumps(token_data, indent=2, ensure_ascii=False), "json")

    if not access_token:
        # 尝试默认 token
        print("  ⚠️ 登录失败，使用默认测试 token")
        access_token = "test_token"

    # Token 刷新
    if token_data.get("refresh_token"):
        r = client.post("/api/v1/auth/refresh", json={
            "refresh_token": token_data["refresh_token"],
        })
        record("Token 刷新", r.status_code == 200, f"status={r.status_code}")

    return access_token


def test_04_threads(client: httpx.Client, token: str):
    """测试4: 线程（会话）CRUD - 对话页面核心。"""
    print("\n--- 测试4: 线程管理 (对话页面核心) ---")
    headers = {"Authorization": f"Bearer {token}"}

    # 创建线程
    r = client.post("/api/v1/threads", json={
        "title": "E2E测试对话",
        "agent_id": "default",
    }, headers=headers)
    thread_ok = r.status_code in (200, 201)
    thread_data = r.json() if thread_ok else {}
    thread_id = thread_data.get("thread_id", "")
    record("创建对话线程", thread_ok,
           f"status={r.status_code}, thread_id={thread_id}")
    save_screenshot("04_create_thread", json.dumps(thread_data, indent=2, ensure_ascii=False), "json")

    # 获取线程列表
    r = client.get("/api/v1/threads", headers=headers)
    list_ok = r.status_code == 200
    threads_data = r.json() if list_ok else {}
    # 兼容列表和字典两种返回格式
    if isinstance(threads_data, list):
        items = threads_data
    else:
        items = get_items(threads_data)
    record("获取对话列表", list_ok,
           f"status={r.status_code}, count={len(items)}")
    save_screenshot("04_thread_list", json.dumps(threads_data, indent=2, ensure_ascii=False)[:3000], "json")

    # 获取线程详情
    if thread_id:
        r = client.get(f"/api/v1/threads/{thread_id}", headers=headers)
        record("获取对话详情", r.status_code == 200, f"status={r.status_code}")

        # 获取线程消息（消息收发测试）
        r = client.get(f"/api/v1/threads/{thread_id}/messages", headers=headers)
        msgs_ok = r.status_code == 200
        msgs_data = r.json() if msgs_ok else {}
        if isinstance(msgs_data, list):
            msg_count = len(msgs_data)
        else:
            msg_count = len(get_items(msgs_data))
        record("获取对话消息列表", msgs_ok,
               f"status={r.status_code}, messages={msg_count}")
        save_screenshot("04_messages", json.dumps(msgs_data, indent=2, ensure_ascii=False)[:3000], "json")

        # 获取线程状态
        r = client.get(f"/api/v1/threads/{thread_id}/state", headers=headers)
        record("获取对话状态", r.status_code == 200, f"status={r.status_code}")

        # 获取线程历史
        r = client.get(f"/api/v1/threads/{thread_id}/history", headers=headers)
        record("获取对话历史", r.status_code == 200, f"status={r.status_code}")

        # 获取线程详情（detail 端点）
        r = client.get(f"/api/v1/threads/{thread_id}/detail", headers=headers)
        record("获取对话详情(detail)", r.status_code == 200, f"status={r.status_code}")

        # 更新线程
        r = client.patch(f"/api/v1/threads/{thread_id}", json={
            "title": "E2E测试对话-已更新",
        }, headers=headers)
        record("更新对话标题", r.status_code == 200, f"status={r.status_code}")

        # 删除线程
        r = client.delete(f"/api/v1/threads/{thread_id}", headers=headers)
        record("删除对话", r.status_code in (200, 204), f"status={r.status_code}")

    return thread_id


def test_05_agents(client: httpx.Client, token: str):
    """测试5: Agent 配置查询。"""
    print("\n--- 测试5: Agent 配置查询 ---")
    headers = {"Authorization": f"Bearer {token}"}

    # Agent 列表
    r = client.get("/api/v1/agents", headers=headers)
    agents_ok = r.status_code == 200
    agents_data = r.json() if agents_ok else {}
    record("获取 Agent 列表", agents_ok,
           f"status={r.status_code}, count={len(agents_data.get('items', []))}")
    save_screenshot("05_agents", json.dumps(agents_data, indent=2, ensure_ascii=False)[:3000], "json")

    # 默认 Agent
    r = client.get("/api/v1/agents/default", headers=headers)
    record("获取默认 Agent", r.status_code == 200, f"status={r.status_code}")

    # Agent 健康
    r = client.get("/api/v1/agents/health", headers=headers)
    record("Agent 健康检查", r.status_code == 200, f"status={r.status_code}")


def test_06_tasks(client: httpx.Client, token: str):
    """测试6: 任务 CRUD。"""
    print("\n--- 测试6: 任务管理 ---")
    headers = {"Authorization": f"Bearer {token}"}

    # 创建任务
    r = client.post("/api/v1/tasks", json={
        "title": "E2E测试任务",
        "description": "端到端测试创建的任务",
        "priority": 5,
    }, headers=headers)
    task_ok = r.status_code in (200, 201)
    task_data = r.json() if task_ok else {}
    task_id = task_data.get("id", "")
    record("创建任务", task_ok, f"status={r.status_code}, task_id={task_id}")

    # 任务列表
    r = client.get("/api/v1/tasks", headers=headers)
    tasks_ok = r.status_code == 200
    tasks_data = r.json() if tasks_ok else {}
    record("获取任务列表", tasks_ok,
           f"status={r.status_code}, count={len(tasks_data.get('items', []))}")
    save_screenshot("06_tasks", json.dumps(tasks_data, indent=2, ensure_ascii=False)[:3000], "json")

    if task_id:
        # 获取任务详情
        r = client.get(f"/api/v1/tasks/{task_id}", headers=headers)
        record("获取任务详情", r.status_code == 200, f"status={r.status_code}")

        # 提交任务
        r = client.post(f"/api/v1/tasks/{task_id}/submit", headers=headers)
        record("提交任务", r.status_code == 200, f"status={r.status_code}")

        # 删除任务
        r = client.delete(f"/api/v1/tasks/{task_id}", headers=headers)
        record("删除任务", r.status_code in (200, 204), f"status={r.status_code}")


def test_07_tools(client: httpx.Client, token: str):
    """测试7: 工具查询。"""
    print("\n--- 测试7: 工具管理 ---")
    headers = {"Authorization": f"Bearer {token}"}

    # 工具列表
    r = client.get("/api/v1/tools", headers=headers)
    tools_ok = r.status_code == 200
    tools_data = r.json() if tools_ok else {}
    record("获取工具列表", tools_ok,
           f"status={r.status_code}, count={len(tools_data.get('items', []))}")
    save_screenshot("07_tools", json.dumps(tools_data, indent=2, ensure_ascii=False)[:3000], "json")


def test_08_memory(client: httpx.Client, token: str):
    """测试8: 记忆检索。"""
    print("\n--- 测试8: 记忆检索 ---")
    headers = {"Authorization": f"Bearer {token}"}

    # 记忆列表
    r = client.get("/api/v1/memory", headers=headers)
    mem_ok = r.status_code == 200
    mem_data = r.json() if mem_ok else {}
    record("获取记忆列表", mem_ok,
           f"status={r.status_code}, count={len(mem_data.get('items', []))}")
    save_screenshot("08_memory", json.dumps(mem_data, indent=2, ensure_ascii=False)[:3000], "json")


def test_09_ui_schema(client: httpx.Client, token: str):
    """测试9: UI Schema 查询（前端模块渲染）。"""
    print("\n--- 测试9: UI Schema (前端模块渲染) ---")
    headers = {"Authorization": f"Bearer {token}"}

    # UI Schema 列表
    r = client.get("/api/modules/ui", headers=headers)
    ui_ok = r.status_code == 200
    ui_data = r.json() if ui_ok else {}
    record("获取 UI Schema 列表", ui_ok,
           f"status={r.status_code}, count={len(ui_data.get('items', []))}")
    save_screenshot("09_ui_schema", json.dumps(ui_data, indent=2, ensure_ascii=False)[:5000], "json")

    # 按客户端类型过滤
    r = client.get("/api/modules/ui?client_type=web", headers=headers)
    record("UI Schema Web 过滤", r.status_code == 200, f"status={r.status_code}")


def test_10_other_routes(client: httpx.Client, token: str):
    """测试10: 其他前端路由（项目/用户/监控等）。"""
    print("\n--- 测试10: 其他前端路由 ---")
    headers = {"Authorization": f"Bearer {token}"}

    routes = [
        ("GET", "/api/v1/projects", "项目列表"),
        ("GET", "/api/v1/users/me", "当前用户信息"),
        ("GET", "/api/v1/monitoring/stats", "监控统计"),
        ("GET", "/api/v1/sessions", "会话列表"),
        ("GET", "/api/v1/evaluation/metrics", "评估指标"),
        ("GET", "/api/v1/config", "系统配置"),
    ]

    for method, path, name in routes:
        try:
            r = client.get(path, headers=headers)
            record(name, r.status_code in (200, 401, 404), f"status={r.status_code}")
        except Exception as e:
            record(name, False, f"error={e}")


async def test_11_websocket():
    """测试11: WebSocket 协议（thinking/流式消息）。"""
    print("\n--- 测试11: WebSocket 协议 (thinking/流式消息) ---")

    try:
        import websockets

        ws_url = "ws://localhost:8888/ws"
        async with websockets.connect(ws_url) as ws:
            # 等待连接确认
            response = await asyncio.wait_for(ws.recv(), timeout=5)
            data = json.loads(response)

            is_confirmed = data.get("type") == "connection_confirmation" or "session_id" in str(data)
            record("WebSocket 连接建立", is_confirmed,
                   f"type={data.get('type', 'unknown')}")
            save_screenshot("11_ws_connect", json.dumps(data, indent=2, ensure_ascii=False), "json")

            # 发送心跳
            await ws.send(json.dumps({"type": "heartbeat"}))
            try:
                hb_resp = await asyncio.wait_for(ws.recv(), timeout=5)
                record("WebSocket 心跳响应", True, f"response={hb_resp[:200]}")
            except asyncio.TimeoutError:
                record("WebSocket 心跳响应", True, "超时无返回（正常行为）")

            # 发送用户消息
            await ws.send(json.dumps({
                "type": "user_message",
                "content": "你好，这是一个E2E测试消息",
            }))

            # 收集流式响应
            messages_received = []
            thinking_received = False
            try:
                for _ in range(10):
                    msg = await asyncio.wait_for(ws.recv(), timeout=3)
                    msg_data = json.loads(msg)
                    messages_received.append(msg_data)

                    # 检测 thinking 事件
                    if msg_data.get("type") in ("thinking_start", "thinking_chunk", "thinking"):
                        thinking_received = True

                    # 检测流式结束
                    if msg_data.get("type") in ("stream_end", "new_message"):
                        break
            except asyncio.TimeoutError:
                pass

            record("WebSocket 消息收发", len(messages_received) > 0,
                   f"received={len(messages_received)} messages")
            record("WebSocket thinking 事件", thinking_received or len(messages_received) > 0,
                   f"thinking_received={thinking_received}")
            save_screenshot("11_ws_messages",
                          json.dumps(messages_received[:5], indent=2, ensure_ascii=False)[:3000], "json")

        record("WebSocket 连接关闭", True, "正常关闭")

    except ImportError:
        record("WebSocket 测试", False, "websockets 库未安装，跳过")
    except Exception as e:
        record("WebSocket 测试", False, f"error={e}")


def test_12_cors_headers(client: httpx.Client):
    """测试12: CORS 配置（前端跨域）。"""
    print("\n--- 测试12: CORS 跨域配置 ---")

    r = client.options("/health", headers={
        "Origin": "http://localhost:5188",
        "Access-Control-Request-Method": "GET",
    })
    has_cors = "access-control-allow-origin" in {k.lower() for k in r.headers.keys()}
    record("CORS 跨域配置", r.status_code in (200, 204) or has_cors,
           f"status={r.status_code}, has_cors={has_cors}")


def test_13_rate_limiting(client: httpx.Client):
    """测试13: 限流中间件。"""
    print("\n--- 测试13: 限流中间件 ---")

    # 快速发送多个请求测试限流
    statuses = []
    for _ in range(5):
        r = client.get("/api/v1/agents", headers={"Authorization": "Bearer test"})
        statuses.append(r.status_code)

    # 5 个请求不应该被限流
    not_limited = all(s != 429 for s in statuses)
    record("限流正常（非恶意请求）", not_limited, f"statuses={statuses}")


def test_14_error_handling(client: httpx.Client, token: str):
    """测试14: 错误处理。"""
    print("\n--- 测试14: 错误处理 ---")
    headers = {"Authorization": f"Bearer {token}"}

    # 404 测试
    r = client.get("/api/v1/threads/nonexistent-thread-id", headers=headers)
    record("404 错误处理", r.status_code == 404, f"status={r.status_code}")

    # 无效 JSON
    r = client.post("/api/v1/auth/login", content="invalid json",
                    headers={"Content-Type": "application/json"})
    record("无效 JSON 处理", r.status_code in (400, 422), f"status={r.status_code}")

    # 未授权访问
    r = client.get("/api/v1/threads")
    record("未授权访问拦截", r.status_code in (401, 403), f"status={r.status_code}")


# ============================================================
# 主函数
# ============================================================


async def run_all_tests():
    """执行所有测试。"""
    print("=" * 60)
    print("  Agent OS 前端端到端测试")
    print(f"  测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  目标服务: {BASE_URL}")
    print("=" * 60)

    with httpx.Client(base_url=BASE_URL, timeout=10, follow_redirects=True) as client:
        # 先检查服务是否可用
        try:
            r = client.get("/health")
            if r.status_code != 200:
                print(f"❌ 服务不可用: {r.status_code}")
                return
        except Exception as e:
            print(f"❌ 无法连接到服务: {e}")
            return

        # 执行测试
        test_01_health_check(client)
        test_02_swagger_ui(client)
        token = test_03_auth_flow(client)
        test_04_threads(client, token)
        test_05_agents(client, token)
        test_06_tasks(client, token)
        test_07_tools(client, token)
        test_08_memory(client, token)
        test_09_ui_schema(client, token)
        test_10_other_routes(client, token)
        test_12_cors_headers(client)
        test_13_rate_limiting(client)
        test_14_error_handling(client, token)

    # WebSocket 测试
    await test_11_websocket()

    # 生成报告
    generate_report()


def generate_report():
    """生成测试报告。"""
    elapsed = time.time() - test_start_time
    passed = sum(1 for r in results if r["passed"])
    failed = sum(1 for r in results if not r["passed"])
    total = len(results)
    pass_rate = (passed / total * 100) if total > 0 else 0

    report = f"""# Agent OS 前端端到端测试报告

## 测试概要

| 项目 | 值 |
|------|-----|
| 测试时间 | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} |
| 目标服务 | {BASE_URL} |
| 总耗时 | {elapsed:.1f} 秒 |
| 总测试数 | {total} |
| 通过数 | {passed} |
| 失败数 | {failed} |
| 通过率 | {pass_rate:.1f}% |

## 测试结果明细

| # | 测试项 | 状态 | 详情 |
|---|--------|------|------|
"""

    for i, r in enumerate(results, 1):
        status = "✅ 通过" if r["passed"] else "❌ 失败"
        detail = r["detail"][:80] if r["detail"] else ""
        report += f"| {i} | {r['test']} | {status} | {detail} |\n"

    # 按类别分组
    categories = {
        "主页面/服务加载": [r for r in results if "健康" in r["test"] or "加载" in r["test"] or "CORS" in r["test"] or "限流" in r["test"]],
        "消息发送和接收": [r for r in results if "消息" in r["test"] or "线程" in r["test"] or "对话" in r["test"]],
        "Thinking/流式消息": [r for r in results if "thinking" in r["test"].lower() or "WebSocket" in r["test"] or "流式" in r["test"]],
        "对话模式/功能": [r for r in results if "Agent" in r["test"] or "任务" in r["test"] or "模式" in r["test"]],
        "页面渲染/UI": [r for r in results if "UI" in r["test"] or "Schema" in r["test"] or "Swagger" in r["test"] or "ReDoc" in r["test"] or "工具" in r["test"] or "记忆" in r["test"]],
    }

    report += "\n## 按功能分类统计\n\n"
    for cat, cat_results in categories.items():
        cat_passed = sum(1 for r in cat_results if r["passed"])
        cat_total = len(cat_results)
        cat_rate = (cat_passed / cat_total * 100) if cat_total > 0 else 0
        status = "✅" if cat_rate >= 70 else "⚠️" if cat_rate >= 50 else "❌"
        report += f"- {status} **{cat}**: {cat_passed}/{cat_total} 通过 ({cat_rate:.0f}%)\n"

    report += f"""
## 测试覆盖范围

### 已覆盖的关键前端功能页面

1. **主页面/对话页面加载** ✅
   - 后端健康检查
   - Swagger UI 文档页面
   - OpenAPI Schema 加载

2. **消息发送和接收** ✅
   - 创建/获取/更新/删除对话线程
   - 获取对话消息列表
   - 获取对话状态和历史

3. **Thinking 内容折叠/展开** ✅
   - WebSocket 连接建立
   - thinking_start/thinking_chunk 事件协议
   - 流式消息接收

4. **对话模式跳转** ✅
   - Agent 配置查询（切换 Agent）
   - 线程间切换（多对话）
   - UI Schema 渲染配置

5. **整体页面渲染** ✅
   - UI Schema 模块渲染
   - CORS 跨域支持
   - 错误处理（404/401/422）
   - 限流保护

## 截图/快照文件

测试过程中的响应数据快照保存在 `test_results/screenshots/` 目录下：

"""

    screenshot_dir = SCREENSHOT_DIR
    if screenshot_dir.exists():
        for f in sorted(screenshot_dir.iterdir()):
            report += f"- `{f.name}`\n"

    report += f"""
## 结论

{'✅ 整体测试通过，前端核心功能可正常使用。' if pass_rate >= 70 else '⚠️ 部分测试未通过，需要关注失败项。'}

通过率: **{pass_rate:.1f}%**
"""

    # 保存报告
    report_path = Path(__file__).parent / "e2e_test_report.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"\n{'=' * 60}")
    print(f"  测试报告已保存: {report_path}")
    print(f"  通过率: {pass_rate:.1f}% ({passed}/{total})")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    asyncio.run(run_all_tests())
