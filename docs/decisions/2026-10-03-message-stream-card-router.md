# 2026-10-03 消息流渲染器化：卡片路由分发器 + 卡组件抽取（绞杀者第一刀）

## 背景

MessageItem 承担消息行的全部职责（880 行）：role 分支骨架、卡片路由判定
（tool 活动卡 / 插件 message_style 卡两处提前 return 内联在渲染流里）、分片
渲染、气泡形态、操作菜单。插件要上一种新消息形态只有两条路：走 message_style
写 webview 包（重），或往宿主塞分支（耦合递增）。

用户裁定（2026-10-03 连续多轮收敛）：聊天消息流应是「卡片加载渲染器」——
只负责渲染插件推送的消息卡片；骨架（气泡/编辑/操作/时间戳）是 fallback；
业务知识全部后置。**虚拟化经数据核查后撤除**：消息流为首屏 50 条 + 游标
分页按需加载，DOM 规模 = 用户实际浏览范围，"全量渲染"前提不存在；升级
触发条件 = 单管道已加载消息 > 300 条或首屏挂载 > 500ms 实测达标时引入
react-virtuoso（届时 overflow-anchor 失效需以 firstItemIndex/followOutput
接管滚动三件套）。

## 决策

**分发器 + 卡组件，绞杀者迁移，行为逐字节不变。**

1. **路由分发器**（`messageCardRouter.ts` 纯函数）：`resolveMessageCardRoute`
   返回 tool-card / style-card / null，判定条件与既有渲染顺序逐字对齐
   （tool 优先无门；style 非流式 assistant/system + 声明命中）。路由决策
   单点可测，新卡形态 = router 加 kind + 一个卡组件文件。
2. **卡组件抽取**：`ToolMessageCard`（activity 构造 + PresenterScope +
   ToolMessageBody 呈现态叙事）、`StyleMessageCard`（webview 卡容器 +
   头像 + 压缩块「查看原始」入口）各自成文件；`presenterScope.tsx` 抽出
   PresenterContext/PresenterScope 共享（MessageItem 骨架与卡组件共用）。
3. **MessageItem 瘦身为分发器调用 + fallback 骨架**：气泡/编辑器/操作菜单/
   状态卡追加/呈现作用域不动（这是"最基本的宿主职责"）。
4. **阶段 0 特征化护栏**（前一 commit）：role 骨架形态、分片基础映射正门。

## 行为等价的实证

- 阶段 0 + 既有护栏 20 文件 186 用例全程保绿，含两个被抓住的回归：
  - tool 分支真正使用的 `resolveToolStatus` 是**带未知状态警告版**
    （未知 → pending + console.warn 一次，MessageItemUnknownToolStatus
    锁定）；迁移时误写为 `?? 'completed'` 无警告版，护栏变红当即修正。
  - 唯一调用方随分支迁走后，MessageItem 内带警告版残留为死代码，一并退役
    （逻辑归卡组件，非删除行为）。

## Alternatives Considered

1. **一步重写 MessageItem 为纯分发器**：否——880 行主路径一次替换风险
   不可控，绞杀者逐类迁移护栏全程在线。
2. **虚拟化一并落地**：否——分页加载下全量渲染前提不存在（见背景数据），
   复杂度（prepend 补偿/动态测量/滚动三件套重做）为低频深翻场景买单，
   留量化触发条件。
3. **交互卡/投票迁入分发器**：否——两者已挂 ChatContainer 层
   （GlobalInteractionOverlay / ActiveVotingPanels），本就是独立组件，
   无宿主耦合可迁。
4. **卡片路由走 contributionRegistry 声明化**（消息 → 卡的映射也声明化）：
   本刀不做——现有三种卡（tool/style/state）语义异构，先收敛路由单点，
   声明化路由待插件侧出现真实多形态需求再议（避免发明无人使用的抽象）。

## 影响

- MessageItem 880 → ~750 行（路由判定与两卡 JSX 迁出）；新文件
  messageCardRouter / ToolMessageCard / StyleMessageCard / presenterScope。
- 新消息卡接入路径：卡组件文件 + router 一个 kind（+ 必要时端点/声明），
  宿主主文件零改动。
- 后续刀（未排期）：状态卡追加块、呈现作用域随插件需求评估是否入卡；
  webview 卡上行桥放行段端点后，「查看原始」入口可收进卡内。

## 归档

- 实现：messageCardRouter.ts / ToolMessageCard.tsx / StyleMessageCard.tsx /
  presenterScope.tsx / MessageItem.tsx 接线。
- 测试：messageCardRouter.test（4 用例）+ 阶段 0 两个特征化文件 +
  既有 20 文件护栏全程保绿。
