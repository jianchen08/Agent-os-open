"""R154 台账更新：BUG-45 关闭 + R154 轮记 + OBS-23。UTF-8 直写。"""
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

FP = r"D:\myproject\container_e17cc5927dfd\docs\working\packtest\测试台账.md"
text = open(FP, encoding="utf-8").read()

# 1) R154 轮记（插在 R153 行后）
R154 = "- **R154（2026-09-18 21:0x~21:5x 本地，BUG-45 断点2 派修+回归关闭 ✓）**：①**断点2 修复 agent 回报 58ab83965**（kernel chat_send_handler.rs 任务出生写面：task_submit 携归属锚点建管道时同步切 sessions.active_pipeline_id；语义=创建即切/失败保持/无锚点不切；6 例新增测试先红后绿+agentos-api 917 绿+diff cov 100%；内核重建+重启 PID 64664）。②**回归三证齐 ✓**：DB 指针已切（0cb28aae91fc→78cce76c4d3f）；API sessions/{id}/messages 返回任务管道消息（goal+assistant+tool 3 条）；GUI 重载后聊天区切显任务会话（goal 首条+待办+Task Evaluate chip 截图 r154_opening_visible.png）——**开场白零可见缺陷修复成立，BUG-45 关闭**。③**两项附带观察**：(a) 开场白正文产出依赖模型遵循度——本轮 assistant 回复仅 116 字待办清单（勾了「输出开场」却无正文），R153 风暴轮 summary 则有完整开场描述＝模型方差×待办清单工作流模板挤占回复正文；建议 mode 包 owner 在 goal_description 钉「开场白全文必须先写在回复正文再调 task_evaluate」；(b) 开演后聊天区不实时推送新消息，需刷新/重进会话才见——UX 增强候选非缺陷。④**路由超时新鲜度皱纹复现+自愈**：内核重启后 13:17 三次 504 timeout_ms=5000（manifest 实为 60000），/api/v1/schema 稍后实测已 60000——boot 注册读陈旧清单值的第二现（R153 已记首现），并 OBS-23。⑤**普通聊天冒烟 ✓**（新内核 WS 协议：新会话发「R154 冒烟」→「收到」stream_end，无副作用）。⑥**未测行为补两 tab ✓**：世界书 tab（出厂演示世界三世界线：月光神殿/新九龙城/星见高中，关键词+启用+before_char 注入位全渲染）、开演记录 tab（渲染 2 条 completed 管道；未含今日 5 条开演任务管道＝模式归集口径待核，轻观察）。\n"
anchor = "- **R153（2026-09-18 19:0x~19:5x 本地，roleplay 开演首测全链"
i = text.find(anchor)
line_end = text.find("\n", i) + 1
text = text[:line_end] + R154 + text[line_end:]

# 2) BUG-45 行状态更新为关闭
old45_head = "| BUG-45 | major |"
i45 = text.find(old45_head)
if i45 >= 0:
    line_end45 = text.find("\n", i45)
    old_line = text[i45:line_end45]
    # 替换状态列与结尾（保守：整行重写，保留根因叙述骨架）
    new_line = "| BUG-45 | major | 【R153 立案→R154 断点2 修复回归通过关闭】**开演一键三派 + 开场白零可见（双断点）**。断点1（重复派发，R153 已修）＝play/regenerate 端点 timeout_ms=5000 < task_submit 真实耗时 → 内核 504 → 前端 apiClient 自动重试×2（client.ts:265）→ 非幂等 POST 重放＝一次点击 3 条任务；修=user-space plugin.json action 路由 timeout→60000，回归单任务零风暴。断点2（核心）＝任务管道把开场白写进自己管道的 message_slots 但线程 active_pipeline_id 不切换 → sessions/{id}/messages 只读 active 单管道 → 聊天区永远看不到开场白；**修复 58ab83965**（kernel chat_send_handler 任务出生写面：携归属锚点建管道即切 sessions.active_pipeline_id；语义=创建即切/失败保持/无锚点不切；6 测先红后绿+917 绿+diff cov 100%）| **关闭**（R154 三证回归：DB 指针已切+API 返回任务管道消息+GUI 重载后切显任务会话；附带观察：开场白正文产出依赖模型遵循度（待办清单模板挤占），开演结果不实时推送需刷新——均记 R154 轮记） | 修复 agent 回报 58ab83965 | packtest_r154_opening_visible.png + checkpoint/sessions DB 取证 + .packtest/r154_*.py |"
    text = text[:i45] + new_line + text[line_end45:]
    print("BUG-45 closed")
else:
    print("BUG-45 anchor NOT FOUND")

# 3) OBS-23 新增（OBS-22 行后）
OBS23 = "\n| OBS-23 | 观察 | **http 路由配置热更新/重启注册的新鲜度皱纹**：plugin.json timeout_ms 改动后，首次路由注册（watcher 复验重注册或内核重启 boot）可能仍读陈旧清单值——R153 首现（改 60000 后 11:04:37 注册仍 5000，后续 reload 自愈）、R154 二现（内核重启 13:11 注册后 13:17 三次 504 timeout_ms=5000 而 manifest=60000，稍后 /api/v1/schema 实测 60000 自愈）。功能最终收敛正确，中间窗口行为与清单不符；验证路由配置改动须以 /api/v1/schema routes 段为准（实时注册表），勿信清单文件即生效 | 观察中 | — | kernel.log 13:17:43/51+13:18:00 504×3 vs plugin.json 60000 + /api/v1/schema 复核 |\n"
anchor4 = "| OBS-22 |"
i4 = text.find(anchor4)
line_end4 = text.find("\n", i4) + 1
text = text[:line_end4] + OBS23 + text[line_end4:]

open(FP, "w", encoding="utf-8", newline="\n").write(text)
print("ledger updated:", len(text), "chars")
