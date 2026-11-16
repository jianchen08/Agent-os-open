# PipelineManager · 任务管理面板（组件族）

> 位置：前端 schema 组件族下的「任务管理面板」渲染面。
> 本文档是面板**用户契约的唯一在仓真值**——契约条目与落地状态同表维护，
> 需求新增/变更先改本表，再改实现与测试。

## 一、需求（用户契约清单与落地状态）

> 落地状态口径：`✅ 已落地` = 主仓代码+测试双面在位；`❌ 未落地` = 缺失。
> 「来源」列给用户实报/裁定出处；「实现载体」列给主仓代码或测试的落点。

| # | 契约条目 | 来源 | 落地状态 | 实现载体 |
|---|---------|------|---------|---------|
| 1 | 面板缺省只看运行中（无活跃会话时） | 用户裁定 2026-09-21 | ✅ 已落地 | `PipelineManagerWidget.tsx` 状态筛选缺省 |
| 2 | 主管道锚点重锚：兄弟子任务不互挂假父子，窄筛视图补渲染主管道锚点行 | 用户实报 2026-09-24 | ✅ 已落地 | `PipelineManagerTreeParts.tsx` `resolveThreadTop`/`ensureAnchorNode` |
| 3 | 交互收敛：列表视图退役恒树视图；「当前会话」按钮退役（会话筛选=用户选择的会话） | 用户裁定 2026-10-10 | ✅ 已落地 | `PipelineManagerWidget.tsx` 树视图单渲染面 |
| 4 | 平铺筛选项改标签形态（胶囊+描边，非按钮组） | 用户复核 2026-10-10 第二轮 | ✅ 已落地 | `PipelineManagerFilterBar.tsx` |
| 5 | 筛选第三轮：去「全部」项改可叠加标签组；状态组「运行中」独占档互斥反选；会话筛选激活时不默认运行中 | 用户裁定 2026-10-17 | ✅ 已落地 | `PipelineManagerFilterBar.tsx` 筛选模型 |
| 6 | 行 hover 铺底改主题语义 muted 面（禁饱和紫铺底） | R9 巡检 top 场景 | ✅ 已落地 | `PipelineManagerTreeParts.tsx` 行 className |
| 7 | 停止原因（run_stop_reason）人话对照透出 | 失败归因传播 | ✅ 已落地 | `PipelineManagerTreeParts.tsx` 详情面板「停止原因」行 |
| 8 | 会话记录缺失时行点按不静默（重建会话条目打开 / 真孤儿显式反馈） | 用户实报 | ✅ 已落地 | `PipelineManagerWidget.tsx` 打开链 |
| 9 | 主管道身份按字段本身判定（提交者管道不算任务、显示会话标题而非裸管道 ID） | 用户裁定 | ✅ 已落地 | `plugins/shared/tasks/events.py` 判据（提交者侧记账键排除） |
| 10 | **条目行布局契约**：标题独占信息区弹性位（超长省略号+悬停给全文）；agent 只显 stem 短名（全名进 tooltip）；整行点按切换详情展开/收起；详情按钮带箭头+aria-expanded 提示展开态；行级「只看此会话」入口下线（挤占操作区并把其后按钮挤出视野）；操作按钮区独立于信息区且不压缩 | 用户实报 2026-10-23 | ✅ 已落地 | `PipelineManagerTreeParts.tsx` `EntryRow`；契约测试 `PipelineManagerWidget.rowLayout.test.tsx` |
| 11 | **信息去重契约**（同类信息只留一种表达形态）：独立状态图标下线只留状态文字标签；任务原始状态文字下线（进详情面板承载）；模式徽标纯文字单形态且模式 ID 只显短名 | 用户实报 2026-10-25 | ✅ 已落地 | `PipelineManagerTreeParts.tsx` `EntryRow`；`ModePanelBadge.tsx` |
| 12 | **信息架构分层契约**：分清共有特征与模式特有特征并分层放置——共有特征区（标题→状态→运行时长→token，位置恒定可扫读对比）与模式/执行者次要区（模式标记+agent 短名+循环体阶段）并列且次要区在后，模式信息不插入共有区内部 | 用户裁定 2026-10-26 | ✅ 已落地 | `PipelineManagerTreeParts.tsx` `EntryRow` 双层信息区 |
| 13 | 会话维度驱动源：入口下线后由活跃会话驱动（切换会话即跟随） | 契约 10 的连带 | ✅ 已落地 | `PipelineManagerWidget.tsx` 会话筛选效应 |

> **落地史（供追溯）**：契约 10/11/12 的初版实现曾只存在于任务分支（未合入主仓），
> 主仓长期缺该三族契约的用户可见效果；2026-10-27 按用户需求真值补齐并落测试契约载体。

