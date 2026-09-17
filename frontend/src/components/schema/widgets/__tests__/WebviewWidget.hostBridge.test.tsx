/** @feature: FP-0.2.四 前端 Schema | @ci: frontend-test */
/**
 * WebviewWidget · 宿主→iframe 下行桥（面板-宿主融合协议）
 *
 * 协议（下行消息信封同 buildWebviewMessage，不经 validateWebviewEvent——那是上行校验）：
 * - theme.sync：{ __agentos_webview:true, method:'theme.sync', params:{tokens} }，
 *   tokens 固定 12 键（--ag-*）；就绪时推当前生效主题，主题变化重推
 * - ctx.sync：{ method:'ctx.sync', params:{sessionId} }；就绪时推当前活跃会话，
 *   会话切换重推
 * - bootstrapJs 预置 window.agentos.ctx = { sessionId: 挂载时活跃会话 }
 *
 * 生效主题口径：会话 override 栈顶 > 全局 themeConfig（base 配置 + 插件皮肤变量 overlay）。
 *
 * 时序说明：jsdom 对 srcdoc iframe 异步派发 load，与测试内手动 __ready 都能把
 * webviewReady 置位——spy 安装前的就绪推送不可捕获。故 helper 在 spy 安装后用
 * 「新身份状态变更」强制一次确定性重推，断言一律基于 spy 捕获到的消息（≥1 + at(-1)），
 * 不依赖初始推送是否被捕获。
 */
