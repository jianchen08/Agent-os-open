/**
 * 消息操作自定义 Hook
 */

import { useCallback } from 'react'

/**
 * 消息操作 Hook
 */
export const useMessageActions = (_sessionId?: string) => {
  const editMessage = useCallback(
    async (_messageId: string, _newContent: string) => {
      throw new Error('编辑功能暂不可用')
    },
    [_sessionId],
  )

  const deleteMessage = useCallback(
    async (_messageId: string, _includeTarget: boolean = true) => {
      throw new Error('删除功能暂不可用')
    },
    [_sessionId],
  )

  const retryMessageWithScope = useCallback(
    async (_messageId: string, _scope: string = 'all', _targetToolId?: string) => {
      throw new Error('重试功能暂不可用')
    },
    [_sessionId],
  )

  const getMessageVersions = useCallback(
    async (_messageId: string) => {
      return []
    },
    [_sessionId],
  )

  return {
    editMessage,
    deleteMessage,
    retryMessageWithScope,
    getMessageVersions,
  }
}
