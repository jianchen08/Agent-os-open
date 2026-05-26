/**
 * 布局组件导出
 */

// 五渲染空间核心布局
export { FiveSpaceLayout } from './FiveSpaceLayout'
export type { FiveSpaceLayoutProps } from './FiveSpaceLayout'
export { FloatingWindowManager } from './FloatingWindowManager'
export type { FloatingWindowInstance } from '@/types/layout'
export { WorkspacePanel } from './WorkspacePanel'
export type { WorkspaceTab } from '@/types/layout'
export { DockBar } from './DockBar'
export type { DockItem } from '@/types/layout'
export { FullscreenOverlay } from './FullscreenOverlay'
export { ConnectionStatusIndicator } from './ConnectionStatusIndicator'

// 适配的旧布局组件
export { Sidebar } from './Sidebar'
export { TopNav, NAV_ITEMS, isNavItemActive } from './TopNav'
export type { NavItem } from './TopNav'
export { SplitPane } from './SplitPane'
export type { SplitPaneProps } from './SplitPane'
export { ThemeButton } from './ThemeButton'
export { ThemePanel } from './ThemePanel'
export { CollapsedStatusBar } from './CollapsedStatusBar'
export type { AgentInfo } from './CollapsedStatusBar'
