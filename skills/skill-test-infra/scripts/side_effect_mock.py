# -*- coding: utf-8 -*-
"""
副作用 Mock 和验证

提供轻量级 HTTP Mock 服务，用于端到端测试中捕获和验证副作用（通知推送、消息发送等）。

两个子命令：
- serve: 启动 Mock HTTP 服务器，记录所有收到的请求
- verify: 验证记录的副作用是否符合预期
"""

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


# ──── 全局记录存储 ────

class RecordStore:
    """线程安全的请求记录存储"""

    def __init__(self):
        self.records: list[dict[str, Any]] = []
        self.lock = threading.Lock()

    def add(self, record: dict[str, Any]):
        with self.lock:
            self.records.append(record)

    def get_all(self) -> list[dict[str, Any]]:
        with self.lock:
            return list(self.records)

    def clear(self):
        with self.lock:
            self.records.clear()

    def save(self, output_path: str):
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "records": self.records,
            "total": len(self.records),
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        with open(out, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)


# 全局 store 实例
_store = RecordStore()


class MockHandler(BaseHTTPRequestHandler):
    """Mock HTTP 请求处理器"""

    def _read_body(self) -> str:
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > 0:
            return self.rfile.read(content_length).decode("utf-8", errors="replace")
        return ""

    def _record_request(self, method: str):
        body = self._read_body()
        parsed = urlparse(self.path)

        # 尝试解析 JSON body
        body_data = None
        if body:
            try:
                body_data = json.loads(body)
            except (json.JSONDecodeError, ValueError):
                body_data = body

        record = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "method": method,
            "path": parsed.path,
            "query": parsed.query,
            "headers": dict(self.headers),
            "body": body_data,
        }

        _store.add(record)

        # 返回 200 OK
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        response = json.dumps({"ok": True, "message": "mock received", "index": len(_store.get_all())})
        self.wfile.write(response.encode("utf-8"))

    def do_GET(self):
        self._record_request("GET")

    def do_POST(self):
        self._record_request("POST")

    def do_PUT(self):
        self._record_request("PUT")

    def do_DELETE(self):
        self._record_request("DELETE")

    def do_PATCH(self):
        self._record_request("PATCH")

    def log_message(self, format, *args):
        """覆盖默认日志，输出到 stdout"""
        record_count = len(_store.get_all())
        print(f"  📩 [{record_count}] {args[0]}")


def run_server(port: int, output_path: str | None, save_interval: int = 5):
    """启动 Mock HTTP 服务器"""
    server = HTTPServer(("0.0.0.0", port), MockHandler)
    server.timeout = save_interval

    print(f"🚀 Mock 服务已启动: http://0.0.0.0:{port}")
    print(f"   所有请求将被记录")
    if output_path:
        print(f"   定期保存到: {output_path} (每 {save_interval}s)")
    print("   按 Ctrl+C 停止\n")

    try:
        while True:
            # 处理请求（带超时以允许定期保存）
            server.handle_request()

            # 定期保存
            if output_path:
                _store.save(output_path)
    except KeyboardInterrupt:
        print("\n⏹️  停止 Mock 服务...")
    finally:
        # 最终保存
        if output_path:
            _store.save(output_path)
            print(f"💾 记录已保存: {output_path} ({len(_store.get_all())} 条)")

        records = _store.get_all()
        if records:
            print(f"\n📊 共收到 {len(records)} 条请求:")
            for i, r in enumerate(records, 1):
                body_preview = ""
                if r["body"]:
                    body_str = json.dumps(r["body"], ensure_ascii=False) if isinstance(r["body"], dict) else str(r["body"])
                    body_preview = f" body={body_preview}{body_str[:60]}"
                print(f"  {i}. [{r['method']}] {r['path']}{body_preview}")

        server.server_close()
        print("✅ Mock 服务已关闭")


