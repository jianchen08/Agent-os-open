/** @feature FP-0.2.待发送队列 发送落定通道 | @ci: frontend-test
 * 发送落定通道测试（发送态生命周期重定义 2026-11-04）
 *
 * 两条落定通道按 client_message_id 收敛发送态：
 *   - 直跑：user_message_confirmed / new_message → claimUserMessage 认领成功处落定
 *     （落定点收敛在 pipelineMessageStore.claimUserMessage 内，三条调用路径共用）；
 *   - 排队：pending_inputs_changed 条目 / GET 对账拉回条目出现即为落点（队列条出现）。
 * 未知 cmid（注入消息/触发器来源）幂等零动作。
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import * as api from '@/services/api/pipelines'
import { useChatInputStore } from '@/stores/chatInputStore'
import { usePendingInputStore } from '@/stores/pendingInputStore'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'

vi.mock('@/services/api/pipelines', async (importOriginal) => {
  const actual = await importOriginal<typeof api>()
  return {
    ...actual,
    deletePendingInput: vi.fn().mockResolvedValue(undefined),
    clearPendingInputs: vi.fn().mockResolvedValue(undefined),
    fetchPendingInputs: vi.fn().mockResolvedValue([]),
  }
})

vi.mock('@/utils/logger', async () => {
  const helpers = await import('./helpers/storeTestMocks')
  return helpers.loggerMockFull()
})
vi.mock('@/services/api/session', async () => {
  const helpers = await import('./helpers/storeTestMocks')
  return helpers.apiSessionMockFull()
})
vi.mock('@/utils/retry', async () => {
  const helpers = await import('./helpers/storeTestMocks')
  return helpers.retryMockBase()
})

const PIPELINE_ID = 'pipe-landing-1'
const SESSION_ID = 'sess-landing-1'

describe('发送落定通道', () => {
  beforeEach(() => {
    // 不 resetModules：三条落定链路（claim/sync/load）必须与顶部 import 的
    // chatInputStore 共享同一模块实例，跨 store 调用才落在被断言的同一状态上
    localStorage.clear()
    useChatInputStore.setState({
      pendingInsert: null,
      drafts: {},
      sendInFlightByPipeline: {},
      sendOutcomes: [],
    })
    usePendingInputStore.setState({ byPipeline: {} })
    usePipelineMessageStore.setState({
      messagesByPipeline: {},
      pipelines: {},
      pipelineSessionMap: { [PIPELINE_ID]: SESSION_ID },
      streamingState: {},
      activePipelineId: PIPELINE_ID,
      topCursorsByPipeline: {},
      bottomCursorsByPipeline: {},
      hasMoreOlderByPipeline: {},
      isLoadingOlderByPipeline: {},
      reconciledByPipeline: {},
    })
  })

  /** 前置：登记一条在途发送 */
  const beginInFlight = (cmid: string, content = '落定通道消息') => {
    useChatInputStore.getState().beginSend(PIPELINE_ID, cmid, content, 'tab-landing')
  }

  it('直跑：claimUserMessage 认领成功 → 按 cmid 落定发送态', () => {
    beginInFlight('cmid-claim-1')

    usePipelineMessageStore.getState().claimUserMessage(PIPELINE_ID, 'cmid-claim-1', {
      id: 'rec-claim-1',
      content: '落定通道消息',
      sequence: 1,
    })

    expect(useChatInputStore.getState().sendInFlightByPipeline[PIPELINE_ID]).toEqual([])
    const outcomes = useChatInputStore.getState().sendOutcomes
    expect(outcomes[outcomes.length - 1]).toMatchObject({
      clientMessageId: 'cmid-claim-1',
      pipelineId: PIPELINE_ID,
      kind: 'landed',
    })
  })

  it('claimUserMessage 幂等（skip）不产生重复落定 outcome', () => {
    beginInFlight('cmid-claim-2')

    usePipelineMessageStore.getState().claimUserMessage(PIPELINE_ID, 'cmid-claim-2', {
      id: 'rec-claim-2',
      content: '落定通道消息',
      sequence: 1,
    })
    const count = useChatInputStore.getState().sendOutcomes.length
    // 重复事件（new_message 与 confirmed 双通道先后到达）→ skip → 不再加 outcome
    usePipelineMessageStore.getState().claimUserMessage(PIPELINE_ID, 'cmid-claim-2', {
      id: 'rec-claim-2',
      content: '落定通道消息',
      sequence: 1,
    })
    expect(useChatInputStore.getState().sendOutcomes).toHaveLength(count)
  })

  it('排队：pending_inputs_changed 条目（含 client_message_id）出现即落定', () => {
    beginInFlight('cmid-queue-1')

    usePendingInputStore.getState().syncFromEvent(PIPELINE_ID, [
      {
        id: 'input-1',
        pipeline_id: PIPELINE_ID,
        content: '排队消息',
        source: 'user',
        client_message_id: 'cmid-queue-1',
        created_at: '2026-11-04T00:00:00Z',
      },
    ])

    expect(useChatInputStore.getState().sendInFlightByPipeline[PIPELINE_ID]).toEqual([])
    const outcomes = useChatInputStore.getState().sendOutcomes
    expect(outcomes[outcomes.length - 1]).toMatchObject({ clientMessageId: 'cmid-queue-1', kind: 'landed' })
  })

  it('排队：GET 对账拉回条目同样落定（事件漏接窗口兜底）', async () => {
    beginInFlight('cmid-queue-2')
    vi.mocked(api.fetchPendingInputs).mockResolvedValueOnce([
      {
        id: 'input-2',
        pipeline_id: PIPELINE_ID,
        content: '对账消息',
        source: 'user',
        client_message_id: 'cmid-queue-2',
        created_at: '2026-11-04T00:00:00Z',
      },
    ])

    await usePendingInputStore.getState().load(PIPELINE_ID)

    expect(useChatInputStore.getState().sendInFlightByPipeline[PIPELINE_ID]).toEqual([])
    const outcomes = useChatInputStore.getState().sendOutcomes
    expect(outcomes[outcomes.length - 1]).toMatchObject({ clientMessageId: 'cmid-queue-2', kind: 'landed' })
  })

  it('无 cmid 条目（trigger/task 来源）不影响在途发送', () => {
    beginInFlight('cmid-keep-1')

    usePendingInputStore.getState().syncFromEvent(PIPELINE_ID, [
      {
        id: 'input-3',
        pipeline_id: PIPELINE_ID,
        content: '触发器注入',
        source: 'trigger',
        created_at: '2026-11-04T00:00:00Z',
      },
    ])

    expect(useChatInputStore.getState().sendInFlightByPipeline[PIPELINE_ID]).toHaveLength(1)
    expect(useChatInputStore.getState().sendOutcomes).toEqual([])
  })
})
