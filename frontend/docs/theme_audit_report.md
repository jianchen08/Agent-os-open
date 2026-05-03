# 前端组件主题控制审计报告

> **审计日期**: 2026-05-01
> **审计范围**: 10 个核心前端组件
> **主题系统**: Tailwind CSS 语义化 class + CSS 变量双层主题架构
> **参考文件**: `types/theme.ts` · `config/themes/presets/dark.ts` · `stores/themeStore.ts` · `services/themeService.ts`

---

## 1. 审计摘要

### 1.1 总体主题控制覆盖率

| 指标 | 数值 |
|------|------|
| 审计组件总数 | 10 |
| 组件代码总行数 | 3,581 |
| 样式控制点总数 | ~143 |
| 主题感知样式点（tailwind-semantic + theme-variable） | ~110 |
| 硬编码样式点（tailwind-fixed + inline-style 硬编码） | ~33 |
| **主题控制覆盖率** | **≈ 77%** |
| 完全合规组件（0 硬编码） | 3 / 10 |
| 需改进组件 | 7 / 10 |

### 1.2 控制方式分布

| 控制方式 | 数量 | 占比 | 说明 |
|----------|------|------|------|
| tailwind-semantic | ~85 | 59.4% | `bg-background`, `text-foreground`, `border-border` 等 |
| theme-variable | ~25 | 17.5% | `var(--bubble-user-bg)`, `var(--accent-running)` 等 |
| tailwind-fixed | ~20 | 14.0% | `bg-blue-400`, `text-red-600` 等固定 Tailwind 色值 |
| inline-style | ~13 | 9.1% | 行内 `style={}` 写入，部分为硬编码、部分为 CSS 变量 |
| css-hardcoded | 0 | 0% | 无独立 CSS 文件中的硬编码色值 |

### 1.3 各组件硬编码统计

| 组件 | 硬编码数 | 严重程度 | 说明 |
|------|----------|----------|------|
| MessageItem | 12 | 🔴 高 | 工具状态徽章、系统头像、Agent 标签等多处固定色值 |
| ChatInput | 3 | 🟡 中 | Token 用量进度条固定红/黄/绿色 |
| ActivityCard | 5 | 🟡 中 | 错误区固定红色、hover 固定黑白透明色 |
| FiveSpaceLayout | 4 | 🟡 中 | 待交互提示、进度条、指示器颜色 |
| ThemePanel | 5 | 🟡 中 | 预览色块回退默认值、面板阴影 |
| TopNav | 3 | 🟢 低 | 移动端遮罩透明黑、用户菜单最小宽度 |
| Sidebar | 2 | 🟢 低 | 移动端遮罩、固定像素宽度 |
| ChatContainer | 0 | ✅ 无 | 完全使用语义化 class |
| LoginPage | 0 | ✅ 无 | 完全使用语义化 class |
| MessageList | 0 | ✅ 无 | 完全使用语义化 class |

### 1.4 改进优先级总览

| 优先级 | 改进项 | 预估工作量 |
|--------|--------|------------|
| P0-紧急 | MessageItem 工具状态徽章改用主题变量 | 2h |
| P1-高 | ChatInput Token 进度条改用主题变量 | 1h |
| P1-高 | ActivityCard 错误区改用主题变量 | 1.5h |
| P2-中 | FiveSpaceLayout 指示器颜色统一 | 1h |
| P2-中 | ThemePanel 预览色回退值提取为主题常量 | 1h |
| P3-低 | TopNav/Sidebar 遮罩色提取为 CSS 变量 | 0.5h |

---

## 2. 逐组件审计表

### 2.1 FiveSpaceLayout

**文件**: `frontend/src/components/layout/FiveSpaceLayout.tsx` (381 行)
**角色**: 五空间主布局容器，整合 Chat/Workspace/Floating/Dock/Fullscreen
**主题集成**: 使用 `useThemeStore` 读取当前主题，通过 `safeLoadLayout` 解析布局配置

