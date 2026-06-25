# 前端 Playwright E2E 测试代码审查报告

- **审查目标**: `frontend/e2e/` 目录下的 Playwright E2E 测试代码
- **审查类型**: 前端 E2E 测试代码法定审查
- **创建时间**: 2026-06-18
- **审查范围**: login.spec.ts、chat.spec.ts、task-board.spec.ts、utils/test-helpers.ts、utils/mock-server.ts、helpers/auth.ts、helpers/navigation.ts、helpers/assertions.ts（共 8 个文件）

---

## 二、物理保险检查结果（自动拦截，违反即失败）

| # | 检查项 | 结果 | 证据 |
|---|--------|------|------|
| 1 | 模块边界物理化 | ✅ 通过 | helpers/ 和 utils/ 模块分离清晰，spec 文件仅通过公开导出函数引用 |
| 2 | 架构约束测试 | ✅ 通过 | 依赖方向：spec → helpers + utils，无循环依赖 |
| 3 | 需求覆盖扫描 | ✅ 通过 | 三个 spec 文件头部均标注 features.md 场景来源（login→场景7, chat→场景1, task-board→场景2） |
| 4 | 安全与风格Lint | ❌ 失败 | 6 处 `any` 类型绕过（test-helpers.ts:147/154/161/173/187/231）、1 个 .bak 死文件（chat.spec.ts.bak）、3 处硬编码 localhost URL |
| 5 | 冗余模式检测 | ❌ 失败 | helpers/auth.ts 与 utils/test-helpers.ts 登录逻辑重复（auth.ts:59-157 vs test-helpers.ts:56-99），消息发送重复（auth.ts:162-169 vs test-helpers.ts:246-259） |

**物理保险结论**: 第 4、5 项未通过 → 审查结论直接判定为 **Request Changes**。

---

## 三、静态扫描指标

> 环境：当前无 Node.js/npm，指标基于 ripgrep 搜索 + 人工逐行验证。

| 指标 | 精确值 | 涉及文件和行号 | 评级 |
|------|--------|---------------|------|
| `any` 类型使用 | **6 处** | test-helpers.ts:147(`as any`), 154(`any[]`), 161(`as any`), 173(`as any`), 187(`as any`), 231(`as any`) | 差 |
| `.catch(()=>false/{}/null)` 静默吞错 | **7 处**（task-board）+ **3 处**（chat）+ **2 处**（assertions）= **12 处** | task-board: 65/78/101/105/116/125/127; chat: 141/195/209; assertions: 47/72 | 差 |
| `waitForTimeout` 硬编码 sleep（目标3文件） | **3 处** | chat.spec.ts:103(`5_000`); test-helpers.ts:215(`500`), 308(`timeout 参数`) | 一般 |
| 脆弱选择器（text=/hasText/CSS）（task-board 全文） | **21 处** | task-board: 27/32/38/51/54/58/64/71/77/93/100/115/124/140/150/162/169/175/220/229/236 | 差 |
| 硬编码 localhost URL | **3 处** | mock-server.ts:256, auth.ts:14, auth.ts:16 | 差 |
| .bak 死文件 | **1 个** | chat.spec.ts.bak（8.1KB，内容与 chat.spec.ts 完全重复） | 差 |
| 重复登录逻辑 | **2 套** | auth.ts:59-157（API注入登录4函数） vs test-helpers.ts:56-99（UI级登录1函数） | 差 |
| 重复消息发送 | **2 套** | auth.ts:162-169(`sendMessage`) vs test-helpers.ts:246-259(`sendChatMessage`) | 差 |

---

## 四、需求追溯审查结果

### 追踪映射

| 测试文件 | 声称覆盖的需求（features.md） | 实际覆盖验证 | 判定 |
|----------|------------------------------|-------------|------|
| login.spec.ts | 场景 7（认证全链路） | ✅ 6 个测试用例：登录表单展示(L21-48)、账号密码登录跳转(L50-66)、Token 持久化(L68-87)、刷新保持认证(L89-130)、空用户名验证(L132-148)、错误密码提示(L150-166) | 合格 |
| chat.spec.ts | 场景 1（对话流程） | ✅ 5 个测试用例：流式响应渲染(L27-48)、WS 事件序列(L50-77)、多消息不串台(L79-114)、工具调用卡片(L116-160)、审批交互弹窗(L162-221) | 合格 |
| task-board.spec.ts | 场景 2（任务全流程） | ✅ 5 个测试用例：任务列表展示(L18-41)、表格结构验证(L43-83)、状态过滤(L85-130)、任务详情/工作空间(L132-210)、空状态(L212-241) | 合格 |

