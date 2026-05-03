/**
 * 五渲染空间主布局
 *
 * 管理五个渲染空间的排列：聊天/工作区/悬浮窗/Dock/全屏
 * 布局参数从主题配置读取，通过 safeLoadLayout → resolveLayout 获取
 */

import React, { useState, useCallback, useEffect, useMemo } from 'react'
import { safeLoadLayout, resolveLayout } from '@/services/layout/resolver'
import { useThemeStore } from '@/stores/themeStore'
import type { ResolvedLayout, ViewportBreakpoint } from '@/types/layout'

/** 主布局组件属性 */
interface MainLayoutProps {
  /** 侧边栏内容 */
  sidebarContent?: React.ReactNode
  /** 顶部导航内容 */
  topNavContent?: React.ReactNode
  /** 聊天面板内容 */
  chatContent?: React.ReactNode
  /** 工作区面板内容 */
  workspaceContent?: React.ReactNode
  /** Dock 栏内容 */
  dockContent?: React.ReactNode
  /** 悬浮窗内容 */
  floatingContent?: React.ReactNode
  /** 全屏覆盖层内容 */
  fullscreenContent?: React.ReactNode
}

/**
 * 获取当前视口断点
 *
 * 根据视口宽度和断点配置返回对应的断点类型
 */
function getBreakpoint(
  width: number,
  breakpoints: { mobile: number; tablet: number; desktop: number; widescreen: number },
): ViewportBreakpoint {
  if (width < breakpoints.mobile) return 'mobile'
  if (width < breakpoints.tablet) return 'tablet'
  if (width < breakpoints.desktop) return 'desktop'
  return 'widescreen'
}

/**
 * 五渲染空间主布局组件
 *
 * 管理侧边栏、聊天面板、工作区面板、Dock 栏、悬浮窗容器和全屏覆盖层的排列
 */
export function MainLayout({
  sidebarContent,
  topNavContent,
  chatContent,
  workspaceContent,
  dockContent,
  floatingContent,
  fullscreenContent,
}: MainLayoutProps) {
  const themeConfig = useThemeStore((s) => s.currentTheme)
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false)
  const [viewportWidth, setViewportWidth] = useState(
    typeof window !== 'undefined' ? window.innerWidth : 1280,
  )

  useEffect(() => {
    const handleResize = () => setViewportWidth(window.innerWidth)
    window.addEventListener('resize', handleResize)
    return () => window.removeEventListener('resize', handleResize)
  }, [])

  const layoutConfig = useMemo(() => safeLoadLayout((themeConfig as any)?.layout), [themeConfig])
  const resolved = useMemo(
    () => resolveLayout(layoutConfig, viewportWidth),
    [layoutConfig, viewportWidth],
  )
  const breakpoint = useMemo(
    () => getBreakpoint(viewportWidth, layoutConfig.breakpoints),
    [viewportWidth, layoutConfig.breakpoints],
  )

  const isMobile = breakpoint === 'mobile'
  const showWorkspace = resolved.workspacePanel.width > 0 && !isMobile

  const toggleSidebar = useCallback(() => setSidebarCollapsed((prev) => !prev), [])

  return (
    <div
      className="bg-background flex h-screen w-screen flex-col overflow-hidden"
      style={{ fontFamily: 'var(--font-family)' }}
    >
      {/* 水平区域：侧边栏 + 主内容 */}
      <div className="flex min-h-0 flex-1 overflow-hidden">
        {/* 侧边栏 */}
        <aside
          className="border-border flex-shrink-0 overflow-hidden border-r transition-all duration-300"
          style={{
            width: sidebarCollapsed ? 0 : resolved.sidebar.width,
            minWidth: sidebarCollapsed ? 0 : resolved.sidebar.minWidth,
            zIndex: layoutConfig.zIndex.sidebar,
          }}
        >
          {!sidebarCollapsed && (
            <div className="flex h-full flex-col">
              <div className="text-foreground p-4 text-sm font-medium">导航</div>
              <div className="flex-1 overflow-y-auto p-2">
                {sidebarContent}
              </div>
            </div>
          )}
        </aside>

        {/* 主内容区 */}
        <main className="flex min-w-0 flex-1 flex-col">
          {/* 顶部导航 */}
          <header
            className="border-border flex flex-shrink-0 items-center border-b px-4"
            style={{ height: 48 }}
          >
            <button
              onClick={toggleSidebar}
              className="hover:bg-accent text-foreground rounded-md p-2"
            >
              <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
                <rect y="2" width="16" height="1.5" rx="0.75" />
                <rect y="7" width="16" height="1.5" rx="0.75" />
                <rect y="12" width="16" height="1.5" rx="0.75" />
              </svg>
            </button>
            <div className="text-foreground ml-4 text-sm font-medium">超级终端</div>
            <div className="flex-1" />
            {topNavContent}
          </header>

          {/* 聊天 + 工作区面板 */}
          <div className="flex min-h-0 flex-1">
            {/* 聊天面板 */}
            <section
              className="border-border flex-shrink-0 overflow-hidden border-r"
              style={{
                width: resolved.chatPanel.width,
                minWidth: resolved.chatPanel.minWidth,
              }}
            >
              {chatContent}
            </section>

            {/* 工作区面板 */}
            {showWorkspace && (
              <section
                className="min-w-0 flex-1 overflow-hidden"
                style={{ minWidth: resolved.workspacePanel.minWidth }}
              >
                {workspaceContent}
              </section>
            )}
          </div>
        </main>
      </div>

      {/* Dock 栏 */}
      <div
        className="border-border flex flex-shrink-0 items-center justify-center gap-1 border-t px-2"
        style={{
          height: resolved.dockBar.height,
          zIndex: layoutConfig.zIndex.dockBar,
        }}
      >
        {dockContent}
      </div>

      {/* 悬浮窗容器 */}
      <div
        id="floating-container"
        className="pointer-events-none fixed inset-0"
        style={{ zIndex: layoutConfig.zIndex.floatingWindow }}
      >
        {floatingContent}
      </div>

      {/* 全屏覆盖层 */}
      <div
        id="fullscreen-container"
        className="fixed inset-0 hidden"
        style={{ zIndex: layoutConfig.zIndex.fullscreen }}
      >
        {fullscreenContent}
      </div>
    </div>
  )
}