## 二、逻辑

### 2.1 条目行的信息分层

条目行由三块构成，位置与职责固定：

1. **类型标识位**（任务/会话/项目徽标）：行首固定位，不参与弹性压缩。
2. **信息区**（`pipeline-entry-meta`，可压缩自裁剪）：内含两块并列——
   - **共有特征区**（`pipeline-entry-common`）：标题 → 状态 → 运行时长 → token，
     顺序恒定，跨任务同位（可扫读、可对比）。
   - **模式/执行者次要区**（`pipeline-entry-mode`）：模式标记 + agent 短名 +
     循环体阶段；仅该模式/执行者存在时渲染，差异只在区内容，不改行布局。
3. **操作按钮区**（`pipeline-entry-actions`，独立于信息区且不压缩）：详情展开、
   打开对话、暂停/恢复/取消、复制 ID、打开工作空间等。

### 2.2 单一表达形态原则

同一类信息只留一种可见形态，其余承载面（tooltip / 详情面板）保留完整信息：
状态只留文字标签；任务原始状态只在详情面板「任务状态」行；模式徽标纯文字、
只显短名（全路径进 tooltip）；agent 只显 stem 短名（全名进 tooltip）。

### 2.3 点按语义分工

- 整行点按 = 切换详情展开/收起（不触发打开对话，避免误触）。
- 打开对话 = 行内操作区「打开对话」按钮（显式意图）。
- 树子级展开 = 行首 chevron（独立于详情，详情不受树展开影响）。

### 2.4 短名解析

`mode_xxx/yyy` 形态的 id 行内只显末段短名，前缀与全名进 tooltip；
解析收敛为单一来源（`pipelineShortLabel.ts` 的 `stemLabel`），条目行与模式徽标共用。

## 三、结构

### 3.1 文件清单

> 创建时间取 git 首次入库日（`git log --diff-filter=A`），非估计值。

| 文件 | 用途 | 创建时间 |
|------|------|---------|
| `PipelineManagerWidget.tsx` | 面板主组件：数据装配（runs/states/任务/会话/项目）、筛选模型（状态/类型/关键词/会话）、详情与树展开状态、打开链与工作空间动作 | 2026-08-16 |
| `PipelineManagerTreeParts.tsx` | 树族渲染部件（PipelineTree/TreeGroup/TreeChildren/EntryRow/EntryDetail）：条目行信息分层与详情面板 | 2026-09-23 |
| `PipelineManagerFilterBar.tsx` | 筛选工具条：状态/类型可叠加标签组、关键词搜索、会话筛选 chip | 2026-10-09 |
| `PipelineManagerEntryOpen.ts` | 打开链纯逻辑（条目→会话/子标签解析与导航参数） | 2026-10-14 |
| `ModePanelBadge.tsx` | 模式徽标与模式标签集渲染（纯文字单形态，模式 ID 只显短名） | 2026-09-15 |
| `pipelineStatusVisuals.tsx` | 管道运行状态展示（文案/颜色/终态集） | 2026-09-16 |
| `pipelineShortLabel.ts` | 短名解析（末段）：条目行 agent 名与模式 ID 共用的单一来源 | 2026-10-27 |
| `__tests__/PipelineManagerWidget.rowLayout.test.tsx` | 条目行布局/信息去重/信息分层三族用户契约测试 | 2026-10-27 |
| `__tests__/pmTestUtils.tsx` | 面板测试共享夹具（播种容器 + mock 工厂 + 公共渲染序列） | 2026-09-24 |

### 3.2 测试面

| 文件 | 覆盖 |
|------|------|
| `__tests__/PipelineManagerWidget.rowLayout.test.tsx` | 契约 10/11/12（布局/去重/分层） |
| `__tests__/PipelineManagerWidget.test.tsx` | 条目渲染、状态映射、详情面板、筛选叠加、会话维度 |
| `__tests__/PipelineManagerWidget.gaps.test.tsx` | 行内操作全量、打开对话分流、详情面板边界、空态、删除弹窗 |
| `__tests__/PipelineManagerWidget.fourState.test.tsx` | 四态统一渲染面 |
| `__tests__/PipelineManagerWidget.ghostTerminal.test.tsx` | 终态幽灵行对账 |
| `__tests__/PipelineManagerWidget.bug21.test.tsx` | 未知状态任务不被丢弃 |
| `__tests__/PipelineManagerWidget.focus.test.tsx` | 通知定位跳转与高亮 |
| `__tests__/ModePanelBadge.test.tsx` | 模式徽标渲染形态与面板页导航 |
| `__tests__/PipelineManagerEntryOpen.test.ts` | 打开链纯逻辑 |
