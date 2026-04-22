/**
 * 五渲染空间布局类型定义
 *
 * 定义布局配置、解析结果和各空间的状态
 */

/** 布局断点配置 */
export interface Breakpoints {
  mobile: number
  tablet: number
  desktop: number
  widescreen: number
}

/** 侧边栏配置 */
export interface SidebarConfig {
  minWidth: number
  maxWidth: number
  defaultWidth: number
  resizable: boolean
  collapseDuration: number
}

/** 面板配置 */
export interface PanelConfig {
  minWidth: number
  maxWidth: number
  defaultWidth: number
  resizable: boolean
}

/** 悬浮窗配置 */
export interface FloatingWindowConfig {
  defaultWidth: number
  defaultHeight: number
  minWidth: number
  minHeight: number
  draggable: boolean
  resizable: boolean
  cascadeOffset: number
  closeButtonPosition: 'top-right' | 'top-left'
}

/** Dock 栏配置 */
export interface DockBarConfig {
  height: number
  iconSize: number
  iconGap: number
  position: 'bottom' | 'left' | 'right'
  showLabels: boolean
  indicatorSize: number
}

/** 面板分割配置 */
export interface PanelSplitConfig {
  chatRatio: number
  workspaceRatio: number
  adjustable: boolean
  divider: {
    width: number
    color: string
    hoverColor: string
    activeColor: string
  }
}

/** 间距配置 */
export interface GapsConfig {
  betweenSpaces: number
  spacePadding: number
}

/** 过渡动画配置 */
export interface TransitionsConfig {
  panelDuration: number
  floatingDuration: number
  dockDuration: number
  easing: string
}

/** z-index 配置 */
export interface ZIndexConfig {
  sidebar: number
  chatPanel: number
  workspacePanel: number
  floatingWindow: number
  dockBar: number
  fullscreen: number
  overlay: number
}

/** 完整布局配置 */
export interface LayoutConfig {
  breakpoints: Breakpoints
  sidebar: SidebarConfig
  chatPanel: PanelConfig
  workspacePanel: PanelConfig
  floatingWindow: FloatingWindowConfig
  dockBar: DockBarConfig
  panelSplit: PanelSplitConfig
  gaps: GapsConfig
  transitions: TransitionsConfig
  zIndex: ZIndexConfig
}

/** 解析后的布局值 */
export interface ResolvedLayout {
  sidebar: { width: number; minWidth: number; maxWidth: number }
  chatPanel: { width: number; minWidth: number }
  workspacePanel: { width: number; minWidth: number }
  floatingWindow: { width: number; height: number }
  dockBar: { height: number }
}

/** 悬浮窗实例 */
export interface FloatingWindowInstance {
  id: string
  title: string
  icon?: string
  component: string
  props?: Record<string, unknown>
  dataSource?: string
  position: { x: number; y: number }
  size: { width: number; height: number }
  zIndex: number
  isMinimized: boolean
  isMaximized: boolean
}

/** 工作区 Tab */
export interface WorkspaceTab {
  id: string
  title: string
  icon?: string
  moduleId: string
  component?: string
  layout?: Record<string, unknown>
  dataSource?: string
  isActive: boolean
  isPinned: boolean
}

/** Dock 图标项 */
export interface DockItem {
  id: string
  moduleId: string
  icon: string
  label: string
  indicator: 'none' | 'dot' | 'badge'
  indicatorColor?: string
  badgeCount?: number
  isActive: boolean
  onClick: () => void
}

/** 渲染空间类型 */
export type RenderingSpace = 'chat' | 'workspace' | 'floating' | 'dock' | 'fullscreen'

/** 视口断点类型 */
export type ViewportBreakpoint = 'mobile' | 'tablet' | 'desktop' | 'widescreen'