| 样式效果 | 控制方式 | 具体代码位置 | 改进建议 |
|----------|----------|-------------|----------|
| 主容器背景/文字 | tailwind-semantic | L260: `bg-background text-foreground` | ✅ 合规 |
| 全局字体 | theme-variable | L262: `style={{ fontFamily: 'var(--font-family)' }}` | ✅ 合规 |
| 顶栏边框 | tailwind-semantic | L265: `border-border` | ✅ 合规 |
| 侧边栏切换按钮 hover | tailwind-semantic | L270: `hover:bg-accent` | ✅ 合规 |
| 待交互提示背景/文字 | tailwind-fixed | L288: `bg-orange-500/10 text-orange-400` | ❌ 改为 `bg-status-waiting/10 text-status-waiting` |
| Dock 栏执行指示器颜色 | inline-style | L148: `indicatorColor: '#f59e0b'` | ❌ 改为 `indicatorColor: 'var(--accent-waiting)'` |
| 执行进度条轨道 | tailwind-semantic | L341: `bg-muted` | ✅ 合规 |
| 执行进度条填充 | tailwind-fixed | L343: `bg-blue-400` | ❌ 改为 `bg-status-running` |
| 面板边框 | tailwind-semantic | L293/298/306/318: `border-border` | ✅ 合规 |
| 占位内容文字 | tailwind-semantic | L213/218: `text-muted-foreground` | ✅ 合规 |
| 布局切换按钮文字 | tailwind-semantic | L292: `text-muted-foreground` | ✅ 合规 |
| Chat 面板宽度 | theme-variable | L296: `style={{ width: resolved.chatPanel.width }}` | ✅ 布局配置驱动 |
| Dock 栏高度 | theme-variable | L326: `style={{ height: resolved.dockBar.height }}` | ✅ 布局配置驱动 |

### 2.2 ChatContainer

**文件**: `frontend/src/components/chat/ChatContainer.tsx` (153 行)
**角色**: 聊天容器，整合消息列表 + 输入区域

| 样式效果 | 控制方式 | 具体代码位置 | 改进建议 |
|----------|----------|-------------|----------|
| 加载状态图标 | tailwind-semantic | L67: `text-primary` | ✅ 合规 |
| 加载状态文字 | tailwind-semantic | L68: `text-muted-foreground` | ✅ 合规 |
| 搜索栏背景 | tailwind-semantic | L80: `bg-background` | ✅ 合规 |
| 搜索图标颜色 | tailwind-semantic | L84: `text-muted-foreground` | ✅ 合规 |
| 搜索结果计数 | tailwind-semantic | L105: `text-muted-foreground` | ✅ 合规 |

> **结论**: ✅ 完全合规，无硬编码。

### 2.3 LoginPage

**文件**: `frontend/src/pages/auth/LoginPage.tsx` (222 行)
**角色**: 用户登录页面

| 样式效果 | 控制方式 | 具体代码位置 | 改进建议 |
|----------|----------|-------------|----------|
| 页面背景 | tailwind-semantic | L131: `bg-background` | ✅ 合规 |
| 标题文字 | tailwind-semantic | L134: `text-foreground` | ✅ 合规 |
| 副标题文字 | tailwind-semantic | L135: `text-muted-foreground` | ✅ 合规 |
| 全局错误提示背景/文字 | tailwind-semantic | L140: `bg-destructive/10 text-destructive` | ✅ 合规 |
| 必填标记 | tailwind-semantic | L148/164: `text-destructive` | ✅ 合规 |
| 输入框错误边框 | tailwind-semantic | L152/168: `border-destructive` | ✅ 合规 |
| 错误文字 | tailwind-semantic | L158/174: `text-destructive` | ✅ 合规 |
| 注册链接 | tailwind-semantic | L188: `text-primary` | ✅ 合规 |
| 辅助文字 | tailwind-semantic | L185: `text-muted-foreground` | ✅ 合规 |

> **结论**: ✅ 完全合规，无硬编码。

### 2.4 ChatInput

**文件**: `frontend/src/components/chat/ChatInput.tsx` (741 行)
**角色**: 统一聊天输入组件，支持文件上传/语音/思考模式

| 样式效果 | 控制方式 | 具体代码位置 | 改进建议 |
|----------|----------|-------------|----------|
| 错误附件背景/边框 | tailwind-semantic | L153: `bg-destructive/10 border-destructive/50` | ✅ 合规 |
| 正常附件背景/边框 | tailwind-semantic | L154: `bg-muted/50 border-border/30` | ✅ 合规 |
| 预览图标容器 | tailwind-semantic | L166: `bg-background/80` | ✅ 合规 |
| 文件图标颜色 | tailwind-semantic | L169/171: `text-muted-foreground` | ✅ 合规 |
| 上传状态 spinner | tailwind-semantic | L181: `text-primary` | ✅ 合规 |
| 错误图标 | tailwind-semantic | L182: `text-destructive` | ✅ 合规 |
| 删除按钮 hover | tailwind-semantic | L187: `hover:bg-destructive/10 hover:text-destructive` | ✅ 合规 |
| 上传错误提示 | tailwind-semantic | L533: `text-destructive bg-destructive/10` | ✅ 合规 |
| 输入框容器背景 | tailwind-semantic | L545: `bg-background/80 border-border/50` | ✅ 合规 |
| 输入框焦点环 | tailwind-semantic | L547: `focus-within:ring-ring/50 focus-within:border-primary/50` | ✅ 合规 |
| 拖拽高亮环 | tailwind-semantic | L523: `ring-primary ring-2` | ✅ 合规 |
| 文本输入框文字 | tailwind-semantic | L578: `text-foreground placeholder:text-muted-foreground/40` | ✅ 合规 |
| 附件按钮 | tailwind-semantic | L589: `text-muted-foreground hover:text-foreground hover:bg-muted` | ✅ 合规 |
| 模型信息区背景 | tailwind-semantic | L607: `bg-primary/10 border-primary/20` | ✅ 合规 |
| 模型图标/文字 | tailwind-semantic | L608/609: `text-primary` | ✅ 合规 |
| **Token 进度条-红色** | **tailwind-fixed** | **L627: `bg-red-500`** | ❌ 改为 `bg-status-error` |
| **Token 进度条-黄色** | **tailwind-fixed** | **L628: `bg-amber-500`** | ❌ 改为 `bg-status-waiting` |
| **Token 进度条-绿色** | **tailwind-fixed** | **L629: `bg-emerald-500`** | ❌ 改为 `bg-status-success` |
| 停止按钮 | tailwind-semantic | L639: `variant="destructive"` | ✅ 合规 |
| 发送按钮-可发送 | tailwind-semantic | L648: `bg-primary hover:bg-primary/90` | ✅ 合规 |
| 发送按钮-不可发送 | tailwind-semantic | L649: `bg-muted text-muted-foreground` | ✅ 合规 |

