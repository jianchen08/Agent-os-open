"""R162 台账更新：R162 轮记。UTF-8 直写。"""
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

FP = r"D:\myproject\container_e17cc5927dfd\docs\working\packtest\测试台账.md"
text = open(FP, encoding="utf-8").read()

R162 = "- **R162（2026-09-19 03:4x~04:1x 本地，低频巡检 ✓ 三保持+主链+写作注入抽查）**：①保持性双 ✓（索引在/99 插件六者齐）。②主链冒烟 ✓（C02 十四测，「正常」stream_end）。③写作注入抽查 ✓：execution_context mode=writing → state.mode='writing' PASS（≥15s 判读窗口按 R157 教训执行，一次读数即中）。无新产品缺陷。巡检结论：系统稳态维持，低频节奏不变。\n"
anchor2 = "- **R161（2026-09-19 02:3x~03:2x 本地，栈全停恢复+低频巡检 ✓）**"
i2 = text.find(anchor2)
line_end2 = text.find("\n", i2) + 1
text = text[:line_end2] + R162 + text[line_end2:]

open(FP, "w", encoding="utf-8", newline="\n").write(text)
print("ledger updated:", len(text), "chars")
