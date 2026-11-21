/**
 * ChatInput 全局桥接 Store
 *
 * 职责一：解决跨组件向 ChatInput 插入文本的问题。
 * 外部组件（FileInput 的选中引用浮动按钮等）调用 requestInsert 写入待插入文本，
 * ChatInput 组件订阅 pendingInsert 消费后调用 consumeInsert 清空。
 *
 * 职责二：草稿保存功能。
 * 当用户在不同会话/Tab之间切换时，输入框中的未发送文本通过 drafts 状态保留，
 * 切回来时恢复之前输入的内容。
 *
 * 草稿持久化：drafts 走 localStorage persist——刷新页面后输入框
 * 未发送内容不丢（用户"刷新后输入框应该还在"）。
 *
 * 职责三：发送态生命周期（2026-11-04 落定语义重定义；2026-11-18 并发抢占修订）。
 * 发送态终点 = 消息落进消息流（气泡经 user_message_confirmed 认领上屏 / 队列条
 * 经 pending_inputs_changed 出现），不是前端受理。受理（beginSend）只开始发送态：
 *   - 落定（landSendByCmid）= 成功：撤发送态，输入清空经 outcome 订阅在 ChatInput
 *     侧按快照比对执行（组件不存活时草稿清空在本 store 落地，按 draftKey 生效）；
 *   - 5s 未落定 = 判死出口：撤发送态 + timeout outcome（操作点报错）。**超时不再
 *     自动停 run**（2026-11-18 用户裁定修订 2026-11-04「超时=消息已坏应停止」：
 *     闸门满载/通道拥塞场景实测多为「消息活着、信号被挤丢」，自动停 run 制造
 *     零执行取消与连带误杀；停止权交还用户）；
 *   - ack 对账宽限：user_input_ack(ok:true) 到达 = 内核已受理，落定计时放宽到
 *     30s（SEND_LANDING_ACKED_GRACE_MS，闸门等待/抢占腾槽的启动延迟窗口）；
 *     宽限内仍未落定按 acked 专属 reason 判死（文案不诱导重发，消息在服务端
 *     排队/执行中）；
 *   - 失败（endSendByCmid，ack ok:false / 20s ack 看门狗）= 判死出口：撤发送态 +
 *     failed outcome，文本从未离开输入框，天然可重试。
 * 判死终局唯一（timeout/failed 先到先算），20s 看门狗对已判死 cmid 幂等零动作。
 */

import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { truncateCitationExcerpt } from '@/utils/citationCompose'
import { createTolerantStorage } from '@/utils/tolerantStorage'
import type { CitationKind } from '@/utils/citationKind'

/** 落定超时：5s 未落定 → 判死出口（撤发送态+操作点报错）。超时**不再自动停
 *  run**（2026-11-18 用户裁定修订）：ack 已受理的消息另有 30s 宽限窗口（见
 *  markSendAcked），停止权交还用户（超时≠消息已坏，不代内核杀执行） */
export const SEND_LANDING_TIMEOUT_MS = 5000

/** ack 已受理消息的落定宽限：闸门等待/抢占腾槽期间启动延迟可达数十秒，
 *  ack 到达即把落定计时放宽到此刻起 25s（受理总窗口 ≤ ~30s 有界） */
export const SEND_LANDING_ACKED_GRACE_MS = 25_000

/** 单条发送在途记录 */
export interface SendInFlightEntry {
  clientMessageId: string
  /** 受理时输入框文本快照：落定清空仅当当前文本 == 快照（不吞超时后的新输入） */
  content: string
  /** 受理时草稿键：落定清草稿经本 store 按 key 生效，不依赖 ChatInput 组件存活 */
  draftKey?: string
  /** 受理时刻（5s 落定超时起点） */
  startedAt: number
}

/** 发送终局记录：ChatInput 订阅消费（landed=清空+撤锁；timeout/failed=判死+报错）。
 *  timeout **不触发停止链**（2026-11-18 修订：超时≠消息已坏，停止权交还用户）。 */
export interface SendOutcome {
  /** 单调递增：ChatInput 按 id 去重消费（重挂载重放历史 outcome 收敛视图） */
  id: number
  clientMessageId: string
  pipelineId: string
  kind: 'landed' | 'timeout' | 'failed'
  /** 受理时输入框文本快照（清空比对用） */
  content: string
  draftKey?: string
  /** failed 时的原因（ack 拒收原文 / 看门狗 TTL 文案） */
  reason?: string
}