### 2.5 MessageItem

**文件**: `frontend/src/components/chat/MessageItem.tsx` (405 行)
**角色**: 单条消息渲染，支持 user/assistant/tool/system 四种角色

| 样式效果 | 控制方式 | 具体代码位置 | 改进建议 |
|----------|----------|-------------|----------|
| 消息 hover 背景 | tailwind-semantic | L223/268: `hover:bg-muted/30` | ✅ 合规 |
| 工具名称文字 | tailwind-semantic | L228: `text-muted-foreground` | ✅ 合规 |
| 用户头像背景/文字 | tailwind-semantic | L280: `bg-primary text-primary-foreground` | ✅ 合规 |
| Bot 头像背景/文字 | tailwind-semantic | L282: `bg-secondary text-secondary-foreground` | ✅ 合规 |
| 消息气泡-编辑模式 | theme-variable | L291-300: `var(--bubble-user-bg/ai-bg/user-text/ai-text/...)` | ✅ 合规 |
| 消息气泡-正常模式 | theme-variable | L324-334: `var(--bubble-user-bg/ai-bg/user-text/ai-text/...)` | ✅ 合规 |
| 模型名文字 | tailwind-semantic | L310: `text-muted-foreground` | ✅ 合规 |
| 时间戳文字 | tailwind-semantic | L350: `text-muted-foreground` | ✅ 合规 |
| 消息操作栏 | tailwind-semantic | L356: `opacity-0 group-hover:opacity-100` | ✅ 合规 |
| **工具状态-已完成徽章** | **tailwind-fixed** | **L234: `bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400`** | ❌ 改为 `bg-status-success/10 text-status-success` |
| **工具状态-失败徽章** | **tailwind-fixed** | **L236: `bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400`** | ❌ 改为 `bg-status-error/10 text-status-error` |
| **工具状态-执行中徽章** | **tailwind-fixed** | **L238: `bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400`** | ❌ 改为 `bg-status-running/10 text-status-running` |
| **工具状态-默认徽章** | **tailwind-fixed** | **L240: `bg-gray-100 text-gray-700 dark:bg-gray-900/30 dark:text-gray-400`** | ❌ 改为 `bg-status-pending/10 text-status-pending` |
| **工具错误文字** | **tailwind-fixed** | **L248: `text-red-600 dark:text-red-400`** | ❌ 改为 `text-destructive` |
| **系统消息头像** | **tailwind-fixed** | **L282: `bg-amber-100 text-amber-600 dark:bg-amber-900/30 dark:text-amber-400`** | ❌ 改为 `bg-status-waiting/20 text-status-waiting` |
| **系统消息左侧边框** | **tailwind-fixed** | **L321: `border-amber-400`** | ❌ 改为 `border-status-waiting` |
| **等待响应图标** | **tailwind-fixed** | **L338: `text-blue-500`** | ❌ 改为 `text-status-running` |
| **等待响应文字** | **tailwind-fixed** | **L339: `text-blue-600 dark:text-blue-400`** | ❌ 改为 `text-status-running` |
| **Agent 标签背景/文字** | **tailwind-fixed** | **L354: `bg-blue-50 text-blue-700 dark:bg-blue-900/20 dark:text-blue-400`** | ❌ 改为 `bg-primary/10 text-primary` |

### 2.6 MessageList

**文件**: `frontend/src/components/chat/MessageList.tsx` (282 行)
**角色**: 消息列表，集成 Virtuoso 虚拟滚动

