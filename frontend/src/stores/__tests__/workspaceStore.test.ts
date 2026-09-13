// @feature: FP-T12 workspaceStore 补测 | @ci: frontend-test
/** workspaceStore 行为测试：动作、归一化回退、persist 序列化契约 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

const apiMocks = vi.hoisted(() => ({
  getWorkspace: vi.fn(),
  getWorkspaceFileTree: vi.fn(),
  getWorkspaceArtifacts: vi.fn(),
  createEntry: vi.fn(),
  deleteEntry: vi.fn(),
  renameEntry: vi.fn(),
  moveEntry: vi.fn(),
}))

vi.mock('@/services/api/workspaces', () => apiMocks)

import { useWorkspaceStore } from '../workspaceStore'

const WS_PAYLOAD = {
  id: 'ws-1',
  containerTaskId: 'ct-1',
  sessionId: 'sess-1',
  title: '任务一',
  description: '描述',
  fileTree: [
    {
      name: 'src',
      type: 'directory',
      path: 'src',
      children: [{ name: 'main.py', type: 'file', path: 'src/main.py' }],
    },
  ],
  createdAt: '2026-09-13T00:00:00Z',
  updatedAt: '2026-09-13T01:00:00Z',
}

describe('workspaceStore', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.clearAllMocks()
    vi.spyOn(window, 'alert').mockImplementation(() => {})
    vi.spyOn(console, 'error').mockImplementation(() => {})
    useWorkspaceStore.setState({
      workspaces: {},
      activeWorkspaceId: null,
      expandedPaths: new Set<string>(),
      selectedFilePath: null,
      loading: false,
      error: null,
    })
  })

  describe('fetchWorkspace', () => {
    it('成功时归一化并缓存工作空间', async () => {
      apiMocks.getWorkspace.mockResolvedValue(WS_PAYLOAD)
      const ws = await useWorkspaceStore.getState().fetchWorkspace('ct-1')
      expect(ws?.id).toBe('ws-1')
      expect(ws?.fileTree[0].type).toBe('directory')
      expect(ws?.fileTree[0].children?.[0].path).toBe('src/main.py')
      expect(useWorkspaceStore.getState().workspaces['ct-1'].title).toBe('任务一')
      expect(useWorkspaceStore.getState().loading).toBe(false)
    })

    it('error 信封时写入 message 并返回 null', async () => {
      apiMocks.getWorkspace.mockResolvedValue({ error: { message: '不存在' } })
      expect(await useWorkspaceStore.getState().fetchWorkspace('ct-x')).toBeNull()
      expect(useWorkspaceStore.getState().error).toBe('不存在')
    })

    it('无 message 的 error 信封回退默认文案', async () => {
      apiMocks.getWorkspace.mockResolvedValue({ error: {} })
      await useWorkspaceStore.getState().fetchWorkspace('ct-x')
      expect(useWorkspaceStore.getState().error).toBe('工作空间加载失败')
    })

    it('网络异常时 error 取异常 message', async () => {
      apiMocks.getWorkspace.mockRejectedValue(new Error('网络中断'))
      expect(await useWorkspaceStore.getState().fetchWorkspace('ct-x')).toBeNull()
      expect(useWorkspaceStore.getState().error).toBe('网络中断')
    })
  })

  describe('fetchFileTree', () => {
    it('更新已缓存工作空间的树并返回归一化节点', async () => {
      apiMocks.getWorkspace.mockResolvedValue(WS_PAYLOAD)
      await useWorkspaceStore.getState().fetchWorkspace('ct-1')

      apiMocks.getWorkspaceFileTree.mockResolvedValue({
        tree: [{ name: 'a.txt', type: 'weird-type', path: 'a.txt' }],
      })
      const tree = await useWorkspaceStore.getState().fetchFileTree('ct-1')
      expect(tree[0].type).toBe('file') // 契约外值回退 file
      expect(useWorkspaceStore.getState().workspaces['ct-1'].fileTree[0].name).toBe('a.txt')
    })

    it('未缓存的工作空间不改 state 但仍返回树', async () => {
      apiMocks.getWorkspaceFileTree.mockResolvedValue({
        tree: [{ name: 'b.txt', type: 'file', path: 'b.txt' }],
      })
      const tree = await useWorkspaceStore.getState().fetchFileTree('ghost')
      expect(tree).toHaveLength(1)
      expect(useWorkspaceStore.getState().workspaces['ghost']).toBeUndefined()
    })

    it('接口异常返回空数组', async () => {
      apiMocks.getWorkspaceFileTree.mockRejectedValue(new Error('x'))
      expect(await useWorkspaceStore.getState().fetchFileTree('ct-1')).toEqual([])
    })
  })

  describe('fetchWorkspaceArtifacts', () => {
    it('归一化制品：契约外类型回退 text', async () => {
      apiMocks.getWorkspaceArtifacts.mockResolvedValue({
        items: [
          { id: 'a1', artifactType: 'image', taskId: 't1', title: '图' },
          { id: 'a2', artifactType: 'hologram', taskId: 't1' },
          { artifactType: 'text' }, // 缺 id：仅告警不崩
        ],
      })
      const items = await useWorkspaceStore.getState().fetchWorkspaceArtifacts('ct-1')
      expect(items[0].artifactType).toBe('image')
      expect(items[1].artifactType).toBe('text')
      expect(items).toHaveLength(3)
    })

    it('接口异常返回空数组', async () => {
      apiMocks.getWorkspaceArtifacts.mockRejectedValue(new Error('x'))
      expect(await useWorkspaceStore.getState().fetchWorkspaceArtifacts('ct-1')).toEqual([])
    })
  })

  describe('条目写动作：成功后刷新树，失败告警', () => {
    it.each([
      ['createEntry', 'createEntry', ['ct-1', 'new.py', 'file'] as const],
      ['deleteEntry', 'deleteEntry', ['ct-1', 'old.py'] as const],
      ['renameEntry', 'renameEntry', ['ct-1', 'a.py', 'b.py'] as const],
      ['moveEntry', 'moveEntry', ['ct-1', 'a.py', 'dir'] as const],
    ])('%s 成功路径', async (_label, fn, args) => {
      apiMocks.getWorkspaceFileTree.mockResolvedValue({ tree: [] })
      apiMocks[fn].mockResolvedValue({})
      const ok = await (useWorkspaceStore.getState() as any)[fn](...args)
      expect(ok).toBe(true)
      expect(apiMocks[fn]).toHaveBeenCalledTimes(1)
      expect(apiMocks.getWorkspaceFileTree).toHaveBeenCalledWith('ct-1') // 树已刷新
    })

    it('失败路径弹告警并返回 false，文案前缀按动作区分', async () => {
      apiMocks.getWorkspaceFileTree.mockResolvedValue({ tree: [] })
      const cases: Array<[string, string]> = [
        ['createEntry', '创建失败'],
        ['deleteEntry', '删除失败'],
        ['renameEntry', '重命名失败'],
        ['moveEntry', '移动失败'],
      ]
      for (const [fn, prefix] of cases) {
        apiMocks[fn].mockRejectedValueOnce(new Error('x'))
        await (useWorkspaceStore.getState() as any)[fn]('ct-1', 'x', 'y')
        expect(window.alert).toHaveBeenLastCalledWith(`${prefix}: x`)
      }
    })
  })

  describe('轻量动作与 resolveContainerTask', () => {
    it('setActiveWorkspace / setSelectedFile / togglePathExpanded', () => {
      const s = useWorkspaceStore.getState()
      s.setActiveWorkspace('ct-1')
      s.setSelectedFile('a.txt')
      s.togglePathExpanded('src')
      let st = useWorkspaceStore.getState()
      expect(st.activeWorkspaceId).toBe('ct-1')
      expect(st.selectedFilePath).toBe('a.txt')
      expect(st.expandedPaths.has('src')).toBe(true)

      st.togglePathExpanded('src')
      st.setSelectedFile(null)
      st = useWorkspaceStore.getState()
      expect(st.expandedPaths.has('src')).toBe(false)
      expect(st.selectedFilePath).toBeNull()
    })

    it('resolveContainerTask：命中缓存返回键，未命中原样返回', async () => {
      apiMocks.getWorkspace.mockResolvedValue(WS_PAYLOAD)
      await useWorkspaceStore.getState().fetchWorkspace('ct-1')
      expect(await useWorkspaceStore.getState().resolveContainerTask('ct-1')).toBe('ct-1')
      expect(await useWorkspaceStore.getState().resolveContainerTask('other')).toBe('other')
    })

    it('clearCache 清空全部缓存态', async () => {
      const s = useWorkspaceStore.getState()
      s.setActiveWorkspace('ct-1')
      s.togglePathExpanded('src')
      s.setSelectedFile('a.txt')
      useWorkspaceStore.getState().clearCache()
      const st = useWorkspaceStore.getState()
      expect(st.workspaces).toEqual({})
      expect(st.activeWorkspaceId).toBeNull()
      expect(st.expandedPaths.size).toBe(0)
      expect(st.selectedFilePath).toBeNull()
    })
  })

  describe('persist 契约', () => {
    it('expandedPaths 以数组形态落盘，loading/error 不持久化', async () => {
      apiMocks.getWorkspace.mockResolvedValue(WS_PAYLOAD)
      await useWorkspaceStore.getState().fetchWorkspace('ct-1')
      useWorkspaceStore.getState().togglePathExpanded('src')

      const raw = localStorage.getItem('workspace-store')
      expect(raw).toBeTruthy()
      const parsed = JSON.parse(raw!)
      expect(Array.isArray(parsed.state.expandedPaths)).toBe(true)
      expect(parsed.state.expandedPaths).toContain('src')
      expect(parsed.state.loading).toBeUndefined()
      expect(parsed.state.error).toBeUndefined()
      expect(parsed.version).toBe(1)
    })

    it('rehydrate 时数组还原为 Set，运行时字段强制重置', async () => {
      localStorage.setItem(
        'workspace-store',
        JSON.stringify({
          state: {
            workspaces: { 'ct-9': { id: 'ws-9', title: '旧' } },
            activeWorkspaceId: 'ct-9',
            expandedPaths: ['a', 'b'],
            loading: true,
            error: '上次残留',
          },
          version: 1,
        }),
      )
      await useWorkspaceStore.persist.rehydrate()
      const st = useWorkspaceStore.getState()
      expect(st.expandedPaths).toBeInstanceOf(Set)
      expect(st.expandedPaths.has('a')).toBe(true)
      expect(st.loading).toBe(false)
      expect(st.error).toBeNull()
    })

    it('rehydrate 遇到损坏的 expandedPaths 类型回退空 Set', async () => {
      localStorage.setItem(
        'workspace-store',
        JSON.stringify({ state: { expandedPaths: 'not-an-array' }, version: 1 }),
      )
      await useWorkspaceStore.persist.rehydrate()
      expect(useWorkspaceStore.getState().expandedPaths.size).toBe(0)
    })
  })
})
