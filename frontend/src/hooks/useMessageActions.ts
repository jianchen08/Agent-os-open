/**
 * 消息操作自定义 Hook
 *
 * 提供消息的编辑、删除、重试等功能的封装
 */

import { useCallback } from 'react'
import { toast } from 'sonner'
import { messageApi } from '@/services/api/messages'
import { ErrorType, reportError } from '@/services/errorReporting'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { useSessionStore } from '@/stores/sessionStore'
import type { RetryScope } from '@/types/models'

/**
 * 消息操作 Hook
 */
export const useMessageActions = (sessionId?: string) => {
  const sessionStore = useSessionStore()

  /**
   * 编辑消息
   */
  const editMessage = useCallback(
    async (messageId: string, newContent: string) => {
      if (!sessionId) {
        throw new Error('sessionId is required for editing message')
      }
      try {
        const result = await messageApi.editMessage(sessionId, messageId, newContent)
        toast.success('消息已更新')
        return result
      } catch (error) {
        reportError(
          error instanceof Error ? error.message : String(error),
          ErrorType.SERVER,
          undefined,
          {
            componentName: 'useMessageActions',
            operation: 'editMessage',
            messageId,
          },
        )
        toast.error('编辑消息失败')
        throw error
      }
    },
    [sessionId],
  )

  /**
   * 删除消息
   *
   * @param messageId 消息 ID
   * @param includeTarget 是否包含目标消息本身（默认 true，删除当前消息及之后的所有消息）
   */
  const deleteMessage = useCallback(
    async (messageId: string, includeTarget: boolean = true) => {
      if (!sessionId) {
        throw new Error('sessionId is required for deleting message')
      }
      try {
        // 直接调用 pipelineStore 的 deleteMessage（包含乐观更新 + API 调用）
        usePipelineMessageStore.getState().deleteMessage(sessionId, messageId, includeTarget)

        toast.success('消息已删除')
      } catch (error) {
        reportError(
          error instanceof Error ? error.message : String(error),
          ErrorType.SERVER,
          undefined,
          {
            componentName: 'useMessageActions',
            operation: 'deleteMessage',
            messageId,
          },
        )
        toast.error('删除消息失败')
        throw error
      }
    },
    [sessionId, sessionStore],
  )

  /**
   * 重试消息（支持部分重试）
   *
   * @param messageId 消息 ID
   * @param scope 重试范围：all | failed_tools | specific_tool
   * @param targetToolId 目标工具ID（scope='specific_tool' 时必需）
   */
  const retryMessageWithScope = useCallback(
    async (messageId: string, scope: RetryScope = 'all', targetToolId?: string) => {
      if (!sessionId) {
        throw new Error('sessionId is required for retrying message')
      }

      // 临时消息不能重试
      if (messageId.startsWith('temp-')) {
        console.warn('[useMessageActions] 临时消息不能重试 | messageId:', messageId)
        toast.error('消息正在保存中,请稍后重试')
        return
      }

      try {
        // 使用 sessionStore 的重试方法
        await sessionStore.retryMessage(sessionId, messageId, scope, targetToolId)

        // 根据重试范围显示不同的提示
        const scopeText = {
          all: '全部内容',
          failed_tools: '失败的工具',
          specific_tool: '特定工具',
        }[scope]

        toast.success(`开始重新生成${scopeText}`)
      } catch (error: unknown) {
        const errorMessage = error instanceof Error ? error.message : String(error)
        const errorCode = (error as { code?: number })?.code

        // 404 错误或消息不存在，静默处理（可能是临时消息）
        const isNotFoundError =
          errorCode === 404 ||
          errorMessage.includes('消息不存在') ||
          errorMessage.includes('[VALIDATION] 消息不存在') ||
          errorMessage.includes('不存在')

        if (isNotFoundError) {
          console.warn('[useMessageActions] 重试失败（消息不存在）| messageId:', messageId)
          toast.error('消息不存在，无法重试')
          throw error
        }

        // 其他错误才上报
        reportError(errorMessage, ErrorType.SERVER, undefined, {
          componentName: 'useMessageActions',
          operation: 'retryMessageWithScope',
          messageId,
          scope,
          targetToolId,
        })
        toast.error('重试消息失败')
        throw error
      }
    },
    [sessionId, sessionStore],
  )

  /**
   * 创建消息版本快照
   *
   * @param messageId 消息 ID
   * @returns 版本号
   */
  const createMessageVersion = useCallback(
    (messageId: string) => {
      if (!sessionId) {
        throw new Error('sessionId is required for creating message version')
      }

      try {
        const version = sessionStore.createMessageVersion(sessionId, messageId)
        toast.success(`已创建版本 ${version}`)
        return version
      } catch (error) {
        reportError(
          error instanceof Error ? error.message : String(error),
          ErrorType.SERVER,
          undefined,
          {
            componentName: 'useMessageActions',
            operation: 'createMessageVersion',
            messageId,
          },
        )
        toast.error('创建版本失败')
        throw error
      }
    },
    [sessionId, sessionStore],
  )

  /**
   * 恢复到指定版本
   *
   * @param messageId 消息 ID
   * @param version 版本号
   */
  const restoreMessageVersion = useCallback(
    (messageId: string, version: number) => {
      if (!sessionId) {
        throw new Error('sessionId is required for restoring message version')
      }

      try {
        sessionStore.restoreMessageVersion(sessionId, messageId, version)
        toast.success(`已恢复到版本 ${version}`)
      } catch (error) {
        reportError(
          error instanceof Error ? error.message : String(error),
          ErrorType.SERVER,
          undefined,
          {
            componentName: 'useMessageActions',
            operation: 'restoreMessageVersion',
            messageId,
            version,
          },
        )
        toast.error('恢复版本失败')
        throw error
      }
    },
    [sessionId, sessionStore],
  )

  /**
   * 获取消息版本列表
   */
  const getMessageVersions = useCallback(
    async (messageId: string) => {
      if (!sessionId) {
        throw new Error('sessionId is required for getting message versions')
      }

      // 提前检查临时消息
      if (messageId.startsWith('temp-')) {
        return []
      }

      try {
        // 从后端 API 获取版本列表
        const response = await messageApi.getMessageVersions(sessionId, messageId)
        return response.versions
      } catch (error: unknown) {
        const axiosError = error as {
          response?: { status?: number; data?: { detail?: string } }
          code?: number
        }
        const errorCode = axiosError.response?.status || axiosError.code
        const errorMessage =
          axiosError.response?.data?.detail ||
          (error instanceof Error ? error.message : String(error))

        // 404 错误或消息不存在错误，静默处理
        const isNotFoundError =
          errorCode === 404 ||
          errorMessage.includes('消息不存在') ||
          errorMessage.includes('[VALIDATION] 消息不存在') ||
          errorMessage.includes('不存在')

        if (isNotFoundError) {
          console.warn(
            '[useMessageActions] 消息不存在，跳过版本加载 | messageId:',
            messageId,
            'error:',
            errorMessage,
          )
          return []
        }

        // 网络错误或超时错误，也静默处理
        const isNetworkError =
          errorCode === 0 ||
          errorMessage.includes('Network Error') ||
          errorMessage.includes('timeout') ||
          errorMessage.includes('ECONNREFUSED')

        if (isNetworkError) {
          console.warn(
            '[useMessageActions] 网络错误，跳过版本加载 | messageId:',
            messageId,
            'error:',
            errorMessage,
          )
          return []
        }

        // 其他错误才上报和显示提示
        reportError(errorMessage, ErrorType.SERVER, undefined, {
          componentName: 'useMessageActions',
          operation: 'getMessageVersions',
          messageId,
          errorCode,
        })
        toast.error('获取消息版本失败')
        throw error
      }
    },
    [sessionId],
  )

  return {
    editMessage,
    deleteMessage,
    retryMessageWithScope,
    createMessageVersion,
    restoreMessageVersion,
    getMessageVersions,
  }
}
