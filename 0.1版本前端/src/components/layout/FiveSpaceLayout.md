# FiveSpaceLayout

## 需求说明

### 功能概述

五空间布局组件，将整个应用界面划分为五个渲染空间：

1. **Chat Panel（聊天面板）** — 左侧区域，承载现有聊天交互界面
2. **Workspace Panel（工作区面板）** — 右侧区域，承载 Schema 渲染的内容模块
3. **Floating Windows（浮动窗口）** — 可拖拽、可缩放的覆盖层窗口
4. **Dock Bar（停靠栏）** — 底部快捷工具栏，包含工具入口和状态指示器
5. **Fullscreen Overlay（全屏覆盖层）** — 用于沉浸式交互的全屏遮罩

组件还集成了顶部导航栏（含侧边栏切换、连接状态指示、布局模式切换）和可选的侧边栏，并支持响应式布局：

- **Mobile**：聊天面板全屏，工作区隐藏（通过 Dock 栏访问）
- **Tablet**：聊天 60%，工作区 40%
- **Desktop+**：聊天 45%，工作区 55%

### 用户故事

- 作为用户，我希望在不同设备上都能获得合理的布局体验，移动端全屏聊天，桌面端同时查看聊天和工作区
- 作为用户，我希望通过侧边栏切换按钮控制侧边栏的显示/隐藏，节省屏幕空间
- 作为用户，我希望浮动窗口可以承载独立的工具或内容面板，不影响主界面布局
- 作为用户，我希望通过底部 Dock 栏快速访问工具和查看执行状态
- 作为用户，我希望在需要沉浸式操作时进入全屏覆盖层，按 ESC 可退出
- 作为用户，我希望在运行中的任务能在 Dock 栏显示进度指示，待处理的交互请求有明确提示

### 验收标准

- [AC1] 组件渲染为全屏布局（h-screen × w-screen），不产生页面滚动
- [AC2] 顶部导航栏高度固定 40px，包含侧边栏切换、标题、连接状态、布局切换按钮
- [AC3] 移动端（< mobile 断点）聊天面板占满宽度，工作区和侧边栏自动隐藏
- [AC4] 平板端聊天面板与工作区按布局配置的宽度比例显示
- [AC5] 桌面端聊天面板与工作区按布局配置的宽度比例显示，且支持手动折叠/展开工作区
- [AC6] 侧边栏可通过按钮切换显示/隐藏，宽度固定 224px（w-56）
- [AC7] 工作区面板支持标签切换、标签关闭，通过 useLayoutModeStore 管理
- [AC8] 浮动窗口管理器挂载在固定定位层，z-index 由布局配置决定
- [AC9] Dock 栏位于底部，高度由布局配置决定，展示基础 Dock 项 + 动态执行状态项 + 待处理交互项
- [AC10] 全屏覆盖层激活时覆盖整个视口，ESC 键可退出
- [AC11] 活跃执行任务在 Dock 栏区域显示进度条和百分比
- [AC12] 待处理交互请求数量在顶部导航栏以橙色徽标形式提示
- [AC13] 窗口 resize 时自动更新视口宽度和断点

## 逻辑说明

### 数据流

```
外部调用方
  │
  ├─ chatContent (ReactNode) ──→ 聊天面板插槽
  ├─ topNavContent (ReactNode) ──→ 顶部导航中心区域插槽
  ├─ sidebarContent (ReactNode) ──→ 侧边栏插槽
  └─ onToggleMode (callback) ──→ 切换至经典布局模式
        │
        ▼
useThemeStore ──→ currentTheme ──→ safeLoadLayout() ──→ layoutConfig
                                                           │
                                                           ▼
                                                    resolveLayout(layoutConfig, viewportWidth)
                                                           │
                                                           ▼
                                              resolved (ResolvedLayout): 各面板宽度/高度数值
                                                           │
                                                           ▼
                                              getBreakpoint(viewportWidth, breakpoints)
                                                           │
                                                           ▼
                                              breakpoint: 'mobile' | 'tablet' | 'desktop' | 'widescreen'
```

```
useLayoutModeStore ──→ floatingWindows ──→ FloatingWindowManager
                   ├─→ workspaceTabs ──→ WorkspacePanel
                   ├─→ dockItems ──→ DockBar（合并执行状态和交互请求后）
                   ├─→ fullscreenActive / fullscreenTitle / fullscreenContent ──→ FullscreenOverlay
                   ├─→ activeExecutions ──→ Dock 栏进度条 + 动态 Dock 项
                   └─→ pendingInteractions ──→ 顶部徽标 + 动态 Dock 项
```

### 状态流转

**侧边栏折叠/展开：**
```
sidebarCollapsed: false ←→ true
  触发: toggleSidebar 按钮
  效果: 侧边栏区域显示/隐藏（带 transition-all 动画）
  移动端强制隐藏（isMobile 时宽度为 0）
```

**工作区折叠/展开：**
```
workspaceCollapsed: false ←→ true
  触发: 工作区切换手柄按钮
  效果: 工作区面板显示/隐藏，聊天面板自动扩展填充
  移动端强制隐藏
```

**视口宽度监听：**
```
viewportWidth: number
  触发: window resize 事件
  效果: 重新计算断点（breakpoint）和布局解析（resolved）
  初始值: window.innerWidth（SSR 安全，默认 1280）
```

