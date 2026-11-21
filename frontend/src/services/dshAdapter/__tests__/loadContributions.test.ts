/** @feature FP-T12 前端适配(dsh 适配贡献装载) | @ci: frontend-test */
/**
 * loadDshAdapterContributions 装载契约——schema 失败如实入 failures、
 * 非 dsh 条目跳过、info/loaded 识别、renderers 兜底注册（合法卡型注册/
 * 非法条目失败隔离不中断）。mock 外部依赖（getSchema/addRenderIntent）。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/services/api/schema', () => ({ getSchema: vi.fn() }))
// client-ui 清单拉取在装载末尾同步槽位页：测试里钉成空清单，避免真实网络重试
vi.mock('@/services/api/client', () => ({
  apiClient: { get: vi.fn().mockResolvedValue({ data: { bundles: [] } }) },
}))
vi.mock('@/utils/renderIntent', () => ({ addRenderIntent: vi.fn() }))

import { getSchema } from '@/services/api/schema'
import { addRenderIntent } from '@/utils/renderIntent'
import { loadDshAdapterContributions } from '../index'

describe('loadDshAdapterContributions — 装载契约', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('schema 获取失败 → loaded=false + failures 如实记录（不静默）', async () => {
    vi.mocked(getSchema).mockRejectedValue(new Error('kernel down'))
    const r = await loadDshAdapterContributions()
    expect(r.loaded).toBe(false)
    expect(r.failures[0]).toContain('schema')
    expect(r.failures[0]).toContain('kernel down')
  })

  it('识别 dsh_adapter 条目：info/loaded 落位 + renderers 合法注册', async () => {
    vi.mocked(getSchema).mockResolvedValue({
      plugin_contributes: [
        { plugin_id: 'other', contributes: { foo: 1 } },
        { plugin_id: 'dsh_adapter', contributes: { dsh_adapter: { version: '1.2' }, renderers: [
          { tool: 'run_cmd', card: 'terminal' },
          { tool: 'bad_card', card: 'nope' },
          { tool: 'find_file', card: 'read' },
        ] } },
      ],
    })
    const r = await loadDshAdapterContributions()
    expect(r.loaded).toBe(true)
    expect(r.info).toEqual({ version: '1.2' })
    expect(r.renderersRegistered).toBe(2)
    expect(r.failures).toHaveLength(1)
    expect(r.failures[0]).toContain('bad renderer entry')
    expect(addRenderIntent).toHaveBeenCalledWith('run_cmd', { card: 'terminal' })
  })

  it('校验面 = DSH 映射子集而非全量词表：form 在全量词表但非 DSH 面产出 → 拒注册', async () => {
    vi.mocked(getSchema).mockResolvedValue({
      plugin_contributes: [
        { plugin_id: 'dsh_adapter', contributes: { dsh_adapter: { version: '1' }, renderers: [
          { tool: 'form_tool', card: 'form' },
          { tool: 'diff_tool', card: 'diff' },
        ] } },
      ],
    })
    const r = await loadDshAdapterContributions()
    expect(r.renderersRegistered).toBe(1)
    expect(r.failures).toHaveLength(1)
    expect(r.failures[0]).toContain('form_tool')
    expect(addRenderIntent).toHaveBeenCalledTimes(1)
    expect(addRenderIntent).toHaveBeenCalledWith('diff_tool', { card: 'diff' })
  })

  it('贡献空/无 dsh 条目 → loaded=false 零注册零失败', async () => {
    vi.mocked(getSchema).mockResolvedValue({ plugin_contributes: [] })
    const r = await loadDshAdapterContributions()
    expect(r.loaded).toBe(false)
    expect(r.renderersRegistered).toBe(0)
    expect(r.failures).toEqual([])
  })
})
