# Game Host SDK — 游戏宿主桥契约（RP 批 5）

> 读者：游戏宿主开发者——出厂 web 游戏页（`game_host.html`，第一宿主）与外部游戏
> mod（第二宿主，扩展波 A）按本文档实现**同一桥协议**。本文档自足：端点清单、载荷
> 形状、时序契约、页内桥对象签名、服务类型框架、外部宿主最小实现示例齐备，按此
> 可独立实现同协议宿主（机械对账测试执法文档与 manifest 不漂移）。
>
> 双域分治（设计文档 §二）：游戏域宿主权威——游戏状态（HP/位置/物品）以宿主为唯一
> 权威，后端只做薄 sanity（形态面）+ 审计，不否决宿主；对话域（narration/好感/剧情
> 记忆）以后端为权威。无宿主 = Tier 0 内置逻辑默认形态，完整可玩（不用游戏页 = 现状
> 零变化）。
>
> 呈现位（2026-10-14 单页化裁定）：出厂游戏页**无独立工作区 tab**——作为 RP 面板
> 玩法子页的内嵌挂载页消费（has_page 包点玩法卡直达包自包含玩法页；无页包走玩法
> 子页「工作区·玩法界面」签挂载本页），桥协议不受影响。

## 一、端点清单

全部端点在 mode_roleplay 插件 manifest `http_endpoints` 声明（`auth: user`，需
`Authorization: Bearer <token>`；本机回环 + token，A2A 同构）：

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | /ext/mode_roleplay/game/session/attach | 单宿主租约登记（新租/接管/续租三态） |
| POST | /ext/mode_roleplay/game/snapshot | 宿主快照 push（薄 sanity 后只留最新 + 审计） |
| GET | /ext/mode_roleplay/game/state | 最新快照 + 租约 + 待发 delta（取后清） |
| POST | /ext/mode_roleplay/game/turn | 玩家输入派发（服务类型框架，§五） |
| POST | /ext/mode_roleplay/game/operation | 自由操作→结构化命令（Propose→Adjudicate，§2.8） |
| POST | /ext/mode_roleplay/game/tick | 世界演化周期推进（确定性引擎结算零 LLM，§2.9） |
| POST | /ext/mode_roleplay/game/detach | 租约释放（快照保留 = 离线降级读面） |
| GET | /ext/mode_roleplay/game/config | 会话绑定卡+pack 的 Tier 1 声明聚合（v3.1 增透 ui_page/init_ui；刀6 增透 choices） |
| GET | /ext/mode_roleplay/game/page | 自包含玩法页装配载荷 {page_html}（v3.1；双通道 `?session_id=`（会话解析）/ `?pack=`（刀4 setup：会话未开按包直取）；无声明=page_html null 回落通用渲染） |
| GET | /ext/mode_roleplay/data/sessions | 活跃 RP 会话列表（会话绑定跟随数据源） |
| GET | /ext/mode_roleplay/data/messages | 会话消息流（叙事回流读面，`?pipeline_id=`） |
| GET | /ext/mode_roleplay/data/cards | 卡库（NPC 深聊入口过滤 tags 含 npc 的卡；开演设置的角色卡数据源） |
| GET | /ext/mode_roleplay/data/packs | 玩法包清点（开演设置数据源：init_ui 声明/has_page/variables_count，刀4） |

GET 端点参数走 query string；POST 端点载荷为 JSON 对象。响应统一为 JSON 对象，
错误形态 `{"error": "<可读原因>"}` 配 HTTP 400（参数/占位）/401（token）/409（租约
冲突）/502（派发失败）。

## 二、载荷形状

### 2.1 attach — `POST /game/session/attach`

```json
// 请求
{ "session_id": "<RP会话 pipeline_id>", "host_id": "<宿主id>", "game_save_id": "<可选，读档恢复>" }
// 响应（三态）
{ "status": "attached", "lease": { "host_id": "...", "game_save_id": null, "attached_at": "<ISO>" } }
{ "status": "renewed",  "lease": { ... } }                                    // 同 host 重入（幂等续租）
{ "status": "takeover", "previous_host": { ... }, "reconcile_required": true,
  "lease": { ... } }                                                          // 第二宿主=接管+对账
```

接管语义：租约换手 + 清前宿主待发 delta（新宿主从自己的存档起步）；接管方须对账后
重推快照。`session_id` / `host_id` 形态约束 `^[A-Za-z0-9_.:-]{1,128}$`（fail-closed 400）。

