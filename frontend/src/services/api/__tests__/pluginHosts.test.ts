// @feature: FP-T12 前端适配(插件宿主 API) | @ci: frontend-test
/**
 * pluginHosts API 服务测试（插件进程观测面）
 *
 * apiClient 是外部依赖（网络层），用可控 mock 替换并断言请求契约；
 * 返回值映射断言可观察输出（清单字段 null/缺省兜底空数组）。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

const getMock = vi.fn()

vi.mock('../client', () => {
  const mockClient = {
    get: (...args: unknown[]) => getMock(...args),
  }
  return { default: mockClient, apiClient: mockClient }
})

import { getPluginHosts, getPluginRuntimeRows } from '../pluginHosts'

beforeEach(() => {
  getMock.mockReset()
})

describe('getPluginHosts', () => {
  it('GET /api/v1/plugins/hosts，完整响应透传', async () => {
    const snapshot = {
      hosts: [{ host_key: 'group:light:1', kind: 'group', alive: true }],
      pending_spawn: [{ plugin_id: 'metrics_admin', enabled: true, last_call_at: null }],
    }
    getMock.mockResolvedValue({ data: snapshot })

    await expect(getPluginHosts()).resolves.toEqual(snapshot)
    expect(getMock).toHaveBeenCalledWith('/api/v1/plugins/hosts')
  })

  it('清单字段 null → 兜底空数组（对后端缺字段健壮）', async () => {
    getMock.mockResolvedValue({ data: { hosts: null, pending_spawn: null } })

    await expect(getPluginHosts()).resolves.toEqual({ hosts: [], pending_spawn: [] })
  })

  it('响应体缺清单字段 → 兜底空数组', async () => {
    getMock.mockResolvedValue({ data: {} })

    await expect(getPluginHosts()).resolves.toEqual({ hosts: [], pending_spawn: [] })
  })
})

describe('getPluginRuntimeRows', () => {
  it('GET /ext/monitoring/plugins，rows 透传（含崩溃时间戳 join 列）', async () => {
    const rows = [
      { plugin_id: 'bash_tool', alive: 1, status: 'running', last_crash_ts: 1758950400 },
      { plugin_id: 'ghost', alive: 0, status: 'dead', last_crash_ts: 0 },
    ]
    getMock.mockResolvedValue({ data: { rows, total: 2, lifecycle: { plugin_load_total: 9 } } })

    await expect(getPluginRuntimeRows()).resolves.toEqual(rows)
    expect(getMock).toHaveBeenCalledWith('/ext/monitoring/plugins')
  })

  it('rows null → 兜底空数组（对后端缺字段健壮）', async () => {
    getMock.mockResolvedValue({ data: { rows: null } })

    await expect(getPluginRuntimeRows()).resolves.toEqual([])
  })

  it('响应体缺 rows 字段 → 兜底空数组', async () => {
    getMock.mockResolvedValue({ data: {} })

    await expect(getPluginRuntimeRows()).resolves.toEqual([])
  })
})
