/**
 * WindowManager — 窗口管理抽象（阶段5 detachable P1）
 *
 * 把 PageDeclaration 的 detachable 配置（popout / childWindow / desktopWidget）
 * 与浮窗状态管理（layoutModeStore.floatingWindows）接通，让任何 page 都能
 * 以浮窗形式弹出，独立于 FiveSpaceLayout 的当前 Tab。
 *
 * 设计目标：
 * - 抽象接口 `WindowManager` 为 P2（Electron 子窗口）/ P3（桌面小组件）预留；
 *   Web 版只完整实现 openPopout，openChildWindow / openDesktopWidget 内部
 *   降级到 openPopout（并打 console.info 提示降级）。
 * - 行为由 PageDeclaration.detachable 决定：
 *     detachable.popout === false  → 显式禁止 popout，openPopout 返回空串
 *     其余（true / undefined）     → 允许（降级路径依赖此宽松门控）
 *
 * 关联：
 * - stores/layoutModeStore.ts 的 addFloatingWindow/closeFloatingWindow/updateFloatingWindow
 * - components/layout/FloatingWindowManager.tsx 的 renderFloatingWindowContent
 *   （消费这里写入的 FloatingWindowInstance.props.pageId）
 */

import { useLayoutModeStore } from '@/stores/layoutModeStore'
import type { FloatingWindowInstance } from '@/types/layout'
import type { PageDeclaration } from '@/services/schema/ContributionRegistry'
import type { ElectronOpenWindowOptions } from '@/types/electron'

/** 浮窗基础 z-index（位于布局容器 z:50 之上，确保浮窗始终可见） */
const FLOATING_BASE_Z = 1000

/** 无 defaultSize 时的内置默认尺寸 */
const DEFAULT_SIZE = { width: 320, height: 480 }

/** 视口兜底尺寸（SSR / jsdom 等无 window 环境） */
const FALLBACK_VIEWPORT = { width: 1024, height: 768 }

/**
 * 进程级单调计数器，保证 windowId 唯一
 *
 * 同一毫秒内连续弹出同一 page 时，Date.now() 会碰撞；叠加计数器确保稳定唯一。
 */
let windowSeq = 0

/** openPopout 等方法的可选参数 */
export interface OpenWindowOptions {
  /** 显式指定窗口位置（缺省居中） */
  position?: { x: number; y: number }
  /** 显式指定窗口尺寸（覆盖 page.detachable.defaultSize） */
  size?: { w: number; h: number }
}

/**
 * 窗口管理抽象接口
 *
 * P2/P3（Electron 环境）将提供原生实现；Web 版由 WebWindowManager 实现，
 * childWindow / desktopWidget 降级为 popout。
 */
export interface WindowManager {
  /** 以独立浮窗弹出 page；返回 windowId（被显式禁止时返回空串） */
  openPopout(page: PageDeclaration, opts?: OpenWindowOptions): string
  /** 跨页面子窗口（P2）；Web 版降级到 popout */
  openChildWindow(page: PageDeclaration, opts?: OpenWindowOptions): string
  /** 桌面小组件（P3）；Web 版降级到 popout */
  openDesktopWidget(page: PageDeclaration, opts?: OpenWindowOptions): string
  /** 关闭窗口 */
  close(windowId: string): void
  /** 把窗口提到最前（提升 zIndex） */
  focus(windowId: string): void
}

/**
 * 计算下一个可用的 z-index（新窗口置顶）
 *
 * 取当前所有浮窗的最大 zIndex + 1；无浮窗时返回 FLOATING_BASE_Z + 1。
 */
function nextZIndex(): number {
  const wins = useLayoutModeStore.getState().floatingWindows
  if (wins.length === 0) return FLOATING_BASE_Z + 1
  return Math.max(...wins.map((w) => w.zIndex)) + 1
}

/** 解析窗口尺寸：opts.size > page.detachable.defaultSize > 默认 */
function resolveSize(page: PageDeclaration, opts?: OpenWindowOptions): { width: number; height: number } {
  if (opts?.size) return { width: opts.size.w, height: opts.size.h }
  if (page.detachable?.defaultSize) {
    return { width: page.detachable.defaultSize.w, height: page.detachable.defaultSize.h }
  }
  return { ...DEFAULT_SIZE }
}

