"""R159 台账更新：A-R159 行 + R159 轮记。UTF-8 直写。"""
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

FP = r"D:\myproject\container_e17cc5927dfd\docs\working\packtest\测试台账.md"
text = open(FP, encoding="utf-8").read()

# 1) A-R159 行（插在 A-R158 之前——即 A-R157 行后；找 A-R157 行尾）
A_R159 = "| A-R159 | 打包链 | 全流程第九轮（A01 构建/A03 装/A04 启动/A06 冒烟/A05 卸载） | **通过** | 1 | **NSIS assisted 新模板首验（93af307b3）全通过**：A01 EXIT=0（118,720,459B，setup_r159.exe 留存；构建前清残留进程配方执行，一次成）；**A03 静默 /S 在 assisted（oneClick:false+allowToChangeInstallationDirectory）下不受影响**——安装目录仍 %LOCALAPPDATA%\\Programs\\agent-os（appid 优先于 productName 命名，R155 同址；实录勘误：ADR 默认目录表述「灵汐助手」与实测 agent-os 不符但契约要点 per-user/HKCU 不变），三证齐+resources 四大件+六模式包在包；**A04** ASCII 副本+CDP 9226 启动 ✓ 复用 dev 内核；**A06** 登录→「R159 打包件冒烟」→**精确回复「包通」**（截图 r159_packaged_smoke.png）；**A05** 三证清零（本轮无空目录壳残留）| release/setup_r159.exe + .packtest/r159_build.log + packtest_r159_packaged_smoke.png |\n"
anchor = "| A-R157 |"
i = text.find(anchor)
line_end = text.find("\n", i) + 1
text = text[:line_end] + A_R159 + text[line_end:]

# 2) R159 轮记（插在 R158 行后）
R159 = "- **R159（2026-09-19 00:4x~01:3x 本地，打包第九轮 A01→A06 全通过——NSIS assisted 首验）**：①**A-R159 全通过**（详见行）：93af307b3 的 assisted 安装器（oneClick:false+可选目录）**静默链不受影响**，A05 卸载三证干净清零；默认安装目录实测仍 agent-os（ADR 默认目录表述与实测不符已记行内勘误，契约要点不变）。②GUI 视觉证据通道部分恢复：测试 Chrome CDP 本轮绑定 **IPv4**（[::1] 空、127.0.0.1 通——与既往相反，配方双栈都试）；登录后主界面截图落盘，但 roleplay 面板帧未能在新登录工作区直接激活（无会话上下文），卡片视觉复核仍以 R153/R154 截图+本轮 API 读数为准。③R158 透项状态：BUG-48 环境侧已修；产品侧迁移工具缺口（APPDATA→user_root 路径）转 owner 未动。④R158 提到的 d184d15b8（FE 通知确认递归栈溢出修复）等 FE 改动随本包携带，dev 侧冒烟间接覆盖。\n"
anchor2 = "- **R158（2026-09-19 00:5x~01:5x 本地，user_root 迁移缺口发现+环境修复 ✓）**"
i2 = text.find(anchor2)
line_end2 = text.find("\n", i2) + 1
text = text[:line_end2] + R159 + text[line_end2:]

open(FP, "w", encoding="utf-8", newline="\n").write(text)
print("ledger updated:", len(text), "chars")
