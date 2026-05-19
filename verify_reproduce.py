#!/usr/bin/env python3
"""
功能验证复现脚本 - 前端消息渲染修复与 E2E 测试完整性验证

运行方式: python3 verify_reproduce.py
前置条件: 在项目根目录下执行
"""

import ast
import re
import sys


def verify_ws_handler():
    """验证后端 ws_handler.py 修复"""
    print("=" * 60)
    print("1. 后端 ws_handler.py WebSocket 路由回退逻辑验证")
    print("=" * 60)

    with open("ws_handler.py", "r") as f:
        src = f.read()

    results = []

    # 1a. register_global 清理旧连接残留
    reg_global = src[src.find("def register_global"):src.find("def unregister_global")]
    check = "for tid in list(self._active_connections.keys())" in reg_global
    results.append(("register_global 清理 _active_connections 残留", check))

    # 1b. unregister_all_for_ws 同时清理两个字典
    unreg = src[src.find("def unregister_all_for_ws"):src.find("async def notify_request")]
    check = "_global_connections" in unreg and "_active_connections" in unreg
    results.append(("unregister_all_for_ws 同时清理两个连接字典", check))

    # 1c. send_to_thread 回退到 _global_connections
    send = src[src.find("async def send_to_thread"):src.find("# 全局 WebSocket 通知器")]
    check = "for user_id, ws in list(self._global_connections.items())" in send
    results.append(("send_to_thread 回退到 _global_connections", check))

    # 1d. 所有通知方法回退一致性
    for method in ["notify_request", "notify_cancel", "notify_timeout", "notify_timeout_reminder"]:
        start = src.find(f"async def {method}")
        if start == -1:
            results.append((f"{method} 回退一致性", False))
            continue
        end = min(
            src.find("\n    async def ", start + 1) or len(src),
            src.find("\n    def ", start + 1) or len(src),
        )
        block = src[start:end]
        has_both = "_active_connections" in block and "_global_connections" in block
        results.append((f"{method} 回退一致性", has_both))

    fallback_count = src.count("for user_id, ws in list(self._global_connections.items())")
    results.append((f"_global_connections 回退模式共 {fallback_count} 处（期望 5）", fallback_count == 5))

    for desc, ok in results:
        print(f"  {'✅' if ok else '❌'} {desc}")

    passed = all(ok for _, ok in results)
    print(f"\n  结论: {'通过 ✅' if passed else '失败 ❌'}\n")
    return passed


def verify_stream_handler():
    """验证前端 streamHandler.ts pipeline_id 回退"""
    print("=" * 60)
    print("2. 前端 streamHandler.ts 流式事件处理验证")
    print("=" * 60)

    with open("frontend/src/services/websocket/streaming/handlers/streamHandler.ts", "r") as f:
        src = f.read()

    results = []
    handlers = {
        "handleStreamStart": (src.find("export function handleStreamStart"), src.find("export function handleStreamChunk")),
        "handleStreamChunk": (src.find("export function handleStreamChunk"), src.find("export function handleStreamEnd")),
        "handleStreamEnd": (src.find("export function handleStreamEnd"), src.find("export function handleStreamError")),
        "handleStreamError": (src.find("export function handleStreamError"), src.find("export function handleStreamKeepalive")),
        "handleStreamKeepalive": (src.find("export function handleStreamKeepalive"), len(src)),
    }

    for name, (s, e) in handlers.items():
        block = src[s:e]
        ok = "resolvePipelineId" in block and ("if (!pipelineId)" in block or "if (pipelineId)" in block)
        results.append((f"{name} resolvePipelineId + null guard", ok))

    for desc, ok in results:
        print(f"  {'✅' if ok else '❌'} {desc}")

    passed = all(ok for _, ok in results)
    print(f"\n  结论: {'通过 ✅' if passed else '失败 ❌'}\n")
    return passed


def verify_tool_handler():
    """验证前端 toolHandler.ts 工具调用处理"""
    print("=" * 60)
    print("3. 前端 toolHandler.ts 工具调用事件处理验证")
    print("=" * 60)

    with open("frontend/src/services/websocket/streaming/handlers/toolHandler.ts", "r") as f:
        src = f.read()

    results = []

    for name in ["handleToolStart", "handleToolResult"]:
        start = src.find(f"export function {name}")
        end = src.find("export function ", start + 1) if src.find("export function ", start + 1) != -1 else len(src)
        block = src[start:end]
        ok = "resolvePipelineId" in block and "!pipelineId" in block
        results.append((f"{name} resolvePipelineId + null guard", ok))

    # call_id 精确匹配 + fallback
    results.append(("handleToolResult call_id 精确匹配", "tc.call_id === callId" in src))
    results.append(("handleToolResult tool_name fallback", "tc.tool_name === toolName" in src))
    results.append(("handleToolResult 未匹配时自动创建", "if (!matched)" in src))

    for desc, ok in results:
        print(f"  {'✅' if ok else '❌'} {desc}")

    passed = all(ok for _, ok in results)
    print(f"\n  结论: {'通过 ✅' if passed else '失败 ❌'}\n")
    return passed