| 样式效果 | 控制方式 | 具体代码位置 | 改进建议 |
|----------|----------|-------------|----------|
| 加载中 spinner/文字 | tailwind-semantic | L177: `text-muted-foreground` | ✅ 合规 |
| 加载更多提示 | tailwind-semantic | L180: `text-muted-foreground` | ✅ 合规 |
| 空状态文字 | tailwind-semantic | L200: `text-muted-foreground` | ✅ 合规 |
| AI 思考中头像背景 | tailwind-semantic | L209: `bg-primary/10` | ✅ 合规 |
| AI 思考中 spinner | tailwind-semantic | L210: `text-primary` | ✅ 合规 |
| AI 思考中气泡 | tailwind-semantic | L212: `bg-secondary/50` | ✅ 合规 |
| AI 思考中文字 | tailwind-semantic | L213: `text-muted-foreground` | ✅ 合规 |

> **结论**: ✅ 完全合规，无硬编码。

### 2.7 Sidebar

**文件**: `frontend/src/components/layout/Sidebar.tsx` (442 行)
**角色**: 侧边栏，显示会话列表和搜索

| 样式效果 | 控制方式 | 具体代码位置 | 改进建议 |
|----------|----------|-------------|----------|
| 侧边栏背景 | theme-variable | L292: `bg-[var(--sidebar-bg-light)] dark:bg-[var(--sidebar-bg-dark)]` | ✅ 合规 |
| 侧边栏边框 | tailwind-semantic | L289: `border-border/50` | ✅ 合规 |
| 标题文字 | tailwind-semantic | L298: `text-foreground` | ✅ 合规 |
| 头部分割线 | tailwind-semantic | L307: `border-border` | ✅ 合规 |
| 搜索区分割线 | tailwind-semantic | L339: `border-border/50` | ✅ 合规 |
| 加载/空状态文字 | tailwind-semantic | L352/354/361: `text-muted-foreground` | ✅ 合规 |
| **移动端遮罩层** | **tailwind-fixed** | **L279: `bg-black/50`** | ⚠️ 可提取为 `--overlay-bg` 变量 |
| 侧边栏宽度 | inline-style | L294-300: `width: '280px'/'200px'` | ⚠️ 已在 SIDEBAR_STYLES 常量管理，可改为 CSS 变量 |

### 2.8 TopNav

**文件**: `frontend/src/components/layout/TopNav.tsx` (441 行)
**角色**: 顶部导航栏，含系统标题、导航菜单、主题按钮、用户菜单

| 样式效果 | 控制方式 | 具体代码位置 | 改进建议 |
|----------|----------|-------------|----------|
| 导航栏背景/边框 | tailwind-semantic | L164: `bg-background/95 border-border/50` | ✅ 合规 |
| 导航栏高度 | theme-variable | L166: `style={{ height: 'var(--topnav-height, 40px)' }}` | ✅ 合规 |
| 导航栏间距 | theme-variable | L171: `style={{ gap: 'var(--spacing-3, 12px)' }}` | ✅ 合规 |
| 系统标题文字 | tailwind-semantic | L180: `text-foreground` | ✅ 合规 |
| 导航项-非激活 | tailwind-semantic | L193: `text-muted-foreground hover:text-foreground` | ✅ 合规 |
| 按钮通用 hover | tailwind-semantic | L173/193/237/252: `hover:bg-muted/80` | ✅ 合规 |
| 移动端菜单激活 | tailwind-semantic | L229: `bg-primary/20 text-primary` | ✅ 合规 |
| 用户头像背景/文字 | theme-variable | L258-260: `hsl(var(--primary))` / `hsl(var(--primary-foreground))` | ✅ 合规 |
| 下拉菜单背景 | theme-variable | L280: `hsl(var(--popover))` | ✅ 合规 |
| 菜单项高度/间距 | theme-variable | L296-300: `var(--user-menu-item-height)`, `var(--user-menu-item-padding-x)` | ✅ 合规 |
| 菜单边框 | tailwind-semantic | L278/286: `border-border/50` | ✅ 合规 |
| 用户名/邮箱文字 | tailwind-semantic | L289/290: `text-popover-foreground` / `text-muted-foreground` | ✅ 合规 |
| 登出按钮文字 | tailwind-semantic | L295: `text-destructive hover:bg-destructive/10` | ✅ 合规 |
| **移动端导航遮罩** | **tailwind-fixed** | **L319: `bg-black/30`** | ⚠️ 可提取为 `--overlay-bg-light` |
| **菜单外部关闭遮罩** | **inline-style** | **L334: `className="fixed inset-0 z-40"`** (无背景色) | ✅ 透明遮罩，无需改进 |
| 下拉菜单最小宽度 | inline-style | L276: `minWidth: '200px'` | ⚠️ 可提取为 CSS 变量 |

