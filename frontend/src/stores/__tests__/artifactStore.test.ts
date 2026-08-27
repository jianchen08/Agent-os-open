/**
 * artifactStore 单测：制品缓存、版本历史、WS 事件、fetch 错误路径
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useArtifactStore } from '../artifactStore'

describe('artifactStore', () => {
  const originalFetch = globalThis.fetch

  beforeEach(() => {
    useArtifactStore.getState().clearCache()
  })

  afterEach(() => {
    globalThis.fetch = originalFetch
    vi.restoreAllMocks()
  })

  describe('fetchArtifact', () => {
    it('成功：规范化并写入缓存，loading 复位', async () => {
      globalThis.fetch = vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ id: 'art-1', taskId: 't1', title: '报告', version: 3 }), { status: 200 }),
      )

      const artifact = await useArtifactStore.getState().fetchArtifact('art-1')

      expect(artifact).toMatchObject({ id: 'art-1', taskId: 't1', title: '报告', version: 3 })
      expect(useArtifactStore.getState().artifacts['art-1']).toBe(artifact)
      expect(useArtifactStore.getState().loading).toBe(false)
      expect(useArtifactStore.getState().error).toBeNull()
      expect(globalThis.fetch).toHaveBeenCalledWith('/ext/artifacts/art-1')
    })

    it('响应含 error → 返回 null 且 error 置位', async () => {
      globalThis.fetch = vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ error: { message: 'not found' } }), { status: 404 }),
      )

      const artifact = await useArtifactStore.getState().fetchArtifact('art-1')

      expect(artifact).toBeNull()
      expect(useArtifactStore.getState().error).toBe('not found')
      expect(useArtifactStore.getState().artifacts).toEqual({})
    })

    it('网络异常 → 返回 null 且 error 置位', async () => {
      globalThis.fetch = vi.fn().mockRejectedValue(new Error('network down'))

      const artifact = await useArtifactStore.getState().fetchArtifact('art-1')

      expect(artifact).toBeNull()
      expect(useArtifactStore.getState().error).toBe('network down')
    })
  })

  describe('fetchArtifactsByTask', () => {
    it('成功：items 规范化并合并进缓存', async () => {
      globalThis.fetch = vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ items: [{ id: 'a1', title: 'A' }, { id: 'a2', title: 'B' }] }), { status: 200 }),
      )

      const items = await useArtifactStore.getState().fetchArtifactsByTask('task-9')

      expect(items).toHaveLength(2)
      expect(Object.keys(useArtifactStore.getState().artifacts)).toEqual(['a1', 'a2'])
      expect(globalThis.fetch).toHaveBeenCalledWith('/ext/artifacts?task_id=task-9')
    })

    it('taskId 含特殊字符时 URL 编码', async () => {
      globalThis.fetch = vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ items: [] }), { status: 200 }),
      )

      await useArtifactStore.getState().fetchArtifactsByTask('a b&c')

      expect(globalThis.fetch).toHaveBeenCalledWith('/ext/artifacts?task_id=a%20b%26c')
    })

    it('网络异常 → 返回 [] 且 error 置位', async () => {
      globalThis.fetch = vi.fn().mockRejectedValue(new Error('boom'))

      const items = await useArtifactStore.getState().fetchArtifactsByTask('t')

      expect(items).toEqual([])
      expect(useArtifactStore.getState().error).toBe('boom')
    })
  })

  describe('fetchVersionHistory', () => {
    it('成功：写入 versionHistories 并回填 artifacts 缓存', async () => {
      globalThis.fetch = vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ items: [{ id: 'a1', version: 1 }, { id: 'a1', version: 2 }] }), { status: 200 }),
      )

      const items = await useArtifactStore.getState().fetchVersionHistory('a1')

      expect(items).toHaveLength(2)
      expect(useArtifactStore.getState().versionHistories['a1']).toHaveLength(2)
      expect(useArtifactStore.getState().artifacts['a1'].version).toBe(2)
      expect(globalThis.fetch).toHaveBeenCalledWith('/ext/artifacts/a1/versions')
    })

    it('网络异常 → 返回 [] 且 error 置位', async () => {
      globalThis.fetch = vi.fn().mockRejectedValue(new Error('boom'))

      const items = await useArtifactStore.getState().fetchVersionHistory('a1')

      expect(items).toEqual([])
      expect(useArtifactStore.getState().error).toBe('boom')
    })
  })

  describe('fetchVersionDiff', () => {
    it('成功：返回 diff 文本', async () => {
      globalThis.fetch = vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ diff: '+line1\n-line2' }), { status: 200 }),
      )

      const diff = await useArtifactStore.getState().fetchVersionDiff('a1', 1, 2)

      expect(diff).toBe('+line1\n-line2')
      expect(globalThis.fetch).toHaveBeenCalledWith('/ext/artifacts/a1/diff?from=1&to=2')
    })

    it('异常 → 返回空串（不抛）', async () => {
      globalThis.fetch = vi.fn().mockRejectedValue(new Error('boom'))

      const diff = await useArtifactStore.getState().fetchVersionDiff('a1', 1, 2)

      expect(diff).toBe('')
    })
  })

  describe('WS 事件', () => {
    it('updateArtifactFromWS：已缓存制品版本 +1（无 version 字段时）', () => {
      const s = useArtifactStore.getState()
      s.addArtifactFromWS({ artifact_id: 'a1', task_id: 't1', title: 'T', artifact_type: 'text' })
      s.updateArtifactFromWS({ artifact_id: 'a1' })

      expect(useArtifactStore.getState().artifacts['a1'].version).toBe(2)
    })

    it('updateArtifactFromWS：显式 version 覆盖', () => {
      const s = useArtifactStore.getState()
      s.addArtifactFromWS({ artifact_id: 'a1' })
      s.updateArtifactFromWS({ artifact_id: 'a1', version: 7 })

      expect(useArtifactStore.getState().artifacts['a1'].version).toBe(7)
    })

    it('updateArtifactFromWS：未缓存制品不落库', () => {
      useArtifactStore.getState().updateArtifactFromWS({ artifact_id: 'ghost' })
      expect(useArtifactStore.getState().artifacts).toEqual({})
    })

    it('addArtifactFromWS：创建默认制品（缺省字段兜底）', () => {
      useArtifactStore.getState().addArtifactFromWS({ artifact_id: 'a1' })

      const artifact = useArtifactStore.getState().artifacts['a1']
      expect(artifact).toMatchObject({
        id: 'a1',
        taskId: '',
        title: '',
        artifactType: 'text',
        content: '',
        version: 1,
        metadata: {},
      })
      expect(artifact.createdAt).toBeTruthy()
    })

    it('addArtifactFromWS：无 artifact_id 忽略', () => {
      useArtifactStore.getState().addArtifactFromWS({ task_id: 't1' })
      expect(useArtifactStore.getState().artifacts).toEqual({})
    })
  })

  describe('clearCache', () => {
    it('清空 artifacts/versionHistories/error', () => {
      const s = useArtifactStore.getState()
      s.addArtifactFromWS({ artifact_id: 'a1' })
      s.clearCache()

      const state = useArtifactStore.getState()
      expect(state.artifacts).toEqual({})
      expect(state.versionHistories).toEqual({})
      expect(state.error).toBeNull()
    })
  })
})
