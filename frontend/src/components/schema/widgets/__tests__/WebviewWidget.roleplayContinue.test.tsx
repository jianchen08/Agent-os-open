/** @feature FP-0.2.四 前端Schema | @ci: frontend-test */
/**
 * WebviewWidget · roleplay.continue 转续演/会话化开演桥路由测试
 *
 * 白名单宿主侧方法：面板以某卡开扮演会话 → 建会话 + 会话执行选项绑定卡身份
 * + 可选开演档（greeting/personaText 随快照键生效）——
 * - 合法载荷（含开演档）→ 建会话载荷四键齐备 + 回执 result（含 session_id）
 * - 转续演缺省（无开演档）→ 载荷仅 card_id/name 两键
 * - 载荷校验失败（fail-closed）→ 丢弃回 error、建会话零发生（≥2 组非法输入）
 *
 * 外部依赖 mock：apiClient（HTML 取数）、sessionStore（宿主会话态）、
 * roleplayContinue 的 createRoleplaySession（会话创建编排 = WS/store 编排面，
 * parseRoleplayContinuePayload 纯校验走真实实现）。
 */
import { render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { apiClient } from '@/services/api/client'
import { findDownMessage, postUp as bridgePostUp } from './webviewTestBridge'
import type { Mock } from 'vitest'

const createRoleplaySession = vi.hoisted(() => vi.fn())
vi.mock('@/services/api/client', () => ({
  apiClient: { get: vi.fn(), post: vi.fn() },
}))
vi.mock('@/services/roleplayContinue', async (importOriginal) => ({
  ...(await importOriginal<object>()),
  createRoleplaySession: (...args: unknown[]) => createRoleplaySession(...args),
}))
vi.mock('@/stores/sessionStore', async () => {
  const { create } = await import('zustand')
  return {
    useSessionStore: create<{ activeSessionId: string | null }>(() => ({
      activeSessionId: 'sess-continue',
    })),
  }
})

import { WebviewWidget } from '../WebviewWidget'

beforeEach(() => {
  vi.clearAllMocks()
  createRoleplaySession.mockResolvedValue({
    sessionId: 'sess-rp-1',
    agentId: 'mode_roleplay/card_luna',
    name: '月见',
  })
  ;(apiClient.get as unknown as Mock).mockResolvedValue({ data: '<html><body></body></html>' })
})

async function renderWidget(): Promise<Mock> {
  render(<WebviewWidget pluginId="demo" widgetId="w1" />)
  await waitFor(() => expect(screen.getByTitle('Webview')).toBeInTheDocument())
  const iframe = screen.getByTitle('Webview') as HTMLIFrameElement
  return vi.spyOn(iframe.contentWindow!, 'postMessage')
}

describe('WebviewWidget — roleplay.continue 会话化开演桥', () => {
  it('开演档全量载荷：card_id/name/greeting/personaText 四键随快照键建会话 + 回执 session_id', async () => {
    const downSpy = await renderWidget()

    bridgePostUp(
      'roleplay.continue',
      { card_id: 'card_luna', name: '月见', greeting: '夜色降临……', personaText: '你是月见。' },
      'wv_rc1',
    )

    await waitFor(() => {
      expect(findDownMessage(downSpy, 'roleplay.continue.result')).toBeDefined()
    })
    // 开演档随载荷整体下传（快照键由服务层落执行选项）
    expect(createRoleplaySession).toHaveBeenCalledWith({
      card_id: 'card_luna',
      name: '月见',
      greeting: '夜色降临……',
      personaText: '你是月见。',
    })
    const down = findDownMessage(downSpy, 'roleplay.continue.result')!
    expect(down.params).toEqual({ applied: true, session_id: 'sess-rp-1' })
  })

  it('转续演缺省载荷：仅 card_id/name 两键（开演档缺席不伪造）', async () => {
    const downSpy = await renderWidget()

    bridgePostUp('roleplay.continue', { card_id: 'card_rin', name: '凛' }, 'wv_rc2')

    await waitFor(() => {
      expect(findDownMessage(downSpy, 'roleplay.continue.result')).toBeDefined()
    })
    expect(createRoleplaySession).toHaveBeenCalledWith({ card_id: 'card_rin', name: '凛' })
  })

  it('载荷校验失败 fail-closed：零建会话并回执 error（空 name/非对象/空 greeting 三组非法输入）', async () => {
    const downSpy = await renderWidget()

    bridgePostUp('roleplay.continue', { card_id: 'card_x', name: '   ' }, 'wv_bad1')
    bridgePostUp('roleplay.continue', 'not-an-object', 'wv_bad2')
    bridgePostUp('roleplay.continue', { card_id: 'card_y', name: '月见', greeting: '' }, 'wv_bad3')

    await waitFor(() => {
      expect(findDownMessage(downSpy, 'roleplay.continue.error')).toBeDefined()
    })
    expect(findDownMessage(downSpy, 'roleplay.continue.result')).toBeUndefined()
    expect(createRoleplaySession).not.toHaveBeenCalled()
  })
})
