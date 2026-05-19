#!/usr/bin/env python3
"""
移动端响应式适配功能验证脚本

自动化检查全站移动端响应式 CSS 类是否正确应用。
用法: python verify_reproduce.py [--fix-hint]

输出: 每个检查项的 通过/未通过 状态，以及汇总统计。
"""

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# ============================================================
# 配置
# ============================================================

# 项目根目录（脚本位于 frontend/ 下）
BASE_DIR = Path(__file__).resolve().parent
SRC_DIR = BASE_DIR / "src"


@dataclass
class CheckResult:
    """单个检查项结果"""
    category: str
    item: str
    expected: str
    found: bool
    evidence: str  # 匹配到的行内容或未找到说明
    line_number: Optional[int] = None


def read_file(relative_path: str) -> tuple[str, list[str]]:
    """读取文件，返回 (完整内容, 行列表)"""
    filepath = SRC_DIR / relative_path
    if not filepath.exists():
        return "", []
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
    return content, content.splitlines()


def find_class_in_file(relative_path: str, class_pattern: str) -> tuple[bool, Optional[str], Optional[int]]:
    """
    在文件中搜索包含指定 CSS 类模式的行。
    class_pattern 可以是部分字符串（如 "grid-cols-1" "sm:grid-cols-3"），
    函数会检查这些关键部分是否出现在同一行。

    返回 (是否找到, 匹配行内容, 行号)
    """
    content, lines = read_file(relative_path)
    if not lines:
        return False, None, None

    # 如果模式包含空格，拆分为多个必须同时出现的关键字
    keywords = class_pattern.split()

    for i, line in enumerate(lines, 1):
        if all(kw in line for kw in keywords):
            return True, line.strip(), i

    return False, None, None


def find_any_class_in_file(relative_path: str, *patterns: str) -> tuple[bool, Optional[str], Optional[int]]:
    """
    在文件中搜索多个模式中的任意一个。
    返回 (是否找到, 匹配行内容, 行号)
    """
    for pattern in patterns:
        found, line, num = find_class_in_file(relative_path, pattern)
        if found:
            return True, line, num
    return False, None, None


def check_tsc_no_emit() -> tuple[bool, str]:
    """运行 tsc --noEmit 检查类型错误"""
    try:
        result = subprocess.run(
            ["npx", "tsc", "--noEmit"],
            capture_output=True,
            text=True,
            cwd=str(BASE_DIR),
            timeout=60,
        )
        if result.returncode == 0:
            return True, "tsc --noEmit 通过，零错误"
        else:
            errors = result.stdout.strip() or result.stderr.strip()
            # 只取前几行
            error_lines = errors.splitlines()[:5]
            return False, f"tsc --noEmit 失败:\n" + "\n".join(error_lines)
    except FileNotFoundError:
        return False, "npx/tsc 未找到，请确认 Node.js 环境已安装"
    except subprocess.TimeoutExpired:
        return False, "tsc --noEmit 超时（60秒）"


# ============================================================
# 验证检查项定义
# ============================================================

