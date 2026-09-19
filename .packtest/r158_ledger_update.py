"""R158 台账更新：BUG-48 行 + R158 轮记。UTF-8 直写。"""
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

FP = r"D:\myproject\container_e17cc5927dfd\docs\working\packtest\测试台账.md"
text = open(FP, encoding="utf-8").read()

# 1) BUG-48 行（插在 BUG-47 行后）
BUG48 = "| BUG-48 | major | 【R158 发现→同轮环境修复；产品侧工具缺口转 owner】**dev 用户空间钉入项目（ed81704e7）迁移不完整——六个用户项目插件丢失+卡修复回退+账本孤儿**。user_root/plugins 种子不含 %APPDATA% 旧用户空间的全部插件：缺 **mode_learning（edu）/mode_livestream/godot_mcp/omnisearch/stream_control/video_gen**（运行内核插件面 93，六者全无）；角色卡被仓内占位符版覆盖（**BUG-46 修复回退**，user_root 卡 yaml 复现 {description}）；edu 账本 game_ledger.json（用户 S 评通关数据 xp 1065/3 结算）遗留在旧位置成孤儿。根因=migrate_to_user_root.py 只管仓内旧物（DB/data/.env/dirty config），**没有 %APPDATA%→user_root 的插件迁移路径**；种子副本系陈旧快照。**修复（编排者数据迁移，非产品代码）**：robocopy 六插件目录（排除 \_\_pycache\_\_/.venv）→user_root/plugins，%APPDATA% 修复版三卡覆盖回 user_root，账本随 mode_learning/data 到位；补 user_root/plugins/_host junction（PowerShell New-Item Junction→repo plugins/shared/_host；旧布局同款，缺它 light 组宿主探测失败=插件端点 502）。**验证**：内核热发现后插件面 93→99（六者全 PRESENT）；cards 端点 200 三卡齐；账本 xp1065/3 结算在新路径可读。**产品侧转 owner**：①迁移工具应补 %APPDATA%→user_root 路径（含插件+插件目录内数据）；②种子须以迁移时刻的 %APPDATA% 现状为准而非陈旧快照 | **关闭**（R156 式环境修复+当轮验证） | 编排者直修 user_root 数据 | .zctmp/plug2.json（99 插件含六者）+ cards 端点 200 三卡 + user_root 卡零占位符 + game_ledger.json 新路径可读 |\n"
anchor = text.find("| BUG-47 |")
line_end = text.find("\n", anchor) + 1
text = text[:line_end] + BUG48 + text[line_end:]

# 2) R158 轮记（插在 R157 行后）
R158 = "- **R158（2026-09-19 00:5x~01:5x 本地，user_root 迁移缺口发现+环境修复 ✓）**：①并行会话两个大改动落地后的定向回归：**ed81704e7（dev 用户空间钉入项目 user_root/）+ 93af307b3（NSIS 改 assisted 可选目录，下轮打包 A03/A05 按新行为验）**。②**发现 BUG-48（major）**：user_root 迁移不完整——六用户项目插件丢失（mode_learning/mode_livestream/godot_mcp/omnisearch/stream_control/video_gen，插件面 93 缺六）、角色卡回退占位符版（BUG-46 修复被仓内旧副本覆盖）、edu 账本孤儿。③**同轮环境修复 ✓**：robocopy 六插件（排 .venv/__pycache__）+修复版三卡覆盖+账本随迁+补 \_host junction（缺它 light 组 502）→ 内核热发现 93→99、cards 端点 200 三卡、账本新路径可读。④GUI 截图通道本轮劣化（CDP connect 反复超时，Chrome 重启后仍不稳）——本轮回归证据以 API/DB 级为准（cards/账本/插件面均为权威读数），GUI 视觉复核下轮顺带。⑤注：R157 五路扫通过不代表全绿——该扫不含六丢失插件的任何路径（盲区实证），后续大迁移类改动需插件面对照清单（93→99 基线入账）。\n"
anchor2 = "- **R157（2026-09-18 23:5x~00:3x 本地，受控重启应用 BUG-47 索引层 ✓ + 五路全扫 ✓）**"
i2 = text.find(anchor2)
line_end2 = text.find("\n", i2) + 1
text = text[:line_end2] + R158 + text[line_end2:]

open(FP, "w", encoding="utf-8", newline="\n").write(text)
print("ledger updated:", len(text), "chars")