### 2.9 ThemePanel

**文件**: `frontend/src/components/layout/ThemePanel.tsx` (186 行)
**角色**: 主题选择面板，展示所有可用主题的预览卡片

| 样式效果 | 控制方式 | 具体代码位置 | 改进建议 |
|----------|----------|-------------|----------|
| 卡片边框/选中态 | tailwind-semantic | L102-103: `border-primary/50`, `border-primary ring-primary/20 ring-2` | ✅ 合规 |
| 未选中卡片边框 | tailwind-semantic | L103: `border-border/50` | ✅ 合规 |
| 选中勾选图标 | tailwind-semantic | L120: `text-primary` | ✅ 合规 |
| 分区标题 | tailwind-semantic | L141/154: `text-muted-foreground` | ✅ 合规 |
| 设置按钮文字 | tailwind-semantic | L170: `text-muted-foreground hover:text-foreground` | ✅ 合规 |
| 设置按钮 hover | tailwind-semantic | L171: `hover:bg-muted/50` | ✅ 合规 |
| 面板背景 | theme-variable | L161: `var(--modal-bg, hsl(var(--card)))` | ✅ 合规（带回退值） |
| **预览色块-浅色默认** | **inline-style** | **L39: `bg: '#f8fafc', primary: '#2563eb', text: '#0f172a'`** | ⚠️ 预览回退色，建议提取为常量 |
| **预览色块-深色默认** | **inline-style** | **L41: `bg: '#0f172a', primary: '#3b82f6', text: '#f8fafc'`** | ⚠️ 预览回退色，建议提取为常量 |
| 预览色块背景 | inline-style | L131: `style={{ backgroundColor: colors.bg }}` | ✅ 动态取自主题数据 |
| 预览色块圆点 | inline-style | L135: `style={{ backgroundColor: colors.primary }}` | ✅ 动态取自主题数据 |
| **面板阴影** | **inline-style** | **L163: `boxShadow: '0 20px 40px -12px rgba(0, 0, 0, 0.3)'`** | ❌ 改为 `var(--shadow-modal, ...)` CSS 变量 |

### 2.10 ActivityCard

**文件**: `frontend/src/components/chat/ActivityCard.tsx` (428 行)
**角色**: 统一活动卡片组件，渲染工具调用、任务阶段等活动

| 样式效果 | 控制方式 | 具体代码位置 | 改进建议 |
|----------|----------|-------------|----------|
| 状态色系统 | theme-variable | L30-82: `var(--accent-waiting/running/success/error/pending, #fallback)` | ✅ 合规（带 fallback） |
| 状态背景色 | theme-variable | L38: `color-mix(in srgb, var(--accent-*) 8%, transparent)` | ✅ 合规 |
| 卡片容器样式 | theme-variable | L266-270: `borderColor: themeVars.border`, `backgroundColor: themeVars.bg` | ✅ 合规 |
| 进度条颜色 | theme-variable | L300: `backgroundColor: themeVars.color` | ✅ 合规 |
| 标题文字 | tailwind-semantic | L279: `text-foreground` | ✅ 合规 |
| 时长文字 | tailwind-semantic | L283: `text-muted-foreground/70` | ✅ 合规 |
| 进度轨道 | tailwind-semantic | L294: `bg-muted/50` | ✅ 合规 |
| 展开详情区 | tailwind-semantic | L324: `bg-muted/5` | ✅ 合规 |
| 代码块背景 | tailwind-semantic | L330: `bg-muted/30` | ✅ 合规 |
| 确认弹窗背景 | tailwind-semantic | L399: `bg-background border-border` | ✅ 合规 |
| 确认按钮 | tailwind-semantic | L415: `bg-primary text-primary-foreground hover:bg-primary/90` | ✅ 合规 |
| 默认操作按钮 | tailwind-semantic | L393: `bg-primary/10 text-primary hover:bg-primary/20` | ✅ 合规 |
| Ghost 操作按钮 | tailwind-semantic | L385: `hover:bg-muted/70 text-muted-foreground hover:text-foreground` | ✅ 合规 |
| Outline 操作按钮 | tailwind-semantic | L387: `border-border hover:bg-muted/70 text-muted-foreground` | ✅ 合规 |
| **头部 hover-亮色** | **tailwind-fixed** | **L288: `hover:bg-black/[0.03]`** | ⚠️ 可改为 `hover:bg-foreground/[0.03]` |
| **头部 hover-暗色** | **tailwind-fixed** | **L289: `dark:hover:bg-white/[0.04]`** | ⚠️ 可改为 `dark:hover:bg-foreground/[0.04]` |
| **错误标签文字** | **tailwind-fixed** | **L367: `text-red-500 dark:text-red-400`** | ❌ 改为 `text-destructive` |
| **错误内容背景/文字** | **tailwind-fixed** | **L368: `bg-red-50/50 text-red-600 dark:bg-red-900/10 dark:text-red-400`** | ❌ 改为 `bg-destructive/10 text-destructive` |
| **破坏性操作按钮** | **tailwind-fixed** | **L379: `bg-red-100 text-red-700 ... dark:bg-red-900/30 dark:text-red-400`** | ❌ 改为 `bg-destructive/10 text-destructive hover:bg-destructive/20` |
| 确认弹窗遮罩 | tailwind-fixed | L401: `bg-black/50` | ⚠️ 可提取为 `--overlay-bg` 变量 |