### 无需求代码标记

| 文件 | 位置 | 问题 | 判定 |
|------|------|------|------|
| chat.spec.ts.bak | 第 1-223 行（整个文件） | 与 chat.spec.ts 内容完全重复的备份残留文件，无任何需求依据 | **Must Fix** — 死文件需删除 |

---

## 五、架构边界四问审查结果

### 散点检查

| 业务概念 | 出现位置 | 详细证据 | 判定 |
|----------|---------|----------|------|
| 登录逻辑 | auth.ts L59-157（`loginViaAPI`+`injectTokensAndReload`+`loginAndWaitReady`+`login`）+ test-helpers.ts L56-99（`loginViaUI`） | 两套独立实现：auth.ts 走 API→token 注入→reload；test-helpers.ts 走 UI 表单 fill→click。login.spec.ts 同时引用两套（`loginViaUI` from test-helpers + `API_BASE/APP_URL/TEST_USER` from auth） | **Must Fix** |
| 消息发送 | auth.ts L162-169（`sendMessage`）+ test-helpers.ts L246-259（`sendChatMessage`） | auth.ts 版本仅 fill+click；test-helpers.ts 版本含 click→fill→toHaveValue 断言→click。功能重叠 | **Must Fix** |
| API_BASE/APP_URL 常量 | auth.ts L14-16 定义 + mock-server.ts L256 硬编码重定义 + comprehensive-e2e.spec.ts L15-16 重定义 + message-render-e2e.spec.ts L19-20 重定义 | 同一常量在 4+ 文件中各自定义 | **Must Fix** |

### 分叉点检查

| 位置 | 问题 | 判定 |
|------|------|------|
| task-board.spec.ts L65/78/101/116/125 | 调用方需要判断 `.isVisible().catch(() => false)` 来决定是否继续操作，分叉逻辑散落在测试用例中 | **Should Fix** — 应封装为辅助函数 |
| chat.spec.ts L132-145 | 工具卡片检测遍历 3 个选择器数组，调用方需处理多选择器 fallback | 合格（封装在工具函数内） |

### 信息泄漏检查

| 位置 | 问题 | 判定 |
|------|------|------|
| test-helpers.ts L108-115 | 直接访问 localStorage 键名 `access_token`/`refresh_token`/`access_token_expiry`/`auth_user` | 合格（E2E 测试验证前端行为，属可接受范围） |

### 变化方向检查

| 位置 | 问题 | 判定 |
|------|------|------|
| navigation.ts L12-44 | `ROUTES` 常量与 `frontend/src/constants/routes.ts` 手动同步，注释已标注但无自动化机制 | 合格（注释已标注同步来源） |

---

## 六、冗余与质量审查结果

| 检查项 | 结果 | 详细证据 |
|--------|------|----------|
| 翻译式注释 | ✅ 通过 | 注释解释"为什么"（如 test-helpers.ts L140 "必须在页面加载后、WS 连接建立前调用"） |
| 无效错误处理 | ❌ 失败 | task-board.spec.ts L105/127: `.catch(() => {})` 完全吞断言；assertions.ts L47-49: `.catch(() => { console.log(...) })` 仅日志；assertions.ts L72-74: `catch { return null; }` 吞错误 |
| 死代码 | ❌ 失败 | chat.spec.ts.bak（8.1KB 完整重复文件）；auth.ts L162-169 `sendMessage`（被 test-helpers.ts `sendChatMessage` 替代，无人调用） |
| 风格不一致 | ❌ 失败 | login.spec.ts 100% 使用 data-testid；task-board.spec.ts 21 处使用 text=/hasText/CSS 选择器，零 data-testid |

---

## 七、Must Fix 问题清单（含四要素）

### MF-1: 删除 chat.spec.ts.bak 死文件

