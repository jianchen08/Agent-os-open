/**
 * 文件树上下文菜单组件
 *
 * 提供文件/目录的右键操作菜单，支持创建、删除、重命名、移动等操作。
 * 包含 inline rename 输入框和移动目标选择对话框。
 *
 * @module FileTreeContextMenu
 */

import {
  Plus,
  Trash2,
  Edit3,
  FolderPlus,
  Move,
  Folder,
  X,
} from 'lucide-react'
import React, { useState, useCallback, useEffect, useRef } from 'react'
import { useWorkspaceStore } from '@/stores/workspaceStore'

/** 上下文菜单上下文数据 */
export interface ContextMenuContext {
  /** 工作空间 container_task_id */
  containerTaskId: string
  /** 右键目标节点路径（null 表示空白区域） */
  targetPath: string | null
  /** 右键目标节点名称 */
  targetName: string | null
  /** 右键目标是否为目录 */
  isDirectory: boolean
  /** 右键目标父目录路径（用于空白区域新建） */
  parentDir: string
  /** 当前文件树根数据（用于移动对话框列出目录） */
  treeData: ContextMenuTreeNode[]
  /** 操作完成后的刷新回调 */
  onRefresh: () => void
}

/** 菜单可用的简化树节点 */
export interface ContextMenuTreeNode {
  /** 节点名称 */
  name: string
  /** 节点路径 */
  path: string
  /** 是否为目录 */
  isDirectory: boolean
  /** 子节点 */
  children?: ContextMenuTreeNode[]
}

/** 菜单项定义 */
interface MenuItem {
  /** 唯一标识 */
  id: string
  /** 显示文本 */
  label: string
  /** 图标 */
  icon: React.ReactNode
  /** 是否显示 */
  visible: boolean
  /** 是否危险操作 */
  danger?: boolean
}

/**
 * 从菜单项数据中递归提取所有目录节点（用于移动对话框的目标选择）
 */
function collectDirectories(
  nodes: ContextMenuTreeNode[],
  excludePath?: string | null,
  prefix: string = '',
): { path: string; label: string; depth: number }[] {
  const result: { path: string; label: string; depth: number }[] = []
  for (const node of nodes) {
    if (node.isDirectory) {
      // 排除自身（不能移动到自身）
      if (node.path !== excludePath) {
        result.push({
          path: node.path,
          label: `${prefix}${node.name}`,
          depth: prefix.split('/').length - 1,
        })
        if (node.children) {
          result.push(...collectDirectories(node.children, excludePath, `${prefix}${node.name}/`))
        }
      }
    }
  }
  return result
}

/**
 * 文件树上下文菜单组件
 *
 * @param props - 菜单属性
 * @returns 上下文菜单渲染结果
 */