---

## 3. 硬编码样式清单

### 3.1 硬编码色值

| # | 文件 | 行号 | 硬编码值 | 用途 | 严重程度 |
|---|------|------|----------|------|----------|
| 1 | FiveSpaceLayout.tsx | L148 | `'#f59e0b'` | 执行状态指示器颜色 | 🟡 中 |
| 2 | FiveSpaceLayout.tsx | L288 | `bg-orange-500/10`, `text-orange-400` | 待交互提示背景/文字 | 🟡 中 |
| 3 | FiveSpaceLayout.tsx | L343 | `bg-blue-400` | 执行进度条填充色 | 🟡 中 |
| 4 | ChatInput.tsx | L627 | `bg-red-500` | Token 使用率 ≥90% 进度条 | 🟡 中 |
| 5 | ChatInput.tsx | L628 | `bg-amber-500` | Token 使用率 ≥70% 进度条 | 🟡 中 |
| 6 | ChatInput.tsx | L629 | `bg-emerald-500` | Token 使用率 <70% 进度条 | 🟡 中 |
| 7 | MessageItem.tsx | L234 | `bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400` | 工具状态-已完成徽章 | 🔴 高 |
| 8 | MessageItem.tsx | L236 | `bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400` | 工具状态-失败徽章 | 🔴 高 |
| 9 | MessageItem.tsx | L238 | `bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400` | 工具状态-执行中徽章 | 🔴 高 |
| 10 | MessageItem.tsx | L240 | `bg-gray-100 text-gray-700 dark:bg-gray-900/30 dark:text-gray-400` | 工具状态-默认徽章 | 🔴 高 |
| 11 | MessageItem.tsx | L248 | `text-red-600 dark:text-red-400` | 工具错误文字 | 🔴 高 |
| 12 | MessageItem.tsx | L282 | `bg-amber-100 text-amber-600 dark:bg-amber-900/30 dark:text-amber-400` | 系统消息头像 | 🔴 高 |
| 13 | MessageItem.tsx | L321 | `border-amber-400` | 系统消息左边框 | 🔴 高 |
| 14 | MessageItem.tsx | L338 | `text-blue-500` | 等待响应图标 | 🟡 中 |
| 15 | MessageItem.tsx | L339 | `text-blue-600 dark:text-blue-400` | 等待响应文字 | 🟡 中 |
| 16 | MessageItem.tsx | L354 | `bg-blue-50 text-blue-700 dark:bg-blue-900/20 dark:text-blue-400` | Agent 标签 | 🟡 中 |
| 17 | Sidebar.tsx | L279 | `bg-black/50` | 移动端遮罩层 | 🟢 低 |
| 18 | TopNav.tsx | L319 | `bg-black/30` | 移动端导航遮罩 | 🟢 低 |
| 19 | ThemePanel.tsx | L39 | `'#f8fafc'`, `'#2563eb'`, `'#0f172a'` | 浅色主题预览回退色 | 🟡 中 |
| 20 | ThemePanel.tsx | L41 | `'#0f172a'`, `'#3b82f6'`, `'#f8fafc'` | 深色主题预览回退色 | 🟡 中 |
| 21 | ThemePanel.tsx | L163 | `'0 20px 40px -12px rgba(0, 0, 0, 0.3)'` | 面板阴影 | 🟡 中 |
| 22 | ActivityCard.tsx | L288 | `hover:bg-black/[0.03]` | 卡片头部 hover（亮色） | 🟢 低 |
| 23 | ActivityCard.tsx | L289 | `dark:hover:bg-white/[0.04]` | 卡片头部 hover（暗色） | 🟢 低 |
| 24 | ActivityCard.tsx | L367 | `text-red-500 dark:text-red-400` | 错误标签文字 | 🟡 中 |
| 25 | ActivityCard.tsx | L368 | `bg-red-50/50 text-red-600 dark:bg-red-900/10 dark:text-red-400` | 错误内容区 | 🟡 中 |
| 26 | ActivityCard.tsx | L379 | `bg-red-100 text-red-700 hover:bg-red-200 dark:bg-red-900/30 dark:text-red-400` | 破坏性操作按钮 | 🟡 中 |
| 27 | ActivityCard.tsx | L401 | `bg-black/50` | 确认弹窗遮罩 | 🟢 低 |

