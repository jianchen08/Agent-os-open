"""R160 台账更新：R160 轮记。UTF-8 直写。"""
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

FP = r"D:\myproject\container_e17cc5927dfd\docs\working\packtest\测试台账.md"
text = open(FP, encoding="utf-8").read()

R160 = "- **R160（2026-09-19 01:5x~02:1x 本地，低频巡检 ✓ 三保持+主链+GUI 补证）**：①**保持性核对双 ✓**：BUG-47 索引（idx_traces_tenant_created）仍在实库；BUG-48 迁移保持（插件面 99、六用户插件全 PRESENT）——R158 修复无回退。②**主链巡检冒烟 ✓（C02 十二测）**：WS 协议「R160 巡检」→stream_end→「正常」精确回复（FE 近期 tsc/jscpd/覆盖率清理提交 d184d15b8~16b413b83 不涉内核/管道行为，dev vite 实况已在跑新 FE）。③**R158 欠的 GUI 视觉证据补齐 ✓**：测试 Chrome（本轮 CDP 绑 IPv4）登录后角色扮演面板三卡从 user_root 新源全部渲染（凯尔·铁手/塞拉菲娜·月语/星野未来 true,true,true），截图 r158_userroot_cards.png——BUG-48 修复的视觉面闭环。无新产品缺陷。巡检结论：系统稳态，循环维持低频。\n"
anchor2 = "- **R159（2026-09-19 00:4x~01:3x 本地，打包第九轮 A01→A06 全通过——NSIS assisted 首验）**"
i2 = text.find(anchor2)
line_end2 = text.find("\n", i2) + 1
text = text[:line_end2] + R160 + text[line_end2:]

open(FP, "w", encoding="utf-8", newline="\n").write(text)
print("ledger updated:", len(text), "chars")