### 2.2 snapshot — `POST /game/snapshot`

```json
// 请求（宿主权威全量快照 push）
{ "session_id": "...", "host_id": "...", "turn_id": 3, "seq": 9, "state": { "hp": 80 } }
// 响应
{ "status": "accepted", "snapshot": { "turn_id": 3, "seq": 9, "state": {...}, "host_id": "...", "ts": "<ISO>" } }
```

薄 sanity 仅形态面（`state` 为 JSON 对象、键全为字符串、`turn_id`/`seq` 为非负整
数），内容零校验零 clamp（不否决宿主权威）；只留最新快照；审计幂等键含
`turn_id + seq`（同键重推不双记）。未 attach / 非租约持有宿主 = 409。

### 2.3 state — `GET /game/state?session_id=`

```json
// 响应（无快照 = 显式空对象非 404，宿主轮询友好；未知会话 = 全空形态 200）
{
  "session_id": "...",
  "lease": { ... } | null,
  "snapshot": { "turn_id": 3, "seq": 9, "state": {...}, ... } | {},
  "pending_deltas": [ { "key": "game.hp", "value": "+5", "turn_seq": 7 } ]
}
```

`pending_deltas` = 后端标记解析步产出的 `game.*` 变更行（`mode.game.push_delta`
入队，队列上限 200 丢最旧），**取后清**。宿主按自身规则解释 raw 值（如 `"+5"`）。

### 2.4 turn — `POST /game/turn`

```json
// 请求
{ "session_id": "...", "text": "往北走", "service": "main" }
// 响应（background 后台派发，即时回执；不等 LLM）
{ "status": "dispatched", "session_id": "...", "service": "main", "pipeline_id": "..." }
```

- `service` 缺省 `"main"`；玩家主循环输入即 main。
- 拒绝面（fail-closed）：`text` 空 = 400；`service` 非法 = 400；`npc_chat` /
  `barks` / `event` = 400 可读占位（§五）；未 attach = 409；session 非扮演会话行
  = 400；token 缺失/无效 = 401。
- 派发语义：后端薄代理注入分支（`pipeline_id` 定位既有会话管道 + `background:
  true` 后台派发）——宿主不等待 LLM；叙事经既有 `data/messages` 轮询自然回流，
  **零新回推通道**。

### 2.5 detach — `POST /game/detach`

```json
// 请求
{ "session_id": "...", "host_id": "..." }
// 响应（无租约幂等 released=false；他宿主 409 不可释他租约）
{ "status": "detached", "released": true }
```

### 2.8 operation — `POST /game/operation`（世界内嵌批次二：自由操作→结构化命令）

玩家在页内对世界直接操作（移动/使用/交互）——页上可发的操作**只来自声明清单**
（枚举声明制：清单来自会话绑定包 `operations` 声明，经 `GET /game/config` 的
`operations` 键透出；条目 `{id, label, params_schema?, effects}`）。操作进管道后
走与对话同一套校验结算（玩家也是 Agent——同一队列同一校验，支柱⑦）：

```json
// 请求（params 形态按所选操作的 params_schema；无参数操作 params 省略或 {}）
{ "session_id": "...", "operation": { "id": "meditate", "params": { "时长": 3 } } }
// 响应（Adjudicate 通过——state_delta = 结算后落账键值，可含 clamp 边界值）
{ "applied": true, "operation": "meditate", "state_delta": { "player.气血": 100 } }
// 响应（Adjudicate 拒绝——拒绝必须带原因，具体到键与原因码；不静默）
{ "applied": false, "operation": "corrupt", "reason": "效果结算未通过：npc.阿比.好感度 被拒（unknown_key/hard）" }
```

- **Propose（fail-closed 400，拒绝必须带原因）**：`operation.id` 须在会话绑定
  包 `operations` 清单（**未声明=不可发**）；`params` 按 `params_schema` 校验
  （未声明键/类型不符/min-max 越界/必填缺席 = 400 带字段定位）；请求**不携
  effects**——效果只来自包声明（玩家不能发明效果，`effects` 键域由结算面
  Validator 对 variables 声明 fail-closed）。
- **Adjudicate（回执二态，200）**：声明 effects 走 State Contract Validator
  同源结算（`state.adjudicate` 服务，与 behaviors 同通道：op 解析/类型/clamp/
  hard_rules 全套），accepted 经 character_state.update 落账；**原子结算**——
  任一效果被拒 = 整体 `applied: false` + 具体原因（无部分应用）；会话行状态
  契约（`context.rp_state_schema`）缺席 = fail-closed 回执拒绝（不透传）。
  clamp 不是拒绝（钳到声明边界后 applied，delta 为钳后值）。
