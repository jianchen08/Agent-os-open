/** 管道消息 Store 的状态契约（纯类型 + 账户清账常量，无行为）——从 pipelineMessageStore 抽出
 *  （巨型文件冻结门禁：store 本体只许缩小；类型面独立成件，行为面不动）。
 *  对外消费路径不变：PipelineMeta / StreamingStatus / PIPELINE_MESSAGE_ACCOUNT_RESET
 *  仍可从 pipelineMessageStore 导入（re-export 保持单一入口）。 */

import type { SegmentDetail, SegmentMeta } from '@/services/api/messageSegments'
import type { MessagePart } from '@/types/messageParts'
import type { Message } from '@/types/models'

/** 管道元数据 */
export interface PipelineMeta {
  /** 管道唯一标识 */
  pipelineId: string
  /** 所属会话 ID */
  sessionId: string
  /** 管道层级：1=主管道，2=子管道，3=孙管道 */
  level: 1 | 2 | 3
  /** 关联的 Tab ID（主管道为 null） */
  tabId: string | null
  /** Agent 名称 */
  agentName: string
  /** 管道状态 */
  status: 'idle' | 'running' | 'completed' | 'error'
  /** 父管道 ID（主管道为 null） */
  parentId: string | null
  /** 未读消息计数 */
  unreadCount: number
}

/** 单个管道的流式状态 */
export interface StreamingStatus {
  /** 是否正在流式传输 */
  isStreaming: boolean
  /** 正在流式传输的消息 ID */
  messageId: string | null
}

/** Store 状态接口 */
export interface PipelineMessageState {
  /** 一级索引：pipelineId → 消息列表 */
  messagesByPipeline: Record<string, Message[]>
  /** 管道元数据 */
  pipelines: Record<string, PipelineMeta>
  /** 管道归属映射：pipelineId → sessionId */
  pipelineSessionMap: Record<string, string>
  /** 流式状态 */
  streamingState: Record<string, StreamingStatus>
  /** 压缩中标记：pipelineId → boolean。
   *  上下文压缩是分钟级同步调用（实测 6~18 分钟），期间管道不产出任何流式
   *  内容——只显示通用「思考中」用户无法区分「模型在思考」与「正在压缩」，
   *  压缩卡死时也无从判断（用户裁定 2026-10-28）。运行时态，不持久化。 */
  compressingByPipeline: Record<string, boolean>
  /** 轮次收尾快照：pipelineId → 队列剩余深度（pipeline_round_finished 携带）。
   *  >0 = run 结束但链尾 drain 还要继续消费，前端不得显示空闲。运行时态。 */
  roundQueueDepthByPipeline: Record<string, number>
  /** 停止受理中：pipelineId → boolean（点停止 → run 级终止事件之间）。
   *  run_status 已变 suspended 但引擎长步骤未退出时，前端据此显示「停止中」
   *  而不是回到可发送态。运行时态，终止事件/新 stream_start 清除。 */
  stoppingByPipeline: Record<string, boolean>
  /** 消息加载失败标记：pipelineId → boolean（X1-F1）。会话切换加载失败由
   *  sessionListStore 落标记（通知策略在调用方，见 loadPipelineMessages 的
   *  { ok, error } 返回契约），消息区据此渲染错误态 + 「重新加载」而非误导性
   *  空态；该管道下次加载成功（loadPipelineMessages 成功路径）自然清除。
   *  运行时态，不持久化。 */
  loadErrorByPipeline: Record<string, boolean>
  /** 当前激活的管道 ID */
  activePipelineId: string | null
  /** 顶部游标：pipelineId → 已加载的最小 sequence（用于向上翻页） */
  topCursorsByPipeline: Record<string, number>
  /** 底部游标：pipelineId → 已确认的最大 sequence（用于断线补漏） */
  bottomCursorsByPipeline: Record<string, number>
  /** 是否还有更早的消息：pipelineId → boolean */
  hasMoreOlderByPipeline: Record<string, boolean>
  /** 是否正在加载更早的消息 */
  isLoadingOlderByPipeline: Record<string, boolean>
  /** 首屏消息加载中：pipelineId → boolean（无 before/after 游标的全量 GET await
   *  窗口）。会话切换首屏消息 GET 期间消息区渲染骨架而非「开始新的对话」空态
   *  （空 ≠ 加载，OBS-R258-1 四态约定）。运行时态，不持久化。 */
  initialLoadingByPipeline: Record<string, boolean>
  /** 运行时标记：本次会话已与后端全量对账过的 pipeline（不持久化，rehydrate 后重置）。
   *  防止流式断线残留的不可信 bottomCursor 导致刷新后只走增量补漏、已加载区间内空洞永远补不上。 */
  reconciledByPipeline: Record<string, boolean>

