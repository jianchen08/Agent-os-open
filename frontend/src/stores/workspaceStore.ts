/** Workspace Store 管理工作空间的加载和文件树展示。 */

import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { toast } from '@/components/ui/sonner'
import {
  createEntry as apiCreateEntry,
  deleteEntry as apiDeleteEntry,
  renameEntry as apiRenameEntry,
  moveEntry as apiMoveEntry,
  getWorkspace as apiGetWorkspace,
  getWorkspaceFileTree as apiGetFileTree,
  getWorkspaceArtifacts as apiGetArtifacts,
  type WorkspacePayload,
  type FileTreeNodePayload,
  type ArtifactPayload,
} from '@/services/api/workspaces'
import { createTolerantStorage } from '@/utils/tolerantStorage'
import type { Artifact } from '@/types/artifact'
import type { Workspace, FileTreeNode } from '@/types/workspace'

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
  /** 条目写操作进行中的目标路径（新建/重命名/删除/移动 await 窗口；null=空闲）。
   *  右键菜单/输入框据此刻 disabled+pending，树 refetch 期间旧树保持可见。
   *  运行时态，不持久化。 */
  mutatingPath: string | null
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

/** 账户数据初始片段（cacheBoundary 切户清账消费；与 persist partialize 字段同域，
 * loading/error/mutatingPath 为运行时态随附重置） */
export const WORKSPACE_ACCOUNT_RESET = {
  workspaces: {},
  activeWorkspaceId: null,
  expandedPaths: new Set<string>(),
  selectedFilePath: null,
  loading: false,
  error: null,
  mutatingPath: null,
}

