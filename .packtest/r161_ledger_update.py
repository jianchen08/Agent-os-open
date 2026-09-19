"""R161 台账更新：R161 轮记。UTF-8 直写。"""
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

FP = r"D:\myproject\container_e17cc5927dfd\docs\working\packtest\测试台账.md"
text = open(FP, encoding="utf-8").read()

R161 = "- **R161（2026-09-19 02:3x~03:2x 本地，栈全停恢复+低频巡检 ✓）**：①发现**栈全停**（9100/6390 双零监听，原因不明——并行会话停栈或控制台窗被关；新提交均为测试稳定化/文档面 aa33fb2dd~16b413b83 无运行时行为变化）。②**恢复遇并行构建锁**：launcher 全量启动卡在 cargo build 排队（并行会话跑 cargo-llvm-cov 持 target 目录锁，多 cargo 进程并发）；按「绕开构建直接拉起」配方恢复——**kernel 裸启配方：`AGENTOS_USER_ROOT=<repo>\\user_root AGENTOS_KERNEL_PORT=9100 kernel\\target\\release\\agentos-kernel.exe`（后台任务，exe 本就含全部已提交运行时改动）+ vite `npm run dev`**，慢启动约 2.5 分钟（1.5GB 库 init×磁盘被 coverage 构建占用）。③**巡检三件套 ✓**：索引保持（idx_traces_tenant_created 在）、迁移保持（99 插件六者齐）、主链冒烟「正常」（C02 十三测）。无新产品缺陷。④环境注记：launcher 构建排队窗口内勿反复重试（cargo 锁公平队列，重试只会更后）；直接裸启是锁竞争期的正解。\n"
anchor2 = "- **R160（2026-09-19 01:5x~02:1x 本地，低频巡检 ✓ 三保持+主链+GUI 补证）**"
i2 = text.find(anchor2)
line_end2 = text.find("\n", i2) + 1
text = text[:line_end2] + R161 + text[line_end2:]

open(FP, "w", encoding="utf-8", newline="\n").write(text)
print("ledger updated:", len(text), "chars")