| 要素 | 内容 |
|------|------|
| **问题描述** | `chat.spec.ts.bak` 是 `chat.spec.ts` 的完整备份残留文件（8.1KB），内容与正本完全一致，无任何需求依据。Playwright 配置 `testMatch: '**/*.spec.ts'` 不会匹配 `.bak` 文件，但该文件占用目录空间、造成混淆。 |
| **代码定位** | `frontend/e2e/chat.spec.ts.bak` 第 1-223 行（整个文件） |
| **修复方案** | 删除该文件：`rm frontend/e2e/chat.spec.ts.bak`。同时将 `*.bak` 添加到 `.gitignore` 防止未来再次产生。 |
| **验收标准** | 文件系统中不存在 `chat.spec.ts.bak`；`.gitignore` 包含 `*.bak` 条目。 |

### MF-2: 统一登录逻辑，消除散点

| 要素 | 内容 |
|------|------|
| **问题描述** | 登录逻辑在两个文件中重复实现。`helpers/auth.ts` L59-157 提供 4 个 API 级函数（`loginViaAPI`→`injectTokensAndReload`→`loginAndWaitReady`/`login`），`utils/test-helpers.ts` L56-99 提供 UI 级函数（`loginViaUI`）。两者各自注册用户、各自管理 token 流程，职责边界模糊。 |
| **代码定位** | auth.ts: `loginViaAPI`(L59-74), `injectTokensAndReload`(L79-113), `loginAndWaitReady`(L121-146), `login`(L153-157); test-helpers.ts: `loginViaUI`(L56-99) |
| **修复方案** | 明确职责划分：①auth.ts 负责配置常量（API_BASE/APP_URL/TEST_USER）+ API 级快速登录（`login`/`loginAndWaitReady`，用于非登录场景的测试准备）；②test-helpers.ts 复用 auth.ts 的常量（`import { API_BASE, APP_URL, TEST_USER } from '../helpers/auth'`），仅保留 UI 级登录封装（`loginViaUI`，用于验证登录流程本身）。③test-helpers.ts 中 `loginViaUI` 内部的注册逻辑（L62-69）改为调用 auth.ts 的 `registerUser`。 |
| **验收标准** | 登录逻辑仅在 helpers/auth.ts 中定义配置常量和注册函数；test-helpers.ts 通过 import 复用；无重复的 API_BASE/APP_URL 定义。 |

### MF-3: 删除 auth.ts 中重复的 sendMessage 函数

| 要素 | 内容 |
|------|------|
| **问题描述** | `helpers/auth.ts` L162-169 的 `sendMessage` 函数与 `utils/test-helpers.ts` L246-259 的 `sendChatMessage` 功能完全重叠（都定位输入框→fill→点击发送），且 `sendMessage` 功能更弱（无 toHaveValue 断言）。 |
| **代码定位** | auth.ts L162-169（`sendMessage`）；test-helpers.ts L246-259（`sendChatMessage`） |
| **修复方案** | 删除 auth.ts 中的 `sendMessage` 函数。如有调用方引用，改为 `import { sendChatMessage } from '../utils/test-helpers'`。 |
| **验收标准** | auth.ts 中不存在 `sendMessage` 函数；全局搜索确认无对 auth.ts `sendMessage` 的导入引用。 |

### MF-4: 移除 task-board.spec.ts 静默吞断言

| 要素 | 内容 |
|------|------|
| **问题描述** | task-board.spec.ts 中 7 处使用 `.catch(() => false)` 或 `.catch(() => {})` 静默吞掉错误/断言失败。特别是 L105 和 L127 的 `await expect(...).not.toBeVisible({...}).catch(() => {})`，断言失败被完全忽略，测试不会因加载状态异常而失败，使测试失去验证价值。 |
| **代码定位** | L65: `.catch(() => false)`, L78: `.catch(() => false)`, L101: `.catch(() => false)`, L105: `.catch(() => {})`, L116: `.catch(() => false)`, L125: `.catch(() => false)`, L127: `.catch(() => {})` |
| **修复方案** | ①L105/127（断言吞错）：移除 `.catch(() => {})`，让断言正常抛出。②L65/78/101/116/125（isVisible 条件判断）：这些是条件分支而非断言，可接受 `.catch(() => false)` 但建议改为 `await locator.isVisible().catch(() => false)` 的明确条件判断模式，或使用 Playwright 的 `locator.count()` 替代。 |
| **验收标准** | task-board.spec.ts 中不存在 `.catch(() => {})`（空 catch）；L105/127 的 expect 断言无 catch 包裹。 |

### MF-5: 消除 test-helpers.ts 中的 any 类型使用

