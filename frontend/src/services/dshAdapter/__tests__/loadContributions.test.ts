/** @feature FP-T12 前端适配(dsh 适配贡献装载) | @ci: frontend-test */
/**
 * loadDshAdapterContributions 装载契约——schema 失败如实入 failures、
 * 非 dsh 条目跳过、info/loaded 识别、renderers 兜底注册（合法卡型注册/
 * 非法条目失败隔离不中断）。mock 外部依赖（getSchema/addRenderIntent）。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/services/api/schema', () => ({ getSchema: vi.fn() }))
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

  it('贡献空/无 dsh 条目 → loaded=false 零注册零失败', async () => {
    vi.mocked(getSchema).mockResolvedValue({ plugin_contributes: [] })
    const r = await loadDshAdapterContributions()
    expect(r.loaded).toBe(false)
    expect(r.renderersRegistered).toBe(0)
    expect(r.failures).toEqual([])
  })
})
