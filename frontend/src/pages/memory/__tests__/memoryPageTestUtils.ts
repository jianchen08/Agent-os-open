/**
 * MemoryPage 测试族共享脚手架。
 *
 * 只承载 vi.mock 工厂形状与默认成功态 arrange；vi.mock 声明因提升语义
 * 必须留在各测试文件原地，不入本模块。本模块顶层不得 import 被 mock 的
 * `@/services/api/memory`（工厂动态 import 本模块时会造成互相等待），
 * arrange 经函数内动态 import 取已 mock 的模块。
 */
import { vi } from 'vitest'

/** @/services/api/memory 的整模块 mock 形状（vi.mock 工厂每次调用产出新桩） */
export function memoryApiModuleMock() {
  return {
    getEpisodes: vi.fn(),
    getMemoryStats: vi.fn(),
    getSemanticMemory: vi.fn(),
    searchHindsight: vi.fn(),
    getMemoryById: vi.fn(),
    deleteMemoryById: vi.fn(),
  }
}

/** 主链默认统计（7 情景 / 3 知识 / 共 10） */
export const MEMORY_STATS = { episode_count: 7, knowledge_count: 3, total_count: 10 }

/** 默认成功态：统计 + 空情景/语义/搜索（episodes 可由用例覆写） */
export async function arrangeMemoryBaseResponses(
  episodes: unknown = { items: [], total: 0, page: 1 },
) {
  const { getEpisodes, getMemoryStats, getSemanticMemory, searchHindsight } = await import(
    '@/services/api/memory',
  )
  vi.mocked(getMemoryStats).mockResolvedValue(MEMORY_STATS as never)
  vi.mocked(getEpisodes).mockResolvedValue(episodes as never)
  vi.mocked(getSemanticMemory).mockResolvedValue({ items: [] } as never)
  vi.mocked(searchHindsight).mockResolvedValue({ items: [], total: 0, query: '' } as never)
}
