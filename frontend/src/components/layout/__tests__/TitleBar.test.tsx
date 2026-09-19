/** @feature FP-T12 前端适配 | @ci: frontend-test */
/** @ci: frontend-test */
/**
 * TitleBar — Electron 主窗口自定义标题栏
 *
 * 断行为不断实现：按钮点击 → windowControls 出站调用；最大化状态
 * （初始查询 + 推送订阅）→ 按钮图标语义；占位类随挂载/卸载增删。
 * 环境判定三分支（Web 无 API / 子浮窗 / 主窗口）经 isDesktopMainWindow 观测。
 */

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { isDesktopMainWindow, TitleBar } from '../TitleBar'
import type { ElectronAPI, ElectronWindowControlsAPI } from '@/types/electron'

type ControlsMock = ElectronWindowControlsAPI & { unsubscribe: ReturnType<typeof vi.fn> }

/** 打桩 windowControls + electronAPI（isChildWindow 可指定），返回调用记录 */
function stubElectronApi(opts: { isChildWindow?: boolean; withControls?: boolean } = {}): ControlsMock {
  const unsubscribe = vi.fn()
  const controls = {
    minimize: vi.fn().mockResolvedValue(undefined),
    toggleMaximize: vi.fn().mockResolvedValue(true),
    close: vi.fn().mockResolvedValue(undefined),
    isMaximized: vi.fn().mockResolvedValue(false),
    onMaximizedChange: vi.fn(() => unsubscribe),
    unsubscribe,
  }
  const api = {
    window: {},
    isChildWindow: opts.isChildWindow ?? false,
    ...(opts.withControls === false ? {} : { windowControls: controls }),
  }
  window.electronAPI = api as unknown as ElectronAPI
  return controls as ControlsMock
}

beforeEach(() => {
  document.documentElement.className = ''
})

afterEach(() => {
  cleanup()
  delete (window as { electronAPI?: unknown }).electronAPI
})

describe('isDesktopMainWindow — 环境判定三分支', () => {
  it('Web 环境（无 electronAPI）→ false', () => {
    expect(isDesktopMainWindow()).toBe(false)
  })

  it('子浮窗（isChildWindow=true）→ false', () => {
    stubElectronApi({ isChildWindow: true })
    expect(isDesktopMainWindow()).toBe(false)
  })

  it('主窗口（有 windowControls 且非子浮窗）→ true', () => {
    stubElectronApi()
    expect(isDesktopMainWindow()).toBe(true)
  })

  it('electronAPI 无 windowControls（旧 preload）→ false', () => {
    stubElectronApi({ withControls: false })
    expect(isDesktopMainWindow()).toBe(false)
  })
})

describe('TitleBar — 渲染与环境门控', () => {
  it('主窗口渲染控制簇（三个窗口控制按钮），并挂占位类', () => {
    stubElectronApi()
    render(<TitleBar />)

    expect(screen.getByTestId('custom-titlebar')).toBeInTheDocument()
    expect(screen.queryByText('AgentOS')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '最小化' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '最大化' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '关闭' })).toBeInTheDocument()
    expect(document.documentElement.classList.contains('has-custom-titlebar')).toBe(true)
  })

  it('Web 环境不渲染，也不挂占位类', () => {
    render(<TitleBar />)
    expect(screen.queryByTestId('custom-titlebar')).not.toBeInTheDocument()
    expect(document.documentElement.classList.contains('has-custom-titlebar')).toBe(false)
  })

  it('子浮窗不渲染', () => {
    stubElectronApi({ isChildWindow: true })
    render(<TitleBar />)
    expect(screen.queryByTestId('custom-titlebar')).not.toBeInTheDocument()
  })
})

describe('TitleBar — 窗口控制出站调用', () => {
  it('点击最小化/最大化/关闭分别触发对应 IPC 封装', () => {
    const controls = stubElectronApi()
    render(<TitleBar />)

    fireEvent.click(screen.getByRole('button', { name: '最小化' }))
    fireEvent.click(screen.getByRole('button', { name: '最大化' }))
    fireEvent.click(screen.getByRole('button', { name: '关闭' }))

    expect(controls.minimize).toHaveBeenCalledTimes(1)
    expect(controls.toggleMaximize).toHaveBeenCalledTimes(1)
    expect(controls.close).toHaveBeenCalledTimes(1)
  })
})

describe('TitleBar — 最大化状态与图标语义', () => {
  it('初始已最大化（isMaximized 查询为 true）→ 按钮语义为「还原」', async () => {
    const controls = stubElectronApi()
    controls.isMaximized.mockResolvedValue(true)
    render(<TitleBar />)

    await waitFor(() => expect(screen.getByRole('button', { name: '还原' })).toBeInTheDocument())
  })

  it('isMaximized 查询失败 → 记录错误日志，标题栏仍按初始态渲染', async () => {
    const controls = stubElectronApi()
    controls.isMaximized.mockRejectedValue(new Error('ipc down'))
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    render(<TitleBar />)

    await waitFor(() => expect(errSpy).toHaveBeenCalledWith('[TitleBar] 查询最大化状态失败:', expect.any(Error)))
    expect(screen.getByRole('button', { name: '最大化' })).toBeInTheDocument()
    errSpy.mockRestore()
  })

  it('推送事件切换状态：true→「还原」，false→「最大化」；卸载时退订', async () => {
    const controls = stubElectronApi()
    let push: ((maximized: boolean) => void) | undefined
    controls.onMaximizedChange.mockImplementation((cb: (m: boolean) => void) => {
      push = cb
      return controls.unsubscribe
    })
    const { unmount } = render(<TitleBar />)

    await waitFor(() => expect(push).toBeDefined())
    act(() => push?.(true))
    expect(screen.getByRole('button', { name: '还原' })).toBeInTheDocument()
    act(() => push?.(false))
    expect(screen.getByRole('button', { name: '最大化' })).toBeInTheDocument()

    unmount()
    expect(controls.unsubscribe).toHaveBeenCalledTimes(1)
    expect(document.documentElement.classList.contains('has-custom-titlebar')).toBe(false)
  })
})
