/**
 * 请求类别（与 fetchMessages 写路径同序判定：after → 补漏，before → 翻页，
 * 都无 → 全量 init）。三类窗口读的是不重叠的 sequence 区间（init/补漏尾部
 * 锚定、翻页头部锚定），写面各自 initFromAPI/append/prepend 互不冲突。
 */
export type FetchKind = 'init' | 'older' | 'newer'

export function fetchKindOf(options?: { before_sequence?: number; after_sequence?: number }): FetchKind {
  if (options?.after_sequence !== undefined) return 'newer'
  if (options?.before_sequence !== undefined) return 'older'
  return 'init'
}

/** 在途 fetch 的取消令牌键：管道 × 请求类别（\u0000 不会出现在 id 中） */
export function abortKeyOf(pipelineId: string, kind: FetchKind): string {
  return `${pipelineId}\u0000${kind}`
}

/**
 * 每管道 × 每类别在途 fetch 的取消令牌：同类别新请求发起即 abort 旧请求
 * （切换会话/重复进入同一管道时，在途旧响应被作废，不再有覆盖新状态的
 * 机会；旧机制靠 initFromAPI 90s 保鲜窗启发式防覆盖，此处为显式取消）。
 * 跨类别不互取消（BUG-20）：翻页在途时 WS 重连补漏并发，两窗不重叠、
 * 写面互不冲突，必须都落库——任一方被对方取消都会静默掐死该方向的加载。
 * 被 abort 的请求以取消语义静默收场：不写状态、不上抛（上层把失败一律
 * 当故障通知用户，取消不是故障，不得误报）。
 */
export const _fetchAbortControllers = new Map<string, AbortController>()
