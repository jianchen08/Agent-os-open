/**
 * 布局解析器
 *
 * 从主题配置解析出安全的布局值
 * 实现四层保障机制
 */

import type { LayoutConfig, ResolvedLayout } from '@/types/layout'

/** 默认布局配置 */
export const DEFAULT_LAYOUT_CONFIG: LayoutConfig = {
  breakpoints: { mobile: 768, tablet: 1024, desktop: 1280, widescreen: 1920 },
  sidebar: {
    minWidth: 180,
    maxWidth: 320,
    defaultWidth: 220,
    resizable: true,
    collapseDuration: 300,
  },
  chatPanel: { minWidth: 320, maxWidth: Infinity, defaultWidth: 480, resizable: false },
  workspacePanel: { minWidth: 400, maxWidth: Infinity, defaultWidth: 560, resizable: true },
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
  dockBar: {
    height: 48,
    iconSize: 20,
    iconGap: 6,
    position: 'bottom',
    showLabels: false,
    indicatorSize: 6,
  },
  panelSplit: {
    chatRatio: 0.45,
    workspaceRatio: 0.55,
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

/**
 * 从主题配置解析出安全的布局值
 *
 * 根据视口宽度计算各面板的实际尺寸
 * 当空间不足时，优先保证聊天面板
 */
export function resolveLayout(config: LayoutConfig, viewportWidth: number): ResolvedLayout {
  const dockWidth = config.dockBar.position !== 'bottom' ? config.dockBar.height : 0
  const availableWidth =
    viewportWidth - config.sidebar.defaultWidth - dockWidth - config.gaps.betweenSpaces * 2

  const desiredChat = availableWidth * config.panelSplit.chatRatio
  const desiredWorkspace = availableWidth * config.panelSplit.workspaceRatio

  const minChat = config.chatPanel.minWidth
  const minWorkspace = config.workspacePanel.minWidth

  let chatWidth: number
  let workspaceWidth: number

  if (desiredChat >= minChat && desiredWorkspace >= minWorkspace) {
    chatWidth = desiredChat
    workspaceWidth = desiredWorkspace
  } else if (availableWidth >= minChat + minWorkspace) {
    chatWidth = minChat
    workspaceWidth = availableWidth - minChat
  } else {
    chatWidth = availableWidth
    workspaceWidth = 0
  }

  const maxFloatingW = viewportWidth * 0.9
  const maxFloatingH = typeof window !== 'undefined' ? window.innerHeight * 0.9 : 600

  return {
    sidebar: {
      width: Math.min(config.sidebar.defaultWidth, config.sidebar.maxWidth),
      minWidth: config.sidebar.minWidth,
      maxWidth: config.sidebar.maxWidth,
    },
    chatPanel: { width: chatWidth, minWidth: minChat },
    workspacePanel: { width: workspaceWidth, minWidth: minWorkspace },
    floatingWindow: {
      width: Math.min(config.floatingWindow.defaultWidth, maxFloatingW),
      height: Math.min(config.floatingWindow.defaultHeight, maxFloatingH),
    },
    dockBar: { height: config.dockBar.height },
  }
}
