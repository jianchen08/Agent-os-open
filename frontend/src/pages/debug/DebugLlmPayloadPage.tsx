/**
 * 调试 LLM 请求页面（列表 query 化：useLlmPayloadDiagQuery 缓存 SWR，重挂零请求）
 *
 * 展示最近发送给大模型的真实请求体快照（payload_diag）：每次 LLM 调用前
 * 由 llm adapter 落盘的最终 HTTP body（含 model + 完整 messages）。
 * 点击条目展开逐条消息渲染；原始 JSON 可折叠查看。
 */

import { useState, useEffect, useCallback } from 'react'
import { ErrorState } from '@/components/shared/ErrorState'
import { LoadingState } from '@/components/shared/LoadingState'
import { PageShell } from '@/components/shared/PageShell'
import { useLlmPayloadDiagQuery } from '@/hooks/queries/useDebugQueries'
import { getPayloadDiagFile } from '@/services/api/llmPayload'
import type { PayloadDiagItem } from '@/services/api/llmPayload'

/** LLM content 分段（OpenAI 视觉块形态 {type:'text', text}） */
interface ContentBlock {
  text?: unknown
}

/** 从消息 content 提取纯文本（string 或分段数组） */
function contentText(content: unknown): string {
  if (typeof content === 'string') return content
  if (Array.isArray(content)) {
    return content
      .map((part) =>
        typeof part === 'string'
          ? part
          : part && typeof part === 'object'
            ? String((part as ContentBlock).text ?? '')
            : '',
      )
      .filter(Boolean)
      .join('\n')
  }
  return ''
}

/** 工具调用条目（OpenAI 形态：{id, function: {name, arguments}}） */
interface ToolCallItem {
  id?: string
  function?: { name?: string; arguments?: string }
}

/** 单条消息渲染（角色徽章 + 内容预览 + 工具调用/思考折叠） */
function MessageItem({ msg, index }: { msg: Record<string, unknown>; index: number }) {
  const text = contentText(msg.content)
  const role = typeof msg.role === 'string' ? msg.role : '?'
  const name = typeof msg.name === 'string' ? msg.name : null
  const toolCalls = Array.isArray(msg.tool_calls) ? (msg.tool_calls as ToolCallItem[]) : null
  const reasoning = typeof msg.reasoning_content === 'string' ? msg.reasoning_content : null

  return (
    <div className="rounded-lg border p-2">
      <div className="mb-1 flex items-center gap-2">
        <span className="bg-accent/40 rounded px-1.5 py-0.5 font-mono text-xs">#{index}</span>
        <span className="bg-primary/10 text-primary rounded px-1.5 py-0.5 text-xs font-medium">
          {role}
        </span>
        {name && <span className="text-muted-foreground text-xs">{name}</span>}
      </div>
      {text && (
        <pre className="max-h-40 overflow-auto whitespace-pre-wrap break-words rounded bg-accent/20 p-2 font-mono text-xs">
          {text.length > 2000 ? `${text.slice(0, 2000)}\n…（共 ${text.length} 字符，展开原始 JSON 看全文）` : text}
        </pre>
      )}
      {toolCalls && (
        <div className="mt-1 space-y-1">
          {toolCalls.map((tc, i) => (
            <div key={tc?.id ?? i} className="bg-accent/20 rounded p-1.5 font-mono text-xs break-all">
              🔧 {tc?.function?.name ?? `call-${i}`}
              {tc?.function?.arguments && (
                <span className="text-muted-foreground"> {String(tc.function.arguments).slice(0, 200)}</span>
              )}
            </div>
          ))}
        </div>
      )}
      {reasoning && (
        <details className="mt-1">
          <summary className="text-muted-foreground cursor-pointer text-xs">
            思考过程（{reasoning.length} 字符）
          </summary>
          <pre className="max-h-40 overflow-auto whitespace-pre-wrap break-words rounded bg-accent/20 p-2 font-mono text-xs">
            {reasoning.slice(0, 2000)}
          </pre>
        </details>
      )}
    </div>
  )
}

