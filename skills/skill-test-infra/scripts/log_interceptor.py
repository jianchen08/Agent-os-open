# -*- coding: utf-8 -*-
"""
日志拦截 + 按规则提取匹配

从日志文件中按预定义规则提取匹配项，支持：
- 按正则模式匹配
- 按日志级别过滤
- 提取捕获组内容
- 实时跟踪模式（--follow）
- 输出结构化 JSON 结果
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any


def load_rules(rules_path: str) -> list[dict[str, Any]]:
    """加载规则文件"""
    path = Path(rules_path)
    if not path.exists():
        print(f"❌ 规则文件不存在: {rules_path}")
        sys.exit(1)

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    rules = data.get("rules", [])
    # 预编译正则
    compiled = []
    for rule in rules:
        name = rule.get("name", "unnamed")
        pattern = rule.get("pattern", "")
        level = rule.get("level")
        extract_groups = rule.get("extract_groups", False)

        try:
            regex = re.compile(pattern)
        except re.error as e:
            print(f"⚠️  规则 '{name}' 正则编译失败: {e}，已跳过")
            continue

        compiled.append({
            "name": name,
            "regex": regex,
            "level": level,
            "extract_groups": extract_groups,
        })

    print(f"📋 已加载 {len(compiled)} 条规则")
    return compiled


def match_line(line: str, rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """对一行日志执行所有规则匹配"""
    results = []
    for rule in rules:
        match = rule["regex"].search(line)
        if match:
            entry = {
                "rule": rule["name"],
                "matched_text": match.group(0),
                "line": line.rstrip("\n"),
            }
            if rule["extract_groups"] and match.lastindex:
                entry["groups"] = list(match.groups())
            if rule["level"]:
                entry["level"] = rule["level"]
            results.append(entry)
    return results


def detect_log_level(line: str) -> str | None:
    """自动检测日志行中的级别"""
    level_patterns = [
        (r"\bDEBUG\b", "DEBUG"),
        (r"\bINFO\b", "INFO"),
        (r"\bWARNING\b|\bWARN\b", "WARNING"),
        (r"\bERROR\b", "ERROR"),
        (r"\bCRITICAL\b|\bFATAL\b", "CRITICAL"),
    ]
    for pattern, level in level_patterns:
        if re.search(pattern, line, re.IGNORECASE):
            return level
    return None


def process_file(log_path: str, rules: list[dict[str, Any]],
                 level_filter: str | None = None) -> list[dict[str, Any]]:
    """处理日志文件，返回所有匹配结果"""
    path = Path(log_path)
    if not path.exists():
        print(f"❌ 日志文件不存在: {log_path}")
        sys.exit(1)

    all_matches = []
    line_count = 0
    match_count = 0

    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line_count += 1

            # 级别过滤
            if level_filter:
                detected = detect_log_level(line)
                if detected and detected != level_filter:
                    # 如果规则本身指定了级别，以规则为准
                    has_matching_level = any(
                        r["level"] == level_filter for r in rules
                    )
                    if not has_matching_level:
                        continue

            results = match_line(line, rules)
            if results:
                for r in results:
                    r["line_number"] = line_count
                all_matches.extend(results)
                match_count += len(results)

    print(f"📊 扫描完成: 共 {line_count} 行, 匹配 {match_count} 项")
    return all_matches


def follow_file(log_path: str, rules: list[dict[str, Any]],
                duration: int = 60, poll_interval: float = 0.5) -> list[dict[str, Any]]:
    """实时跟踪日志文件（类似 tail -f）"""
    path = Path(log_path)

    # 如果文件不存在，先创建
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()

    all_matches = []
    line_count = 0

    print(f"👁️  开始跟踪日志: {log_path} (最长 {duration}s)")
    print("按 Ctrl+C 提前结束\n")

    start_time = time.time()

    with open(path, encoding="utf-8", errors="replace") as f:
        # 跳到文件末尾
        f.seek(0, 2)

        try:
            while time.time() - start_time < duration:
                line = f.readline()
                if line:
                    line_count += 1
                    results = match_line(line, rules)
                    if results:
                        for r in results:
                            r["line_number"] = line_count
                        all_matches.extend(results)
                        # 实时打印匹配
                        for r in results:
                            print(f"  🎯 [{r['rule']}] {r['matched_text'][:80]}")
                else:
                    time.sleep(poll_interval)
        except KeyboardInterrupt:
            print("\n⏹️  用户中断跟踪")

    elapsed = time.time() - start_time
    print(f"\n📊 跟踪完成: {elapsed:.1f}s, {line_count} 行, 匹配 {len(all_matches)} 项")
    return all_matches


def write_output(matches: list[dict[str, Any]], output_path: str | None):
    """输出结果"""
    if output_path:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump({"matches": matches, "total": len(matches)}, f, ensure_ascii=False, indent=2)
        print(f"💾 结果已写入: {output_path}")
    else:
        if matches:
            print("\n--- 匹配结果 ---")
            for m in matches:
                line_info = f"L{m.get('line_number', '?')}"
                groups_info = f" groups={m['groups']}" if "groups" in m else ""
                print(f"  [{m['rule']}] {line_info}: {m['matched_text'][:100]}{groups_info}")
        else:
            print("📭 无匹配结果")


def main():
    parser = argparse.ArgumentParser(
        description="日志拦截 + 按规则提取匹配",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 处理现有日志文件
  python log_interceptor.py --log-file app.log --rules rules.json --output result.json

  # 实时跟踪日志
  python log_interceptor.py --follow --log-file app.log --rules rules.json --duration 120
        """,
    )
    parser.add_argument("--log-file", required=True, help="日志文件路径")
    parser.add_argument("--rules", required=True, help="规则 JSON 文件路径")
    parser.add_argument("--output", help="输出结果 JSON 文件路径（不指定则打印到终端）")
    parser.add_argument("--follow", action="store_true", help="实时跟踪模式（类似 tail -f）")
    parser.add_argument("--duration", type=int, default=60, help="跟踪模式最长秒数（默认 60）")
    parser.add_argument("--level", help="按日志级别过滤（DEBUG/INFO/WARNING/ERROR/CRITICAL）")

    args = parser.parse_args()

    print("=" * 60)
    print("📋 日志拦截器")
    print("=" * 60)

    rules = load_rules(args.rules)

    if args.follow:
        matches = follow_file(args.log_file, rules, duration=args.duration)
    else:
        matches = process_file(args.log_file, rules, level_filter=args.level)

    write_output(matches, args.output)

    # 如果有匹配结果，退出码为 0；无匹配退出码为 2（便于脚本判断）
    sys.exit(0 if matches else 2)


if __name__ == "__main__":
    main()