- **闸门**：会话校验/单宿主租约与 turn 同闸（未 attach = 409；非扮演会话 =
  400）；**不做 cheat 旁路**（操作恒走全校验）。
- **审计**：操作行进 rp_state_audit 流水（`source=operation`，`actor=player`，
  summary 带操作 id 与校验后 params）——沉淀回路（可枚举转移聚合）自动可见。
- **传输面失败**（现值读取/结算服务/账本落账不可用）= 502 error 形态（非规则
  拒绝，可重试）。

### 2.9 tick — `POST /game/tick`（世界内嵌批次三：世界演化周期推进）

玩法包声明世界演化规则（包 yaml 顶层 `tick: {steps: [{when?, effects}]}`），宿主/
触发器**直达管道接口**推进世界状态——不经任务壳（任务状态机/评估闸门与对话轮
语义错配）；逐 tick × 逐 step **确定性引擎结算零 LLM**（去 AI 退化判据：AI 只
出现在枚举必败处）。`ticks` 正整数缺省 1（非正整数 400），上限钳制 10（防打爆，
回执回显钳后值）。触发器定时调用本端点 = **离台推进**（登记面；触发器侧接线属
触发器插件域）。

```json
// 请求
{ "session_id": "...", "ticks": 3 }
// 响应（有声明且有演化步生效）
{ "applied": true, "ticks": 3, "advanced": true,
  "state_delta": { "world.日期": 4, "world.季节": "夏" }, "steps_fired": [0, 0, 1, 0] }
// 响应（无声明包 = 空转，不 404——tick 是通用推进接口；条件步全跳过同形态）
{ "applied": true, "ticks": 3, "advanced": false }
```

- **声明面**：steps 声明序逐条判定，`when` 缺省恒真；`when` = 结构化简单条件
  `{key, op, value}`（op ∈ `==`/`!=`/`>=`/`<`，key 须在本包 variables 声明内
  fail-closed——不引入表达式语言）；effects 值 = op 串（`±`/`=`，Validator
  同源）或 enum cycle 算子 `{cycle: [非空列表]}`（枚举循环是引擎事实非叙述约定）。
- **结算语义**：每步一次 State Contract Validator 结算（`state.adjudicate`
  单源；cycle 算子由服务端编排侧解析为绝对值，Validator 契约零改动）；条件判定
  与 cycle 基线 = running（随 accepted 更新，后续步见前序结算结果）；被拒步不入
  `steps_fired`、审计照记全流水（漂移可辨不静默，不劫持其余步）；全部 tick 终了
  一次账本合并落账（键后者胜 = 序结算语义）。`steps_fired` = 步下标按开火序平铺
  （跨 tick 重复计入）。有声明但会话行无状态契约 = 400 fail-closed（无契约可结）。
- **闸门**：会话校验/单宿主租约与 turn 同闸（未 attach = 409；非扮演会话 = 400）。
- **审计**：效果行 `source=tick`、actor=narrator 缺省（世界演化），批标记行
  summary 载 ticks 数与 fired steps——沉淀回路输入自动可见。
- **消费位**：出厂游戏页世界演化区（`GET /game/config` 的 `tick` 键在场才渲染
  ⏩推进）；stardew 自包含页不接 tick UI（登记留白）；无 window.agentos 宿主的
  外部 mod 同协议直呼本端点即可。

### 2.6 config — `GET /game/config?session_id=`

会话绑定卡 + pack 的 Tier 1 声明聚合：`{session_id, card_id?, rules?,
narrative_graph?, actions?, template?, ui?}`——**键缺席式**：无声明 = 键缺席
（宿主引擎件空转 = Tier 0 兼容态）。合并序：pack 声明序后者逐键覆盖 → 卡覆盖
pack；actions 逐条带 `target` 缺省 `engine`。

RP 面板 v3.1 刀1 增透：`ui_page`（布尔，会话绑定包中存在有效 `ui.page` 声明
时在键——出厂游戏页据此把引擎渲染区整区交给包页 document 注入）与 `init_ui`
（声明序浅合并的包 `init_ui` 声明本体；刀4 起由 webview 页消费——无页包的
开演设置在出厂游戏宿主页通用声明式表单渲染，has_page 包的 setup 在包页内）。

