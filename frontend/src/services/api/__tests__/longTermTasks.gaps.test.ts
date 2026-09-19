// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/** toggleAutoExecute 停用分支：auto-execute 标签从既有标签中移除 */
import { describe, expect, it, vi, beforeEach } from 'vitest'

const { mockGet, mockPatch } = vi.hoisted(() => ({
  mockGet: vi.fn(),
  mockPatch: vi.fn(),
}))

vi.mock('@/services/api/client', () => ({
  apiClient: { get: (...a: unknown[]) => mockGet(...a), patch: (...a: unknown[]) => mockPatch(...a) },
}))

import { toggleAutoExecute } from '@/services/api/longTermTasks'

describe('toggleAutoExecute', () => {
  beforeEach(() => {
    mockGet.mockReset().mockResolvedValue({
      data: { id: 't-1', tags: ['long-term', 'auto-execute'] },
    })
    mockPatch.mockReset().mockImplementation((_url: unknown, body: { tags: string[] }) =>
      Promise.resolve({ data: { id: 't-1', tags: body.tags } }),
    )
  })

  it('enabled=false → auto-execute 标签被移除且 PATCH 携带新标签集', async () => {
    const task = await toggleAutoExecute('t-1', false)
    expect(task.tags).toEqual(['long-term'])
    expect(mockPatch).toHaveBeenCalledTimes(1)
    expect(mockPatch.mock.calls[0][1]).toEqual({ tags: ['long-term'] })
  })

  it('enabled=true → 标签去重后追加 auto-execute', async () => {
    mockGet.mockResolvedValue({ data: { id: 't-1', tags: ['auto-execute', 'auto-execute'] } })
    const task = await toggleAutoExecute('t-1', true)
    expect(task.tags).toEqual(['auto-execute'])
  })
})
