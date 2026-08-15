/**
 * dshAdapter 前端服务测试（task_dsh_plugin_adapter 任务 2）。
 *
 * mock /api/v1/schema：验证贡献装载、失败隔离、renderers 兜底注册到
 * dshRenderIntent 注册表。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { clearRenderIntents, getRenderIntent } from '@/utils/dshRenderIntent'
import { loadDshAdapterContributions } from '../index'

vi.mock('@/services/api/schema', () => ({
  getSchema: vi.fn(),
}))

const { getSchema } = await import('@/services/api/schema')

describe('loadDshAdapterContributions', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    clearRenderIntents()
  })

  it('装载 dsh_adapter 贡献：版本记录 + renderers 兜底注册', async () => {
    getSchema.mockResolvedValue({
      plugin_contributes: [
        {
          plugin_id: 'dsh_adapter',
          contributes: {
            dsh_adapter: {
              source_commit: '47f9438',
              source_version: '0.1.0-rc.5',
              backend_channel: 'node-runtime-bridge',
              frontend_channel: 'vendor-port + render-intent',
              components: ['DiffBlock'],
              out_of_scope: [],
            },
            renderers: [
              { tool: 'dsh_read', card: 'read' },
              { tool: 'dsh_glob', card: 'search' },
            ],
          },
        },
      ],
    })
    const result = await loadDshAdapterContributions()
    expect(result.loaded).toBe(true)
    expect(result.renderersRegistered).toBe(2)
    expect(result.failures).toEqual([])
    expect(result.info?.source_commit).toBe('47f9438')
    expect(getRenderIntent('dsh_read')?.card).toBe('read')
    expect(getRenderIntent('dsh_glob')?.card).toBe('search')
  })

  it('坏 renderer 条目被隔离，不影响其他条目', async () => {
    getSchema.mockResolvedValue({
      plugin_contributes: [
        {
          plugin_id: 'dsh_adapter',
          contributes: {
            dsh_adapter: { source_commit: 'x' },
            renderers: [
              { tool: 'ok_tool', card: 'terminal' },
              { tool: 'bad_card', card: 'hologram' },
              { card: 'read' }, // 缺 tool
            ],
          },
        },
      ],
    })
    const result = await loadDshAdapterContributions()
    expect(result.renderersRegistered).toBe(1)
    expect(result.failures).toHaveLength(2)
    expect(getRenderIntent('ok_tool')?.card).toBe('terminal')
  })

  it('无 dsh_adapter 插件时静默空载', async () => {
    getSchema.mockResolvedValue({ plugin_contributes: [{ plugin_id: 'other', contributes: {} }] })
    const result = await loadDshAdapterContributions()
    expect(result.loaded).toBe(false)
    expect(result.renderersRegistered).toBe(0)
  })

  it('schema 获取失败不抛出（fail-soft）', async () => {
    getSchema.mockRejectedValue(new Error('network down'))
    const result = await loadDshAdapterContributions()
    expect(result.loaded).toBe(false)
    expect(result.failures[0]).toContain('network down')
  })
})