RP 面板 v3.1 刀6 增透：`choices`（包级静态选项清单
`[{text, category?, hint?}]`，text 必填；pack 声明序后者非空清单整体覆盖，卡
侧不参与；坏条目剔除，无声明=键缺席）。点击语义 = 选项 text **原文**经
`POST /game/turn`（service=main）发入会话——下游由既有通道结算（动作清单
id/name 全等选取 / behaviors 叙述触发，与游戏页 choices「点击发送」同构）。
消费方 = 对话流 choices 卡（rp_bubble.html 尾随段，只在最新 assistant 消息
尾随一次）。

世界内嵌批次二增透：`operations`（自由操作→结构化命令声明清单，§2.8——包级
声明面同 choices 口径：pack 声明序后者非空清单整体覆盖，卡侧不参与；坏条目
整条剔除，无声明=键缺席）。条目 `{id, label, params_schema?, effects}`，页
据此渲染操作区（按钮+内联参数输入），点击经 `POST /game/operation` 发结构化
命令——**未声明 id 不可发**（枚举声明制）。

世界内嵌批次三增透：`tick`（世界演化周期推进声明 `{steps: [{when?, effects}]}`，
§2.9——包级声明面同 operations 口径：pack 声明序后者非空清单整体覆盖，卡侧
不参与；when 键域不在包 variables 声明内的步剔除，无声明=键缺席）。页据此渲染
世界演化区（⏩推进按钮+ticks 输入），点击经 `POST /game/tick` 推进世界状态。

### 2.7 page — `GET /game/page?session_id=`（刀4 增 `?pack=` 双通道）

自包含玩法页装配载荷（JSON，非 HTML 文档）：`{session_id, page_html}`。命中
规则 = 会话绑定包中**声明序第一个** `ui.page` 有效包（文件实存）；`page_html`
= 包内 page.html 经 serve-time 单源装配（rpSanitizeHtml + agentosFetch/unwrap
注入，与出厂游戏页同机制）+ 同包 `theme_css` 以 `<style>` 内联页头。**无任何
包声明有效 page = `page_html: null`（200 非 404）**——消费方据此回落通用
`/game/config` 引擎渲染。装配失败（作者页缺装配标记）= 同 null 形态 + 服务端
warning（消费方不断链）。包页会话上下文由宿主注入通道
`window.__RP_PAGE_SESSION__` 提供（出厂页 document 注入挂载前写入）。

`?pack=<id>` 通道（刀4）：会话未开（开演设置阶段）无从经 session 解析包——
按包 id 双根直取（用户层同 id 接管 → 回落包内），响应 `{pack_id, page_html}`；
包未知/无有效 page/装配失败 = 同 null 形态。包上下文注入通道
`window.__RP_PAGE_PACK__`（挂载前写入，包页 setup 阶段定位自身 init_ui 与
预选玩法包）。

## 三、时序契约

- **会话绑定**：`session_id` = RP 扮演会话的 `pipeline_id`；出厂游戏页轮询
  `data/sessions` 跟随活跃会话（用户手动选定后固定）。turn / snapshot 均要求
  绑定 + 租约在场。
- **turn_id 单调 + seq**：宿主为快照维护 `turn_id`（单调不减）与 `seq`（同轮内
  递增）；后端审计幂等键 `(session, turn_id, seq)`，重放不双记。
- **单宿主租约**：同会话至多一个宿主持租约；第二宿主 attach = 接管 + 对账要求
  （`reconcile_required: true`）；宿主离线 = 保最后快照降级（读面仍可用）。
- **自推回声防重入**：宿主 push 快照后，下一轮 `state` 轮询会读回自己的快照——
  宿主以「最近自推指纹」（出厂页为 `turn_id:seq`，即页内 `lastSelfPush`）识别回
  声，不重复触发每轮消费（graph 求值/模板渲染），闭环防重入。
- **每轮消费时机**：新 narration（`data/messages` 指纹变化）**或**新快照（非自推
  回声的 `turn_id:seq` 变化）到齐后消费一轮——无 LLM 降级轮（叙事缺席、快照更新/
  节点进入）同样消费，缺模板/缺图 = 空转（Tier 0）。
- **叙事回流**：turn 派发后不等回复；出厂页发送成功即拉一轮 + 既有轮询周期自然
  回流渲染。

## 四、rpHost 桥对象（出厂游戏页内嵌 API 面）

