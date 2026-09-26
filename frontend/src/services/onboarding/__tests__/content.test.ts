/** @feature FP-0.2.四 前端Schema | @ci: frontend-test */
/**
 * onboarding 内容/进度数据层（services/onboarding/content.ts）：
 * - fetchOnboardingContent / fetchOnboardingProgress / postProgressUpdate：
 *   extUrl 拼接 + apiClient 透传（响应体 .data 原样返回）
 * - panelPathToTabId：内置面板查 TOP_NAV_PANELS / 插件页查 contributes.pages
 *   / 双双未命中 null（visitedPanels 判定的同源两段解析）
 *
 * 外部依赖 mock（网络传输 apiClient、宿主页签注册表、插件贡献注册表）；
 * extUrl 拼接走真实实现。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Walkthrough } from '@/services/onboarding/types'

const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())

vi.mock('@/services/api/client', () => ({
  default: { get: (...args: unknown[]) => apiGet(...args), post: (...args: unknown[]) => apiPost(...args) },
}))

vi.mock('@/services/workspacePanelOpener', () => ({
  TOP_NAV_PANELS: {
    '/tasks': { id: 'ws-panel-tasks' },
    '/settings': { id: 'ws-panel-settings' },
  },
}))

vi.mock('@/services/schema/ContributionRegistry', () => ({
  contributionRegistry: {
    getPages: vi.fn(() => [
      { id: 'myplug', path: '/ext/myplug/ui' },
      { id: 'pathless', path: '' },
    ]),
  },
}))

import {
  fetchOnboardingContent,
  fetchOnboardingProgress,
  panelPathToTabId,
  postProgressUpdate,
} from '../content'

beforeEach(() => {
  vi.clearAllMocks()
})

describe('数据层：取数与落账（extUrl 拼接 + .data 透传）', () => {
  it('fetchOnboardingContent：GET /ext/onboarding_service/walkthroughs，返回响应体', async () => {
    const walkthroughs = [{ id: 'w1' }] as unknown as Walkthrough[]
    apiGet.mockResolvedValue({ data: { walkthroughs } })
    await expect(fetchOnboardingContent()).resolves.toEqual({ walkthroughs })
    expect(apiGet).toHaveBeenCalledWith('/ext/onboarding_service/walkthroughs')
  })

  it('fetchOnboardingProgress：GET /ext/onboarding_service/progress', async () => {
    apiGet.mockResolvedValue({ data: { progress: { w1: { s1: { done: true, done_at: 1, how: 'manual' } } } } })
    const res = await fetchOnboardingProgress()
    expect(res.progress.w1.s1.done).toBe(true)
    expect(apiGet).toHaveBeenCalledWith('/ext/onboarding_service/progress')
  })

  it('postProgressUpdate：POST 载荷透传，返回最新进度', async () => {
    apiPost.mockResolvedValue({ data: { progress: { w1: { s1: { done: true, done_at: 9, how: 'manual' } } } } })
    const update = { walkthrough_id: 'w1', step_id: 's1', done: true, how: 'manual' } as const
    const res = await postProgressUpdate(update)
    expect(res.progress.w1.s1.how).toBe('manual')
    expect(apiPost).toHaveBeenCalledWith('/ext/onboarding_service/progress', update)
  })
})

describe('panelPathToTabId：面板路径 → 页签 id（双源解析）', () => {
  it('内置面板命中 TOP_NAV_PANELS（/tasks 与 /settings 两组输入）', () => {
    expect(panelPathToTabId('/tasks')).toBe('ws-panel-tasks')
    expect(panelPathToTabId('/settings')).toBe('ws-panel-settings')
  })

  it('插件页按 path 命中 contributes.pages → ws-plugin-<id>', () => {
    expect(panelPathToTabId('/ext/myplug/ui')).toBe('ws-plugin-myplug')
  })

  it('未命中 → null；插件页按 path 严格相等匹配（空串 path 只匹配空串查询）', () => {
    expect(panelPathToTabId('/nowhere')).toBeNull()
    expect(panelPathToTabId('')).toBe('ws-plugin-pathless')
  })
})