def verify_router():
    """验证 resolvePipelineId 三级回退"""
    print("=" * 60)
    print("4. 前端 router.ts resolvePipelineId 路由解析验证")
    print("=" * 60)

    with open("frontend/src/services/websocket/streaming/router.ts", "r") as f:
        src = f.read()

    results = [
        ("优先级1: data.pipeline_id", "eventData.data?.pipeline_id" in src),
        ("优先级2: 顶层 pipeline_id", "eventData.pipeline_id" in src),
        ("优先级3: _threadId 回退", "_threadId" in src),
        ("空字符串视为无效", "dataPid === 'string' && dataPid.length > 0" in src.replace("\n", "")),
    ]

    for desc, ok in results:
        print(f"  {'✅' if ok else '❌'} {desc}")

    passed = all(ok for _, ok in results)
    print(f"\n  结论: {'通过 ✅' if passed else '失败 ❌'}\n")
    return passed


def verify_image_gallery():
    """验证 ImageGallery.tsx 图片查看器修复"""
    print("=" * 60)
    print("5. ImageGallery.tsx 图片查看器验证")
    print("=" * 60)

    with open("frontend/src/components/media/ImageGallery.tsx", "r") as f:
        src = f.read()

    results = [
        ("缩略图: thumbnailUrl || url", "image.thumbnailUrl || image.url" in src),
        ("Lightbox大图: url || thumbnailUrl", "currentImage.url || currentImage.thumbnailUrl" in src),
        ("onError 回退到 thumbnailUrl", "img.src !== currentImage.thumbnailUrl && currentImage.thumbnailUrl" in src),
        ("data-testid=lightbox", 'data-testid="lightbox"' in src),
        ("Escape 关闭", "case 'Escape'" in src),
    ]

    for desc, ok in results:
        print(f"  {'✅' if ok else '❌'} {desc}")

    passed = all(ok for _, ok in results)
    print(f"\n  结论: {'通过 ✅' if passed else '失败 ❌'}\n")
    return passed


def verify_e2e_coverage():
    """验证 E2E 测试 6 个场景覆盖"""
    print("=" * 60)
    print("6. E2E 测试 message-render-e2e.spec.ts 覆盖验证")
    print("=" * 60)

    with open("frontend/e2e/message-render-e2e.spec.ts", "r") as f:
        src = f.read()

    describes = re.findall(r"test\.describe\('([^']+)'", src)
    tests = re.findall(r"test\('([^']+)'", src)

    required = {
        "消息持续渲染": ["持续渲染", "实时显示"],
        "工具卡片": ["工具调用卡片", "tool_call"],
        "图片放大": ["图片缩略图", "放大"],
        "多轮对话": ["多消息轮次", "连续对话"],
        "并发消息": ["并发", "不串不丢"],
        "交互卡片": ["交互卡片"],
    }
    results = []
    for name, keywords in required.items():
        found = any(any(kw in d for kw in keywords) for d in describes)
        results.append((f"场景覆盖: {name}", found))

    for desc, ok in results:
        print(f"  {'✅' if ok else '❌'} {desc}")

    print(f"\n  test.describe 数量: {len(describes)} (期望 6)")
    print(f"  test 用例数量: {len(tests)} (期望 7)")

    passed = all(ok for _, ok in results) and len(describes) == 6 and len(tests) == 7
    print(f"\n  结论: {'通过 ✅' if passed else '失败 ❌'}\n")
    return passed


def main():
    print("\n功能验证复现脚本 - 前端消息渲染修复与 E2E 测试完整性\n")

    checks = [
        verify_ws_handler,
        verify_stream_handler,
        verify_tool_handler,
        verify_router,
        verify_image_gallery,
        verify_e2e_coverage,
    ]

    results = [fn() for fn in checks]
    total = len(results)
    passed = sum(results)

    print("=" * 60)
    print(f"汇总: {passed}/{total} 项验证通过")
    print("=" * 60)

    if passed == total:
        print("✅ 所有验证项通过")
        return 0
    else:
        print("❌ 存在验证失败项")
        return 1


if __name__ == "__main__":
    sys.exit(main())