/**
 * 输入框引用卡片条目（2026-11-05 R5 卡片化）：引用/评审动作的入列形态。
 * 卡片只是输入面的显示形态，发送产物不变——发送装配期经 citationCompose 拼
 * 回现有 markdown 块引用格式（composeSendParams 消费）。
 * excerpt=定格快照：动作触发时的文本，addCitation 入口统一截断（400 字+省略号，
 * 与 markdown 组装真源同阈值），store 不回读源。
 */
export interface CitationItem {
  /** 条目 id（缺省由 store 补齐生成） */
  id: string
  /** 类型档（文件路径按扩展名判定；消息=message；制品兜底=artifact） */
  kind: CitationKind
  /** 来源注记（文件路径[:函数(行号)] / 消息角色标签） */
  title: string
  /** 定格快照（已截断） */
  excerpt: string
  /** 评审意见（引用条目无） */
  comment?: string
  /** 入列时刻（消息类条目发送装配时还原为来源时间注记） */
  createdAt: number
}

interface ChatInputState {
  /** 待插入的文本（ChatInput 消费后清空） */
  pendingInsert: string | null
  /**
   * 远程发送请求（全屏简化 composer 浮窗 → 真实输入框，2026-11-05 阶段一件一）：
   * 浮窗置位，挂载中（全屏下 CSS 隐藏保活）的 ChatInput 订阅消费并调用自身
   * handleSend——同参发送的到达路径（附件/模型等参数状态是 ChatInput 局部，
   * 浮窗只触发不拼参）。消费即清空。
   */
  pendingRemoteSend: boolean
  /** 按 tabId/sessionId 存储的草稿文本 */
  drafts: Record<string, string>
  /**
   * 已入列引用条目（R5 卡片化，按 draftKey 与 drafts 同键域隔离、同机制持久化）。
   * 发送装配期按序拼 markdown 块引用进正文尾部，落定清列（超时/失败保留）。
   */
  citationsByDraftKey: Record<string, CitationItem[]>
  /**
   * 远程发送同步拒绝反馈（handleSend 拒绝分支：禁用/在途附件/未受理，浮窗
   * 显示用；seq 保证同因多次拒绝也可见）。发送终局（落定/超时/失败）走
   * sendOutcomes 不经此字段。
   */
  remoteSendRefusal: { seq: number; reason: string } | null
  /**
   * 发送在途登记：pipelineId → 在途条目（含快照）。点击发送起挂入；落定
   * （landSendByCmid）/ 失败（endSendByCmid）/ 5s 落定超时撤下——发送按钮转圈
   * 与输入锁定的数据源。ack（受理回执）不撤本登记：ack 与落定之间的死寂窗口
   * 由发送态覆盖（转圈+锁定），消除「消息进黑洞」体感。
   */
  sendInFlightByPipeline: Record<string, SendInFlightEntry[]>
  /** 发送终局序列（landed/timeout/failed 追加；ChatInput 按 id 增量消费） */
  sendOutcomes: SendOutcome[]
  /** 外部调用：请求向 ChatInput 插入文本 */
  requestInsert: (text: string) => void
  /** ChatInput 消费后调用：清除待插入 */
  consumeInsert: () => void
  /** 全屏浮窗调用：请求真实 ChatInput 以自身状态执行一次发送 */
  requestRemoteSend: () => void
  /** ChatInput 消费后调用：清除远程发送请求 */
  consumeRemoteSend: () => void
  /** ChatInput 拒绝分支调用：镜像拒绝原因供浮窗显示 */
  reportRemoteSendRefusal: (reason: string) => void
  /** 浮窗发起下一次发送前清除旧拒绝反馈 */
  clearRemoteSendRefusal: () => void
  /** 保存草稿文本到指定 key */
  saveDraft: (key: string, text: string) => void
  /** 加载指定 key 的草稿文本，不存在时返回空字符串 */
  loadDraft: (key: string) => string
  /** 清除指定 key 的草稿文本 */
  clearDraft: (key: string) => void
  /** 引用/评审动作入列一条引用（excerpt 入口统一截断；id/createdAt 缺省补齐） */
  addCitation: (
    draftKey: string,
    item: Omit<CitationItem, 'id' | 'createdAt'> & { id?: string; createdAt?: number },
  ) => void
  /** 移除指定引用条目（卡片 × 与落定清理共用） */
  removeCitation: (draftKey: string, id: string) => void
  /** 清空指定 key 的引用列（发送落定后调用） */
  clearCitations: (draftKey: string) => void
  /** 发送已受理挂入在途（帧已交给 WS 发送链）；content=输入快照，draftKey=落定清草稿键 */
  beginSend: (pipelineId: string, clientMessageId: string, content?: string, draftKey?: string) => void
  /** 落定：消息已进消息流（认领上屏/队列条出现），按 cmid 撤发送态（幂等） */
  landSendByCmid: (clientMessageId: string) => void
  /** ack 受理对账（2026-11-18）：user_input_ack(ok:true) 到达登记并落定计时
   *  放宽到宽限窗口（幂等；条目已撤下则仅记账零动作） */
  markSendAcked: (clientMessageId: string) => void
  /** 失败判死：ack ok:false / 20s 看门狗按 cmid 撤发送态并落 failed（幂等；
   *  已判死（timeout/failed）的 cmid 零动作） */
  endSendByCmid: (clientMessageId: string, reason?: string) => void
}

