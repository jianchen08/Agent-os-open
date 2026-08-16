/**
 * agentTabStore.mergeToMainTab 行为测试
 *
 * 核心回归：合并的目标桶必须是主管道 ID（session.pipelineIds[0]），
 * 而不是 currentSessionId——否则合并消息写入无人订阅的桶，主管道视图不可见。
 */

import { beforeEach, describe, expect, it } from 'vitest'
import { useAgentTabStore } from '../agentTabStore'
import { usePipelineMessageStore } from '../pipelineMessageStore'
import { useSessionStore } from '../sessionStore'
import type { AgentTab } from '@/types/task'
import type { Message } from '@/types/models'

const SESSION_ID = 'sess-1'
const MAIN_PIPELINE_ID = 'pipe-main' // session.pipelineIds[0]，主管道桶 key
const SUB_PIPELINE_ID = 'pipe-sub'
const SUB_TAB_ID = 'sub-tab-1'

function makeMessage(pipelineId: string, id: string, sequence: number, content: string): Message {
  return {
    id,
    sessionId: SESSION_ID,
    sequence,
    role: 'assistant',
    content,
    timestamp: new Date().toISOString(),
    pipelineId,
  } as unknown as Message
}

function makeTab(partial: Partial<AgentTab> & { id: string; agentLevel: 1 | 2 }): AgentTab {
  return {
    agentId: 'agentos',
    agentName: partial.agentLevel === 1 ? '主Agent' : '子Agent',
    path: [partial.agentLevel === 1 ? '主Agent' : '主Agent/子Agent'],
    status: 'running',
    hasUnread: false,
    canClose: partial.agentLevel !== 1,
    ...partial,
  } as AgentTab
}

describe('mergeToMainTab 合并目标桶', () => {
  beforeEach(() => {
    localStorage.clear()
    useSessionStore.setState({
      sessions: [
        {
          id: SESSION_ID,
          title: 't',
          agentId: 'agentos',
          pipelineIds: [MAIN_PIPELINE_ID],
        },
      ] as never,
    })
    usePipelineMessageStore.setState({
      messagesByPipeline: {
        [MAIN_PIPELINE_ID]: [makeMessage(MAIN_PIPELINE_ID, 'm-main', 1, '主管道消息')],
        [SUB_PIPELINE_ID]: [makeMessage(SUB_PIPELINE_ID, 'm-sub', 2, '子管道结果')],
      },
      topCursorsByPipeline: {},
      bottomCursorsByPipeline: {},
      hasMoreOlderByPipeline: {},
      activePipelineId: MAIN_PIPELINE_ID,
    })
    useAgentTabStore.setState({
      tabs: [
        makeTab({ id: `main-${SESSION_ID}`, agentLevel: 1, pipelineRunId: MAIN_PIPELINE_ID }),
        makeTab({ id: SUB_TAB_ID, agentLevel: 2, pipelineRunId: SUB_PIPELINE_ID }),
      ],
      activeTabId: SUB_TAB_ID,
      currentSessionId: SESSION_ID,
      pipelineTabMap: { [SUB_PIPELINE_ID]: SUB_TAB_ID },
      unreadCounts: {},
      tabMessagesLoading: {},
    })
  })

  it('子 Tab 合并后主管道桶可见合并消息', () => {
    useAgentTabStore.getState().mergeToMainTab(SUB_TAB_ID)

    const mainMsgs = usePipelineMessageStore.getState().getMessages(MAIN_PIPELINE_ID)
    const ids = mainMsgs.map((m) => m.id)
    expect(ids).toContain('m-main')
    expect(ids).toContain('m-sub')

    const merged = mainMsgs.find((m) => m.id === 'm-sub')
    expect(merged?.metadata?.mergedFrom).toBe(SUB_TAB_ID)
    expect(typeof merged?.metadata?.mergedAt).toBe('string')

    // 合并后 sessionId 桶不应被污染（修复前合并消息被误写入 currentSessionId 桶）
    const sessionBucket = usePipelineMessageStore.getState().messagesByPipeline[SESSION_ID]
    expect(sessionBucket).toBeUndefined()

    // 子 Tab 被移除，活跃 Tab 回落主 Tab
    const tabState = useAgentTabStore.getState()
    expect(tabState.tabs.map((t) => t.id)).not.toContain(SUB_TAB_ID)
    expect(tabState.activeTabId).toBe(`main-${SESSION_ID}`)
    expect(tabState.pipelineTabMap[SUB_PIPELINE_ID]).toBeUndefined()
  })
})