/** 解析窗口位置：opts.position > 居中（基于视口与尺寸） */
function resolvePosition(
  size: { width: number; height: number },
  opts?: OpenWindowOptions,
): { x: number; y: number } {
  if (opts?.position) return { x: opts.position.x, y: opts.position.y }
  const vw = typeof window !== 'undefined' ? window.innerWidth : FALLBACK_VIEWPORT.width
  const vh = typeof window !== 'undefined' ? window.innerHeight : FALLBACK_VIEWPORT.height
  return {
    x: Math.max(0, Math.floor((vw - size.width) / 2)),
    y: Math.max(0, Math.floor((vh - size.height) / 2)),
  }
}

/**
 * Web 版窗口管理器
 *
 * 完整实现 openPopout（写入 layoutModeStore.floatingWindows）；
 * openChildWindow / openDesktopWidget 降级到 openPopout。
 */
export class WebWindowManager implements WindowManager {
  openPopout(page: PageDeclaration, opts?: OpenWindowOptions): string {
    // 显式禁止 popout：不加窗口，返回空串
    if (page.detachable?.popout === false) {
      return ''
    }

    const windowId = `${page.id}-popout-${Date.now()}-${++windowSeq}`
    const size = resolveSize(page, opts)
    const position = resolvePosition(size, opts)

    const instance: FloatingWindowInstance = {
      id: windowId,
      title: page.title ?? page.id,
      icon: page.icon,
      // component 兼容旧 FloatingWindowInstance 的 widget 渲染路径
      component: page.widget ?? page.id,
      // props.pageId 供 renderFloatingWindowContent 反查 PageDeclaration
      props: { ...(page.props ?? {}), pageId: page.id },
      dataSource: page.datasourceUri,
      position,
      size,
      zIndex: nextZIndex(),
      isMinimized: false,
      isMaximized: false,
    }

    useLayoutModeStore.getState().addFloatingWindow(instance)
    return windowId
  }

  openChildWindow(page: PageDeclaration, opts?: OpenWindowOptions): string {
    // P2（Electron 子窗口）未落地：Web 版降级到 popout
    console.info(
      `[WindowManager] childWindow not supported in web build; degrading to popout for page "${page.id}"`,
    )
    return this.openPopout(page, opts)
  }

  openDesktopWidget(page: PageDeclaration, opts?: OpenWindowOptions): string {
    // P3（桌面小组件）未落地：Web 版降级到 popout
    console.info(
      `[WindowManager] desktopWidget not supported in web build; degrading to popout for page "${page.id}"`,
    )
    return this.openPopout(page, opts)
  }

  close(windowId: string): void {
    useLayoutModeStore.getState().closeFloatingWindow(windowId)
  }

  focus(windowId: string): void {
    useLayoutModeStore.getState().updateFloatingWindow(windowId, { zIndex: nextZIndex() })
  }
}

/**
 * 是否处于 Electron 环境且窗口管理 API 可用。
 *
 * window.electronAPI 由 preload.ts 经 contextBridge 注入，仅在 Electron 渲染
 * 进程中存在；Web 构建下为 undefined。这是 ElectronWindowManager vs
 * WebWindowManager 的唯一选择依据，启动时（模块加载）检测一次。
 */
export function isElectronWindowAvailable(): boolean {
  return (
    typeof window !== 'undefined' &&
    typeof window.electronAPI !== 'undefined' &&
    !!window.electronAPI &&
    !!window.electronAPI.window
  )
}

/**
 * 把 page + opts 合并为 Electron IPC 的 open 参数。
 *
 * 纯函数（无副作用），便于单测；ElectronWindowManager 内部复用。
 *
 * URL 构造：window.location.origin + '/#/p/' + page.id（深链 hash 路由）。
 * 默认值与 main.ts 的 createChildWindow 对齐：width 320 / height 480 / frame false。
 */
function toElectronOpenOptions(
  page: PageDeclaration,
  opts: OpenWindowOptions,
  overrides: Partial<Pick<ElectronOpenWindowOptions, 'alwaysOnTop' | 'skipTaskbar' | 'frame'>> = {},
): ElectronOpenWindowOptions {
  const size = resolveSize(page, opts)
  const position = resolvePosition(size, opts)
  // Vite SPA：构建产物只运行在浏览器/WebView，window 恒存在（无 SSR 分支）
  const origin = window.location.origin

  return {
    // 用 page.id + 计数器作为请求 id（与 main.ts 注册表 key 对齐）；
    // main.ts 在 id 重复时聚焦已有窗口，故即便碰撞也安全
    id: `${page.id}-ewin-${Date.now()}-${++windowSeq}`,
    url: `${origin}/#/p/${page.id}`,
    title: page.title ?? page.id,
    width: size.width,
    height: size.height,
    x: position.x,
    y: position.y,
    // 悬浮组件默认无边框
    frame: false,
    ...overrides,
  }
}