/** 账户数据初始片段（cacheBoundary 切户清账消费；与 persist partialize 字段同域，
 * 新增持久化字段时同步维护——契约见 docs/working/用户隔离机制_审计与修复_20261018.md §5） */
export const CHAT_INPUT_ACCOUNT_RESET = {
  pendingInsert: null,
  pendingRemoteSend: false,
  remoteSendRefusal: null,
  drafts: {},
  citationsByDraftKey: {},
  sendInFlightByPipeline: {},
  sendOutcomes: [],
}

/** outcome 单调序号（模块级：页面生命周期内不回绕，重放去重的锚） */
let sendOutcomeSeq = 0

/** 远程发送拒绝序号（单调递增：同因多次拒绝也驱动浮窗更新） */
let remoteSendRefusalSeq = 0

/** 引用条目缺省 id 序号（显式 id 优先；模块级单调解碰撞） */
let citationSeq = 0

/** outcome 序列封顶：ChatInput 消费后即失去价值，只留最近段防无界增长 */
const SEND_OUTCOMES_CAP = 50

/** 5s 落定超时计时器（cmid → timer；不进 state，随条目撤下而清理） */
const landingTimers = new Map<string, ReturnType<typeof setTimeout>>()

/** ack 已受理的 cmid（GlobalWebSocket ack ok:true 登记；终局随条目清除）。
 *  模块级不进 state：发送态瞬态，持久化无意义 */
const ackedCmids = new Set<string>()

function disarmLandingTimer(clientMessageId: string): void {
  const timer = landingTimers.get(clientMessageId)
  if (timer) {
    clearTimeout(timer)
    landingTimers.delete(clientMessageId)
  }
}

/** 落定超时点火：撤在途 + timeout outcome（条目即终局，迟到落定/看门狗幂等
 *  零动作）。ack 已受理的消息带专属 reason——文案不诱导重发（消息在服务端
 *  排队/执行中），未受理的走默认超时文案（未确认送达，可重试） */
function fireLandingTimeout(clientMessageId: string): void {
  useChatInputStore.setState((state) => {
    const located = locateEntry(state, clientMessageId)
    if (!located) return state
    const [pid, idx] = located
    const entry = state.sendInFlightByPipeline[pid][idx]
    const outcome: SendOutcome = {
      id: ++sendOutcomeSeq,
      clientMessageId,
      pipelineId: pid,
      kind: 'timeout',
      content: entry.content,
      ...(entry.draftKey ? { draftKey: entry.draftKey } : {}),
      ...(ackedCmids.has(clientMessageId)
        ? { reason: '消息已受理，执行启动较慢（并发繁忙）——稍候可在会话中查看结果，无需重发' }
        : {}),
    }
    return {
      sendInFlightByPipeline: {
        ...state.sendInFlightByPipeline,
        [pid]: state.sendInFlightByPipeline[pid].filter((_, i) => i !== idx),
      },
      sendOutcomes: [...state.sendOutcomes, outcome].slice(-SEND_OUTCOMES_CAP),
    }
  })
}

