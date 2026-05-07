/**
 * Workspace Store
 *
 * 管理工作空间的加载和文件树展示。
 */

import { create } from 'zustand'
import type { Workspace, FileTreeNode } from '@/types/workspace'
import type { Artifact } from '@/types/artifact'

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
  return {
    id: data.id ?? '',
    containerTaskId: data.container_task_id ?? data.containerTaskId ?? '',
    sessionId: data.session_id ?? data.sessionId ?? '',
    title: data.title ?? '',
    description: data.description ?? '',
    fileTree: (data.file_tree ?? data.fileTree ?? []).map(_normalizeFileTreeNode),
    createdAt: data.created_at ?? data.createdAt ?? '',
    updatedAt: data.updated_at ?? data.updatedAt ?? '',
  }
}

function _normalizeFileTreeNode(data: Record<string, any>): FileTreeNode {
  return {
    name: data.name ?? '',
    type: data.type ?? 'file',
    path: data.path ?? '',
    artifactId: data.artifact_id ?? data.artifactId,
    children: data.children ? data.children.map(_normalizeFileTreeNode) : undefined,
    metadata: data.metadata,
  }
}

function _normalizeArtifact(data: Record<string, any>): Artifact {
  return {
    id: data.id ?? '',
    taskId: data.task_id ?? data.taskId ?? '',
    title: data.title ?? '',
    artifactType: data.artifact_type ?? data.artifactType ?? 'text',
    content: data.content ?? '',
    filePath: data.file_path ?? data.filePath,
    version: data.version ?? 1,
    parentArtifactId: data.parent_artifact_id ?? data.parentArtifactId,
    metadata: data.metadata ?? {},
    createdAt: data.created_at ?? data.createdAt ?? '',
    updatedAt: data.updated_at ?? data.updatedAt ?? '',
  }
}