/**
 * Electron 版窗口管理器（P2/P3）
 *
 * 把 openPopout / openChildWindow / openDesktopWidget 全部转发到原生
 * BrowserWindow（通过 ipcRenderer.invoke('window:open')）；close/focus 同理。
 * 不再写入 layoutModeStore.floatingWindows（那是 Web 浮窗的渲染状态）。
 *
 * 防御性降级：若实例化后 electronAPI 被移除（极端场景），自动回退到
 * WebWindowManager 的 popout 路径并打 console.info 提示。
 */
export class ElectronWindowManager implements WindowManager {
  // 内部委托：electronAPI 缺失时降级到 Web 实现（复用 popout 写 floatingWindows）
  private readonly fallback = new WebWindowManager()

  private available(): boolean {
    return isElectronWindowAvailable()
  }

  openPopout(page: PageDeclaration, opts?: OpenWindowOptions): string {
    if (!this.available()) {
      console.info(
        `[WindowManager] electronAPI unavailable; degrading openPopout to web popout for "${page.id}"`,
      )
      return this.fallback.openPopout(page, opts)
    }
    const req = toElectronOpenOptions(page, opts ?? {})
    // fire-and-forget：openChildWindow 的同步契约要求返回 id；
    // 主进程返回 {id,success} 后即便有错也只影响后续 close/focus
    void window.electronAPI!.window.open(req).catch((err) => {
      console.error(`[WindowManager] electronAPI.window.open failed for "${page.id}":`, err)
    })
    return req.id
  }

  openChildWindow(page: PageDeclaration, opts?: OpenWindowOptions): string {
    if (!this.available()) {
      console.info(
        `[WindowManager] electronAPI unavailable; degrading openChildWindow to web popout for "${page.id}"`,
      )
      return this.fallback.openPopout(page, opts)
    }
    // childWindow：常规子窗口；detachable.alwaysOnTop/skipTaskbar 透传，缺省 false
    const req = toElectronOpenOptions(page, opts ?? {}, {
      alwaysOnTop: page.detachable?.alwaysOnTop ?? false,
      skipTaskbar: page.detachable?.skipTaskbar ?? false,
    })
    void window.electronAPI!.window.open(req).catch((err) => {
      console.error(`[WindowManager] electronAPI.window.open failed for "${page.id}":`, err)
    })
    return req.id
  }

  openDesktopWidget(page: PageDeclaration, opts?: OpenWindowOptions): string {
    if (!this.available()) {
      console.info(
        `[WindowManager] electronAPI unavailable; degrading openDesktopWidget to web popout for "${page.id}"`,
      )
      return this.fallback.openPopout(page, opts)
    }
    // desktopWidget：桌面小组件 —— 强制置顶 + 隐藏任务栏 + 无边框
    const req = toElectronOpenOptions(page, opts ?? {}, {
      alwaysOnTop: page.detachable?.alwaysOnTop ?? true,
      skipTaskbar: page.detachable?.skipTaskbar ?? true,
      frame: false,
    })
    void window.electronAPI!.window.open(req).catch((err) => {
      console.error(`[WindowManager] electronAPI.window.open failed for "${page.id}":`, err)
    })
    return req.id
  }

  close(windowId: string): void {
    if (!this.available()) {
      this.fallback.close(windowId)
      return
    }
    void window.electronAPI!.window.close(windowId).catch((err) => {
      console.error('[WindowManager] electronAPI.window.close failed:', err)
    })
  }

  focus(windowId: string): void {
    if (!this.available()) {
      this.fallback.focus(windowId)
      return
    }
    void window.electronAPI!.window.focus(windowId).catch((err) => {
      console.error('[WindowManager] electronAPI.window.focus failed:', err)
    })
  }
}

/**
 * 全局单例（消费方 import 此对象；测试可 new WebWindowManager / new ElectronWindowManager 隔离）。
 *
 * 启动时按 isElectronWindowAvailable() 二选一：Electron 环境用 ElectronWindowManager
 * （真桌面窗口），Web 环境用 WebWindowManager（in-app 浮窗）。
 */
export const windowManager: WindowManager = isElectronWindowAvailable()
  ? new ElectronWindowManager()
  : new WebWindowManager()