  /** 段索引：pipelineId → 段元数据清单（消息段模型：段 = 替换事件冻结的非启用
   *  内容，[来源: docs/working/消息历史双能力方案_多代切换与压缩原文_20260923.md §2.1]）。
   *  打开会话时拉一次清单（loadPipelineMessages），segment_activated 对账后刷新；
   *  运行时态，不持久化。 */
  spanIndex: Record<string, SegmentMeta[]>
  /** 本地乐观段视图：pipelineId → ‹i/n› 切换当前查看的段（乐观换装标记；
   *  segment_activated 对账成功后清除 = 回归服务端权威启用序列） */
  segmentViewByPipeline: Record<string, { baseSeq: number; segmentId: string } | null>

  /** 拉取段清单写入 spanIndex（打开会话时一次）。非核心面：失败降级为空清单
   *  + warn 留痕（切换器隐藏），不影响消息主链路，不上抛。 */
  fetchSegments: (pipelineId: string) => Promise<void>
  /** 某锚点 base_seq 的段列表（多段同 base_seq 按 created_at 升序，代号 = 序数） */
  getSegmentsAt: (pipelineId: string, baseSeq: number) => SegmentMeta[]
  /** 乐观换装：用段成员自 base_seq 起点覆写本地消息视图（激活后缀语义 §2.5：
   *  [base_seq..末尾] 整体呈现为段成员），并记录段视图标记 */
  applySegmentView: (pipelineId: string, detail: SegmentDetail, sessionId: string) => void
  /** 清除乐观段视图标记（segment_activated 对账成功后调用，消息已由 API 权威重建） */
  clearSegmentView: (pipelineId: string) => void

  /** 注册管道 */
  registerPipeline: (meta: PipelineMeta) => void
  /** 激活管道 */
  activatePipeline: (pipelineId: string) => void

  /** 添加消息到指定管道 */
  addMessage: (pipelineId: string, message: Message) => void
  /** 更新指定管道中的消息（部分更新） */
  updateMessage: (pipelineId: string, messageId: string, partial: Partial<Message>) => void
  /** 移除指定管道中的消息 */
  removeMessage: (pipelineId: string, messageId: string) => void
  /** 获取指定管道的消息列表 */
  getMessages: (pipelineId: string) => Message[]

  /** 认领 user 消息（[来源: docs/decisions/2026-08-22-streaming-protocol-rewrite.md]）：
   *  按 cmid 找到乐观 user
   *  消息 → 权威 record_id/seq 记入独立 recordId 字段（UI 寻址 id 永不变），
   *  status='completed'。候选缺失（断线期间确认到达/刷新窗口）→ 以
   *  cmid 为 id 插入权威 user 版——「发送后用户消息消失」结构性不可能。 */
  claimUserMessage: (pipelineId: string, cmid: string, userRecord: {
    id?: string
    content?: unknown
    sequence?: number
    metadata?: Record<string, unknown> | null
  }) => 'upgraded' | 'inserted' | 'skipped'

  /** 无 cmid 注入消息（触发器/任务/HTTP，ADR-2026-08-26）补插 user 气泡：
   *  按 id 幂等（重复事件不双插）；按 sequence 落位——保证触发器消息出现在
   *  assistant 回复之前（与后端消息序一致）。 */
  ensureInjectedUserMessage: (pipelineId: string, userRecord: {
    id?: string
    content?: unknown
    sequence?: number
    metadata?: Record<string, unknown> | null
  }) => void

  /** 权威截断（messages_truncated 事件）：按 sequence 截断 + updated_messages 原地改写，挂起流式态一并清理 */
  applyTruncation: (
    pipelineId: string,
    truncateBeforeSeq: number,
    updated?: Array<{ sequence?: number; content?: unknown }>,
  ) => void

  /** 开始流式传输 */
  startStreaming: (pipelineId: string, messageId: string) => void
  /** 停止流式传输 */
  stopStreaming: (pipelineId: string) => void
  /** 查询指定管道是否正在流式传输 */
  isStreaming: (pipelineId: string) => boolean

  /** 标记/取消指定管道的「压缩中」状态（compression_started / compression_ended 驱动） */
  setCompressing: (pipelineId: string, compressing: boolean) => void
  /** 查询指定管道是否处于上下文压缩中 */
  isCompressing: (pipelineId: string) => boolean
  /** 轮次收尾队列深度快照（pipeline_round_finished.queue_depth 驱动） */
  setRoundQueueDepth: (pipelineId: string, depth: number) => void
  /** 停止受理中标记（点停止 → run 级终止事件/新 stream_start） */
  setStopping: (pipelineId: string, stopping: boolean) => void
  /** 标记/清除指定管道的消息加载失败态（X1-F1：设置点=调用方通知策略，即
   *  sessionListStore 会话切换失败分支；清除点统一收敛在 loadPipelineMessages
   *  成功路径，重试成功/下次进入/WS 重连对账共用） */
  setLoadError: (pipelineId: string, failed: boolean) => void

