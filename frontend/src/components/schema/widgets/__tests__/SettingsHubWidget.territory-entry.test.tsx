// @feature FP-0.2.多端同步 G17-MD1 后继 | @ci: frontend-test
/**
 * SettingsHubWidget — 地盘入口整链（G17-MD1 契约的收敛后形态）：
 *
 * 「我的地盘」独立页已按用户拍板收敛单页（a80b59499，连接/同步/我的地盘三合一；
 * 合并改造后唯一入口=「后端连接」设置面板，设备配对与多端同步为页内区块），
 * G17-MD1 的用户可见契约随之迁形态：设置中枢左导航可见「后端连接」入口 →
 * 点击进入真面板页（非 stub）→ 地盘面（territory-list + 注册表设备卡「看地盘」）可达。
 *
 * 本套件是整链守卫：kernel-pages 套件把 BackendConnectionPage mock 成 stub
 * （只保导航注册），面板套件脱离 hub 直渲染页面——两环各自在位但链上无单点断言。
 * 若收敛再迁移（导航项更名/删除或页面地盘面移位）而两环测试各自随迁，本链测试即红。
 * 仅 mock 外部依赖（schema 端点/HTTP client/探活 fetch），页面走真组件。
 */
import { fireEvent, screen, waitFor } from '@testing-library/react'
import React from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { renderWithProviders } from '@/test/renderWithProviders'

const mockGetSchema = vi.hoisted(() => vi.fn().mockResolvedValue({}))
vi.mock('@/services/api/schema', () => ({
  getSchema: mockGetSchema,
}))

const apiMock = vi.hoisted(() => ({
  get: vi.fn(),
}))
vi.mock('@/services/api/client', () => ({ default: apiMock }))

const usePluginsQueryMock = vi.hoisted(() => vi.fn())
vi.mock('@/hooks/queries/usePluginsQuery', () => ({
  usePluginsQuery: (...args: unknown[]) => usePluginsQueryMock(...args),
}))

const fetchMock = vi.hoisted(() => vi.fn())
vi.stubGlobal('fetch', fetchMock)

vi.mock('@/components/config/PluginConfigEditor', () => ({
  PluginConfigEditor: () => <div data-testid="plugin-config-editor" />,
}))

function resolveOk(value: unknown): Promise<{ data: unknown }> {
  return Promise.resolve({ data: value })
}

/** 本机已配对（desktop-a）+ 注册表含他端 phone-b（在线）→ phone-b 是唯一他端卡 */
function mockChainGets() {
  apiMock.get.mockImplementation((url: string) => {
    if (url.endsWith('/ext/sync_agent/relay/devices')) {
      return resolveOk({
        devices: [
          { device_id: 'desktop-a', tier: 'desktop', online: true, app_version: '0.2.2' },
          { device_id: 'phone-b', tier: 'phone', online: true, app_version: '0.2.2' },
        ],
      })
    }
    if (url.endsWith('/ext/sync_agent/status')) {
      return resolveOk({ loaded: true, paired: true, device_id: 'desktop-a', tier: 'desktop' })
    }
    if (url.endsWith('/ext/sync_agent/mirror/records')) {
      return resolveOk({ records: [] })
    }
    return Promise.reject(new Error(`unexpected GET ${url}`))
  })
}

const { SettingsHubWidget } = await import('../SettingsHubWidget')

beforeEach(() => {
  apiMock.get.mockReset()
  fetchMock.mockReset()
  usePluginsQueryMock.mockReturnValue({ data: { plugins: [] } })
})

describe('SettingsHubWidget — 地盘入口整链（设置中枢 → 后端连接 → 看地盘）', () => {
  it('设置中枢左导航可见「后端连接」入口，点击进入真面板页且地盘面在场', async () => {
    mockChainGets()
    renderWithProviders(<SettingsHubWidget initialActive="kernel-plugins" />)

    await waitFor(() => {
      expect(screen.getByText('内核设置')).toBeInTheDocument()
    })

    // 入口在设置中枢可发现（G17-MD1 契约第一跳：导航项在场）
    const navItem = screen.getAllByText('后端连接')[0]
    expect(navItem).toBeInTheDocument()

    fireEvent.click(navItem)

    // 第二跳：真面板页（非 stub）embedded 渲染，地盘容器在场
    expect(await screen.findByTestId('territory-list')).toBeInTheDocument()

    // 第三跳：注册表他端卡（本机 desktop-a 去重后唯一 peer）带「看地盘」能力面
    expect(await screen.findByTestId('device-card-phone-b')).toBeInTheDocument()
    expect(screen.getByTestId('device-card-toggle-phone-b')).toHaveTextContent('看地盘')
  })

  it('地盘面从导航进入后可展开：点「看地盘」出该地盘的三态呈现区', async () => {
    mockChainGets()
    renderWithProviders(<SettingsHubWidget initialActive="kernel-plugins" />)
    await waitFor(() => {
      expect(screen.getByText('内核设置')).toBeInTheDocument()
    })
    fireEvent.click(screen.getAllByText('后端连接')[0])
    fireEvent.click(await screen.findByTestId('device-card-toggle-phone-b'))
    await waitFor(() => {
      // 在线 + 无镜像 → 诚实空态（①/③口径归页面套件，此处只断链上可达）
      expect(screen.getByTestId('device-card-body-phone-b')).toHaveTextContent(
        '该地盘可达，暂无同步会话。',
      )
    })
  })
})
