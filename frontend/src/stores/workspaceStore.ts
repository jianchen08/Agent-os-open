/**
 * Workspace Store
 *
 * 管理工作空间的加载和文件树展示。
 */

import { create } from 'zustand'
import type { Artifact } from '@/types/artifact'
import type { Workspace, FileTreeNode } from '@/types/workspace'
import {
  createEntry as apiCreateEntry,
  deleteEntry as apiDeleteEntry,
  renameEntry as apiRenameEntry,
  moveEntry as apiMoveEntry,
} from '@/services/api/workspaces'

interface WorkspaceState {
  /** 以 container_task_id 为 key 的工作空间缓存 */
  workspaces: Record<string, Workspace>
  /** 当前活跃工作空间的 container_task_id */
  activeWorkspaceId: string | null
  /** 文件树展开路径集合 */
  expandedPaths: Set<string>
  /** 文件树选中路径 */
  selectedFilePath: string | null
  /** 加载状态 */
  loading: boolean
  /** 错误信息 */
  error: string | null
}

interface WorkspaceActions {
  /** 加载工作空间 */
  fetchWorkspace: (containerTaskId: string) => Promise<Workspace | null>
  /** 加载文件目录树 */
  fetchFileTree: (containerTaskId: string) => Promise<FileTreeNode[]>
  /** 加载工作空间下所有制品 */
  fetchWorkspaceArtifacts: (containerTaskId: string) => Promise<Artifact[]>
  /** 切换活跃工作空间 */
  setActiveWorkspace: (containerTaskId: string | null) => void
  /** 展开/折叠目录 */
  togglePathExpanded: (path: string) => void
  /** 选中文件 */
  setSelectedFile: (path: string | null) => void
  /** 创建文件或目录 */
  createEntry: (containerTaskId: string, path: string, type: 'file' | 'directory') => Promise<boolean>
  /** 删除文件或目录 */
  deleteEntry: (containerTaskId: string, path: string) => Promise<boolean>
  /** 重命名文件或目录 */
  renameEntry: (containerTaskId: string, oldPath: string, newName: string) => Promise<boolean>
  /** 移动文件或目录 */
  moveEntry: (containerTaskId: string, sourcePath: string, destinationDir: string) => Promise<boolean>
  /** 解析任务到容器任务 */
  resolveContainerTask: (taskId: string) => Promise<string>
  /** 清除缓存 */
  clearCache: () => void
}

const API_BASE = '/api/v1/workspaces'

