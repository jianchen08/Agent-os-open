/** @feature FP-0.2.四 前端Schema | @ci frontend-test */
/**
 * 反链投影共享数据面单测：resolveDocIdPath 反查（MF-1 索引驱动解析）。
 *
 * 行为断言：命中返回真实路径；未收录返回 null；投影读取失败原样抛错且
 * 缓存复位（下次调用重试，不缓存失败）；成功缓存常驻（单次 fetch）。
 *
 * 模块持有进程级缓存（成功常驻），每用例 vi.resetModules + 动态导入隔离。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

const apiMock = vi.hoisted(() => ({ get: vi.fn() }))
vi.mock('@/services/api/client', () => ({ default: apiMock, apiClient: apiMock }))

const PROJECTION = {
  meta: {},
  by_target: {},
  path_to_id: { 'docs/b.md': '20251205-agent-three-path-generation' },
  id_to_path: {
    '20251205-agent-three-path-generation':
      'docs/decisions/2025-12-05-agent-three-path-generation.md',
  },
}

beforeEach(() => {
  apiMock.get.mockReset()
  vi.resetModules()
})

describe('resolveDocIdPath — doc_id 反查（MF-1）', () => {
  it('命中：doc_id → 真实仓库路径', async () => {
    apiMock.get.mockResolvedValue({
      data: { success: true, content: JSON.stringify(PROJECTION) },
    })
    const { resolveDocIdPath } = await import('../backlinksProjection')
    expect(await resolveDocIdPath('20251205-agent-three-path-generation')).toBe(
      'docs/decisions/2025-12-05-agent-three-path-generation.md',
    )
  })

  it('未收录（悬空引用）：返回 null 不抛错', async () => {
    apiMock.get.mockResolvedValue({
      data: { success: true, content: JSON.stringify(PROJECTION) },
    })
    const { resolveDocIdPath } = await import('../backlinksProjection')
    expect(await resolveDocIdPath('20251205-not-exist')).toBeNull()
  })

  it('成功缓存常驻：连续两次反查只拉一次投影', async () => {
    apiMock.get.mockResolvedValue({
      data: { success: true, content: JSON.stringify(PROJECTION) },
    })
    const { resolveDocIdPath } = await import('../backlinksProjection')
    await resolveDocIdPath('20251205-agent-three-path-generation')
    await resolveDocIdPath('20251205-agent-three-path-generation')
    expect(apiMock.get).toHaveBeenCalledTimes(1)
  })

  it('失败即弃缓存：报错原样上抛，下次调用重试拉取', async () => {
    apiMock.get.mockRejectedValueOnce(new Error('网络中断'))
    const { resolveDocIdPath } = await import('../backlinksProjection')
    await expect(resolveDocIdPath('x')).rejects.toThrow('网络中断')
    apiMock.get.mockResolvedValue({
      data: { success: true, content: JSON.stringify(PROJECTION) },
    })
    expect(await resolveDocIdPath('20251205-agent-three-path-generation')).toBeTruthy()
    expect(apiMock.get).toHaveBeenCalledTimes(2)
  })

  it('端点 success=false：抛 message（可定位错误如实上抛，禁静默降级）', async () => {
    apiMock.get.mockResolvedValue({ data: { success: false, message: '文件不存在' } })
    const { resolveDocIdPath } = await import('../backlinksProjection')
    await expect(resolveDocIdPath('x')).rejects.toThrow('文件不存在')
  })
})
