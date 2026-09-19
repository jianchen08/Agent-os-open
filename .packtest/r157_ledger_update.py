"""R157 台账更新：R157 轮记 + OBS-24。UTF-8 直写。"""
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

FP = r"D:\myproject\container_e17cc5927dfd\docs\working\packtest\测试台账.md"
text = open(FP, encoding="utf-8").read()

# 1) OBS-24（OBS-23 行后）
OBS24 = "\n| OBS-24 | 观察（运维配方/风险） | **launcher 对内核二进制的构建失败静默降级**：start_web_02.bat 的 cargo build 失败（典型=运行中内核 exe 被 Windows 文件锁，链接报「拒绝访问 os error 5」）不阻断启动——会直接用**旧 exe** 起内核，「重启带新修复」落空且无显式报错（R157 实证：a56aee1c3 的索引 DDL 在 store.rs 已改、重启多次后仍未落库，exe mtime 22:38 < store.rs 22:47）。**配方**：带内核修复的重启必须①先停内核（或「改名让位」`mv agentos-kernel.exe .old` 后重链，运行中也可）②手工 `cargo build` 确认 mtime 变新再启动③启动后按修复内容做存在性验证（本例=sqlite_master 查索引）。旁证：手工改名后重链仅 27s | 观察中（配方已入账） | — | cargo os error 5 输出 + exe/store.rs mtime 对照 + sqlite_master 索引查证 |\n"
anchor4 = "| OBS-23 |"
i4 = text.find(anchor4)
line_end4 = text.find("\n", i4) + 1
text = text[:line_end4] + OBS24 + text[line_end4:]

# 2) R157 轮记（插在 R156 行后）
R157 = "- **R157（2026-09-18 23:5x~00:3x 本地，受控重启应用 BUG-47 索引层 ✓ + 五路全扫 ✓）**：①**发现 BUG-47 索引层未上线**：`idx_traces_tenant_created` 不在实库（内核自 13:10 起未重启；查询层兜底 0.6s 生效中）→ 受控重启。重启过程暴露 **OBS-24**（launcher 构建失败静默降级：运行中 exe 文件锁致 cargo 链接「拒绝访问」→ 用旧 exe 继续启动无报错）；按「改名让位→手工重链 27s→kernel-only 重启」配方完成。②**索引落库三证 ✓**：sqlite_master 见 `idx_traces_tenant_created`、EXPLAIN QUERY PLAN=`SEARCH USING INDEX`（无全表扫描）、窗口查询 0.000s——**BUG-47 修复第二层（索引）实证上线，0.6s 兜底→28ms 最优**。③**五路回归扫 ✓（④复测，R151 后最新 HEAD：58ab83965 指针切换+a56aee1c3 监控修复+主 agent 九桌面工具+2f0f2a719 模式测试）**：冒烟/写作/调研/编码 4 路 WS 全部 stream_end；pipeline state mode 注入 3/3 PASS（首轮编码路空读系 +8s 轮询时序伪影，复读=coding——**读数判据修正：模式注入验证应在管道终态后 ≥15s 或复查**）。④前端 vite 因 kernel-only 启动未起，已手工补起（200）。环境恢复健康（新 exe 内核+索引+前端）。无新产品缺陷。\n"
anchor2 = "- **R156（2026-09-18 23:0x~23:3x 本地，T2 透项闭环——BUG-47 立案+修复+回归关闭 ✓）**"
i2 = text.find(anchor2)
line_end2 = text.find("\n", i2) + 1
text = text[:line_end2] + R157 + text[line_end2:]

open(FP, "w", encoding="utf-8", newline="\n").write(text)
print("ledger updated:", len(text), "chars")