export const useWorkspaceStore = create<WorkspaceState & WorkspaceActions>()((set, get) => ({
  workspaces: {},
  activeWorkspaceId: null,
  expandedPaths: new Set<string>(),
  selectedFilePath: null,
  loading: false,
  error: null,

  fetchWorkspace: async (containerTaskId) => {
    set({ loading: true, error: null })
    try {
      const resp = await fetch(`${API_BASE}/${containerTaskId}`)
      const data = await resp.json()
      if (data.error) {
        set({ loading: false, error: data.error.message })
        return null
      }
      const ws = _normalizeWorkspace(data)
      set((state) => ({
        workspaces: { ...state.workspaces, [containerTaskId]: ws },
        loading: false,
      }))
      return ws
    } catch (e: any) {
      set({ loading: false, error: e.message })
      return null
    }
  },

  fetchFileTree: async (containerTaskId) => {
    try {
      const resp = await fetch(`${API_BASE}/${containerTaskId}/file-tree`)
      const data = await resp.json()
      const tree = (data.tree || []).map(_normalizeFileTreeNode)
      // 更新缓存中的文件树
      set((state) => {
        const ws = state.workspaces[containerTaskId]
        if (ws) {
          return {
            workspaces: {
              ...state.workspaces,
              [containerTaskId]: { ...ws, fileTree: tree },
            },
          }
        }
        return state
      })
      return tree
    } catch {
      return []
    }
  },

  fetchWorkspaceArtifacts: async (containerTaskId) => {
    try {
      const resp = await fetch(`${API_BASE}/${containerTaskId}/artifacts`)
      const data = await resp.json()
      return (data.items || []).map(_normalizeArtifact)
    } catch {
      return []
    }
  },

  setActiveWorkspace: (containerTaskId) => {
    set({ activeWorkspaceId: containerTaskId })
  },

  togglePathExpanded: (path) => {
    set((state) => {
      const newSet = new Set(state.expandedPaths)
      if (newSet.has(path)) {
        newSet.delete(path)
      } else {
        newSet.add(path)
      }
      return { expandedPaths: newSet }
    })
  },

  setSelectedFile: (path) => {
    set({ selectedFilePath: path })
  },

  resolveContainerTask: async (taskId) => {
    // 本地简易实现：如果 workspaces 中有对应记录则直接返回
    // 否则返回 taskId 本身（实际由后端 API 处理）
    const { workspaces } = get()
    for (const [containerTaskId] of Object.entries(workspaces)) {
      if (containerTaskId === taskId) return containerTaskId
    }
    return taskId
  },

  createEntry: async (containerTaskId, path, type) => {
    try {
      await apiCreateEntry(containerTaskId, path, type)
      await get().fetchFileTree(containerTaskId)
      return true
    } catch (e: any) {
      console.error('[workspaceStore] createEntry failed:', e)
      window.alert(`创建失败: ${e?.message ?? '未知错误'}`)
      return false
    }
  },

  deleteEntry: async (containerTaskId, path) => {
    try {
      await apiDeleteEntry(containerTaskId, path)
      await get().fetchFileTree(containerTaskId)
      return true
    } catch (e: any) {
      console.error('[workspaceStore] deleteEntry failed:', e)
      window.alert(`删除失败: ${e?.message ?? '未知错误'}`)
      return false
    }
  },

  renameEntry: async (containerTaskId, oldPath, newName) => {
    try {
      await apiRenameEntry(containerTaskId, oldPath, newName)
      await get().fetchFileTree(containerTaskId)
      return true
    } catch (e: any) {
      console.error('[workspaceStore] renameEntry failed:', e)
      window.alert(`重命名失败: ${e?.message ?? '未知错误'}`)
      return false
    }
  },

  moveEntry: async (containerTaskId, sourcePath, destinationDir) => {
    try {
      await apiMoveEntry(containerTaskId, sourcePath, destinationDir)
      await get().fetchFileTree(containerTaskId)
      return true
    } catch (e: any) {
      console.error('[workspaceStore] moveEntry failed:', e)
      window.alert(`移动失败: ${e?.message ?? '未知错误'}`)
      return false
    }
  },

  clearCache: () => {
    set({
      workspaces: {},
      activeWorkspaceId: null,
      expandedPaths: new Set<string>(),
      selectedFilePath: null,
      error: null,
    })
  },
}))

function _normalizeWorkspace(data: Record<string, any>): Workspace {
  if (!data.id) {
    console.error('[workspaceStore] _normalizeWorkspace: id 字段缺失', data)
  }
  return {
    id: data.id ?? '',
    containerTaskId: data.containerTaskId ?? '',
    sessionId: data.sessionId ?? '',
    title: data.title ?? '',
    description: data.description ?? '',
    fileTree: (data.fileTree ?? []).map(_normalizeFileTreeNode),
    createdAt: data.createdAt ?? '',
    updatedAt: data.updatedAt ?? '',
  }
}

function _normalizeFileTreeNode(data: Record<string, any>): FileTreeNode {
  return {
    name: data.name ?? '',
    type: data.type ?? 'file',
    path: data.path ?? '',
    artifactId: data.artifactId,
    children: data.children ? data.children.map(_normalizeFileTreeNode) : undefined,
    metadata: data.metadata,
  }
}

function _normalizeArtifact(data: Record<string, any>): Artifact {
  if (!data.id) {
    console.error('[workspaceStore] _normalizeArtifact: id 字段缺失', data)
  }
  return {
    id: data.id ?? '',
    taskId: data.taskId ?? '',
    title: data.title ?? '',
    artifactType: data.artifactType ?? 'text',
    content: data.content ?? '',
    filePath: data.filePath,
    version: data.version ?? 1,
    parentArtifactId: data.parentArtifactId,
    metadata: data.metadata ?? {},
    createdAt: data.createdAt ?? '',
    updatedAt: data.updatedAt ?? '',
  }
}
