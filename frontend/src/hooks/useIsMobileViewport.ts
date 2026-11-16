/**
 * 移动断点判定（与 FiveSpaceLayout 单一来源收敛）
 *
 * 断点值取布局配置 mobile 断点（默认 768px，可被主题覆盖），
 * 与主布局的移动形态判定同源——抽屉内 Sidebar 等宿主组件用本钩子
 * 接线 isMobile，保证「移动行为」与「移动形态」用同一把尺。
 */
import { useEffect, useState } from 'react'
import { safeLoadLayout } from '@/services/layout/resolver'

export function isMobileViewport(width: number, mobileBreakpoint: number): boolean {
  return width < mobileBreakpoint
}

/** 非 hook 版本：供 service 等无法用钩子的场合同步判定（如 workspacePanelOpener） */
export function isMobileViewportNow(): boolean {
  if (typeof window === 'undefined') return false
  return isMobileViewport(window.innerWidth, safeLoadLayout(undefined).breakpoints.mobile)
}

export function useIsMobileViewport(): boolean {
  const [viewportWidth, setViewportWidth] = useState(() =>
    typeof window === 'undefined' ? 1280 : window.innerWidth,
  )

  useEffect(() => {
    const handleResize = () => setViewportWidth(window.innerWidth)
    window.addEventListener('resize', handleResize)
    return () => window.removeEventListener('resize', handleResize)
  }, [])

  return isMobileViewport(viewportWidth, safeLoadLayout(undefined).breakpoints.mobile)
}
