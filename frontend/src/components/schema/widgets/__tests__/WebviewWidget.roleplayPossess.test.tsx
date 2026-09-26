/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * WebviewWidget · roleplay.possess 附身桥路由测试
 *
 * 白名单宿主侧方法（与 theme.apply 同款 fail-closed 语义）：面板附身卡成功后
 * 上行 {card_id, name, avatar, personaText?} → 写入 roleplayPossessStore
 * （persist 可恢复；personaText 两形态——携带原样入档 / 缺席容缺空串）——
 * - 合法载荷 → store 记档 + 回执 result + localStorage 落键
 * - 载荷校验失败 → 丢弃（零状态变更）并回执 error（≥2 组非法输入）
 * - 纯宿主行为：不经内核 transport（apiClient 零 POST）
 */
import { render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { apiClient } from '@/services/api/client'
import { useRoleplayPossessStore } from '@/stores/roleplayPossessStore'
import {
  findDownMessage,
  postUp as bridgePostUp,
} from './webviewTestBridge'
import type { Mock } from 'vitest'

vi.mock('@/services/api/client', () => ({
  apiClient: { get: vi.fn(), post: vi.fn() },
}))
// 下行桥（ctx.sync）需订阅活跃会话：mock 用真 zustand store（hook + getState
// 双能力），替代纯对象 getState——附身桥本身不依赖会话，全局态即可。
vi.mock('@/stores/sessionStore', async () => {
  const { create } = await import('zustand')
  return {
    useSessionStore: create<{ activeSessionId: string | null }>(() => ({
      activeSessionId: 'sess-possess',
    })),
  }
})

import { WebviewWidget } from '../WebviewWidget'

const apiGet = apiClient.get as unknown as Mock
const apiPost = apiClient.post as unknown as Mock

const VALID_POSSESS = {
  card_id: 'card_luna', name: '月见', avatar: '🌙',
  personaText: '银发碧眼的月精灵法师，月光神殿的最后一位守望者。',
}

describe('WebviewWidget — roleplay.possess 附身桥', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    useRoleplayPossessStore.setState({ possessed: null })
    apiGet.mockResolvedValue({ data: '<html><body></body></html>' })
  })

  it('合法载荷 → store 记档 + 回执 result + localStorage 落 roleplay.possessed', async () => {
    render(<WebviewWidget pluginId="demo" widgetId="w1" />)
    await waitFor(() => expect(screen.getByTitle('Webview')).toBeInTheDocument())
    const iframe = screen.getByTitle('Webview') as HTMLIFrameElement
    const downSpy = vi.spyOn(iframe.contentWindow!, 'postMessage')

    bridgePostUp('roleplay.possess', VALID_POSSESS, 'wv_ok')

    await waitFor(() => {
      expect(findDownMessage(downSpy, 'roleplay.possess.result')).toBeDefined()
    })
    expect(useRoleplayPossessStore.getState().possessed).toEqual(VALID_POSSESS)
    // persist 契约：重启可恢复（键 roleplay.possessed）
    expect(localStorage.getItem('roleplay.possessed')).toContain('card_luna')
    expect(apiPost).not.toHaveBeenCalled()
  })

  it('personaText 载荷两形态：携带 → 原样入档；缺席 → 容缺空串（整包不拒绝）', async () => {
    render(<WebviewWidget pluginId="demo" widgetId="w1" />)
    await waitFor(() => expect(screen.getByTitle('Webview')).toBeInTheDocument())

    bridgePostUp('roleplay.possess', VALID_POSSESS, 'wv_p1')
    await waitFor(() => {
      expect(useRoleplayPossessStore.getState().possessed?.personaText).toBe(VALID_POSSESS.personaText)
    })

    bridgePostUp('roleplay.possess', { card_id: 'card_rin', name: '凛', avatar: '🎭' }, 'wv_p2')
    await waitFor(() => {
      expect(useRoleplayPossessStore.getState().possessed?.card_id).toBe('card_rin')
    })
    expect(useRoleplayPossessStore.getState().possessed).toMatchObject({
      card_id: 'card_rin', name: '凛', avatar: '🎭', personaText: '',
    })
  })

  it('载荷校验失败 → 丢弃（store 零变更）并回执 error（≥2 组非法输入）', async () => {
    render(<WebviewWidget pluginId="demo" widgetId="w1" />)
    await waitFor(() => expect(screen.getByTitle('Webview')).toBeInTheDocument())
    const iframe = screen.getByTitle('Webview') as HTMLIFrameElement
    const downSpy = vi.spyOn(iframe.contentWindow!, 'postMessage')

    bridgePostUp('roleplay.possess', { card_id: 'card_x', name: '缺 avatar' }, 'wv_bad1')
    bridgePostUp('roleplay.possess', 'not-an-object', 'wv_bad2')
    bridgePostUp('roleplay.possess', { card_id: '', name: '月见', avatar: '🌙' }, 'wv_bad3')

    await waitFor(() => {
      expect(findDownMessage(downSpy, 'roleplay.possess.error')).toBeDefined()
    })
    expect(findDownMessage(downSpy, 'roleplay.possess.result')).toBeUndefined()
    expect(useRoleplayPossessStore.getState().possessed).toBeNull()
    expect(apiPost).not.toHaveBeenCalled()
  })

  it('白名单方法不外漏到 actions 端点：全程无 REST/ action 外呼', async () => {
    render(<WebviewWidget pluginId="demo" widgetId="w1" />)
    await waitFor(() => expect(screen.getByTitle('Webview')).toBeInTheDocument())

    bridgePostUp('roleplay.possess', VALID_POSSESS)
    await waitFor(() => expect(useRoleplayPossessStore.getState().possessed).not.toBeNull())
    expect(apiPost).not.toHaveBeenCalled()
    expect(apiGet.mock.calls.filter((c: unknown[]) => c[0] !== '/ext/demo/webview')).toHaveLength(0)
  })
})
