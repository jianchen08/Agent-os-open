"""R153 台账更新：轮记 + BUG-45/46 + OBS-19 关闭 + OBS-22 新增。UTF-8 直写。"""
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

FP = r"D:\myproject\container_e17cc5927dfd\docs\working\packtest\测试台账.md"
text = open(FP, encoding="utf-8").read()

# 1) R153 轮记（插在 R151 行后，保持 §C 内追加惯例）
R153 = "- **R153（2026-09-18 19:0x~19:5x 本地，roleplay 开演首测全链 + edu 试玩取证 + 双缺陷立案/修复）**：①**残留审批卡清场**（上轮阻塞源：卡上「仅本次/拒绝」按钮在页即拦探索，点拒清零，pending 归零）。②**用户试玩会话取证 ✓**：用户 04:25 亲测 edu「功与能」关卡通关——5 阶段 Boss 战全胜（recon/feynman/variant/berserk/hidden_teach）、结算块完整落消息（boss_fate:已陨落/xp_total:5/rank:S/next_unlock 下一关）＝E15 修复实战生效；账本 game_ledger.json 三结算入账（xp 1065/等级4/双徽记）与 UI 一致；该局结算曾需操作员手工修 completed（DM 字段漂移），**boss_fate→陨落 解析兜底已补，R153 用真实结算文本回放 ALL PASS**（真实文本 completed=True/删值实验不误触/全角冒号变体通过）。③**roleplay 开演面板首测通到第三步**：搜索+真实点击进会话 ✓ → 面板完整渲染（角色卡/世界书/开演记录三 tab + 三张卡：凯尔·铁手/塞拉菲娜·月语/星野未来，**OBS-19 卡播种缺口已消失→关闭**）→ 点卡出详情（设定/性格/场景/三备选开场白+以此角色开演钮）✓ → 点开演派发 ✓（面板「派发中...」→消失）。④**BUG-45 立案（双断点）**：一次点击 3 条重复任务管道（504×apiClient 自动重试风暴，断点1 已修见缺陷行）+ 开场白落任务管道私有槽但线程 active_pipeline_id 不切→聊天区永远看不到开场白（断点2 核心，待修，仓内 tasks 域）。⑤**BUG-46 立案→同轮修复 ✓**：卡 system_prompt {description} 等占位符无人插值原样透传 LLM（checkpoint 实证）→ 三卡 yaml 内联真实值，新任务 checkpoint 实证真实人设注入零占位符。⑥治理观察 **OBS-22 新增**：mode_roleplay 双源（仓内 20000 vs 用户空间 5000 修前）已漂移且双清单每分钟轮流 validated；另记热更新新鲜度皱纹=timeout 修改后首次复验重注册仍挂旧值（504@11:09:50 仍 5000），后续 reload 才刷成 60000。\n"
anchor = "- **R151（2026-09-19 04:3x~05:0x，全面回归扫 ✓ 五路全通过）**"
i = text.find(anchor)
line_end = text.find("\n", i) + 1
text = text[:line_end] + R153 + text[line_end:]

