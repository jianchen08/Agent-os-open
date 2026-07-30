# FiveSpaceLayout

## 需求说明

### 功能概述

Deep Space v2 App Shell 主布局（对齐 Ardot 设计稿 B/C 区），将应用划分为：

1. **TitleBar（顶栏）** — 高度 32px，品牌 + 会话标题 + 右侧图标组
2. **SideBar（单侧边栏）** — 默认 288px（240–360），会话列表；折叠按钮在侧栏最顶部
3. **Chat Panel（聊天面板）** — 默认 520px（420–720），承载聊天交互
4. **Workspace Panel（工作区）** — 弹性剩余宽度，Schema 模块与文件编辑
5. **StatusBar（状态栏）** — 高度 22px，系统状态 + 会话指标（替代原 DockBar）
6. **Floating Windows / Fullscreen Overlay** — 浮层与全屏覆盖

响应式：

- **Mobile**：聊天全宽，侧边栏抽屉，工作区覆盖层
- **Desktop+**：侧栏 + Chat + Workspace 三区并排

### 验收标准（Deep Space v2）

- [AC1] 全屏布局（100dvh × w-screen），无页面级滚动
- [AC2] TitleBar 高度 32px（`var(--layout-titlebar-height)`）
- [AC3] 单侧边栏默认 288px，折叠按钮在侧栏最顶部（不在 TitleBar）
- [AC4] ChatPanel 默认约 45% / 520px，min 420 max 720
- [AC5] WorkspacePanel 占剩余宽度，支持折叠与拖拽比例
- [AC6] StatusBar 高度 22px，左连接/管道/审批，右成本/模型/时间
- [AC7] 不再渲染底部 DockBar 工具条（StatusBar 承接状态展示）
- [AC8] 主题 token 对齐 Deep Space v2（`deep-space-v2.css` + `dark` 预设）

## 逻辑说明

### 数据流

```
外部 props (chatContent / sidebarContent)
  → AppHeader (TitleBar)
  → SideBar 插槽 | Chat Splitter | WorkspacePanel
  → StatusBar (connectionStatus / activeExecutions / pendingInteractions)
useThemeStore.themeConfig → safeLoadLayout → resolveLayout
useLayoutModeStore → workspaceTabs / floatingWindows / connectionStatus
```

### 核心决策

- 用户决策：**不双列 ActivityBar+SideBar**，合并为单侧边栏；折叠按钮置顶
- DockBar 功能由 StatusBar 替代（设计稿 49:331）
- 后端状态：连接态、执行中任务、待审批均从 `useLayoutModeStore` 读取

## 结构说明

### 本文件夹文件

| 文件 | 职责 |
|------|------|
| `FiveSpaceLayout.tsx` | App Shell 主布局编排 |
| `AppHeader.tsx` | TitleBar 32px |
| `Sidebar.tsx` | 会话侧栏 288px，折叠置顶 |
| `StatusBar.tsx` | 底栏 22px 状态与指标 |
| `DockBar.tsx` | 保留导出（兼容/其他入口），主壳不再挂载 |
| `WorkspacePanel.tsx` | 工作区页签 |
| `FloatingWindowManager.tsx` | 浮动窗口 |
| `FullscreenOverlay.tsx` | 全屏覆盖 |
| `ConnectionStatusIndicator.tsx` | 连接指示（可被 StatusBar 复用） |
| `ThemeButton.tsx` / `ThemePanel.tsx` | 主题切换 |
| `index.ts` | 模块导出 |

### 对外接口

| 接口 | 说明 |
|------|------|
| `FiveSpaceLayout` | 主布局组件 |
| `FiveSpaceLayoutProps` | chatContent / sidebarContent / onToggleMode / 主题与登出 |
| `StatusBar` / `StatusBarProps` | 底栏组件 |
| `AppHeader` | 顶栏组件 |
| `Sidebar` | 会话侧栏 |