| 要素 | 内容 |
|------|------|
| **问题描述** | test-helpers.ts 中 6 处使用 `any` 类型：5 处 `(window as any)` 绕过 TypeScript 类型检查访问自定义属性 `__e2e_ws_events`，1 处 `constructor(...args: any[])` 使用 any 参数。违反 TypeScript 严格模式 `noImplicitAny` 要求。 |
| **代码定位** | L147: `(window as any).__e2e_ws_events = []`, L154: `constructor(...args: any[])`, L161: `(window as any).__e2e_ws_events.push({...})`, L173: `(window as any).WebSocket = ProxiedWebSocket`, L187: `return (window as any).__e2e_ws_events || []`, L231: `(window as any).__e2e_ws_events = []` |
| **修复方案** | ①定义全局类型声明（在 test-helpers.ts 顶部或独立 d.ts 文件）：`declare global { interface Window { __e2e_ws_events?: WSEvent[] } }`，然后将所有 `(window as any)` 改为 `window`。②L154 的 `any[]` 改为 `constructor(...args: unknown[])` 并使用 `super(...args as any[])` 或定义具体的 WebSocket 构造参数类型。 |
| **验收标准** | test-helpers.ts 中 grep `as any` 返回 0 条结果；grep `any[` 返回 0 条结果（构造函数参数除外，如有注释说明原因）。 |

### MF-6: 明确模块职责边界，消除隐式耦合

| 要素 | 内容 |
|------|------|
| **问题描述** | helpers/auth.ts 同时包含认证逻辑（登录/注册/token）和聊天操作（`sendMessage`），职责不单一。test-helpers.ts 依赖 auth.ts 的常量但额外实现了自己的登录流程（`loginViaUI`），两个模块的职责边界模糊，存在隐式耦合。 |
| **代码定位** | auth.ts: 认证函数(L35-184) + sendMessage(L162-169) 混合；test-helpers.ts: 登录(L56-99) + WS 收集(L132-233) + 聊天(L235-290) + 断言(L319-367) 混合 |
| **修复方案** | ①auth.ts 仅保留认证相关：`API_BASE`/`APP_URL`/`TEST_USER` 常量 + `registerUser`/`loginViaAPI`/`injectTokensAndReload`/`login`/`loginAndWaitReady`/`logout` 函数；删除 `sendMessage`。②test-helpers.ts 职责为"测试操作封装"：UI 级登录(`loginViaUI`) + WS 事件收集 + 聊天消息操作 + 事件断言工具。③assertions.ts 职责为"通用 DOM 断言"。 |
| **验收标准** | auth.ts 中不包含聊天相关函数；每个模块的导出函数围绕同一职责域；grep 确认无跨模块职责混入。 |

---

## 八、Should Fix 问题清单

| # | 文件:行号 | 问题 | 修复建议 |
|---|----------|------|----------|
| SF-1 | chat.spec.ts:103 | `await page.waitForTimeout(5_000)` 硬编码 sleep 等待第二条消息 | 替换为 `await waitForAssistantReply(page, 60_000)` |
| SF-2 | chat.spec.ts:176-182 | 审批测试在 `!interactionEvent` 时直接 `return` 并 pass，未真正验证审批功能 | 使用 `test.skip()` 或确保测试环境配置了需要审批的 Agent |
| SF-3 | chat.spec.ts:79-114 | 多消息测试仅验证 `secondRoundMessages >= firstRoundMessages`，未验证消息内容不串台 | 增加消息内容断言 |
| SF-4 | task-board.spec.ts:212-241 | 空状态测试不确定：有数据和无数据都 pass | 拆分为两个独立测试，使用测试数据准备确保特定状态 |
| SF-5 | task-board.spec.ts 全文 | 21 处脆弱选择器（text=/hasText/CSS），零 data-testid | 为任务看板页面添加 data-testid |
| SF-6 | test-helpers.ts:201-220 | `waitForWSEvent` 使用 while+waitForTimeout(500) 手动轮询 | 改为 `page.waitForFunction` |
| SF-7 | test-helpers.ts:299-317 | `verifyStreamingResponse` 使用 waitForTimeout 等待文本增长 | 使用 `page.waitForFunction` 检测长度变化 |
| SF-8 | mock-server.ts:256 | 硬编码 `'http://localhost:5188'` | 改为 `import { APP_URL } from '../helpers/auth'` |
| SF-9 | assertions.ts:47-49 | `waitForToolCompleted` catch 仅日志 | 至少记录错误：`catch (e) { console.warn('...', e) }` |
| SF-10 | assertions.ts:72-74 | `waitForInteractionCard` catch 返回 null 吞错误 | 记录错误后返回 null |
| SF-11 | chat.spec.ts:141/195/209 | `.catch(() => false)` 条件判断模式散落在测试中 | 封装为辅助函数 |
| SF-12 | helpers/auth.ts:121-146 | `loginAndWaitReady` 使用 API 注入而非 UI 操作，与真实用户操作模拟不一致 | 在注释中明确标注"API注入模式（非真实UI操作）"，或重命名为 `loginViaAPIAndInject` |
| SF-13 | navigation.ts:96 | `await page.waitForTimeout(500)` 硬编码等待标签切换 | 使用 `page.waitForLoadState` 或 `expect` 断言 |