def verify_records(record_path: str, expect_path: str,
                   output_path: str | None = None) -> bool:
    """验证记录是否符合预期"""
    # 加载记录
    rec_path = Path(record_path)
    if not rec_path.exists():
        print(f"❌ 记录文件不存在: {record_path}")
        return False

    with open(rec_path, encoding="utf-8") as f:
        record_data = json.load(f)
    records = record_data.get("records", [])

    # 加载预期
    exp_path = Path(expect_path)
    if not exp_path.exists():
        print(f"❌ 预期文件不存在: {expect_path}")
        return False

    with open(exp_path, encoding="utf-8") as f:
        expect_data = json.load(f)
    expectations = expect_data.get("expectations", [])

    print(f"📋 记录: {len(records)} 条, 预期: {len(expectations)} 条")

    results = []
    all_passed = True

    for exp in expectations:
        exp_type = exp.get("type", "exact")
        description = exp.get("description", "未命名验证")

        if exp_type == "count":
            # 验证请求总数
            expected_count = exp.get("count", 0)
            actual_count = len(records)
            passed = actual_count == expected_count
            results.append({
                "description": description,
                "type": "count",
                "expected": expected_count,
                "actual": actual_count,
                "passed": passed,
            })
            status = "✅" if passed else "❌"
            print(f"  {status} {description}: 预期 {expected_count} 条, 实际 {actual_count} 条")
            if not passed:
                all_passed = False

        elif exp_type == "method_path":
            # 验证是否有匹配 method + path 的请求
            method = exp.get("method", "POST").upper()
            path = exp.get("path", "/")
            min_count = exp.get("min_count", 1)

            matched = [
                r for r in records
                if r["method"] == method and r["path"] == path
            ]
            passed = len(matched) >= min_count
            results.append({
                "description": description,
                "type": "method_path",
                "expected": f"{method} {path} (>= {min_count} 次)",
                "actual": f"{len(matched)} 次",
                "passed": passed,
            })
            status = "✅" if passed else "❌"
            print(f"  {status} {description}: {method} {path} 出现 {len(matched)} 次 (预期 >= {min_count})")
            if not passed:
                all_passed = False

        elif exp_type == "body_contains":
            # 验证请求 body 是否包含指定字段/值
            path = exp.get("path", "/")
            method = exp.get("method", "POST").upper()
            body_fields = exp.get("fields", {})

            matched = [
                r for r in records
                if r["method"] == method and r["path"] == path
            ]

            if not matched:
                results.append({
                    "description": description,
                    "type": "body_contains",
                    "expected": f"{method} {path} with fields",
                    "actual": "无匹配请求",
                    "passed": False,
                })
                print(f"  ❌ {description}: 未找到 {method} {path} 请求")
                all_passed = False
                continue

            # 检查最后一个匹配请求的 body
            last = matched[-1]
            body = last.get("body", {})
            if isinstance(body, str):
                body = {}

            field_results = {}
            fields_passed = True
            for field, expected_value in body_fields.items():
                actual_value = body.get(field) if isinstance(body, dict) else None
                field_match = actual_value == expected_value
                field_results[field] = {
                    "expected": expected_value,
                    "actual": actual_value,
                    "match": field_match,
                }
                if not field_match:
                    fields_passed = False

            passed = fields_passed
            results.append({
                "description": description,
                "type": "body_contains",
                "field_results": field_results,
                "passed": passed,
            })
            status = "✅" if passed else "❌"
            print(f"  {status} {description}: body 字段检查 {'全部通过' if passed else '存在不匹配'}")
            if not passed:
                for field, fr in field_results.items():
                    if not fr["match"]:
                        print(f"      ❌ {field}: 预期={fr['expected']}, 实际={fr['actual']}")
                all_passed = False

        elif exp_type == "order":
            # 验证请求顺序
            path_order = exp.get("paths", [])
            actual_paths = [r["path"] for r in records]

            # 检查 paths 是否按顺序出现在实际记录中
            idx = 0
            matched_order = []
            for expected_path in path_order:
                found = False
                while idx < len(actual_paths):
                    if actual_paths[idx] == expected_path:
                        matched_order.append(expected_path)
                        idx += 1
                        found = True
                        break
                    idx += 1
                if not found:
                    break

            passed = len(matched_order) == len(path_order)
            results.append({
                "description": description,
                "type": "order",
                "expected": path_order,
                "actual": actual_paths,
                "passed": passed,
            })
            status = "✅" if passed else "❌"
            print(f"  {status} {description}: 顺序检查 {'通过' if passed else '不匹配'}")
            if not passed:
                print(f"      预期顺序: {path_order}")
                print(f"      实际路径: {actual_paths}")
                all_passed = False

    # 汇总
    print(f"\n{'=' * 60}")
    total = len(results)
    passed_count = sum(1 for r in results if r["passed"])
    print(f"📊 验证结果: {passed_count}/{total} 通过")

    report = {
        "total": total,
        "passed": passed_count,
        "failed": total - passed_count,
        "all_passed": all_passed,
        "results": results,
    }

    if output_path:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2, default=str)
        print(f"💾 验证报告已保存: {output_path}")

    if all_passed:
        print("✅ 全部验证通过！")
    else:
        print("❌ 存在验证失败项")

    return all_passed


def cmd_serve(args):
    """执行 serve 子命令"""
    run_server(port=args.port, output_path=args.output, save_interval=args.save_interval)


def cmd_verify(args):
    """执行 verify 子命令"""
    print("=" * 60)
    print("🔍 副作用验证")
    print("=" * 60)

    passed = verify_records(
        record_path=args.record,
        expect_path=args.expect,
        output_path=args.output,
    )
    sys.exit(0 if passed else 1)


def main():
    parser = argparse.ArgumentParser(
        description="副作用 Mock 和验证",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 启动 Mock 服务
  python side_effect_mock.py serve --port 9999 --output records.json

  # 验证副作用
  python side_effect_mock.py verify --record records.json --expect expect.json --output verify_report.json

预期文件格式 (expect.json):
  {
    "expectations": [
      {"type": "count", "description": "请求总数", "count": 3},
      {"type": "method_path", "description": "创建任务通知", "method": "POST", "path": "/notify", "min_count": 1},
      {"type": "body_contains", "description": "通知内容检查", "method": "POST", "path": "/notify",
       "fields": {"type": "task_created", "priority": "high"}},
      {"type": "order", "description": "调用顺序", "paths": ["/notify", "/log", "/callback"]}
    ]
  }
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # serve 子命令
    serve_parser = subparsers.add_parser("serve", help="启动 Mock HTTP 服务器")
    serve_parser.add_argument("--port", type=int, default=9999, help="监听端口（默认 9999）")
    serve_parser.add_argument("--output", help="请求记录保存路径")
    serve_parser.add_argument("--save-interval", type=int, default=5, help="自动保存间隔秒数（默认 5）")

    # verify 子命令
    verify_parser = subparsers.add_parser("verify", help="验证副作用记录")
    verify_parser.add_argument("--record", required=True, help="副作用记录文件路径")
    verify_parser.add_argument("--expect", required=True, help="预期 JSON 文件路径")
    verify_parser.add_argument("--output", help="验证报告输出路径")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "serve":
        cmd_serve(args)
    elif args.command == "verify":
        cmd_verify(args)


if __name__ == "__main__":
    main()