出厂游戏页内嵌单一桥对象 `rpHost`（页内全部数据面调用的唯一出口）。外部 mod 无
`window.agentos` 宿主注入时，同协议经 HTTPS + Bearer 直呼 §一端点集即可。

| 方法 | 签名 | 语义 |
|---|---|---|
| `request` | `request(path, params?) -> Promise` | 低层传输：经 `window.agentos.postMessage`（宿主代发 REST 带 token）；`params` 在场 = POST，缺省 = GET |
| `sessions` | `sessions() -> Promise` | GET `data/sessions` |
| `packs` | `packs() -> Promise` | GET `data/packs`（刀4 开演设置数据源） |
| `messages` | `messages(sid) -> Promise` | GET `data/messages?pipeline_id=sid` |
| `state` | `state(sid) -> Promise` | GET `game/state?session_id=sid` |
| `config` | `config(sid) -> Promise` | GET `game/config?session_id=sid` |
| `cards` | `cards() -> Promise` | GET `data/cards` |
| `attach` | `attach(sid, gameSaveId?) -> Promise` | POST `game/session/attach`（HOST_ID 自动携带） |
| `detach` | `detach(sid) -> Promise` | POST `game/detach` |
| `pushSnapshot` | `pushSnapshot(sid, turnId, seq, state) -> Promise` | POST `game/snapshot` |
| `turn` | `turn(sid, text, service?) -> Promise` | POST `game/turn`（service 缺省 `main`） |
| `operation` | `operation(sid, id, params?) -> Promise` | POST `game/operation`（§2.8；params 缺省 `{}`） |
| `tick` | `tick(sid, ticks?) -> Promise` | POST `game/tick`（§2.9；ticks 缺省 1，上限钳制以服务端为准） |

页内turn 发送前的会话绑定闸（纯函数，外部宿主可同形实现）：

- `rpBuildTurnPayload(lease, sessionId, text, service)` →
  `{ok:true, payload:{session_id, text, service:'main'}} | {ok:false, error}`——
  无租约/无会话拒绝、空文本拒绝、service 页面闸（游戏页玩家输入只走 main）。
- `rpFilterNpcCards(cards)` → `[{id, name, avatar}]`——tags 数组含 `'npc'` 过滤
  （name 缺失回落 id；非数组 = 空表），npc_chat 深聊入口列表源。
- `rpBuildOperationPayload(lease, sessionId, id, params)` →
  `{ok:true, payload:{session_id, operation:{id, params}}} | {ok:false, error}`——
  操作发送闸（§2.8，与 turn 闸同形：无租约/无会话页面同闸拒绝；id 清单匹配由
  服务端 fail-closed 执法）。
- `rpCoerceParamValue(raw, decl)` → 转形值 | null——内联输入按声明 type 尽力
  转形（int/float 数值化、bool true/false、空串 = 缺席交服务端 default/必填
  判定；页面转形是 UX 前置，服务端 params_schema fail-closed 再校验）。
- `rpBuildTickPayload(lease, sessionId, ticks)` →
  `{ok:true, payload:{session_id, ticks}} | {ok:false, error}`——tick 推进闸
  （§2.9，与 turn/operation 闸同形：无租约/无会话页面同闸拒绝；ticks 缺省 1、
  非正整数拒绝；上限钳制以服务端为准）。

传输细节（出厂页）：宿主前端以 sandbox iframe 装载页面，注入 `window.agentos
.postMessage(method, params)`——`method` 为 `/ext/...` REST 路径（路由白名单限本插
件前缀）或桥方法名（`mode.session` 等）；返回 id，结果经 `message` 事件以
`<method>.result` / `<method>.error` 回配。

### 4.1 拆卸契约（`window.__RP_PAGE_DISPOSE__`，刀10）

页面被面板同窗 document 注入挂载——容器清空只杀 DOM 不杀脚本实例，页面必须
**自报清理**：定义 `window.__RP_PAGE_DISPOSE__ = function () {...}`，清掉自己的
定时器/文档级监听器；进行中的异步回包以拆卸标记判废不写 DOM。面板挂载器
（playPageDispose）在每次换页/离场前调用并清空该槽，保证任意时刻至多一个活实
例（多实例并存的定时器交叉写入=重影根因，出厂 game_host 页为参考实现）。

## 五、服务类型（turn `service` 字段）