**全屏覆盖层：**
```
fullscreenActive: false → true → false
  进入: 外部通过 useLayoutModeStore 设置
  退出: ESC 键盘事件触发 exitFullscreen()
```

### 核心处理逻辑

1. **布局解析**：从 `useThemeStore` 获取当前主题配置，通过 `safeLoadLayout()` 安全加载布局配置，再由 `resolveLayout()` 根据视口宽度计算各面板的具体像素宽度
2. **断点计算**：`getBreakpoint()` 函数根据视口宽度和配置的断点阈值返回 `'mobile' | 'tablet' | 'desktop' | 'widescreen'`，驱动条件渲染
3. **Dock 项增强**：`enrichedDockItems` 将基础 Dock 项与活跃执行状态（显示类型图标 + 进度点）和待处理交互请求（显示问号图标 + 徽标）合并，形成最终的 Dock 栏数据
4. **工作区标签管理**：通过 `useLayoutModeStore` 的 `setActiveTab` / `closeWorkspaceTab` 控制标签切换和关闭，标签内容由 `renderTabContent` 回调渲染（当前为占位内容）
5. **浮动窗口管理**：通过 `useLayoutModeStore` 的 `updateFloatingWindow` / `closeFloatingWindow` 控制窗口更新和关闭，窗口内容由 `renderFloatingContent` 回调渲染（当前为占位内容）
6. **ESC 键退出全屏**：全局 keydown 监听，仅在全屏激活状态下响应 Escape 键

## 结构说明

### Props 接口

| 属性名 | 类型 | 必填 | 默认值 | 说明 |
|--------|------|------|--------|------|
| chatContent | `React.ReactNode` | 是 | — | 聊天面板内容插槽，承载完整聊天界面 |
| topNavContent | `React.ReactNode` | 否 | — | 顶部导航栏中心区域内容插槽 |
| sidebarContent | `React.ReactNode` | 否 | — | 侧边栏内容插槽，不传则不渲染侧边栏 |
| onToggleMode | `() => void` | 否 | — | 切换至经典布局模式的回调函数 |

### 状态（State）

| 状态名 | 类型 | 初始值 | 说明 |
|--------|------|--------|------|
| sidebarCollapsed | `boolean` | `false` | 侧边栏是否折叠 |
| workspaceCollapsed | `boolean` | `false` | 工作区面板是否折叠 |
| viewportWidth | `number` | `window.innerWidth`（SSR 默认 1280） | 当前视口宽度，用于响应式计算 |

**外部 Store 状态（useLayoutModeStore）：**

| 状态名 | 说明 |
|--------|------|
| floatingWindows | 浮动窗口实例列表 |
| workspaceTabs | 工作区标签页列表 |
| dockItems | Dock 栏基础项列表 |
| fullscreenActive | 全屏覆盖层是否激活 |
| fullscreenTitle | 全屏覆盖层标题 |
| fullscreenContent | 全屏覆盖层内容 |
| activeExecutions | 活跃执行任务列表（含 status、progress） |
| pendingInteractions | 待处理交互请求列表 |

**外部 Store 状态（useThemeStore）：**

| 状态名 | 说明 |
|--------|------|
| currentTheme | 当前主题配置，包含 layout 子配置 |

### 主题变量依赖

| Tailwind 语义化 Class | 使用位置 | 说明 |
|------------------------|----------|------|
| `bg-background` | 根容器、搜索栏背景 | 页面背景色 |
| `text-foreground` | 根容器、标题文字、标签文字 | 主前景文字色 |
| `border-border` | 顶部导航栏底边框、侧边栏右边框、聊天面板右边框、Dock 栏顶边框、工作区切换手柄右边框 | 统一边框色 |
| `bg-accent` | 侧边栏切换按钮悬停、工作区切换手柄悬停、布局切换按钮悬停 | 交互元素悬停背景 |
| `text-muted-foreground` | 工作区切换手柄箭头、布局切换按钮、占位内容文字、Dock 栏进度百分比 | 辅助/弱化文字色 |
| `bg-muted` | Dock 栏进度条底色 | 弱化背景色 |
| `bg-muted/50` | Dock 栏执行进度项背景 | 半透明弱化背景色 |
| `bg-orange-500/10` | 待处理交互提示背景 | 橙色半透明背景（临时样式） |
| `text-orange-400` | 待处理交互提示文字 | 橙色文字（临时样式） |
| `bg-blue-400` | Dock 栏进度条填充色 | 蓝色进度条（临时样式） |

### 子组件依赖

| 子组件 | 路径 | 说明 |
|--------|------|------|
| DockBar | `./DockBar` | 底部停靠栏，接收 Dock 项列表和图标配置 |
| FloatingWindowManager | `./FloatingWindowManager` | 浮动窗口管理器，处理窗口拖拽、缩放、层级 |
| FullscreenOverlay | `./FullscreenOverlay` | 全屏覆盖层，接收激活状态、标题和退出回调 |
| WorkspacePanel | `./WorkspacePanel` | 工作区面板，支持标签切换和内容渲染 |
| ConnectionStatusIndicator | `./ConnectionStatusIndicator` | 连接状态指示器，显示延迟和队列信息 |

### 对外接口

| 接口 | 类型 | 说明 |
|------|------|------|
| `FiveSpaceLayoutProps` | TypeScript Interface | 组件 Props 类型定义，通过 `export interface` 导出 |
| 默认导出 | `function FiveSpaceLayout` | 命名导出的函数组件 |
