/**
 * 消息操作按钮组件
 *
 * 提供消息的重试、编辑、删除、版本切换等功能
 */

import { ChevronLeft, ChevronRight, Copy, Edit2, RefreshCw, Trash2 } from 'lucide-react'
import { useEffect, useState, type FC } from 'react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { useMessageActions } from '@/hooks/useMessageActions'
import { loggers } from '@/utils/logger'
import type { MessageVersion as ApiMessageVersion } from '@/services/api/messages'
import type { Message } from '@/types/models'

/**
 * 消息操作组件属性
 */
export interface MessageActionsProps {
  /** 消息对象 */
  message: Message
  /** 会话 ID */
  sessionId: string
  /** 是否为用户消息 */
  isUserMessage?: boolean
  /** 是否禁用操作 */
  disabled?: boolean
  /** 是否为最后一条消息（控制重试按钮显示） */
  isLastMessage?: boolean
  /** 消息复制回调 */
  onCopy?: () => void
  /** 消息编辑回调 */
  onEdit?: () => void
  /** 消息内容更新回调（用于版本切换） */
  onContentUpdate?: (content: string) => void
}

/**
 * 消息操作按钮组件
 */
export const MessageActions: FC<MessageActionsProps> = ({
  message,
  sessionId,
  isUserMessage = false,
  disabled = false,
  isLastMessage = false,
  onCopy,
  onEdit,
  onContentUpdate,
}) => {
  const { retryMessageWithScope, getMessageVersions, deleteMessage } = useMessageActions(sessionId)
  const [versions, setVersions] = useState<ApiMessageVersion[]>([])
  const [currentVersionIndex, setCurrentVersionIndex] = useState(0)
  const [isDeleting, setIsDeleting] = useState(false)

  // 消息版本功能暂未启用（后端未实现版本存储），跳过版本加载
  // 启用后取消下方注释：
  // useEffect(() => {
  //   const loadVersions = async () => {
  //     if (!sessionId || !message.id || message.id.startsWith('temp-')) return
  //     if (message.role !== 'assistant' || !message.content?.trim()) return
  //
  //     try {
  //       const versionList = await getMessageVersions(message.id)
  //       setVersions(versionList as ApiMessageVersion[])
  //       const idx = versionList.findIndex((v) => v.is_current)
  //       setCurrentVersionIndex(idx >= 0 ? idx : 0)
  //     } catch {
  //       setVersions([])
  //     }
  //   }
  //   const tid = setTimeout(loadVersions, 50)
  //   return () => clearTimeout(tid)
  // }, [message.id, sessionId, getMessageVersions, message.role, message.content])

  const totalVersions = versions.length
  const hasMultipleVersions = totalVersions > 1

  /**
   * 处理重试操作 - 重新生成 AI 回复
   */
  const handleRetry = async () => {
    try {
      if (message.id.startsWith('temp-')) {
        toast.error('消息正在保存中,请稍后重试')
        return
      }

      await retryMessageWithScope(message.id, 'all')
      toast.success('正在重新生成...')

      setTimeout(async () => {
        try {
          const versionList = await getMessageVersions(message.id)
          if (versionList.length > 0) {
            const validVersions = versionList.filter(
              (v) => v.content && v.content.trim().length > 0,
            )

            if (validVersions.length > 0) {
              setVersions(validVersions as ApiMessageVersion[])
              const latestValidIndex = validVersions.length - 1
              setCurrentVersionIndex(latestValidIndex)
            }
          }
        } catch (_error) {
          // 静默处理延迟加载版本失败
        }
      }, 1500)
    } catch (error) {
      const errorMsg = error instanceof Error ? error.message : String(error)
      toast.error(`重新生成失败: ${errorMsg}`)
    }
  }

  /**
   * 处理删除操作
   */
  const handleDelete = async () => {
    const confirmed = window.confirm('确定要删除这条消息吗？该消息之后的所有消息也会被删除。')

    if (!confirmed) return

    try {
      setIsDeleting(true)
      await deleteMessage(message.id)
    } catch (_error) {
      // 删除失败静默处理
    } finally {
      setIsDeleting(false)
    }
  }

  /**
   * 切换到上一个版本
   */
  const handlePreviousVersion = () => {
    if (currentVersionIndex > 0) {
      const newIndex = currentVersionIndex - 1
      setCurrentVersionIndex(newIndex)
      const prevVersion = versions[newIndex]
      if (onContentUpdate && prevVersion) {
        const content =
          prevVersion.content && prevVersion.content.trim().length > 0
            ? prevVersion.content
            : '(此版本内容为空)'
        onContentUpdate(content)
      }
    }
  }

  /**
   * 切换到下一个版本
   */
  const handleNextVersion = () => {
    if (currentVersionIndex < totalVersions - 1) {
      const newIndex = currentVersionIndex + 1
      setCurrentVersionIndex(newIndex)
      const nextVersion = versions[newIndex]
      if (onContentUpdate && nextVersion) {
        const content =
          nextVersion.content && nextVersion.content.trim().length > 0
            ? nextVersion.content
            : '(此版本内容为空)'
        onContentUpdate(content)
      }
    }
  }

  /**
   * 处理复制操作
   */
  const handleCopy = () => {
    if (onCopy) {
      onCopy()
    } else {
      navigator.clipboard.writeText(message.content)
      toast.success('已复制到剪贴板')
    }
  }

  /**
   * 处理编辑操作
   */
  const handleEdit = () => {
    if (onEdit) {
      onEdit()
    } else {
      toast.info('编辑功能暂不可用')
    }
  }

  // 用户消息显示复制、编辑和删除按钮
  if (isUserMessage) {
    return (
      <div className="flex items-center gap-0.5 opacity-0 transition-opacity group-hover:opacity-100">
        <Button
          variant="ghost"
          size="sm"
          className="h-6 w-6 p-0"
          onClick={handleCopy}
          disabled={disabled}
          title="复制"
        >
          <Copy className="h-3 w-3" />
        </Button>
        <Button
          variant="ghost"
          size="sm"
          className="h-6 w-6 p-0"
          onClick={handleEdit}
          disabled={disabled}
          title="编辑"
        >
          <Edit2 className="h-3 w-3" />
        </Button>
        <Button
          variant="ghost"
          size="sm"
          className="text-destructive hover:text-destructive h-6 w-6 p-0"
          onClick={handleDelete}
          disabled={disabled || isDeleting}
          title="删除"
        >
          <Trash2 className="h-3 w-3" />
        </Button>
      </div>
    )
  }

  // AI 消息显示完整操作按钮
  return (
    <div className="flex items-center gap-0.5 opacity-0 transition-opacity group-hover:opacity-100">
      <Button
        variant="ghost"
        size="sm"
        className="h-6 w-6 p-0"
        onClick={handleCopy}
        disabled={disabled}
        title="复制"
      >
        <Copy className="h-3 w-3" />
      </Button>

      {hasMultipleVersions && (
        <div className="bg-muted mx-0.5 flex items-center gap-0.5 rounded-md px-1.5 py-0.5">
          <Button
            variant="ghost"
            size="sm"
            className="h-5 w-5 p-0"
            onClick={handlePreviousVersion}
            disabled={disabled || currentVersionIndex === 0}
            title="上一个版本"
          >
            <ChevronLeft className="h-3 w-3" />
          </Button>

          <span className="text-muted-foreground min-w-[32px] text-center text-xs">
            {currentVersionIndex + 1}/{totalVersions}
          </span>

          <Button
            variant="ghost"
            size="sm"
            className="h-5 w-5 p-0"
            onClick={handleNextVersion}
            disabled={disabled || currentVersionIndex === totalVersions - 1}
            title="下一个版本"
          >
            <ChevronRight className="h-3 w-3" />
          </Button>
        </div>
      )}

      {isLastMessage && (
        <Button
          variant="ghost"
          size="sm"
          className="h-6 w-6 p-0"
          onClick={handleRetry}
          disabled={disabled}
          title="重新生成"
        >
          <RefreshCw className="h-3 w-3" />
        </Button>
      )}

      <Button
        variant="ghost"
        size="sm"
        className="text-destructive hover:text-destructive h-6 w-6 p-0"
        onClick={handleDelete}
        disabled={disabled}
        title="删除"
      >
        <Trash2 className="h-3 w-3" />
      </Button>
    </div>
  )
}
