/**
 * PromptPreviewModal 组件
 *
 * Agent 管理页「查看组装提示词」入口：调 agent_manager prompt-preview 端点
 * （组装委托 prompt_build prompt.preview 服务，单源零复制），展示组装后的
 * SystemMessage 与动态变量消息。预览态语义：无任务工作空间，
 * {{session_workspace}} 注入为空（与运行时有任务工作空间的组装存在差异）。
 */

import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Copy, X } from '@/assets/icons'
import { getAgentPromptPreview } from '@/services/api/agents'

/** PromptPreviewModal 属性 */
export interface PromptPreviewModalProps {
  /** 当前预览的 Agent（null 时不渲染） */
  agent: { id: string; name?: string } | null
  /** 是否打开 */
  isOpen: boolean
  /** 关闭回调 */
  onClose: () => void
}

/**
 * Agent 组装提示词预览模态框
 *
 * @param props - agent/isOpen/onClose
 * @returns 预览模态框
 */
export function PromptPreviewModal({ agent, isOpen, onClose }: PromptPreviewModalProps) {
  const [copied, setCopied] = useState(false)
  const query = useQuery({
    queryKey: ['agentPromptPreview', agent?.id],
    queryFn: () => getAgentPromptPreview(agent!.id),
    enabled: isOpen && !!agent,
  })

  if (!agent || !isOpen) return null

  const preview = query.data
  const dynContent = preview?.dynamic_vars_message?.content || ''

  const copySystemMessage = () => {
    navigator.clipboard.writeText(preview?.system_message ?? '').then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    })
  }

  return (
    <div
      className="bg-[var(--overlay-strong)] fixed inset-0 z-50 flex items-center justify-center p-6"
      role="dialog"
      aria-modal="true"
      aria-label={`组装提示词 — ${agent.name ?? agent.id}`}
      onClick={onClose}
    >
      <div
        className="bg-background flex max-h-[85vh] w-full max-w-3xl flex-col rounded-lg border shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b px-4 py-3">
          <h2 className="text-sm font-semibold">组装提示词 — {agent.name ?? agent.id}</h2>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={copySystemMessage}
              disabled={!preview}
              className="text-muted-foreground hover:bg-muted/50 hover:text-foreground rounded-md border p-1.5 transition-colors disabled:opacity-50"
              aria-label="复制系统提示词"
              title={copied ? '已复制' : '复制 SystemMessage'}
            >
              <Copy className="h-3.5 w-3.5" />
            </button>
            <button
              type="button"
              onClick={onClose}
              className="text-muted-foreground hover:bg-muted/50 hover:text-foreground rounded-md p-1.5 transition-colors"
              aria-label="关闭预览"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>

        <div className="flex-1 overflow-y-auto px-4 py-3">
          {query.isLoading && (
            <p className="text-muted-foreground py-8 text-center text-sm">组装中...</p>
          )}
          {query.isError && (
            <div className="py-8 text-center">
              <p className="text-destructive mb-2 text-sm">
                获取组装提示词失败：{query.error instanceof Error ? query.error.message : '未知错误'}
              </p>
              <button
                type="button"
                onClick={() => query.refetch()}
                className="hover:bg-muted/50 rounded-md border px-3 py-1 text-xs"
              >
                重试
              </button>
            </div>
          )}
          {preview && (
            <>
              <pre className="text-muted-foreground max-h-[55vh] overflow-auto whitespace-pre-wrap rounded border p-3 font-mono text-xs leading-relaxed">
                {preview.system_message || '（空提示词）'}
              </pre>
              {dynContent && (
                <div className="mt-3">
                  <p className="text-muted-foreground mb-1 text-xs font-semibold">
                    动态变量消息（运行时追加在历史消息之后）
                  </p>
                  <pre className="bg-accent/10 text-muted-foreground overflow-auto whitespace-pre-wrap rounded border p-3 font-mono text-xs leading-relaxed">
                    {dynContent}
                  </pre>
                </div>
              )}
            </>
          )}
        </div>

        <div className="text-muted-foreground border-t px-4 py-2 text-xs">
          预览态：无任务工作空间，session_workspace 注入为空；工具描述/记忆检索等运行时注入不在此呈现。
        </div>
      </div>
    </div>
  )
}

export default PromptPreviewModal