  /** 冷启动：从 API 写入最新消息并设置双游标 */
  initFromAPI: (pipelineId: string, messages: Message[], hasMoreOlder?: boolean) => void
  /** 向上翻页：将更早消息插入头部并更新 topCursor */
  prependMessages: (pipelineId: string, messages: Message[], hasMoreOlder?: boolean) => void
  /** 断线补漏：追加缺失消息到底部并更新 bottomCursor */
  appendMessages: (pipelineId: string, messages: Message[]) => void
  /** 获取指定管道的顶部游标 */
  getTopCursor: (pipelineId: string) => number
  /** 获取指定管道的底部游标 */
  getBottomCursor: (pipelineId: string) => number
  /** 判断指定管道是否已初始化 */
  isInitialized: (pipelineId: string) => boolean
  /** 判断指定管道是否还有更早的消息 */
  hasMoreOlder: (pipelineId: string) => boolean

  /** 直接从 API 加载指定管道的历史消息（底层，调用方应优先用 loadPipelineMessages） */
  fetchMessages: (
    pipelineId: string,
    options?: {
      limit?: number
      before_sequence?: number
      after_sequence?: number
      threadId?: string
      /** 工具结果展开回填专用：旁路截断映射，本页消息携带全量 result/resultData */
      includeFullToolResults?: boolean
    },
  ) => Promise<void>
  /** 加载管道消息的统一入口（收敛所有加载场景的流式保护、双游标决策）。 4 个加载场景（会话切换 / 子 Tab 切换 / 关 Tab 回主 / WS 重连补漏）都应调用本方法， */
  loadPipelineMessages: (
    pipelineId: string,
    options: {
      threadId: string
      mode?: 'auto' | 'init' | 'backfill'
      skipStreamingCheck?: boolean
    },
  ) => Promise<{ ok: boolean; error?: unknown }>

  // Parts 统一修改方法

  /** 追加一个新 Part 到指定消息 */
  appendPart: (pipelineId: string, messageId: string, part: MessagePart) => void
  /** 更新指定消息的某个 Part（按 partIndex 精确定位） */
  updatePart: (pipelineId: string, messageId: string, partIndex: number, updates: Partial<MessagePart>) => void
  /** 向指定 Part 追加文本内容（用于流式增量） */
  appendToPart: (pipelineId: string, messageId: string, partIndex: number, content: string) => void
  /** 结束消息流式状态：所有 Part.state = 'done', 消息 status = 'completed' */
  finalizeMessage: (pipelineId: string, messageId: string) => void
  /** 获取指定消息中最后一个指定类型的 Part 的 index */
  findLastPartIndex: (pipelineId: string, messageId: string, type: MessagePart['type']) => number
  /** 获取指定消息中 state='streaming' 的最后一个 Part 的 index */
  findStreamingPartIndex: (pipelineId: string, messageId: string) => number
  /** 获取指定消息中指定 callId 的 tool_call Part 的 index */
  findToolCallPartIndex: (pipelineId: string, messageId: string, callId: string) => number
}

/** 账户数据初始片段（cacheBoundary 切户清账消费）：create 初始数据域全集，
 * 新增数据字段时同步维护——消息缓存是 1018 跨户泄漏缺陷的直接根因面。
 * 自 pipelineMessageStore 抽出（冻结文件只许缩小；常量属状态契约面，与类型同居）。 */
export const PIPELINE_MESSAGE_ACCOUNT_RESET = {
  messagesByPipeline: {},
  pipelines: {},
  pipelineSessionMap: {},
  streamingState: {},
  compressingByPipeline: {},
  roundQueueDepthByPipeline: {},
  stoppingByPipeline: {},
  loadErrorByPipeline: {},
  activePipelineId: null,
  topCursorsByPipeline: {},
  bottomCursorsByPipeline: {},
  hasMoreOlderByPipeline: {},
  isLoadingOlderByPipeline: {},
  initialLoadingByPipeline: {},
  reconciledByPipeline: {},
  spanIndex: {},
  segmentViewByPipeline: {},
}

/** 首载标记写面（fetchMessages await 窗口置位/落定清除，空 ≠ 加载 OBS-R258-1）。
 *  独立函数承载——store 本体为巨型文件冻结件只许缩小，行为挂本契约文件同源。 */
export function setInitialLoadingFlag(
  setState: (partial: Partial<PipelineMessageState>) => void,
  getState: () => PipelineMessageState,
  pipelineId: string,
  loading: boolean,
): void {
  setState({
    initialLoadingByPipeline: { ...getState().initialLoadingByPipeline, [pipelineId]: loading },
  })
}
