/** @feature FP-0.2.四 前端Schema | @ci: frontend-test */
/**
 * WebviewWidget · mode.session 模式会话出生桥路由测试（批 G② 通用化）
 *
 * 白名单宿主侧方法：模式面板以某模式开新会话（modeSessionBinder 出生通道：
 * 建会话 + modeBinding 快照 + 扩展位 + 跳转）——
 * - 合法载荷（含扩展位）→ 出生调用透传扩展（agentId/agentName/title/
 *   extraContext/firstMessage）+ 回执 result（含 session_id）
 * - 转续演缺省（仅 mode/agent_id/name/title）→ 扩展位缺省不伪造
 * - 载荷校验失败（fail-closed）→ 丢弃回 error、出生零发生（≥3 组非法输入）
 *
 * 外部依赖 mock：apiClient（HTML 取数）、sessionStore（宿主会话态）、
 * modeSessionBinder 的 openModeSession（会话创建编排 = WS/store 编排面，
 * parseModeSessionPayload 纯校验走真实实现）。
 */
import { render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { apiClient } from '@/services/api/client'
import { findDownMessage, postUp as bridgePostUp } from './webviewTestBridge'
import type { Mock } from 'vitest'

const openModeSession = vi.hoisted(() => vi.fn())
vi.mock('@/services/api/client', () => ({
  apiClient: { get: vi.fn(), post: vi.fn() },
}))
vi.mock('@/services/modeSessionBinder', async (importOriginal) => ({
  ...(await importOriginal<object>()),
  openModeSession: (...args: unknown[]) => openModeSession(...args),
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
  openModeSession.mockResolvedValue({
    sessionId: 'sess-rp-1',
    modeBinding: { mode: 'roleplay', pipelineConfigId: 'roleplay' },
  })
  ;(apiClient.get as unknown as Mock).mockResolvedValue({ data: '<html><body></body></html>' })
})

async function renderWidget(): Promise<Mock> {
  render(<WebviewWidget pluginId="demo" widgetId="w1" />)
  await waitFor(() => expect(screen.getByTitle('Webview')).toBeInTheDocument())
  const iframe = screen.getByTitle('Webview') as HTMLIFrameElement
  return vi.spyOn(iframe.contentWindow!, 'postMessage')
}

describe('WebviewWidget — mode.session 模式会话出生桥', () => {
  it('开演档全量载荷：扩展位四件随出生通道下传 + 回执 session_id', async () => {
    const downSpy = await renderWidget()

    bridgePostUp(
      'mode.session',
      {
        mode: 'roleplay',
        agent_id: 'mode_roleplay/card_luna',
        name: '月见',
        title: '扮演·月见',
        extra_context: { roleplay_greeting: '夜色降临……', roleplay_user_persona: '你是月见。' },
        first_message: '（开始）',
      },
      'wv_rc1',
    )

    await waitFor(() => {
      expect(findDownMessage(downSpy, 'mode.session.result')).toBeDefined()
    })
    // 扩展位整体透传出生通道（快照键由 modeSessionBinder 落执行选项）
    expect(openModeSession).toHaveBeenCalledWith('roleplay', {
      agentId: 'mode_roleplay/card_luna',
      agentName: '月见',
      title: '扮演·月见',
      extraContext: { roleplay_greeting: '夜色降临……', roleplay_user_persona: '你是月见。' },
      firstMessage: '（开始）',
    })
    const down = findDownMessage(downSpy, 'mode.session.result')!
    expect(down.params).toEqual({ applied: true, session_id: 'sess-rp-1' })
  })

  it('转续演缺省载荷：仅 mode/agent_id/name/title（扩展位缺席不伪造）', async () => {
    const downSpy = await renderWidget()

    bridgePostUp(
      'mode.session',
      { mode: 'roleplay', agent_id: 'mode_roleplay/card_rin', name: '凛', title: '扮演·凛' },
      'wv_rc2',
    )

    await waitFor(() => {
      expect(findDownMessage(downSpy, 'mode.session.result')).toBeDefined()
    })
    expect(openModeSession).toHaveBeenCalledWith('roleplay', {
      agentId: 'mode_roleplay/card_rin',
      agentName: '凛',
      title: '扮演·凛',
      extraContext: undefined,
      firstMessage: undefined,
    })
  })

  it('载荷校验失败 fail-closed：零出生并回执 error（非法 mode/agent 无 name/空开场白键三组非法输入）', async () => {
    const downSpy = await renderWidget()

    bridgePostUp('mode.session', { mode: 'Roleplay', agent_id: 'mode_roleplay/x', name: 'x' }, 'wv_bad1')
    bridgePostUp('mode.session', { mode: 'roleplay', agent_id: 'mode_roleplay/card_x' }, 'wv_bad2')
    bridgePostUp(
      'mode.session',
      { mode: 'roleplay', agent_id: 'mode_roleplay/card_y', name: '月见', extra_context: { roleplay_greeting: '' } },
      'wv_bad3',
    )

    await waitFor(() => {
      expect(findDownMessage(downSpy, 'mode.session.error')).toBeDefined()
    })
    expect(findDownMessage(downSpy, 'mode.session.result')).toBeUndefined()
    expect(openModeSession).not.toHaveBeenCalled()
  })
})