### 3.2 硬编码间距/尺寸

| # | 文件 | 行号 | 硬编码值 | 用途 | 严重程度 |
|---|------|------|----------|------|----------|
| 1 | Sidebar.tsx | L294-300 | `'280px'`, `'200px'`, `'220px'` | 侧边栏宽度（已在常量中定义） | 🟢 低 |
| 2 | TopNav.tsx | L276 | `'200px'` | 下拉菜单最小宽度 | 🟢 低 |

---

## 4. 改进建议

### P0 - 紧急（影响主题切换一致性）

#### 4.1 MessageItem 工具状态徽章统一为主题状态色

**问题**: 工具状态徽章（completed/failed/running/default）使用了 4 组硬编码 Tailwind 色值，主题切换时不会跟随变化。

**现状** (MessageItem.tsx L232-241):
```tsx
toolStatus === 'completed'
  ? 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400'
  : toolStatus === 'failed'
    ? 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400'
    : ...
```

**建议方案**: Tailwind 配置中已注册 `status.success/error/running/pending` 语义色（映射到 `--accent-*` CSS 变量），直接使用:
```tsx
const statusStyles: Record<string, string> = {
  completed: 'bg-status-success/10 text-status-success',
  failed: 'bg-status-error/10 text-status-error',
  running: 'bg-status-running/10 text-status-running',
}
const statusStyle = statusStyles[toolStatus] || 'bg-status-pending/10 text-status-pending'
```

**涉及文件**: `MessageItem.tsx` L232-241, L248, L282, L321, L338-339, L354
**预估工作量**: 2 小时

---

### P1 - 高优先级（影响用户体验一致性）

#### 4.2 ChatInput Token 进度条改用主题状态色

**问题**: Token 使用率进度条使用固定红/黄/绿色，不随主题变化。

**建议方案**: 使用 `tailwind.config.js` 中已注册的 `status.*` 色值:
```tsx
currentTokenUsage / maxTokens >= 0.9
  ? 'bg-status-error'
  : currentTokenUsage / maxTokens >= 0.7
    ? 'bg-status-waiting'
    : 'bg-status-success'
```

**涉及文件**: `ChatInput.tsx` L625-630
**预估工作量**: 1 小时

#### 4.3 ActivityCard 错误区与破坏性按钮改用语义色

**问题**: 错误展示区和 destructive 操作按钮使用固定红色。

**建议方案**: 使用 `destructive` 语义 class:
```tsx
// 错误标签
<div className="mb-1 text-xs font-medium text-destructive">错误</div>
// 错误内容
<pre className="bg-destructive/10 text-destructive rounded p-2 ...">
// 破坏性按钮
'bg-destructive/10 text-destructive hover:bg-destructive/20'
```

**涉及文件**: `ActivityCard.tsx` L367-368, L379
**预估工作量**: 1.5 小时

---

### P2 - 中优先级（提升主题完整性）

#### 4.4 FiveSpaceLayout 指示器/进度条颜色统一

**问题**: 执行状态指示器 `indicatorColor: '#f59e0b'` 和进度条 `bg-blue-400` 未跟随主题。

**建议方案**:
```tsx
// L148: 指示器颜色
indicatorColor: 'var(--accent-waiting)'

// L288: 待交互提示
'bg-status-waiting/10 text-status-waiting'

// L343: 进度条
'bg-status-running'
```

**涉及文件**: `FiveSpaceLayout.tsx` L148, L288, L343
**预估工作量**: 1 小时

#### 4.5 ThemePanel 预览回退色提取为常量

**问题**: `getPreviewColors` 函数中的默认预览色直接硬编码。

**建议方案**: 提取为 `THEME_PREVIEW_DEFAULTS` 常量，或从 themeStore 的当前主题获取预览色:
```tsx
const THEME_PREVIEW_DEFAULTS = {
  light: { bg: 'var(--bg-main)', primary: 'var(--primary)', text: 'var(--text-primary)' },
  dark: { bg: 'var(--bg-main)', primary: 'var(--primary)', text: 'var(--text-primary)' },
}
```

**涉及文件**: `ThemePanel.tsx` L35-42, L163
**预估工作量**: 1 小时

#### 4.6 MessageItem 系统消息和 Agent 标签统一

**问题**: 系统消息头像、左边框和 Agent 标签使用固定色值。

**建议方案**:
```tsx
// 系统头像: bg-status-waiting/20 text-status-waiting
// 系统边框: border-status-waiting
// 等待图标: text-status-running
// Agent 标签: bg-primary/10 text-primary
```

