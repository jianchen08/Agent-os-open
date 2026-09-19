"""R155 台账更新：A-R155 打包链行 + R155 轮记。UTF-8 直写。"""
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

FP = r"D:\myproject\container_e17cc5927dfd\docs\working\packtest\测试台账.md"
text = open(FP, encoding="utf-8").read()

# 1) A-R155 行（插在 A-R113 行后）
A_R155 = "| A-R155 | 打包链 | 全流程第八轮（A01 构建/A03 装/A04 启动/A06 冒烟/A05 卸载） | **通过** | 1 | R154 后新包全流程（期间新增：BUG-41~45 修复+58ab83965 指针切换+ZCode/WorkBuddy 主 agent 工具面+mode 包多轮）：**A01** 首试 EBUSY rmdir win-unpacked——**R141 桌面测试残留打包件进程 4 个锁目录**（新坑：构建前须清残留打包件进程，不只清目录），taskkill 后重跑 EXIT=0，安装包 118,751,697B（留存 setup_r155.exe）；**A03** 静默装三证齐+**模式包首验**（resources/plugins/shared/modes 六模式+agents 三卡在包，play timeout=仓内 20000=打包源为仓内副本实证）；**A04** 启动+内核复用模式 ✓（已知坑两现：中文 exe 名 cmd start 静默无 CDP 可连（实际会起无旗标实例）；单实例锁顶掉带 CDP 的后发实例——须先清全部残留实例再用输出重定向方式拉起）；**A06** app://bundle/login 渲染→登录→「R155 打包件冒烟」→**精确回复「包通」**（打包前端→dev 内核→MiniMax-M3，截图 r155_packaged_smoke.png）；**A05** 静默卸三证清零（文件/lnk/HKCU 注册表 0；遗留空目录壳系测试脚本 cwd 句柄占用非卸载器缺陷）。卸载器旗标再证：MSYS_NO_PATHCONV=1 下须传裸 `/S`（`//S` 禁转换后原样透传 NSIS 不认=no-op） | release/setup_r155.exe + .packtest/r155_build*.log + r155_launch2.log + r155_packaged_smoke.py + packtest_r155_packaged_smoke.png |\n"
anchor = "| A-R113 |"
i = text.find(anchor)
line_end = text.find("\n", i) + 1
text = text[:line_end] + A_R155 + text[line_end:]

# 2) R155 轮记（插在 R154 行后）
R155 = "- **R155（2026-09-18 21:3x~22:2x 本地，打包第八轮 A01→A05 全通过 + 主链复测）**：①**C02 主链复测 ✓（十一测）**：并行会话 2f42d08f0+1ce8af4aa（主 agent tool_ids 补九件桌面工具+ZCode/WorkBuddy 技能）落地后 WS 协议冒烟「收到」正常——新工具面无主链回归。②**A-R155 打包第八轮全通过**（详见 A-R155 行）：构建双镜像配方+EBUSY 新坑（残留打包件进程锁目录）+A03 模式包/卡首次在包验证+A06「包通」冒烟+A05 三证清零。**134b7a13（标题栏位置+取消置顶）与 BUG-41~45 全部修复已随本包携带**（R113 包后的修复全进包），桌面标题栏行为回归顺延至用户实机或下轮。③T2 透项（工具调用统计视图数据渲染复核）仍未闭：强制 hash `#/monitoring/tool-calls` 只落壳、webview 零帧（iframe 未挂载，须监控页内 tab 激活路径），下轮以页内导航补测。④WS 冒烟配方增补：随机 thread_id 被「拒绝注册非本租户 thread 映射」拒，须先 API 建会话再对该 thread 派发。\n"
anchor2 = "- **R154（2026-09-18 21:0x~21:5x 本地，BUG-45 断点2 派修+回归关闭 ✓）**"
i2 = text.find(anchor2)
line_end2 = text.find("\n", i2) + 1
text = text[:line_end2] + R155 + text[line_end2:]

open(FP, "w", encoding="utf-8", newline="\n").write(text)
print("ledger updated:", len(text), "chars")