export const useWorkspaceStore = create<WorkspaceState & WorkspaceActions>()(
  persist(
    (set, get) => ({
      ...WORKSPACE_ACCOUNT_RESET,

  fetchWorkspace: async (containerTaskId) => {
    set({ loading: true, error: null })
    try {
      // 服务层统一走 apiClient：自动带 Authorization 头（请求拦截器）、
      // 401 进入统一 token 刷新链路、5xx/429 自动重试。
      const data = await apiGetWorkspace(containerTaskId)
      if (data.error) {
        const envelope = data.error as { message?: string }
        set({ loading: false, error: envelope.message ?? '工作空间加载失败' })
        return null
      }
      const ws = _normalizeWorkspace(data)
      set((state) => ({
        workspaces: { ...state.workspaces, [containerTaskId]: ws },
        loading: false,
      }))
      return ws
    } catch (e) {
      set({ loading: false, error: e instanceof Error ? e.message : String(e) })
      return null
    }
  },

  fetchFileTree: async (containerTaskId) => {
    try {
      const { entries } = await apiGetFileTree(containerTaskId)
      const normalized = entries.map(_normalizeFileTreeNode)
        set((state) => {
        const ws = state.workspaces[containerTaskId]
        if (ws) {
          return {
            workspaces: {
              ...state.workspaces,
              [containerTaskId]: { ...ws, fileTree: normalized },
            },
          }
        }
        return state
      })
      return normalized
    } catch {
      return []
    }
  },

  fetchWorkspaceArtifacts: async (containerTaskId) => {
    try {
      const { items } = await apiGetArtifacts(containerTaskId)
      return items.map(_normalizeArtifact)
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

  // 条目写动作族：await 窗口置 mutatingPath（UI pending 真值源），失败 toast
  // 落在操作点（不静默不装成功），成功后 refetch 期间旧树保持可见
  //（fetchFileTree 仅在成功响应后替换树数据）。
  createEntry: async (containerTaskId, path, type) => {
    set({ mutatingPath: path })
    try {
      await apiCreateEntry(containerTaskId, path, type)
      await get().fetchFileTree(containerTaskId)
      return true
    } catch (e) {
      toast.error('创建失败', {
        description: e instanceof Error ? e.message : '未知错误',
      })
      return false
    } finally {
      set({ mutatingPath: null })
    }
  },

  deleteEntry: async (containerTaskId, path) => {
    set({ mutatingPath: path })
    try {
      await apiDeleteEntry(containerTaskId, path)
      await get().fetchFileTree(containerTaskId)
      return true
    } catch (e) {
      toast.error('删除失败', {
        description: e instanceof Error ? e.message : '未知错误',
      })
      return false
    } finally {
      set({ mutatingPath: null })
    }
  },

  renameEntry: async (containerTaskId, oldPath, newName) => {
    set({ mutatingPath: oldPath })
    try {
      await apiRenameEntry(containerTaskId, oldPath, newName)
      await get().fetchFileTree(containerTaskId)
      return true
    } catch (e) {
      toast.error('重命名失败', {
        description: e instanceof Error ? e.message : '未知错误',
      })
      return false
    } finally {
      set({ mutatingPath: null })
    }
  },

  moveEntry: async (containerTaskId, sourcePath, destinationDir) => {
    set({ mutatingPath: sourcePath })
    try {
      await apiMoveEntry(containerTaskId, sourcePath, destinationDir)
      await get().fetchFileTree(containerTaskId)
      return true
    } catch (e) {
      toast.error('移动失败', {
        description: e instanceof Error ? e.message : '未知错误',
      })
      return false
    } finally {
      set({ mutatingPath: null })
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
}),
    // 重登后用户需重新展开目录树、重新选中文件，体验差。
    // 注意：loading/error 是运行时状态，不持久化。
    // expandedPaths 是 Set，需在 partialize/merge 做 数组↔Set 转换。
    {
      name: 'workspace-store',
      version: 1,
      // 配额满时吞掉 QuotaExceededError，避免 toggleExpand 等 action 崩溃
      storage: createTolerantStorage(),
      partialize: (state) => ({
        workspaces: state.workspaces,
        activeWorkspaceId: state.activeWorkspaceId,
        expandedPaths: Array.from(state.expandedPaths),
        selectedFilePath: state.selectedFilePath,
      }),
      merge: (persisted, current) => {
        const p = (persisted as Partial<WorkspaceState> & { expandedPaths?: unknown }) || {}
        return {
          ...current,
          ...p,
          // Set 类型字段从数组还原
          expandedPaths: new Set<string>(
            Array.isArray(p.expandedPaths) ? (p.expandedPaths as string[]) : [],
          ),
          // 运行时状态强制重置
          loading: false,
          error: null,
          mutatingPath: null,
        }
      },
    },
  ),
)

function _normalizeWorkspace(data: WorkspacePayload): Workspace {
  const id = typeof data.id === 'string' ? data.id : ''
  if (!id) {
    console.error('[workspaceStore] _normalizeWorkspace: id 字段缺失', data)
  }
  return {
    id,
    containerTaskId: typeof data.containerTaskId === 'string' ? data.containerTaskId : '',
    sessionId: typeof data.sessionId === 'string' ? data.sessionId : '',
    title: typeof data.title === 'string' ? data.title : '',
    description: typeof data.description === 'string' ? data.description : '',
    fileTree: (Array.isArray(data.fileTree) ? (data.fileTree as FileTreeNodePayload[]) : []).map(
      _normalizeFileTreeNode,
    ),
    createdAt: typeof data.createdAt === 'string' ? data.createdAt : '',
    updatedAt: typeof data.updatedAt === 'string' ? data.updatedAt : '',
  }
}

function _normalizeFileTreeNode(data: FileTreeNodePayload): FileTreeNode {
  return {
    name: data.name ?? '',
    // link（junction/symlink）归 directory 形态；契约外值仍回退 'file'
    type:
      data.type === 'file'
        ? 'file'
        : data.type === 'directory' || data.type === 'link'
          ? 'directory'
          : 'file',
    path: data.path ?? '',
    artifactId: data.artifactId,
    // 单层列举无 children（子层懒加载另列），保持字段形状兼容消费方
    children: undefined,
    metadata: data.metadata,
  }
}

/** 契约内制品类型（ArtifactType 联合的字面量表，用于运行时校验） */
const ARTIFACT_TYPES = ['text', 'image', 'video', 'code', 'document', 'data', 'composite'] as const

function _normalizeArtifact(data: ArtifactPayload): Artifact {
  const id = typeof data.id === 'string' ? data.id : ''
  if (!id) {
    console.error('[workspaceStore] _normalizeArtifact: id 字段缺失', data)
  }
  const rawType = typeof data.artifactType === 'string' ? data.artifactType : ''
  return {
    id,
    taskId: typeof data.taskId === 'string' ? data.taskId : '',
    title: typeof data.title === 'string' ? data.title : '',
    // 契约外类型回退 'text'
    artifactType: (ARTIFACT_TYPES as readonly string[]).includes(rawType)
      ? (rawType as Artifact['artifactType'])
      : 'text',
    content: typeof data.content === 'string' ? data.content : '',
    filePath: typeof data.filePath === 'string' ? data.filePath : undefined,
    version: typeof data.version === 'number' ? data.version : 1,
    parentArtifactId: typeof data.parentArtifactId === 'string' ? data.parentArtifactId : undefined,
    metadata: (data.metadata as Record<string, unknown> | undefined) ?? {},
    createdAt: typeof data.createdAt === 'string' ? data.createdAt : '',
    updatedAt: typeof data.updatedAt === 'string' ? data.updatedAt : '',
  }
}
