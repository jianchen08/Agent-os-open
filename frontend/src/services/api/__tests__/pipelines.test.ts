/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * fetchPipelinePluginCatalog 服务测试
 *
 * 验证 /api/v1/pipelines × /api/v1/plugins 的 join 逻辑：
 * - role 与 enabled/config_files 合并；单边缺失时降级默认
 * - config_type 非 pipeline 的条目被过滤
 * - 结果按 id 排序
 *
 * 测试策略：mock axios 客户端（服务解析逻辑真实运行）。
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'

const mockGet = vi.fn()

vi.mock('@/services/api/client', () => ({
  apiClient: { get: (...args: unknown[]) => mockGet(...args) },
  default: { get: (...args: unknown[]) => mockGet(...args) },
}))

import { fetchPipelinePluginCatalog } from '../pipelines'

function mockResponses(catalog: unknown[], plugins: unknown[]) {
  mockGet.mockImplementation((url: string) => {
    if (url === '/api/v1/pipelines') return Promise.resolve({ data: catalog })
    if (url === '/api/v1/plugins') return Promise.resolve({ data: plugins })
    return Promise.reject(new Error(`unexpected url: ${url}`))
  })
}

describe('fetchPipelinePluginCatalog', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('按 id join 两个接口并合并 role/enabled/config_files', async () => {
    mockResponses(
      [
        {
          id: 'pipeline_tool_schema',
          name: 'Tool Schema',
          version: '1.0.0',
          role: 'input',
          host_type: 'sidecar',
        },
      ],
      [
        {
          plugin_id: 'pipeline_tool_schema',
          name: 'Tool Schema（旧名）',
          config_type: 'pipeline',
          host_type: 'sidecar',
          version: '1.0.0',
          enabled: true,
          config_files: [{ id: 'cfg', label: '配置', path: 'a.yaml' }],
        },
      ],
    )

    const entries = await fetchPipelinePluginCatalog()
    expect(entries).toEqual([
      {
        id: 'pipeline_tool_schema',
        name: 'Tool Schema',
        role: 'input',
        hostType: 'sidecar',
        version: '1.0.0',
        enabled: true,
        configFiles: [{ id: 'cfg', label: '配置', path: 'a.yaml' }],
      },
    ])
    expect(mockGet).toHaveBeenCalledWith('/api/v1/pipelines')
    expect(mockGet).toHaveBeenCalledWith('/api/v1/plugins')
  })

  it('过滤 config_type 非 pipeline 的条目；结果按 id 排序', async () => {
    mockResponses(
      [
        { id: 'pipeline_b', name: 'B', version: null, role: 'core', host_type: 'sidecar' },
        { id: 'pipeline_a', name: 'A', version: null, role: null, host_type: 'sidecar' },
      ],
      [
        {
          plugin_id: 'pipeline_a',
          name: 'A',
          config_type: 'pipeline',
          host_type: 'sidecar',
          version: null,
          enabled: false,
          config_files: [],
        },
        {
          plugin_id: 'system_widget',
          name: 'Widget',
          config_type: 'system',
          host_type: 'sidecar',
          version: null,
          enabled: true,
          config_files: [],
        },
      ],
    )

    const entries = await fetchPipelinePluginCatalog()
    expect(entries.map((e) => e.id)).toEqual(['pipeline_a', 'pipeline_b'])
    expect(entries[0].enabled).toBe(false)
    // 目录缺失 status 侧信息 → 默认可用
    expect(entries[1].enabled).toBe(true)
    expect(entries[1].configFiles).toEqual([])
  })

  it('任一接口失败即抛错（调用方降级）', async () => {
    mockGet.mockRejectedValue(new Error('network down'))
    await expect(fetchPipelinePluginCatalog()).rejects.toThrow('network down')
  })
})