/** 在全部管道中定位条目：返回 [pipelineId, index] */
function locateEntry(
  state: Pick<ChatInputState, 'sendInFlightByPipeline'>,
  clientMessageId: string,
): [string, number] | null {
  for (const [pipelineId, entries] of Object.entries(state.sendInFlightByPipeline)) {
    const idx = entries.findIndex((e) => e.clientMessageId === clientMessageId)
    if (idx >= 0) return [pipelineId, idx]
  }
  return null
}

export const useChatInputStore = create<ChatInputState>()(
  persist(
    (set, get) => ({
      ...CHAT_INPUT_ACCOUNT_RESET,

      requestInsert: (text) => set({ pendingInsert: text }),
      consumeInsert: () => set({ pendingInsert: null }),

      requestRemoteSend: () => set({ pendingRemoteSend: true }),
      consumeRemoteSend: () => set({ pendingRemoteSend: false }),
      reportRemoteSendRefusal: (reason) =>
        set({ remoteSendRefusal: { seq: ++remoteSendRefusalSeq, reason } }),
      clearRemoteSendRefusal: () => set({ remoteSendRefusal: null }),

      /**
       * 保存草稿文本到指定 key
       */
      saveDraft: (key, text) => {
        set((state) => ({
          drafts: { ...state.drafts, [key]: text },
        }))
      },

      /**
       * 加载草稿文本到指定 key
       */
      loadDraft: (key) => {
        return get().drafts[key] || ''
      },

      /**
       * 清除指定 key 的草稿文本
       */
      clearDraft: (key) => {
        set((state) => {
          const { [key]: _, ...rest } = state.drafts
          return { drafts: rest }
        })
      },

      /** 入列引用条目：excerpt 入口统一截断（与发送装配真源同阈值，卡片摘录
       *  即发送摘录，发送产物与旧 requestInsert 通道等价）；comment 空白规范化
       *  掉（评审空意见=纯引用）；id/createdAt 缺省补齐 */
      addCitation: (draftKey, item) => {
        if (!draftKey) return
        const { comment: rawComment, ...itemRest } = item
        const comment = rawComment?.trim()
        const stamped: CitationItem = {
          ...itemRest,
          ...(comment ? { comment } : {}),
          id: item.id || `cit-${++citationSeq}`,
          createdAt: item.createdAt || Date.now(),
          excerpt: truncateCitationExcerpt(item.excerpt),
        }
        set((state) => ({
          citationsByDraftKey: {
            ...state.citationsByDraftKey,
            [draftKey]: [...(state.citationsByDraftKey[draftKey] ?? []), stamped],
          },
        }))
      },

      /** 移除指定引用条目；列空后键删除（与 clearDraft 同净态语义） */
      removeCitation: (draftKey, id) => {
        set((state) => {
          const list = state.citationsByDraftKey[draftKey]
          if (!list) return state
          const next = list.filter((c) => c.id !== id)
          if (next.length === list.length) return state
          const { [draftKey]: _, ...rest } = state.citationsByDraftKey
          return {
            citationsByDraftKey: next.length > 0 ? { ...rest, [draftKey]: next } : rest,
          }
        })
      },

      /** 清空指定 key 引用列（发送落定后清，超时/失败保留） */
      clearCitations: (draftKey) => {
        set((state) => {
          if (!state.citationsByDraftKey[draftKey]) return state
          const { [draftKey]: _, ...rest } = state.citationsByDraftKey
          return { citationsByDraftKey: rest }
        })
      },

      beginSend: (pipelineId, clientMessageId, content = '', draftKey) => {
        if (!pipelineId || !clientMessageId) return
        set((state) => {
          const current = state.sendInFlightByPipeline[pipelineId] ?? []
          if (current.some((e) => e.clientMessageId === clientMessageId)) return state
          const entry: SendInFlightEntry = {
            clientMessageId,
            content,
            ...(draftKey ? { draftKey } : {}),
            startedAt: Date.now(),
          }
          return {
            sendInFlightByPipeline: {
              ...state.sendInFlightByPipeline,
              [pipelineId]: [...current, entry],
            },
          }
        })
        // 5s 落定超时 = 判死出口（2026-11-18 修订：超时不自动停 run）。ack 到达
        // 经 markSendAcked 把计时放宽到宽限窗口；点火逻辑见 fireLandingTimeout。
        if (!landingTimers.has(clientMessageId)) {
          landingTimers.set(
            clientMessageId,
            setTimeout(() => {
              landingTimers.delete(clientMessageId)
              fireLandingTimeout(clientMessageId)
            }, SEND_LANDING_TIMEOUT_MS),
          )
        }
      },

      /** ack ok:true 对账（GlobalWebSocket 回执处理器调用）：登记受理 + 落定
       *  计时放宽到宽限窗口（闸门等待/抢占腾槽的启动延迟有界兜底） */
      markSendAcked: (clientMessageId) => {
        if (!clientMessageId || ackedCmids.has(clientMessageId)) return
        ackedCmids.add(clientMessageId)
        if (!landingTimers.has(clientMessageId)) return
        disarmLandingTimer(clientMessageId)
        landingTimers.set(
          clientMessageId,
          setTimeout(() => {
            landingTimers.delete(clientMessageId)
            fireLandingTimeout(clientMessageId)
          }, SEND_LANDING_ACKED_GRACE_MS),
        )
      },

      landSendByCmid: (clientMessageId) => {
        if (!clientMessageId) return
        disarmLandingTimer(clientMessageId)
        ackedCmids.delete(clientMessageId)
        set((state) => {
          // 判死即终局：超时/失败撤下后在途查无此条 → 迟到落定零动作
          // （不触碰输入框：消息已判死，文本归属用户重试；气泡上屏由消息流自洽）
          const located = locateEntry(state, clientMessageId)
          if (!located) return state
          const entry = state.sendInFlightByPipeline[located[0]][located[1]]
          const pipelineId = located[0]
          const outcome: SendOutcome = {
            id: ++sendOutcomeSeq,
            clientMessageId,
            pipelineId,
            kind: 'landed',
            content: entry.content,
            ...(entry.draftKey ? { draftKey: entry.draftKey } : {}),
          }
          const next: Partial<ChatInputState> = {
            sendInFlightByPipeline: {
              ...state.sendInFlightByPipeline,
              [pipelineId]: state.sendInFlightByPipeline[pipelineId].filter(
                (_, i) => i !== located[1],
              ),
            },
          }
          // 落定清草稿（组件不存活时在本 store 落地）：快照比对守卫——草稿已被
          // 改（超时解锁后的新输入）不吞
          if (entry.draftKey && state.drafts[entry.draftKey] === entry.content) {
            const { [entry.draftKey]: _, ...restDrafts } = state.drafts
            next.drafts = restDrafts
          }
          return {
            ...next,
            sendOutcomes: [...state.sendOutcomes, outcome].slice(-SEND_OUTCOMES_CAP),
          } as ChatInputState
        })
      },

      endSendByCmid: (clientMessageId, reason) => {
        if (!clientMessageId) return
        disarmLandingTimer(clientMessageId)
        ackedCmids.delete(clientMessageId)
        set((state) => {
          const located = locateEntry(state, clientMessageId)
          // 判死即终局：timeout/failed 先到先算，后到的看门狗/拒收幂等零动作
          if (!located) return state
          const entry = state.sendInFlightByPipeline[located[0]][located[1]]
          const pipelineId = located[0]
          const outcome: SendOutcome = {
            id: ++sendOutcomeSeq,
            clientMessageId,
            pipelineId,
            kind: 'failed',
            content: entry.content,
            ...(entry.draftKey ? { draftKey: entry.draftKey } : {}),
            ...(reason ? { reason } : {}),
          }
          return {
            sendInFlightByPipeline: {
              ...state.sendInFlightByPipeline,
              [pipelineId]: state.sendInFlightByPipeline[pipelineId].filter(
                (_, i) => i !== located[1],
              ),
            },
            sendOutcomes: [...state.sendOutcomes, outcome].slice(-SEND_OUTCOMES_CAP),
          } as ChatInputState
        })
      },
    }),
    {
      name: 'chat-input-drafts',
      // 草稿是纯文本小块，localStorage 足够；容错包装防配额满/禁用时崩溃
      storage: createTolerantStorage(),
      // 仅持久化 drafts 与 citationsByDraftKey（pendingInsert 是瞬态桥接；
      // 发送态字段为瞬态运行态，不落盘）
      partialize: (state) => ({
        drafts: state.drafts,
        citationsByDraftKey: state.citationsByDraftKey,
      }),
    },
  ),
)