/** 快照详情：解析后的请求体（model/messages/参数）+ 原始 JSON 折叠 */
function PayloadDetail({ item }: { item: PayloadDiagItem }) {
  const [body, setBody] = useState<Record<string, unknown> | null>(null)
  const [raw, setRaw] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await getPayloadDiagFile(item.name)
      if (res.error) {
        setError(res.error)
      } else {
        setRaw(res.content)
        try {
          setBody(JSON.parse(res.content))
        } catch {
          setError('快照 JSON 解析失败')
        }
      }
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '读取快照失败')
    } finally {
      setLoading(false)
    }
  }, [item.name])

  useEffect(() => {
    load()
  }, [load])

  if (loading) return <div className="py-4"><LoadingState /></div>
  if (error) return <div className="py-2"><ErrorState message={error} /></div>
  if (!body) return null

  const messages: Record<string, unknown>[] = Array.isArray(body.messages) ? (body.messages as Record<string, unknown>[]) : []

  return (
    <div className="mt-2 space-y-2">
      <div className="flex flex-wrap gap-1.5 text-xs">
        <span className="bg-primary/10 text-primary rounded px-1.5 py-0.5 font-mono">{String(body.model ?? '') || item.model}</span>
        {Object.keys(body)
          .filter((k) => k !== 'messages' && k !== 'model')
          .map((k) => (
            <span key={k} className="bg-accent/30 rounded px-1.5 py-0.5 font-mono">
              {k}: {JSON.stringify(body[k]).slice(0, 80)}
            </span>
          ))}
      </div>
      <div className="space-y-1.5">
        {messages.map((msg, i) => (
          <MessageItem key={i} msg={msg} index={i} />
        ))}
      </div>
      <details>
        <summary className="text-muted-foreground cursor-pointer text-xs">原始 JSON</summary>
        <pre className="max-h-96 overflow-auto whitespace-pre-wrap break-words rounded bg-accent/20 p-2 font-mono text-xs">
          {raw}
        </pre>
      </details>
    </div>
  )
}

/** 调试 LLM 请求页面组件 */
export function DebugLlmPayloadPage({ embedded }: { embedded?: boolean } = {}) {
  const [selectedName, setSelectedName] = useState<string | null>(null)

  // 快照列表（query 化）：staleTime 窗口内重挂零请求（UI 无分页，恒第 1 页）
  const listQuery = useLlmPayloadDiagQuery(1)
  const items = listQuery.data?.items ?? []
  const total = listQuery.data?.total ?? 0
  // 无缓存数据时显示 loading（有缓存先渲染缓存不闪 loading）
  const isLoading = listQuery.isPending && !listQuery.data
  const error = listQuery.isError
    ? listQuery.error instanceof Error
      ? listQuery.error.message
      : '获取快照列表失败'
    : null

  return (
    <PageShell
      title="LLM 请求"
      backHref="/debug"
      embedded={embedded}
      actions={<span className="text-muted-foreground text-xs">共 {total} 个快照</span>}
    >
      <p className="text-muted-foreground text-xs">
        最近发送给大模型的真实请求体（每次调用前落盘；由环境变量 AGENTOS_PAYLOAD_DIAG=1 开启）
      </p>

      {isLoading && <LoadingState />}
      {error && <ErrorState message={error} />}

      {!isLoading && !error && items.length === 0 && (
        <div className="text-muted-foreground py-12 text-center">
          暂无快照——需设置 AGENTOS_PAYLOAD_DIAG=1 并产生新的 LLM 调用
        </div>
      )}

      {!isLoading && !error && items.length > 0 && (
        <div className="space-y-2">
          {items.map((item) => (
            <div key={item.name} className="rounded-lg border p-3">
              <button
                type="button"
                onClick={() => setSelectedName(selectedName === item.name ? null : item.name)}
                className="flex w-full flex-wrap items-center gap-2 text-left"
              >
                <span className="bg-primary/10 text-primary rounded px-1.5 py-0.5 font-mono text-xs">
                  {item.model}
                </span>
                <span className="bg-accent/30 rounded px-1.5 py-0.5 text-xs">{item.msg_count} 条消息</span>
                <span className="text-muted-foreground font-mono text-xs">{item.msgs_hash}</span>
                {item.size !== undefined && (
                  <span className="text-muted-foreground text-xs">{(item.size / 1024).toFixed(1)} KB</span>
                )}
                <span className="text-muted-foreground ml-auto text-xs">
                  {new Date(item.ts).toLocaleString()}
                </span>
              </button>
              {selectedName === item.name && <PayloadDetail item={item} />}
            </div>
          ))}
        </div>
      )}
    </PageShell>
  )
}
