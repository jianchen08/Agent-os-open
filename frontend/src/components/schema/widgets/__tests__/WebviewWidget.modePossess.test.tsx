/** @feature FP-0.2.四 前端Schema | @ci: frontend-test */
/**
 * WebviewWidget · mode.possess 人设接管附身桥路由测试（批 G② 通用化）
 *
 * 白名单宿主侧方法（与 theme.apply 同款 fail-closed 语义）：模式面板附身成功
 * 后上行 {mode, card_id, name, avatar, personaText?} → 注入键从 registry 该模式
 * decl.persona.from 解析（resolvePersonaInjectionKey）：
 * - resolved → personaPossessStore 记档（键随档钉住）+ 回执 result + D8 附身
 *   缓存代价提示（一次建立提示一次）+ localStorage 落键 persona.possessed；
 * - unreachable（registry 不可达）→ 附身建立但 personaKey=null（发送链诚实
 *   降级不注入）；
 * - undeclared（模式未声明 persona）→ 整包拒绝回 error、零状态变更；
 * - 载荷非法（缺 mode/avatar 等）→ 丢弃（零状态变更）并回 error（≥2 组非法
 *   输入）；
 * - 纯宿主行为：不经内核 transport（apiClient 零 POST）。
 */
import { render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { apiClient } from '@/services/api/client'
import { queryClient } from '@/services/query/queryClient'
import { useNotificationStore } from '@/stores/notificationStore'
import { usePersonaPossessStore } from '@/stores/personaPossessStore'
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
  mode: 'roleplay',
  card_id: 'card_luna',
  name: '月见',
  avatar: '🌙',
  personaText: '银发碧眼的月精灵法师，月光神殿的最后一位守望者。',
}

/** registry 合成载荷：roleplay 声明 persona.from；writing 未声明 */
function registryOf(modes: Array<Record<string, unknown>>): void {
  apiGet.mockImplementation((url: string) =>
    url === '/ext/agent_manager/modes'
      ? Promise.resolve({ data: { modes, total: modes.length, errors: [] } })
      : Promise.resolve({ data: '<html><body></body></html>' }),
  )
}

const ROLEPLAY_DECL = {
  mode: 'roleplay',
  name: '角色扮演模式',
  pipelines: [{ name: 'roleplay', context: 'conversation' }],
  presenter: { source: 'data_cards' },
  tool_card: 'collapse',
  persona: { replace: true, from: 'roleplay_persona' },
  plugin_id: 'mode_roleplay',
}
const WRITING_DECL = {
  mode: 'writing',
  name: '写作模式',
  pipelines: [],
  presenter: { source: 'none' },
  tool_card: 'native',
  plugin_id: 'mode_writing',
}

async function renderWidget(): Promise<Mock> {
  render(<WebviewWidget pluginId="demo" widgetId="w1" />)
  await waitFor(() => expect(screen.getByTitle('Webview')).toBeInTheDocument())
  const iframe = screen.getByTitle('Webview') as HTMLIFrameElement
  return vi.spyOn(iframe.contentWindow!, 'postMessage')
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  queryClient.clear()
  usePersonaPossessStore.setState({ possessed: null })
  useNotificationStore.setState({ notifications: [] })
  registryOf([ROLEPLAY_DECL, WRITING_DECL])
})

describe('WebviewWidget — mode.possess 附身桥（注入键 registry 派生）', () => {
  it('persona 已声明 → 注入键钉档 + 回执 result + D8 提示 + 落键 persona.possessed', async () => {
    const downSpy = await renderWidget()

    bridgePostUp('mode.possess', VALID_POSSESS, 'wv_ok')

    await waitFor(() => {
      expect(findDownMessage(downSpy, 'mode.possess.result')).toBeDefined()
    })
    expect(usePersonaPossessStore.getState().possessed).toEqual({
      ...VALID_POSSESS,
      personaKey: 'roleplay_persona',
    })
    // D8 附身侧提示（一次建立提示一次）：接管人设 + 缓存代价口径
    const notified = useNotificationStore.getState().notifications
    expect(notified).toHaveLength(1)
    expect(notified[0].title).toContain('月见')
    expect(notified[0].message).toContain('接管人设')
    expect(notified[0].message).toContain('缓存命中')
    // persist 契约：重启可恢复（键 persona.possessed）
    expect(localStorage.getItem('persona.possessed')).toContain('card_luna')
    expect(apiPost).not.toHaveBeenCalled()
  })

  it('registry 不可达 → 附身建立（personaKey=null 诚实降级不注入）+ 提示照发', async () => {
    apiGet.mockImplementation((url: string) =>
      url === '/ext/agent_manager/modes'
        ? Promise.reject(new Error('registry down'))
        : Promise.resolve({ data: '<html><body></body></html>' }),
    )
    const downSpy = await renderWidget()

    // 独立档案名：通知中心按 title+message 指纹短窗去重，避免与首例撞指纹
    bridgePostUp('mode.possess', { ...VALID_POSSESS, card_id: 'card_mio', name: '澪' }, 'wv_deg')

    await waitFor(() => {
      expect(findDownMessage(downSpy, 'mode.possess.result')).toBeDefined()
    })
    expect(usePersonaPossessStore.getState().possessed).toMatchObject({
      mode: 'roleplay',
      name: '澪',
      personaKey: null,
    })
    expect(useNotificationStore.getState().notifications).toHaveLength(1)
  })

  it('模式未声明 persona（registry 可达）→ 整包拒绝回 error、零状态变更', async () => {
    const downSpy = await renderWidget()

    bridgePostUp('mode.possess', { ...VALID_POSSESS, mode: 'writing' }, 'wv_und')

    await waitFor(() => {
      expect(findDownMessage(downSpy, 'mode.possess.error')).toBeDefined()
    })
    expect(findDownMessage(downSpy, 'mode.possess.result')).toBeUndefined()
    expect(usePersonaPossessStore.getState().possessed).toBeNull()
    expect(useNotificationStore.getState().notifications).toHaveLength(0)
  })

  it('载荷校验失败 → 丢弃（store 零变更）并回 error（≥2 组非法输入）', async () => {
    const downSpy = await renderWidget()

    bridgePostUp('mode.possess', { mode: 'roleplay', card_id: 'card_x', name: '缺 avatar' }, 'wv_bad1')
    bridgePostUp('mode.possess', 'not-an-object', 'wv_bad2')
    bridgePostUp('mode.possess', { card_id: 'card_luna', name: '月见', avatar: '🌙' }, 'wv_bad3')

    await waitFor(() => {
      expect(findDownMessage(downSpy, 'mode.possess.error')).toBeDefined()
    })
    expect(findDownMessage(downSpy, 'mode.possess.result')).toBeUndefined()
    expect(usePersonaPossessStore.getState().possessed).toBeNull()
    expect(useNotificationStore.getState().notifications).toHaveLength(0)
    expect(apiPost).not.toHaveBeenCalled()
  })

  it('白名单方法不外漏到 actions 端点：全程无 REST/ action 外呼', async () => {
    const downSpy = await renderWidget()

    bridgePostUp('mode.possess', VALID_POSSESS)
    await waitFor(() => expect(usePersonaPossessStore.getState().possessed).not.toBeNull())
    expect(apiPost).not.toHaveBeenCalled()
    expect(apiGet.mock.calls.filter((c: unknown[]) => c[0] === '/ext/demo/webview')).toHaveLength(1)
    expect(apiGet.mock.calls.filter((c: unknown[]) => c[0] === '/ext/agent_manager/modes')).toHaveLength(1)
  })
})