---

## 九、细节清单核对结果（40 项）

### 维度零-A：需求追溯审查

| # | 检查项 | ✅/❌ | 说明 |
|---|--------|------|------|
| 1 | 每行新增代码有需求依据 | ✅ | 三个 spec 文件均有 features.md 场景映射 |
| 2 | 无需求代码已标记 | ❌ | chat.spec.ts.bak 是无需求死文件（MF-1） |
| 3 | 无防御性冗余逻辑 | ✅ | 未发现无依据的兼容/扩展代码 |

### 维度零-B：架构边界四问

| # | 检查项 | ✅/❌ | 说明 |
|---|--------|------|------|
| 4 | 散点检查 | ❌ | 登录逻辑和消息发送在两文件重复（MF-2/MF-3） |
| 5 | 分叉点检查 | ✅ | 调用方分叉判断已封装在工具函数内 |
| 6 | 信息泄漏检查 | ✅ | E2E 测试访问 localStorage 属可接受范围 |
| 7 | 变化方向检查 | ✅ | ROUTES 常量已注释标注同步来源 |

### 维度零-C：冗余与质量审查

| # | 检查项 | ✅/❌ | 说明 |
|---|--------|------|------|
| 8 | 翻译式注释 | ✅ | 注释解释"为什么" |
| 9 | 无效错误处理 | ❌ | task-board 7 处 + assertions 2 处静默吞错（MF-4） |
| 10 | 死代码 | ❌ | chat.spec.ts.bak + auth.ts sendMessage（MF-1/MF-3） |
| 11 | 风格不一致 | ❌ | login 用 data-testid，task-board 用 CSS 选择器（SF-5） |

### 维度一：功能完整性

| # | 检查项 | ✅/❌ | 说明 |
|---|--------|------|------|
| 12 | 需求功能点全覆盖 | ✅ | 对话/认证/任务看板场景均已覆盖 |
| 13 | 正常路径和失败路径 | ✅ | login 有错误密码/空用户名测试 |
| 14 | 边界条件处理 | ❌ | chat 审批测试过度容错（SF-2） |

### 维度二：状态覆盖

| # | 检查项 | ✅/❌ | 说明 |
|---|--------|------|------|
| 15 | loading 状态有反馈 | ✅ | task-board 有 `text=加载中...` 等待 |
| 16 | error 状态有处理 | ❌ | 静默 catch 掩盖了 error 状态（MF-4） |
| 17 | empty 状态有展示 | ❌ | 空状态测试不确定（SF-4） |

### 维度三：交互完整性

| # | 检查项 | ✅/❌ | 说明 |
|---|--------|------|------|
| 18 | 防重复提交 | ✅ | sendChatMessage 有 toHaveValue 断言验证 |
| 19 | 破坏性操作确认 | ✅ | 审批交互有弹窗确认验证 |

### 维度四：文案与提示

| # | 检查项 | ✅/❌ | 说明 |
|---|--------|------|------|
| 20 | 错误提示清晰 | ✅ | 所有 expect 有描述性第二参数 |
| 21 | 加载文案描述操作 | ✅ | `加载中...` 提示存在 |

### 维度五：可访问性

| # | 检查项 | ✅/❌ | 说明 |
|---|--------|------|------|
| 22 | 表单标签关联 | ✅ | login 使用 data-testid 定位，隐含 label 关联 |
| 23 | 非纯颜色传达信息 | ❌ | task-board L115 `span.rounded-full` 依赖样式类名 |