def run_all_checks() -> list[CheckResult]:
    """执行所有检查，返回结果列表"""
    results: list[CheckResult] = []

    def check(category: str, item: str, file_path: str, class_pattern: str):
        found, evidence, line_num = find_class_in_file(file_path, class_pattern)
        results.append(CheckResult(
            category=category,
            item=item,
            expected=class_pattern,
            found=found,
            evidence=evidence or "未找到匹配",
            line_number=line_num,
        ))

    def check_any(category: str, item: str, file_path: str, *patterns: str):
        found, evidence, line_num = find_any_class_in_file(file_path, *patterns)
        results.append(CheckResult(
            category=category,
            item=item,
            expected=" | ".join(patterns),
            found=found,
            evidence=evidence or "未找到匹配",
            line_number=line_num,
        ))

    # ----------------------------------------------------------
    # 1. 全局响应式基础设施
    # ----------------------------------------------------------

    # 1.1 responsive.css 存在性
    resp_path = "styles/responsive.css"
    content, lines = read_file(resp_path)
    resp_exists = len(lines) > 0
    results.append(CheckResult(
        category="全局基础设施", item="responsive.css 存在",
        expected="文件存在且非空",
        found=resp_exists,
        evidence=f"文件 {resp_path}，{len(lines)} 行" if resp_exists else "文件不存在或为空",
    ))

    # 1.2 触控目标最小尺寸
    check("全局基础设施", "触控目标最小尺寸 (min-height: 44px)",
          resp_path, "min-height: 44px")

    # 1.3 prefers-reduced-motion
    check("全局基础设施", "减少动画媒体查询 (prefers-reduced-motion)",
          resp_path, "prefers-reduced-motion")

    # 1.4 移动端表格转卡片
    check("全局基础设施", "移动端表格转卡片样式 (mobile-card-table)",
          resp_path, "mobile-card-table")

    # 1.5 index.css 第一行导入 responsive.css
    content, lines = read_file("index.css")
    first_line_import = lines[0].strip().startswith("@import") and "responsive.css" in lines[0] if lines else False
    results.append(CheckResult(
        category="全局基础设施", item="index.css 导入 responsive.css",
        expected="第一行 @import responsive.css",
        found=first_line_import,
        evidence=lines[0].strip() if lines else "文件为空",
        line_number=1,
    ))

    # ----------------------------------------------------------
    # 2. 页面级响应式验证
    # ----------------------------------------------------------

    # --- MemoryPage ---
    mp = "pages/memory/MemoryPage.tsx"
    check("MemoryPage", "统计卡片网格 grid-cols-1 sm:grid-cols-3",
          mp, "grid-cols-1 sm:grid-cols-3")
    check("MemoryPage", "搜索区域移动端竖排 flex-col sm:flex-row",
          mp, "flex-col sm:flex-row")
    check("MemoryPage", "内边距 p-3 sm:p-6",
          mp, "p-3 sm:p-6")
    # min-h-[44px] 特殊处理
    content, lines = read_file(mp)
    found_44 = any("min-h-[44px]" in line for line in lines)
    matching_lines = [f"L{i+1}: {lines[i].strip()}" for i in range(len(lines)) if "min-h-[44px]" in lines[i]]
    results.append(CheckResult(
        category="MemoryPage", item="Tab/分页按钮 min-h-[44px]",
        expected="min-h-[44px]",
        found=found_44,
        evidence="; ".join(matching_lines[:3]) if found_44 else "未找到 min-h-[44px]",
    ))

    # --- MonitoringPage ---
    mop = "pages/monitoring/MonitoringPage.tsx"
    check("MonitoringPage", "移动端卡片视图 md:hidden",
          mop, "md:hidden")
    check("MonitoringPage", "桌面端表格视图 hidden md:block",
          mop, "hidden md:block")
    check("MonitoringPage", "内边距 p-3 sm:p-6",
          mop, "p-3 sm:p-6")
    check("MonitoringPage", "Header时间戳移动端隐藏 sm:inline",
          mop, "sm:inline")
    check("MonitoringPage", "自动刷新文字移动端隐藏 hidden sm:inline",
          mop, "hidden sm:inline")

    # --- AdminPage ---
    ap = "pages/admin/AdminPage.tsx"
    check("AdminPage", "统计卡片网格 grid-cols-1 sm:grid-cols-3",
          ap, "grid-cols-1 sm:grid-cols-3")
    check("AdminPage", "用户移动端卡片视图 md:hidden",
          ap, "md:hidden")
    check("AdminPage", "桌面端表格视图 hidden md:block",
          ap, "hidden md:block")
    check("AdminPage", "内边距 p-3 sm:p-6",
          ap, "p-3 sm:p-6")

    # --- ToolsPage ---
    tp = "pages/tools/ToolsPage.tsx"
    check("ToolsPage", "输入框响应式 w-full sm:w-auto",
          tp, "w-full sm:w-auto")
    check("ToolsPage", "内边距 p-3 sm:p-6",
          tp, "p-3 sm:p-6")
    # min-h-[44px]
    content, lines = read_file(tp)
    found_44 = any("min-h-[44px]" in line for line in lines)
    matching_lines = [f"L{i+1}: {lines[i].strip()}" for i in range(len(lines)) if "min-h-[44px]" in lines[i]]
    results.append(CheckResult(
        category="ToolsPage", item="分页按钮 min-h-[44px]",
        expected="min-h-[44px]",
        found=found_44,
        evidence="; ".join(matching_lines[:3]) if found_44 else "未找到 min-h-[44px]",
    ))

    # --- AgentsPage ---
    agp = "pages/agents/AgentsPage.tsx"
    # min-h-[44px]
    content, lines = read_file(agp)
    found_44 = any("min-h-[44px]" in line for line in lines)
    matching_lines = [f"L{i+1}: {lines[i].strip()}" for i in range(len(lines)) if "min-h-[44px]" in lines[i]]
    results.append(CheckResult(
        category="AgentsPage", item="刷新按钮 min-h-[44px]",
        expected="min-h-[44px]",
        found=found_44,
        evidence="; ".join(matching_lines[:3]) if found_44 else "未找到 min-h-[44px]",
    ))
    check("AgentsPage", "内边距 p-3 sm:p-6",
          agp, "p-3 sm:p-6")

    # --- SettingsPage ---
    sp = "pages/settings/SettingsPage.tsx"
    check("SettingsPage", "卡片网格 grid-cols-1 md:grid-cols-2 lg:grid-cols-3",
          sp, "grid-cols-1 md:grid-cols-2 lg:grid-cols-3")
    check("SettingsPage", "内边距 p-3 sm:p-6",
          sp, "p-3 sm:p-6")

    # --- DebugPage ---
    dp = "pages/debug/DebugPage.tsx"
    check("DebugPage", "卡片网格 grid-cols-1 md:grid-cols-2 lg:grid-cols-3",
          dp, "grid-cols-1 md:grid-cols-2 lg:grid-cols-3")
    check("DebugPage", "内边距 p-3 sm:p-6",
          dp, "p-3 sm:p-6")

    # --- LlmSettingsPage ---
    llmp = "pages/settings/LlmSettingsPage.tsx"
    check("LlmSettingsPage", "表单移动端单列 flex-col sm:flex-row",
          llmp, "flex-col sm:flex-row")
    check("LlmSettingsPage", "标签宽度适配 sm:min-w",
          llmp, "sm:min-w-[120px]")
    check("LlmSettingsPage", "内边距 p-3 sm:p-6 (PageShell)",
          llmp, "p-3 sm:p-6")
    # Tab 横向可滚动
    content, lines = read_file(llmp)
    has_overflow = any("overflow-x" in line for line in lines)
    results.append(CheckResult(
        category="LlmSettingsPage", item="Tab 横向可滚动 overflow-x-auto",
        expected="overflow-x-auto",
        found=has_overflow,
        evidence="找到 overflow-x 样式" if has_overflow else "未找到 overflow-x 样式（次要问题：窄屏Tab可能溢出）",
    ))

    # ----------------------------------------------------------
    # 3. 触控友好性 - ChatInput
    # ----------------------------------------------------------
    ci = "components/chat/ChatInput.tsx"
    content, lines = read_file(ci)
    # 发送/停止/附件按钮检查 h-11 w-11（44px）和 sm:h-8 sm:w-8
    send_btn = any("h-11 w-11" in line and "sm:h-8" in line for line in lines)
    results.append(CheckResult(
        category="ChatInput触控", item="发送/停止按钮 44px触控 (h-11 w-11, sm:h-8 sm:w-8)",
        expected="h-11 w-11 sm:h-8 sm:w-8",
        found=send_btn,
        evidence="找到 h-11 w-11 + sm 响应式适配" if send_btn else "未找到 44px 触控尺寸",
    ))

    # 附件按钮
    attach_btn = any("h-11 w-11" in line for line in lines)
    results.append(CheckResult(
        category="ChatInput触控", item="附件按钮 44px触控 (h-11 w-11)",
        expected="h-11 w-11",
        found=attach_btn,
        evidence="找到 h-11 w-11" if attach_btn else "未找到 h-11 w-11",
    ))

    # 文本输入框 min-h-[44px]
    textarea_44 = any("min-h-[44px]" in line for line in lines)
    results.append(CheckResult(
        category="ChatInput触控", item="文本输入框 min-h-[44px]",
        expected="min-h-[44px]",
        found=textarea_44,
        evidence="找到 min-h-[44px]" if textarea_44 else "未找到 min-h-[44px]",
    ))

    # ----------------------------------------------------------
    # 4. 编译验证
    # ----------------------------------------------------------
    tsc_ok, tsc_msg = check_tsc_no_emit()
    results.append(CheckResult(
        category="编译验证", item="tsc --noEmit 零错误",
        expected="exit code 0",
        found=tsc_ok,
        evidence=tsc_msg,
    ))

    return results


