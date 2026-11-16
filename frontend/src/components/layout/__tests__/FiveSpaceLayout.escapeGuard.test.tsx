// @feature FP-0.2.评审引用交互 R4 件②：全屏下点击浮窗输入框浮窗消失 | @ci: frontend-test
/**
 * FiveSpaceLayout Escape 退出全屏 —— 可编辑元素守卫（2026-11-05 R4 件②）。
 *
 * 根因（用户实测：全屏浮窗输入框开始打字 → 浮窗消失）：document 级 keydown
 * 对 Escape 无条件退出 workspaceFullscreen / fullscreenActive overlay，没有
 * 「事件目标在可编辑元素内」守卫。中文输入法在候选激活/取消时会对页面发
 * Escape keydown，落点=正在输入的 textarea → 退出全屏 → 浮窗（门控渲染）消失。
 *
 * 修复口径（VS Code zen mode 同款标准做法）：
 *  - 事件目标（composedPath 兜底）落在 textarea/input/contenteditable 内 →
 *    不执行退出全屏（workspaceFullscreen 与 fullscreenActive overlay 两分支同判）；
 *  - 目标在非编辑区（body/document）→ 既有退出行为不回归；
 *  - mobile 工作区分支不动。
 */

import { act, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useUIStore } from '@/stores/uiStore'
import { FiveSpaceLayout } from '../FiveSpaceLayout'
import { chatContent, ResizeObserverStub, setViewportWidth, sidebarContent } from './helpers/fiveSpaceTestUtils'

vi.mock('@/components/workspace/CodeEditor', () => ({
  CodeEditor: () => <div data-testid="mock-code-editor" />,
}))
vi.mock('@/services/api/costControl', () => ({
  getBudgetStatus: async () => null,
}))
vi.mock('@/services/workspacePanelOpener', () => ({
  openDeclaredPluginPage: vi.fn(() => true),
}))

function renderLayout() {
  return render(
    <MemoryRouter>
      <FiveSpaceLayout chatContent={chatContent} sidebarContent={sidebarContent} />
    </MemoryRouter>,
  )
}

function resetStores() {
  useLayoutModeStore.setState({
    workspaceTabs: [],
    floatingWindows: [],
    fullscreenActive: false,
    fullscreenTitle: null,
    fullscreenContent: null,
    workspaceDataVersion: 0,
    connectionStatus: {
      state: 'connected',
      latencyMs: 5,
      reconnectAttempt: 0,
      lastConnectedAt: null,
      queuedMessages: 0,
    },
    pendingInteractions: [],
  })
  useUIStore.setState({
    sidebarCollapsed: false,
    workspaceCollapsed: false,
    workspaceFullscreen: false,
    sidebarRatio: null,
    workspacePanelRatio: null,
  })
}

/** 往 body 挂一个 textarea（模拟浮窗输入框落点），返回之 */
function attachTextarea(): HTMLTextAreaElement {
  const el = document.createElement('textarea')
  document.body.appendChild(el)
  return el
}

beforeEach(() => {
  globalThis.ResizeObserver = ResizeObserverStub as never
  setViewportWidth(1280)
  resetStores()
})

afterEach(() => {
  document.querySelectorAll('body > textarea[data-escape-guard]').forEach((n) => n.remove())
  vi.restoreAllMocks()
})

describe('Escape 退出全屏的可编辑元素守卫（件②）', () => {
  it('① 事件目标在浮窗 textarea 内发 Escape → workspaceFullscreen 保持 true（输入法候选取消不退全屏）', () => {
    useUIStore.setState({ workspaceFullscreen: true })
    renderLayout()
    const ta = attachTextarea()
    ta.setAttribute('data-escape-guard', '')

    act(() => {
      fireEvent.keyDown(ta, { key: 'Escape' })
    })

    expect(useUIStore.getState().workspaceFullscreen).toBe(true)
  })

  it('② 目标在 body/非编辑区发 Escape → 退出全屏（既有行为不回归）', () => {
    useUIStore.setState({ workspaceFullscreen: true })
    renderLayout()

    act(() => {
      fireEvent.keyDown(document.body, { key: 'Escape' })
    })

    expect(useUIStore.getState().workspaceFullscreen).toBe(false)
  })

  it('③ fullscreenActive overlay 分支同判：目标在 textarea 内 → 覆盖层保持', () => {
    useLayoutModeStore.setState({
      fullscreenActive: true,
      fullscreenTitle: '覆盖层标题',
      fullscreenContent: <div data-testid="fullscreen-body">覆盖层内容</div>,
    })
    renderLayout()
    expect(screen.getByTestId('fullscreen-body')).toBeInTheDocument()
    const ta = attachTextarea()
    ta.setAttribute('data-escape-guard', '')

    act(() => {
      fireEvent.keyDown(ta, { key: 'Escape' })
    })

    expect(useLayoutModeStore.getState().fullscreenActive).toBe(true)
  })

  it('③b overlay 分支既有行为不回归：非编辑区目标 → 覆盖层退出', () => {
    useLayoutModeStore.setState({
      fullscreenActive: true,
      fullscreenTitle: '覆盖层标题',
      fullscreenContent: <div data-testid="fullscreen-body">覆盖层内容</div>,
    })
    renderLayout()
    expect(screen.getByTestId('fullscreen-body')).toBeInTheDocument()

    act(() => {
      fireEvent.keyDown(document.body, { key: 'Escape' })
    })

    expect(useLayoutModeStore.getState().fullscreenActive).toBe(false)
  })

  it('④ input 目标同守卫（文本输入面等价）', () => {
    useUIStore.setState({ workspaceFullscreen: true })
    renderLayout()
    const input = document.createElement('input')
    input.setAttribute('data-escape-guard', '')
    document.body.appendChild(input)

    act(() => {
      fireEvent.keyDown(input, { key: 'Escape' })
    })

    expect(useUIStore.getState().workspaceFullscreen).toBe(true)
  })
})
