/**
 * 布局解析器
 *
 * 从主题配置解析出安全的布局值
 * 实现四层保障机制
 */

import type { LayoutConfig } from '@/types/layout'

/** 默认布局配置 */
export const DEFAULT_LAYOUT_CONFIG: LayoutConfig = {
  breakpoints: { mobile: 768, tablet: 1024, desktop: 1280, widescreen: 1920 },
  // Deep Space v2：SideBar 288 (240-360)，ChatPanel 520 (420-720)
  sidebar: {
    minWidth: 240,
    maxWidth: 360,
    defaultWidth: 288,
    resizable: true,
    collapseDuration: 300,
  },
  chatPanel: { minWidth: 420, maxWidth: 720, defaultWidth: 520, resizable: false },
  workspacePanel: { minWidth: 360, maxWidth: Infinity, defaultWidth: 576, resizable: true },
  floatingWindow: {
    defaultWidth: 480,
    defaultHeight: 360,
    minWidth: 280,
    minHeight: 200,
    draggable: true,
    resizable: true,
    cascadeOffset: 24,
    closeButtonPosition: 'top-right',
  },
  // dockBar 高度改为 StatusBar 22px（设计稿 49:331）
  dockBar: {
    height: 22,
    iconSize: 14,
    iconGap: 16,
    position: 'bottom',
    showLabels: false,
    indicatorSize: 6,
  },
  panelSplit: {
    chatRatio: 0.55,
    workspaceRatio: 0.45,
    adjustable: true,
    divider: {
      width: 1,
      color: 'var(--border-default)',
      hoverColor: 'var(--primary)',
      activeColor: 'var(--primary)',
    },
  },
  gaps: { betweenSpaces: 0, spacePadding: 8 },
  transitions: {
    panelDuration: 300,
    floatingDuration: 200,
    dockDuration: 200,
    easing: 'cubic-bezier(0.4, 0, 0.2, 1)',
  },
  zIndex: {
    sidebar: 10,
    chatPanel: 1,
    workspacePanel: 1,
    floatingWindow: 50,
    dockBar: 40,
    fullscreen: 100,
    overlay: 90,
  },
}

/**
 * 安全加载主题布局配置
 *
 * 将主题配置与默认配置合并，确保所有字段都有有效值
 */
export function safeLoadLayout(themeLayout: LayoutConfig | undefined): LayoutConfig {
  if (!themeLayout) return DEFAULT_LAYOUT_CONFIG

  try {
    const merged: LayoutConfig = {
      breakpoints: { ...DEFAULT_LAYOUT_CONFIG.breakpoints, ...themeLayout.breakpoints },
      sidebar: { ...DEFAULT_LAYOUT_CONFIG.sidebar, ...themeLayout.sidebar },
      chatPanel: { ...DEFAULT_LAYOUT_CONFIG.chatPanel, ...themeLayout.chatPanel },
      workspacePanel: { ...DEFAULT_LAYOUT_CONFIG.workspacePanel, ...themeLayout.workspacePanel },
      floatingWindow: { ...DEFAULT_LAYOUT_CONFIG.floatingWindow, ...themeLayout.floatingWindow },
      dockBar: { ...DEFAULT_LAYOUT_CONFIG.dockBar, ...themeLayout.dockBar },
      panelSplit: { ...DEFAULT_LAYOUT_CONFIG.panelSplit, ...themeLayout.panelSplit },
      gaps: { ...DEFAULT_LAYOUT_CONFIG.gaps, ...themeLayout.gaps },
      transitions: { ...DEFAULT_LAYOUT_CONFIG.transitions, ...themeLayout.transitions },
      zIndex: { ...DEFAULT_LAYOUT_CONFIG.zIndex, ...themeLayout.zIndex },
    }
    return merged
  } catch {
    return DEFAULT_LAYOUT_CONFIG
  }
}
