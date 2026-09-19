"""R156 台账更新：BUG-47 行 + R156 轮记。UTF-8 直写。"""
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

FP = r"D:\myproject\container_e17cc5927dfd\docs\working\packtest\测试台账.md"
text = open(FP, encoding="utf-8").read()

# 1) BUG-47 行（插在 BUG-46 行后）
BUG47 = "| BUG-47 | major | 【R156 立案→同轮关闭】**工具调用记录页双查询在数据增长后必然超时**（性能缺陷）：监控插件 `_query_tool_calls`/`_query_tool_call_stats` 的子查询用 `json_extract(patch_data,'$.tool_results') IS NOT NULL` 内容谓词选行——无索引可用，全表扫描+逐行 JSON 解析大 blob，耗时随 DB 无界增长。实测仓库根实库（1.5GB/traces 20838 行、空闲内核）：明细 8.21s、统计 8.51s，均超端点超时 ⇒ **该页双视图在 grown DB 上永远「查询失败: endpoint timeout」**。勘误：R126 曾归因「内核饱和环境因素」，本轮空闲内核复现＝真缺陷；R126 的「查询失败可见可重试」评价（BUG-42 修复行为）仍成立——失败可见性没问题，坏的是查询本身。**修复 a56aee1c3**：①查询重写为 created_at 有序预窗口（窗口子查询只取 trace_id，大 blob 不进排序器），窗口口径诚实化=「最近 N 条 trace 内的工具结果」（docstring+webview 文案同步）；②内核 DDL 幂等补建 `idx_traces_tenant(tenant_id, created_at DESC)` 索引（对齐 idx_sessions_tenant 先例）。实库计时：明细 8.21s→**0.028s**、统计 8.51s→**0.004s**（无索引兜底也 0.6s<8s）。测试 6 新增先红后绿+monitoring 69 绿+Rust engine 全绿+fmt/clippy/mypy 干净+diff cov 100% | **关闭**（R156 GUI 回归：按工具统计视图渲染 9 工具真实数据——task_manage 12/file_read 5/Snapshot 5 等，含调用次数/成功/失败/平均耗时/最近调用全列+新窗口口径文案「统计窗口：最近 500 条 trace 内的工具调用」，截图 r156_stats_data.png） | 修复 agent 回报 a56aee1c3 | .packtest/r156_stats_full.py + r156_stats_refresh.py + packtest_r156_stats_data.png + 实库计时数字见修复回报 |\n"
anchor = text.find("| BUG-46 |")
line_end = text.find("\n", anchor) + 1
text = text[:line_end] + BUG47 + text[line_end:]

# 2) R156 轮记（插在 R155 行后）
R156 = "- **R156（2026-09-18 23:0x~23:3x 本地，T2 透项闭环——BUG-47 立案+修复+回归关闭 ✓）**：①**T2 工具调用统计视图数据渲染复核**（挂 5+ 轮透项）：导航页卡片路径打通（登录态过期→登录兜底→关 tab→点卡→iframe），页面与「按工具统计|调用明细」双 tab 渲染 ✓；点刷新后**「查询失败: endpoint timeout」在空闲内核复现**——②**立案 BUG-47 并实测钉死根因**（json_extract 谓词全表扫描 8.2/8.5s > 端点超时；数据量增长后永远超时，R126 环境归因勘误），派修 agent 当轮交付 **a56aee1c3**（预窗口查询重写+租户-时间复合索引 DDL，实库 8.5s→0.004s，6 测先红后绿+Rust 索引回归）。③**GUI 回归通过关闭**：热重载后按工具统计渲染 9 工具真实数据（新窗口口径文案可见）。④配方沉淀：R126 的导航页卡片配方有效（工具调用记录=导航页卡片非监控子 tab）；监控端点实际 timeout_ms=8000（非 504 日志里 mode_roleplay 的 5000，两插件各自声明）。\n"
anchor2 = "- **R155（2026-09-18 21:3x~22:2x 本地，打包第八轮 A01→A05 全通过 + 主链复测）**"
i2 = text.find(anchor2)
line_end2 = text.find("\n", i2) + 1
text = text[:line_end2] + R156 + text[line_end2:]

open(FP, "w", encoding="utf-8", newline="\n").write(text)
print("ledger updated:", len(text), "chars")
