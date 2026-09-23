// @feature: FP-T12 前端适配 | @ci: frontend-test
/** @ci: frontend-test */
/**
 * PluginsSettingsPage 失败态契约测试（OBS-R258-1 试点页 2）
 *
 * 用户裁定：所有前端页面必须区分「无数据」与「加载失败」——插件注册表页
 * 主面（插件列表）加载失败时必须显式 ErrorState + 重试，不得伪装空态：
 * - 失败态不出现「暂无已注册的插件」空态文案
 * - 失败态不出现「0/0 启用」类假计数
 * - 非 Error 拒绝回退通用文案（不展示 [object Object] 类脏文本）
 * - 重试成功后列表恢复（重试钮语义）
 *
 * 测试策略：Mock 仅外部依赖（apiClient HTTP 层），组件真实渲染。
 */

import { fireEvent, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { renderWithProviders } from '@/test/renderWithProviders'
import { PluginsSettingsPage } from '../PluginsSettingsPage'
import { MONITORING_PLUGIN } from './pluginsSettingsTestUtils'

const mockGet = vi.fn()
vi.mock('@/services/api/client', () => ({
  default: {
    get: (...args: unknown[]) => mockGet(...args),
    put: vi.fn(),
  },
}))

vi.mock('@/services/modules/GrowthLoop', () => ({
  refreshPluginContributions: vi.fn().mockResolvedValue(undefined),
}))

vi.mock('@/components/ui/sonner', () => ({
  toast: { error: vi.fn(), success: vi.fn(), warning: vi.fn() },
}))

const PLUGINS_PAYLOAD = {
  data: [MONITORING_PLUGIN],
}

function mockHealthy() {
  mockGet.mockImplementation(async (url: string) => {
    if (url === '/api/v1/plugins') return PLUGINS_PAYLOAD
    if (url === '/api/v1/schema') return { data: { tools: [] } }
    if (url === '/api/v1/plugins/contract-status') return { data: { plugins: [] } }
    throw new Error(`unexpected url: ${url}`)
  })
}

beforeEach(() => {
  vi.resetAllMocks()
})

describe('PluginsSettingsPage — 主面加载失败（不伪装空态）', () => {
  it('插件面拒绝 Error → 显式错误文案 + 重试钮，空态文案与假计数不出现', async () => {
    mockGet.mockRejectedValue(new Error('注册表不可达'))
    renderWithProviders(<PluginsSettingsPage />)

    expect(await screen.findByText('注册表不可达')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '重试' })).toBeInTheDocument()
    expect(screen.queryByText('暂无已注册的插件')).not.toBeInTheDocument()
    expect(screen.queryByTestId('plugins-enabled-count')).not.toBeInTheDocument()
  })

  it('非 Error 拒绝 → 回退通用文案（不展示 [object Object] 类脏文本）', async () => {
    mockGet.mockRejectedValue({ code: 'E_IO' })
    renderWithProviders(<PluginsSettingsPage />)

    expect(await screen.findByText('获取插件状态失败')).toBeInTheDocument()
  })

  it('失败后点重试 → 后端恢复则插件列表渲染', async () => {
    mockGet.mockRejectedValueOnce(new Error('注册表不可达'))
    renderWithProviders(<PluginsSettingsPage />)
    await screen.findByText('注册表不可达')

    mockHealthy()
    fireEvent.click(screen.getByRole('button', { name: '重试' }))

    expect(
      await screen.findByRole('heading', { name: 'Monitoring Service' }),
    ).toBeInTheDocument()
    expect(screen.queryByText('注册表不可达')).not.toBeInTheDocument()
    await waitFor(() =>
      expect(screen.getByTestId('plugins-enabled-count')).toHaveTextContent('1/1 启用'),
    )
  })

  it('成功态才出计数：healthy 数据 → 1/1 启用可见', async () => {
    mockHealthy()
    renderWithProviders(<PluginsSettingsPage />)

    expect(
      await screen.findByRole('heading', { name: 'Monitoring Service' }),
    ).toBeInTheDocument()
    expect(screen.getByTestId('plugins-enabled-count')).toHaveTextContent('1/1 启用')
  })

  it('副面降级不伪装主面失败：schema 能力面拒绝 → 插件列表照常渲染，能力区为空', async () => {
    mockGet.mockImplementation(async (url: string) => {
      if (url === '/api/v1/plugins') return PLUGINS_PAYLOAD
      if (url === '/api/v1/schema') throw new Error('schema 面不可达')
      if (url === '/api/v1/plugins/contract-status') return { data: { plugins: [] } }
      throw new Error(`unexpected url: ${url}`)
    })
    renderWithProviders(<PluginsSettingsPage />)

    expect(
      await screen.findByRole('heading', { name: 'Monitoring Service' }),
    ).toBeInTheDocument()
    expect(screen.queryByText('注册表不可达')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '重试' })).not.toBeInTheDocument()
  })

  it('副面形状异常同走降级：schema 缺 tools 字段 → 主面照常', async () => {
    mockGet.mockImplementation(async (url: string) => {
      if (url === '/api/v1/plugins') return PLUGINS_PAYLOAD
      if (url === '/api/v1/schema') return { data: {} }
      if (url === '/api/v1/plugins/contract-status') return { data: { plugins: [] } }
      throw new Error(`unexpected url: ${url}`)
    })
    renderWithProviders(<PluginsSettingsPage />)

    expect(
      await screen.findByRole('heading', { name: 'Monitoring Service' }),
    ).toBeInTheDocument()
  })
})