### 维度六：安全与健壮性

| # | 检查项 | ✅/❌ | 说明 |
|---|--------|------|------|
| 24 | 资源清理 | ❌ | WS 拦截器替换了全局 WebSocket 但无恢复机制（test-helpers.ts:173） |
| 25 | XSS 风险检查 | ✅ | 测试代码不涉及用户输入渲染 |
| 26 | 输入验证 | ❌ | mock-server mock 数据未验证 schema 一致性 |

### 维度七：选择器健壮性

| # | 检查项 | ✅/❌ | 说明 |
|---|--------|------|------|
| 27 | login 使用 data-testid | ✅ | login.spec.ts 100% data-testid（login-page/login-form/login-username-input 等） |
| 28 | chat 使用 data-testid | ✅ | chat-input-textarea/chat-send-button/assistant-message |
| 29 | task-board 使用 data-testid | ❌ | 21 处脆弱选择器（SF-5） |

### 维度八：等待策略

| # | 检查项 | ✅/❌ | 说明 |
|---|--------|------|------|
| 30 | login 等待策略 | ✅ | waitForLoadState/expect().toBeVisible/waitForURL，无 sleep |
| 31 | chat 等待策略 | ❌ | L103 硬编码 waitForTimeout(5_000)（SF-1） |
| 32 | WS 事件等待策略 | ❌ | while+waitForTimeout(500) 手动轮询（SF-6） |

### 维度九：真实用户操作模拟

| # | 检查项 | ✅/❌ | 说明 |
|---|--------|------|------|
| 33 | login UI 操作模拟 | ✅ | loginViaUI 使用 fill/click 完整表单交互 |
| 34 | chat 消息发送模拟 | ✅ | sendChatMessage: click→fill→toHaveValue→click |
| 35 | auth.ts 快速登录模式 | ❌ | loginAndWaitReady 使用 API 注入而非 UI 操作（SF-12） |

### 维度十：TypeScript 类型安全

| # | 检查项 | ✅/❌ | 说明 |
|---|--------|------|------|
| 36 | 无 any 类型 | ❌ | test-helpers.ts 6 处 any（MF-5） |
| 37 | 类型注解完整 | ✅ | 所有公开函数有参数和返回值类型注解 |

### 维度十一：接口与内聚

| # | 检查项 | ✅/❌ | 说明 |
|---|--------|------|------|
| 38 | 模块职责单一 | ❌ | auth.ts 混合认证+聊天操作（MF-6） |
| 39 | 无隐式耦合 | ❌ | test-helpers.ts 与 auth.ts 职责重叠（MF-2） |
| 40 | 公共接口清晰 | ✅ | 所有导出函数有 JSDoc |

### 汇总

| 统计 | 值 |
|------|------|
| 总项数 | 40 |
| 通过 | 21 |
| 未通过 | 19 |
| **通过率** | **52.5%** |

**通过率 52.5% < 80%** → 审查结论为 **Request Changes**。

---

## 十、问题统计摘要

| 级别 | 数量 | 编号 |
|------|------|------|
| **Must Fix** | 6 | MF-1 ~ MF-6 |
| **Should Fix** | 13 | SF-1 ~ SF-13 |
| **Nit** | 2 | C-2（test-helpers.ts evaluate 代码块较长）、N-1（命名不一致） |
| **合计** | 21 | |

---

## 十一、审查结论

### **Request Changes**

**理由**：物理保险第 4/5 项未通过（6 处 any 类型、重复登录/消息发送逻辑），细节清单通过率 52.5% < 80%。

**优势**：
- login.spec.ts 是 E2E 测试优秀范例：100% data-testid、正确等待策略、完整 UI 表单交互
- chat.spec.ts WS 事件拦截器设计巧妙，真实用户操作模拟到位
- 所有文件注释质量高，有明确的需求追溯标注

**关键短板**：
- task-board.spec.ts 是最薄弱环节：21 处脆弱选择器、7 处静默吞断言、不确定的空状态测试
- test-helpers.ts 6 处 any 类型绕过 TypeScript 类型安全
- helpers/auth.ts 与 utils/test-helpers.ts 架构散点（重复登录/消息发送逻辑）

**通过条件**：修复全部 6 个 Must Fix 项后重新审查。