export function FileTreeContextMenu(props: {
  /** 菜单定位 X */
  x: number
  /** 菜单定位 Y */
  y: number
  /** 菜单上下文 */
  context: ContextMenuContext
  /** 关闭菜单回调 */
  onClose: () => void
}): React.ReactNode {
  const { x, y, context, onClose } = props
  const { containerTaskId, targetPath, targetName, isDirectory, parentDir, treeData, onRefresh } = context

  const menuRef = useRef<HTMLDivElement>(null)
  const renameInputRef = useRef<HTMLInputElement>(null)
  const [renaming, setRenaming] = useState(false)
  const [renameValue, setRenameValue] = useState('')
  const [showMoveDialog, setShowMoveDialog] = useState(false)
  const [creating, setCreating] = useState<'file' | 'directory' | null>(null)
  const [createValue, setCreateValue] = useState('')
  const createInputRef = useRef<HTMLInputElement>(null)

  const store = useWorkspaceStore()

  /** 点击外部关闭菜单 */
  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        onClose()
      }
    }
    // 延迟绑定，避免触发菜单的右键事件立即关闭
    const timer = setTimeout(() => {
      document.addEventListener('mousedown', handleClickOutside)
    }, 0)
    return () => {
      clearTimeout(timer)
      document.removeEventListener('mousedown', handleClickOutside)
    }
  }, [onClose])

  /** Esc 关闭 */
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        if (renaming || creating) {
          setRenaming(false)
          setCreating(null)
        } else if (showMoveDialog) {
          setShowMoveDialog(false)
        } else {
          onClose()
        }
      }
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [onClose, renaming, creating, showMoveDialog])

  /** 自动聚焦重命名输入框 */
  useEffect(() => {
    if (renaming && renameInputRef.current) {
      renameInputRef.current.focus()
      renameInputRef.current.select()
    }
  }, [renaming])

  /** 自动聚焦新建输入框 */
  useEffect(() => {
    if (creating && createInputRef.current) {
      createInputRef.current.focus()
    }
  }, [creating])

  /** 开始重命名 */
  const handleStartRename = useCallback(() => {
    if (!targetName) return
    setRenameValue(targetName)
    setRenaming(true)
  }, [targetName])

  /** 确认重命名 */
  const handleConfirmRename = useCallback(async () => {
    const trimmed = renameValue.trim()
    if (!targetPath || !trimmed || trimmed === targetName) {
      setRenaming(false)
      return
    }
    const success = await store.renameEntry(containerTaskId, targetPath, trimmed)
    setRenaming(false)
    if (success) {
      onRefresh()
    }
  }, [renameValue, targetPath, targetName, containerTaskId, store, onRefresh])

  /** 开始新建 */
  const handleStartCreate = useCallback((type: 'file' | 'directory') => {
    setCreating(type)
    setCreateValue(type === 'file' ? 'new_file.txt' : 'new_folder')
  }, [])

  /** 确认新建 */
  const handleConfirmCreate = useCallback(async () => {
    const trimmed = createValue.trim()
    if (!trimmed || !creating) {
      setCreating(null)
      return
    }
    const baseDir = isDirectory && targetPath ? targetPath : parentDir
    const fullPath = baseDir ? `${baseDir}/${trimmed}` : trimmed
    const success = await store.createEntry(containerTaskId, fullPath, creating)
    setCreating(null)
    if (success) {
      onRefresh()
    }
  }, [createValue, creating, isDirectory, targetPath, parentDir, containerTaskId, store, onRefresh])

  /** 删除确认 */
  const handleDelete = useCallback(() => {
    if (!targetPath || !targetName) return
    const confirmed = window.confirm(`确定要删除 "${targetName}" 吗？此操作不可撤销。`)
    if (!confirmed) return
    store.deleteEntry(containerTaskId, targetPath).then((success) => {
      if (success) {
        onRefresh()
      }
    })
    onClose()
  }, [targetPath, targetName, containerTaskId, store, onRefresh, onClose])

  /** 移动操作 */
  const handleMove = useCallback(async (destinationDir: string) => {
    if (!targetPath) return
    const success = await store.moveEntry(containerTaskId, targetPath, destinationDir)
    setShowMoveDialog(false)
    if (success) {
      onRefresh()
    }
    onClose()
  }, [targetPath, containerTaskId, store, onRefresh, onClose])

  /** 构建菜单项列表 */
  const menuItems: MenuItem[] = [
    {
      id: 'new-file',
      label: '新建文件',
      icon: <Plus className="h-3.5 w-3.5" />,
      visible: isDirectory || targetPath === null,
    },
    {
      id: 'new-folder',
      label: '新建文件夹',
      icon: <FolderPlus className="h-3.5 w-3.5" />,
      visible: isDirectory || targetPath === null,
    },
    {
      id: 'rename',
      label: '重命名',
      icon: <Edit3 className="h-3.5 w-3.5" />,
      visible: targetPath !== null,
    },
    {
      id: 'delete',
      label: '删除',
      icon: <Trash2 className="h-3.5 w-3.5" />,
      visible: targetPath !== null,
      danger: true,
    },
    {
      id: 'move',
      label: '移动到...',
      icon: <Move className="h-3.5 w-3.5" />,
      visible: targetPath !== null,
    },
  ]

  const visibleItems = menuItems.filter((item) => item.visible)

  /** 重命名输入框 */
  if (renaming) {
    return (
      <div
        className="fixed inset-0 z-50 flex items-start justify-center pt-20"
        onClick={(e) => { if (e.target === e.currentTarget) { setRenaming(false) } }}
      >
        <div className="bg-background w-72 rounded-lg border p-3 shadow-xl" ref={menuRef}>
          <p className="text-foreground mb-2 text-xs font-medium">重命名</p>
          <input
            ref={renameInputRef}
            type="text"
            value={renameValue}
            onChange={(e) => setRenameValue(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') handleConfirmRename()
              if (e.key === 'Escape') setRenaming(false)
            }}
            className="bg-muted/50 focus:bg-background w-full rounded-md border px-2 py-1.5 text-sm outline-none transition-colors focus:border-status-info/50"
          />
          <div className="mt-2 flex justify-end gap-2">
            <button
              onClick={() => setRenaming(false)}
              className="text-muted-foreground hover:text-foreground rounded px-2 py-1 text-xs transition-colors"
            >
              取消
            </button>
            <button
              onClick={handleConfirmRename}
              className="bg-primary text-primary-foreground rounded px-2 py-1 text-xs transition-colors hover:opacity-90"
            >
              确认
            </button>
          </div>
        </div>
      </div>
    )
  }

  /** 新建输入框 */
  if (creating) {
    return (
      <div
        className="fixed inset-0 z-50 flex items-start justify-center pt-20"
        onClick={(e) => { if (e.target === e.currentTarget) { setCreating(null) } }}
      >
        <div className="bg-background w-72 rounded-lg border p-3 shadow-xl" ref={menuRef}>
          <p className="text-foreground mb-2 text-xs font-medium">
            新建{creating === 'file' ? '文件' : '文件夹'}
          </p>
          <input
            ref={createInputRef}
            type="text"
            value={createValue}
            onChange={(e) => setCreateValue(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') handleConfirmCreate()
              if (e.key === 'Escape') setCreating(null)
            }}
            className="bg-muted/50 focus:bg-background w-full rounded-md border px-2 py-1.5 text-sm outline-none transition-colors focus:border-status-info/50"
          />
          <div className="mt-2 flex justify-end gap-2">
            <button
              onClick={() => setCreating(null)}
              className="text-muted-foreground hover:text-foreground rounded px-2 py-1 text-xs transition-colors"
            >
              取消
            </button>
            <button
              onClick={handleConfirmCreate}
              className="bg-primary text-primary-foreground rounded px-2 py-1 text-xs transition-colors hover:opacity-90"
            >
              确认
            </button>
          </div>
        </div>
      </div>
    )
  }

  /** 移动对话框 */
  if (showMoveDialog) {
    const directories = collectDirectories(treeData, targetPath)
    return (
      <div
        className="fixed inset-0 z-50 flex items-start justify-center pt-20"
        onClick={(e) => { if (e.target === e.currentTarget) { setShowMoveDialog(false) } }}
      >
        <div className="bg-background max-h-80 w-80 overflow-hidden rounded-lg border shadow-xl" ref={menuRef}>
          <div className="flex items-center justify-between border-b px-3 py-2">
            <span className="text-foreground text-xs font-medium">移动到...</span>
            <button
              onClick={() => setShowMoveDialog(false)}
              className="text-muted-foreground hover:text-foreground rounded p-0.5 transition-colors"
            >
              <X className="h-3.5 w-3.5" />
            </button>
          </div>
          {/* 根目录选项 */}
          <button
            onClick={() => handleMove('/')}
            className="hover:bg-accent flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs transition-colors"
          >
            <Folder className="text-status-warning h-3.5 w-3.5 shrink-0" />
            <span className="text-foreground">/ (根目录)</span>
          </button>
          {directories.length === 0 ? (
            <div className="text-muted-foreground px-3 py-4 text-center text-xs">
              没有可用的目标文件夹
            </div>
          ) : (
            <div className="max-h-56 overflow-y-auto">
              {directories.map((dir) => (
                <button
                  key={dir.path}
                  onClick={() => handleMove(dir.path)}
                  className="hover:bg-accent flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs transition-colors"
                  style={{ paddingLeft: `${dir.depth * 12 + 12}px` }}
                >
                  <Folder className="text-status-warning h-3.5 w-3.5 shrink-0" />
                  <span className="text-foreground truncate">{dir.label}</span>
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
    )
  }

  /** 右键菜单 */
  return (
    <div
      ref={menuRef}
      className="bg-background z-50 min-w-40 rounded-lg border py-1 shadow-xl"
      style={{
        position: 'fixed',
        left: x,
        top: y,
      }}
    >
      {visibleItems.map((item) => (
        <button
          key={item.id}
          className={`flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs transition-colors ${
            item.danger
              ? 'text-status-error hover:bg-status-error/10'
              : 'text-foreground hover:bg-accent'
          }`}
          onClick={() => {
            switch (item.id) {
              case 'new-file':
                handleStartCreate('file')
                break
              case 'new-folder':
                handleStartCreate('directory')
                break
              case 'rename':
                handleStartRename()
                break
              case 'delete':
                handleDelete()
                break
              case 'move':
                setShowMoveDialog(true)
                break
            }
          }}
        >
          {item.icon}
          <span>{item.label}</span>
        </button>
      ))}
    </div>
  )
}
