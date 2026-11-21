/** @feature: FP-RP面板UX收敛 | @ci: frontend-test */
/**
 * WebviewWidget · workspace.fullscreen 工作区全屏桥路由测试
 *
 * 白名单宿主侧方法（与 theme.apply/mode.possess 同族）：插件页面进入游玩态时
 * 请求工作区全屏（酒馆玩法页「新开一局」/进局）——
 * - {on:true} / 缺省载荷 → uiStore.workspaceFullscreen=true + 回执 applied
 * - {on:false} → 退出全屏
 * - 非法载荷（on 非 boolean）→ fail-closed 回 error，store 零变更
 *
 * 外部依赖 mock：apiClient（HTML 取数）；uiStore 用真实 zustand store。
 */
import { render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { apiClient } from '@/services/api/client'
import { useUIStore } from '@/stores/uiStore'
import { findDownMessage, postUp as bridgePostUp } from './webviewTestBridge'
import type { Mock } from 'vitest'

vi.mock('@/services/api/client', () => ({
  apiClient: { get: vi.fn(), post: vi.fn() },
}))

import { WebviewWidget } from '../WebviewWidget'

beforeEach(() => {
  vi.clearAllMocks()
  useUIStore.setState({ workspaceFullscreen: false })
  ;(apiClient.get as unknown as Mock).mockResolvedValue({ data: '<html><body></body></html>' })
})

async function renderWidget(): Promise<Mock> {
  render(<WebviewWidget pluginId="demo" widgetId="w1" />)
  await waitFor(() => expect(screen.getByTitle('Webview')).toBeInTheDocument())
  const iframe = screen.getByTitle('Webview') as HTMLIFrameElement
  return vi.spyOn(iframe.contentWindow!, 'postMessage')
}

describe('WebviewWidget — workspace.fullscreen 工作区全屏桥', () => {
  it('{on:true} → 进入全屏 + 回执 applied', async () => {
    const downSpy = await renderWidget()
    bridgePostUp('workspace.fullscreen', { on: true }, 'wv_fs1')
    await waitFor(() => {
      expect(findDownMessage(downSpy, 'workspace.fullscreen.result')).toBeDefined()
    })
    expect(useUIStore.getState().workspaceFullscreen).toBe(true)
  })

  it('缺省载荷 = 进全屏（进局主形态零参数即可）', async () => {
    const downSpy = await renderWidget()
    bridgePostUp('workspace.fullscreen', {}, 'wv_fs2')
    await waitFor(() => {
      expect(findDownMessage(downSpy, 'workspace.fullscreen.result')).toBeDefined()
    })
    expect(useUIStore.getState().workspaceFullscreen).toBe(true)
  })

  it('{on:false} → 退出全屏', async () => {
    useUIStore.setState({ workspaceFullscreen: true })
    const downSpy = await renderWidget()
    bridgePostUp('workspace.fullscreen', { on: false }, 'wv_fs3')
    await waitFor(() => {
      expect(findDownMessage(downSpy, 'workspace.fullscreen.result')).toBeDefined()
    })
    expect(useUIStore.getState().workspaceFullscreen).toBe(false)
  })

  it('非法载荷（on 非 boolean）→ fail-closed 回 error，store 零变更', async () => {
    const downSpy = await renderWidget()
    bridgePostUp('workspace.fullscreen', { on: 'yes' }, 'wv_fs4')
    await waitFor(() => {
      expect(findDownMessage(downSpy, 'workspace.fullscreen.error')).toBeDefined()
    })
    expect(useUIStore.getState().workspaceFullscreen).toBe(false)
  })
})
