/**
 * 工作空间类型定义
 *
 * 定义 Workspace、FileTreeNode 等类型，
 * 对应后端 workspace 模块的数据结构。
 */

/** 文件树节点 */
export interface FileTreeNode {
  name: string
  type: 'file' | 'directory'
  path: string
  artifactId?: string
  children?: FileTreeNode[]
  metadata?: Record<string, any>
}

/** 工作空间 */
export interface Workspace {
  id: string
  containerTaskId: string
  sessionId: string
  title: string
  description: string
  fileTree: FileTreeNode[]
  createdAt: string
  updatedAt: string
}