import { act, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { presetThemes } from '@/config/themes'
import { apiClient } from '@/services/api/client'
import { useSessionStore } from '@/stores/sessionStore'
import { useSessionThemeStore } from '@/stores/sessionThemeStore'
import { useThemeStore } from '@/stores/themeStore'
import { WebviewWidget } from '../WebviewWidget'
import { allDownByMethod, postUp as bridgePostUp } from './webviewTestBridge'
import type { ThemeConfig } from '@/types/theme'
import type { Mock } from 'vitest'

vi.mock('@/services/api/client', () => ({
  apiClient: { get: vi.fn(), post: vi.fn() },
}))
// 下行桥订阅活跃会话：mock 用真 zustand store（hook + setState 双能力）
vi.mock('@/stores/sessionStore', async () => {
  const { create } = await import('zustand')
  return {
    useSessionStore: create<{ activeSessionId: string | null }>(() => ({
      activeSessionId: 'sess-a',
    })),
  }
})
vi.mock('@/stores/themeStore', async () => {
  const { create } = await import('zustand')
  const { presetThemes: themes } = await import('@/config/themes')
  return {
    useThemeStore: create(() => ({
      themeConfig: themes['dark'] as ThemeConfig,
      activePluginTheme: null,
    })),
  }
})

const DARK = presetThemes['dark']
const LIGHT = presetThemes['light']

/** 下行桥协议固定 12 键 */
const TOKEN_KEYS = [
  '--ag-bg',
  '--ag-fg',
  '--ag-muted',
  '--ag-border',
  '--ag-card',
  '--ag-accent',
  '--ag-accent-soft',
  '--ag-chip',
  '--ag-ok',
  '--ag-warn',
  '--ag-err',
  '--ag-radius',
]

function postUp(method: string, params?: unknown, id = 'wv_hb'): void {
  bridgePostUp(method, params, id, { wrapAct: true })
}

function tokensOf(msg: Record<string, unknown>): Record<string, string> {
  return (msg.params as { tokens: Record<string, string> }).tokens
}

async function renderReady(): Promise<Mock> {
  render(<WebviewWidget pluginId="demo" widgetId="w-hb" />)
  await waitFor(() => expect(screen.getByTitle('Webview')).toBeInTheDocument())
  const iframe = screen.getByTitle('Webview') as HTMLIFrameElement
  const downSpy = vi.spyOn(iframe.contentWindow!, 'postMessage')
  postUp('__ready')
  // 强制确定性重推：jsdom 的 iframe load 与手动 __ready 都可能已把 webviewReady
  // 置位（推送落在 spy 之前），这里用新身份状态变更保证两条下行通道各重推一次。
  // theme.sync：themeConfig 克隆出新对象身份；ctx.sync：会话 id 两段提交
  //（单 act 内两次 setState 会被 React 批处理成一次提交，终值不变则 effect 不重跑）。
  act(() => {
    useThemeStore.setState({ themeConfig: { ...useThemeStore.getState().themeConfig! } })
  })
  act(() => {
    useSessionStore.setState({ activeSessionId: 'tmp-bounce' })
  })
  act(() => {
    useSessionStore.setState({ activeSessionId: 'sess-a' })
  })
  await waitFor(() => expect(allDownByMethod(downSpy, 'theme.sync').length).toBeGreaterThanOrEqual(1))
  await waitFor(() => expect(allDownByMethod(downSpy, 'ctx.sync').length).toBeGreaterThanOrEqual(1))
  return downSpy
}

beforeEach(() => {
  vi.clearAllMocks()
  useSessionThemeStore.setState({ stacks: {} })
  // mock store 状态跨用例留存：每例归零，保证用例顺序无关
  useThemeStore.setState({ themeConfig: DARK, activePluginTheme: null })
  useSessionStore.setState({ activeSessionId: 'sess-a' })
  document.documentElement.style.removeProperty('--bg-main')
  vi.mocked(apiClient.get).mockResolvedValue({ data: '<html><body></body></html>' })
})

describe('WebviewWidget — 宿主下行桥（面板-宿主融合协议）', () => {
  it('bootstrapJs 预置 agentos.ctx（挂载时活跃会话）', async () => {
    render(<WebviewWidget pluginId="demo" widgetId="w-hb" />)
    await waitFor(() => expect(screen.getByTitle('Webview')).toBeInTheDocument())
    const srcDoc = (screen.getByTitle('Webview') as HTMLIFrameElement).getAttribute('srcdoc') ?? ''
    expect(srcDoc).toMatch(/window\.agentos = \{ postMessage: post, ctx: \{ sessionId: "sess-a" \} \}/)
  })

  it('就绪即推初值：theme.sync 固定 12 键全非空且取生效主题；ctx.sync 推当前会话', async () => {
    const downSpy = await renderReady()

    const themeMsg = allDownByMethod(downSpy, 'theme.sync').at(-1)!
    const tokens = tokensOf(themeMsg)
    expect(Object.keys(tokens).sort()).toEqual([...TOKEN_KEYS].sort())
    for (const key of TOKEN_KEYS) {
      expect(typeof tokens[key]).toBe('string')
      expect(tokens[key].length).toBeGreaterThan(0)
    }
    // 生效主题（全局 dark）声明值：同主题重复推送值不变（幂等性质）
    expect(tokens['--ag-bg']).toBe(DARK.colors.background.main)
    expect(tokens['--ag-fg']).toBe(DARK.colors.text.primary)
    expect(tokens['--ag-radius']).toBe(DARK.components.borderRadius.md)

    const ctxMsg = allDownByMethod(downSpy, 'ctx.sync').at(-1)!
    expect(ctxMsg.params).toEqual({ sessionId: 'sess-a' })
    expect(themeMsg.__agentos_webview).toBe(true)
  })

  it('全局主题切换 → theme.sync 重推，token 随生效主题变化（dark/light 两组有区分度）', async () => {
    const downSpy = await renderReady()
    const beforeCount = allDownByMethod(downSpy, 'theme.sync').length

    act(() => {
      useThemeStore.setState({ themeConfig: LIGHT })
    })

    await waitFor(() =>
      expect(allDownByMethod(downSpy, 'theme.sync').length).toBeGreaterThan(beforeCount),
    )
    const before = tokensOf(allDownByMethod(downSpy, 'theme.sync')[0])
    const after = tokensOf(allDownByMethod(downSpy, 'theme.sync').at(-1)!)
    // 键集固定不变，值随生效主题切换（dark→light 对照组）
    expect(Object.keys(after).sort()).toEqual(Object.keys(before).sort())
    expect(after['--ag-bg']).toBe(LIGHT.colors.background.main)
    expect(after['--ag-fg']).toBe(LIGHT.colors.text.primary)
    expect(after['--ag-bg']).not.toBe(before['--ag-bg'])
    expect(after['--ag-fg']).not.toBe(before['--ag-fg'])
  })

  it('会话 override 栈顶生效 → theme.sync 跟随会话档；清栈回落全局主题', async () => {
    const downSpy = await renderReady()

    act(() => {
      useSessionThemeStore.setState({ stacks: { 'sess-a': [LIGHT] } })
    })
    await waitFor(() => expect(allDownByMethod(downSpy, 'theme.sync').length).toBeGreaterThanOrEqual(2))
    expect(tokensOf(allDownByMethod(downSpy, 'theme.sync').at(-1)!)).toMatchObject({
      '--ag-bg': LIGHT.colors.background.main,
    })

    act(() => {
      useSessionThemeStore.setState({ stacks: {} })
    })
    await waitFor(() => expect(allDownByMethod(downSpy, 'theme.sync').length).toBeGreaterThanOrEqual(3))
    expect(tokensOf(allDownByMethod(downSpy, 'theme.sync').at(-1)!)).toMatchObject({
      '--ag-bg': DARK.colors.background.main,
    })
  })

  it('插件主题变量 overlay：声明 token 键覆盖 base 值，未声明键保持 base', async () => {
    const downSpy = await renderReady()

    act(() => {
      useThemeStore.setState({
        themeConfig: DARK,
        activePluginTheme: {
          id: 'skin-x',
          name: 'Skin X',
          base: 'dark',
          pluginId: 'p1',
          variables: { '--ag-accent': '#123456' },
        },
      })
    })

    await waitFor(() => expect(allDownByMethod(downSpy, 'theme.sync').length).toBeGreaterThanOrEqual(2))
    expect(tokensOf(allDownByMethod(downSpy, 'theme.sync').at(-1)!)).toMatchObject({
      '--ag-accent': '#123456',
      '--ag-bg': DARK.colors.background.main,
    })
  })

  it('活跃会话切换 → ctx.sync 重推新会话；无活跃会话推空串', async () => {
    const downSpy = await renderReady()
    expect(
      (allDownByMethod(downSpy, 'ctx.sync').at(-1)!.params as { sessionId: string }).sessionId,
    ).toBe('sess-a')

    act(() => {
      useSessionStore.setState({ activeSessionId: 'sess-b' })
    })
    await waitFor(() => {
      expect(
        (allDownByMethod(downSpy, 'ctx.sync').at(-1)!.params as { sessionId: string }).sessionId,
      ).toBe('sess-b')
    })

    act(() => {
      useSessionStore.setState({ activeSessionId: null })
    })
    await waitFor(() => {
      expect(
        (allDownByMethod(downSpy, 'ctx.sync').at(-1)!.params as { sessionId: string }).sessionId,
      ).toBe('')
    })
  })

  it('配置缺省键回退宿主实际 CSS 变量值（documentElement 已落地值）', async () => {
    document.documentElement.style.setProperty('--bg-main', '#010203')
    const downSpy = await renderReady()

    act(() => {
      useThemeStore.setState({ themeConfig: null, activePluginTheme: null })
    })

    await waitFor(() => expect(allDownByMethod(downSpy, 'theme.sync').length).toBeGreaterThanOrEqual(2))
    expect(tokensOf(allDownByMethod(downSpy, 'theme.sync').at(-1)!)).toMatchObject({
      '--ag-bg': '#010203',
    })
  })
})