# 2) BUG-45/46 行（插在 BUG-44 行后）
BUG45 = "| BUG-45 | major | 【R153 立案——断点1 已修、断点2 待修】**开演一键三派 + 开场白零可见（双断点）**。断点1（重复派发）＝play/regenerate 端点 timeout_ms=5000 < task_submit 真实耗时（本轮实测 5~27s）→ 内核 504 → 前端 apiClient 对 retryable 错误自动重试×2（client.ts:265，指数退避 1s/2s）→ 非幂等 POST 被重放＝一次点击 3 条重复角色扮演任务（10:43:11/16/26，3×LLM 成本）。已修：user-space plugin.json 两 action 路由 timeout→60000，回归=一次点击单任务（58041a09d93a）零风暴；附带皱纹记 BUG-45 行内（timeout 修改后首次复验重注册仍读旧清单，后续 reload 才生效）。断点2（核心，未解）＝任务管道把开场白写进自己管道的 message_slots（3 条齐：goal/开场白/收尾）但线程 active_pipeline_id 不切换（仍指旧聊天管道）→ sessions/{id}/messages 只读 active 单管道 → 聊天区永远看不到开场白，用户视角＝点开演无反应。修复方向（仓内 tasks 域）：task_submit 携 session_id 锚定会话时同步切 sessions.active_pipeline_id（设计语义「对话在聊天区继续」的必要条件），需联动评估消息视图单管道语义与完成后续聊路由 | **断点1 已修待复确认；断点2 待修** | 编排者直修 user-space plugin.json + .packtest/r153_kaiyan*.py | kernel.log 10:43:01/09 504×2+三管道创建 + client.ts:265 + registry.py find_http_route + checkpoint 58041a09d93a + message_slots 计数 |\n"
BUG46 = "| BUG-46 | minor | 【R153 立案→同轮关闭】**角色卡 agent system_prompt 模板占位符未插值**：card_*.yaml system_prompt 含 {description}/{personality}/{scenario}，运行时无人插值原样透传 LLM（checkpoint system_message 实证占位符在链路里）——卡 yaml 本身带真值字段，LLM 靠 goal_description 的角色段落兜底才没全瞎。修复＝三张卡 system_prompt 文本级内联真实字段值（yaml 解析复验零占位符、注释保留）；新任务 58041a09d93a checkpoint 实证 system_message 含真实人设（银发碧眼的月精灵法师…）零占位符。注：若未来卡字段要支持动态化，需在 context_build/模式包装载层做受控插值机制（通用 {key}←同配置字段），当前内联够用 | **关闭**（R153 同轮真机验证） | 编排者直修 user-space agents/card_*.yaml | .packtest/r153_fix_card_prompts.py + checkpoint 58041a09d93a system_message 取证 |\n"
anchor2 = text.find("| BUG-44 |")
# BUG-44 行尾
i2 = text.find("\n", anchor2) + 1
text = text[:i2] + BUG45 + BUG46 + text[i2:]

# 3) OBS-19 行更新为关闭
old_obs19 = "| OBS-19 | 观察 | **roleplay 活面板数据源缺口**"
i3 = text.find(old_obs19)
if i3 >= 0:
    line_end3 = text.find("\n", i3)
    new_obs19 = "| OBS-19 | 观察→**关闭（R153）** | roleplay 面板数据源缺口已消失：R153 实测面板完整渲染（角色卡/世界书/开演记录三 tab），三张出厂卡可见（凯尔·铁手/塞拉菲娜·月语/星野未来，含标签与头像），点卡出完整详情（设定/性格/场景/三备选开场白）——用户空间卡文件播种已生效（并行特性线落地）。面板骨架诚实降级定性不再需要 | 关闭 | — | packtest_r153_roleplay_panel.png + r153_card_clicked.png |"
    text = text[:i3] + new_obs19 + text[line_end3:]
    print("OBS-19 closed")
else:
    print("OBS-19 anchor NOT FOUND")

# 4) OBS-22 新增（放 OBS-21 行后）
OBS22 = "\n| OBS-22 | 观察（治理债） | **mode_roleplay 双源并存且已漂移**：仓内 plugins/shared/modes/mode_roleplay（action timeout 20000，commit 18917c422）与用户空间副本（修前 5000）同 id 双清单，内核每分钟轮流 validated 两份；运行时注册哪份取决于 watcher 时序（R153 实测用户空间副本胜出=504 timeout_ms=5000）。双真值源违反「一字段一真值」裁定精神，且打包件（仓内源）与开发环境（用户空间源）行为可能分叉。处置方向待拍板：单源化（仓内为准+用户空间播种化，或反之） | 观察中（待拍板） | — | kernel.log 11:04:46 双路径 Manifest validated + git log 18917c422 + /api/v1/schema routes timeout 对照 |\n"
anchor4 = "| OBS-21 |"
i4 = text.find(anchor4)
line_end4 = text.find("\n", i4) + 1
text = text[:line_end4] + OBS22 + text[line_end4:]

open(FP, "w", encoding="utf-8", newline="\n").write(text)
print("ledger updated:", len(text), "chars")