| service | 状态 | 消费形态 |
|---|---|---|
| `main` | **一期实开**（缺省） | 玩家主循环输入 → chat.send_message 注入分支派发进绑定会话，单 LLM 群像扮演（每轮 1 调用） |
| `npc_chat` | 一期消费（不经 turn） | **mode.session 专属会话**：按 NPC 卡开独立会话（每卡 = 独立 agent 键，开演机制复用）。出厂页 NPC 深聊面板：`data/cards` 过滤 tags 含 `npc` 的卡，点击 → `mode.session` 桥载荷 `{mode:'roleplay', agent_id:'mode_roleplay/<npc卡id>', name, title:'深聊·<名>', first_message:'（开始）'}`，会话出生 + 宿主跳转，会话内对话在宿主聊天区进行。turn 端点收到 `npc_chat` = 400 可读指引 |
| `barks` | **后置占位**（400） | 环境台词批量：宿主批量请求一次生成 N 条、宿主缓存取用。**触发条件**：出现首个真实批量台词消费方（宿主 mod 提出批量 LLM 调用需求）后，在 turn 端点扩分支实现（批量调用面 + 计量），不预开发 |
| `event` | **后置占位**（400） | 事件叙事/摘要：事件包装进主循环（现状复用）；独立事件叙事点位待宿主声明事件流消费。**触发条件**：宿主 mod 需要独立于主循环的事件叙事生成（如离线事件池补叙事）后扩分支实现 |

每类型独立 prompt 模板与计量为终态契约；一期仅 main 实开（无消费方不实现死代
码——后置池登记）。单会话单宿主租约不变：服务点位 = 同租约内请求类型。

## 六、外部宿主最小实现（同协议第二宿主）

最小端点集（四个）+ 对应时序即可接入同一后端协议：

```text
1. POST /game/session/attach     # 开机挂租约（session_id 绑定；game_save_id 读档）
2. GET  /game/state?session_id=  # 轮询：pending_deltas 取后清 + 快照对账（识别自推回声）
3. POST /game/snapshot           # 宿主权威状态 push（turn_id 单调 + seq 递增）
4. POST /game/turn               # 玩家输入派发（service=main）
```

Node 存根示例（最小同协议宿主骨架）：

```js
const BASE = 'http://127.0.0.1:<port>/ext/mode_roleplay';
const HDRS = { 'Content-Type': 'application/json', authorization: 'Bearer <token>' };

async function attach(sessionId, hostId) {
  const res = await fetch(`${BASE}/game/session/attach`, {
    method: 'POST', headers: HDRS,
    body: JSON.stringify({ session_id: sessionId, host_id: hostId }),
  });
  const out = await res.json();
  if (out.status === 'takeover') reconcile(out.previous_host);  // 对账后重推快照
  return out.lease;
}

async function pollState(sessionId, lastSelfPush) {
  const out = await (await fetch(`${BASE}/game/state?session_id=${sessionId}`, { headers: HDRS })).json();
  const snapKey = out.snapshot.turn_id + ':' + out.snapshot.seq;
  if (out.snapshot.turn_id !== undefined && snapKey !== lastSelfPush) {
    consumeTurn(out.snapshot, out.pending_deltas);  // 每轮消费（自推回声防重入）
  }
  return snapKey;  // 宿主自推后记录，识别下一轮回读
}

function pushSnapshot(sessionId, hostId, turnId, seq, state) {
  return fetch(`${BASE}/game/snapshot`, {
    method: 'POST', headers: HDRS,
    body: JSON.stringify({ session_id: sessionId, host_id: hostId, turn_id: turnId, seq: seq, state }),
  }).then((r) => r.json());
}

function turn(sessionId, text) {
  return fetch(`${BASE}/game/turn`, {
    method: 'POST', headers: HDRS,
    body: JSON.stringify({ session_id: sessionId, text, service: 'main' }),
  }).then((r) => r.json());  // {status:'dispatched'} 即回执；叙事随后经 state 轮询外的消息读面回流
}
```

行为红线：不绕会话绑定发送（未 attach = 服务端 409，客户端应同闸拒绝）；不否决
后端 delta（宿主按自身规则解释后并入权威快照）；`barks`/`event` 服务类型在端点
扩分支前调用只会得到 400 占位指引。

## 七、兼容态（Tier 0）

不使用游戏页 = 现状零变化：全部通道端点只在被调用时生效；无宿主会话 delta 无去
处（`push_delta` 如实丢弃 = Tier 0）；无声明卡 `game/config` 键缺席；游戏域行为
与批 4 前完全一致。
