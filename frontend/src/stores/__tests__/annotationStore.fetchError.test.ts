/** @feature FP-0.2.四/五 fallback-audit FE项 批注加载失败置错误态 @ci frontend-test */
/**
 * annotationStore.fetchAnnotations：查 resp.ok；失败置 error 状态——
 * 评审工作流故障不得伪装成"无批注"（审批决策基于不全信息）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useAnnotationStore } from '../annotationStore'

describe('annotationStore.fetchAnnotations 失败可见（FE10）', () => {
  const originalFetch = globalThis.fetch

  beforeEach(() => {
    useAnnotationStore.getState().clearCache()
  })

  afterEach(() => {
    globalThis.fetch = originalFetch
    vi.restoreAllMocks()
  })

  it('HTTP 非 2xx → 返回 [] 且 error 置位（不写缓存）', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ error: 'boom' }), { status: 500 }),
    )

    const items = await useAnnotationStore.getState().fetchAnnotations('art-1')

    expect(items).toEqual([])
    expect(useAnnotationStore.getState().error).toBe('批注加载失败（HTTP 500）')
    expect(useAnnotationStore.getState().annotations).toEqual({})
  })

  it('网络异常 → 返回 [] 且 error 置位', async () => {
    globalThis.fetch = vi.fn().mockRejectedValue(new Error('network down'))

    const items = await useAnnotationStore.getState().fetchAnnotations('art-1')

    expect(items).toEqual([])
    expect(useAnnotationStore.getState().error).toBe('network down')
  })

  it('成功 → error 清空、批注入缓存', async () => {
    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({
        items: [{ id: 'a1', artifactId: 'art-1', content: '批注1' }],
      }), { status: 200 }),
    )

    const items = await useAnnotationStore.getState().fetchAnnotations('art-1')

    expect(items).toHaveLength(1)
    expect(useAnnotationStore.getState().error).toBeNull()
    expect(useAnnotationStore.getState().getAnnotationsForArtifact('art-1')).toHaveLength(1)
  })
})