# ============================================================
# 输出格式化
# ============================================================

PASS_MARK = "✅"
FAIL_MARK = "❌"
WARN_MARK = "⚠️"


def print_results(results: list[CheckResult]):
    """打印格式化的验证结果"""
    print("=" * 70)
    print("  移动端响应式适配功能验证报告")
    print("=" * 70)
    print()

    current_category = ""
    passed = 0
    failed = 0

    for r in results:
        if r.category != current_category:
            current_category = r.category
            print(f"\n── {current_category} ──")

        mark = PASS_MARK if r.found else FAIL_MARK
        line_info = f" (第{r.line_number}行)" if r.line_number else ""

        if r.found:
            passed += 1
            print(f"  {mark} {r.item}{line_info}")
            # 只显示简短证据
            ev_short = r.evidence[:100] + "..." if len(r.evidence) > 100 else r.evidence
            print(f"      → {ev_short}")
        else:
            failed += 1
            print(f"  {mark} {r.item}")
            print(f"      期望: {r.expected}")
            print(f"      实际: {r.evidence}")

    total = passed + failed
    rate = (passed / total * 100) if total > 0 else 0

    print()
    print("=" * 70)
    print(f"  汇总: {passed}/{total} 通过 ({rate:.1f}%)")
    if failed > 0:
        print(f"  {FAIL_MARK} 未通过: {failed} 项")
    print("=" * 70)

    # 返回是否全部通过
    return failed == 0


# ============================================================
# 主入口
# ============================================================

if __name__ == "__main__":
    show_hint = "--fix-hint" in sys.argv

    results = run_all_checks()
    all_passed = print_results(results)

    if not all_passed and show_hint:
        print("\n💡 修复建议:")
        for r in results:
            if not r.found:
                print(f"  - [{r.category}] {r.item}:")
                print(f"    需要在对应文件中添加 \"{r.expected}\" 样式类")
        print()

    sys.exit(0 if all_passed else 1)