**涉及文件**: `MessageItem.tsx` L282, L321, L338-339, L354
**预估工作量**: 1 小时（可与 P0 合并处理）

---

### P3 - 低优先级（锦上添花）

#### 4.7 遮罩层颜色提取为 CSS 变量

**问题**: 多处遮罩层使用 `bg-black/50` 或 `bg-black/30`。

**建议方案**: 在 `theme.css` 或 `design-tokens.css` 中新增:
```css
--overlay-bg: rgba(0, 0, 0, 0.5);
--overlay-bg-light: rgba(0, 0, 0, 0.3);
```

**涉及文件**: `Sidebar.tsx` L279, `TopNav.tsx` L319, `ActivityCard.tsx` L401
**预估工作量**: 0.5 小时

#### 4.8 侧边栏/菜单固定宽度提取为 CSS 变量

**问题**: 侧边栏宽度和下拉菜单最小宽度使用 JS 硬编码。

**建议方案**: 已有 `--sidebar-width` CSS 变量定义，在 `style` 属性中引用:
```tsx
style={{ width: 'var(--sidebar-width, 200px)' }}
```

**涉及文件**: `Sidebar.tsx` L294-300, `TopNav.tsx` L276
**预估工作量**: 0.5 小时

---

## 附录 A: 主题变量速查表

以下为 `tailwind.config.js` 中已注册、可直接在组件中使用的语义化 class:

### 基础语义色（shadcn/ui 体系）

| Tailwind Class | CSS 变量 | 用途 |
|---------------|----------|------|
| `bg-background` | `hsl(var(--background))` | 页面/容器背景 |
| `text-foreground` | `hsl(var(--foreground))` | 主要文字 |
| `text-muted-foreground` | `hsl(var(--muted-foreground))` | 次要/辅助文字 |
| `bg-muted` | `hsl(var(--muted))` | 弱化背景 |
| `bg-primary` | `hsl(var(--primary))` | 主色背景 |
| `text-primary` | `hsl(var(--primary))` | 主色文字 |
| `bg-secondary` | `hsl(var(--secondary))` | 次色背景 |
| `bg-accent` | `hsl(var(--accent))` | 强调背景 |
| `bg-destructive` | `hsl(var(--destructive))` | 危险/错误背景 |
| `text-destructive` | `hsl(var(--destructive))` | 危险/错误文字 |
| `border-border` | `hsl(var(--border))` | 边框色 |

### 状态色（Deep Space 主题扩展）

| Tailwind Class | CSS 变量 | 用途 |
|---------------|----------|------|
| `bg-status-running` | `var(--accent-running)` | 执行中/运行状态 |
| `bg-status-waiting` | `var(--accent-waiting)` | 等待/警示状态 |
| `bg-status-success` | `var(--accent-success)` | 完成/成功状态 |
| `bg-status-error` | `var(--accent-error)` | 失败/错误状态 |
| `bg-status-pending` | `var(--accent-pending)` | 待处理状态 |
| `text-status-*` | 同上 | 对应文字色 |

### 消息气泡变量（CSS 变量直接引用）

| CSS 变量 | 用途 |
|----------|------|
| `var(--bubble-user-bg)` | 用户消息气泡背景 |
| `var(--bubble-user-text)` | 用户消息气泡文字 |
| `var(--bubble-user-radius)` | 用户消息气泡圆角 |
| `var(--bubble-user-shadow)` | 用户消息气泡阴影 |
| `var(--bubble-user-border)` | 用户消息气泡边框 |
| `var(--bubble-user-padding)` | 用户消息气泡内边距 |
| `var(--bubble-ai-bg)` | AI 消息气泡背景 |
| `var(--bubble-ai-text)` | AI 消息气泡文字 |
| `var(--bubble-ai-radius)` | AI 消息气泡圆角 |
| `var(--bubble-ai-shadow)` | AI 消息气泡阴影 |
| `var(--bubble-ai-border)` | AI 消息气泡边框 |
| `var(--bubble-ai-padding)` | AI 消息气泡内边距 |

---

## 附录 B: 审计方法说明

1. **逐行扫描**: 对每个组件的 JSX 返回值进行逐行扫描，识别 `className` 和 `style` 属性中的样式控制方式
2. **分类标注**: 按控制方式（tailwind-semantic / theme-variable / tailwind-fixed / inline-style / css-hardcoded）分类
3. **交叉验证**: 对照 `tailwind.config.js` 确认语义化 class 注册情况，对照 `themeService.ts` 确认 CSS 变量输出情况
4. **严重程度评估**: 根据硬编码对主题切换的影响程度和出现频率评定优先级

---

*报告结束*
