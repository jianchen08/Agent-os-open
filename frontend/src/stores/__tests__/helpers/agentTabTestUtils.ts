/**
 * agentTab 测试家族共享的实体工厂（makeSession/makeSubTab/makeSubTabInput）。
 * 曾在三个测试文件逐字复制（jscpd 克隆门禁重复源）；常量改为参数注入。
 */
import type { Session } from '@/types/sessions'
import type { AgentTab } from '@/types/task'

export function makeSessionFactory(sessionId: string, mainPid: string) {
  return (overrides: Partial<Session> = {}): Session =>
    ({
      id: sessionId,
      title: '测试会话',
      agentId: 'agentos',
      activePipelineId: mainPid,
      pipelineIds: [mainPid],
      starred: false,
      pinned: false,
      createdAt: '2026-01-01T00:00:00.000Z',
      updatedAt: '2026-01-01T00:00:00.000Z',
      ...overrides,
    }) as Session
}

export function makeSubTabFactory(subTabId: string, subPid: string) {
  return (overrides: Partial<AgentTab> = {}): AgentTab => ({
    id: subTabId,
    agentId: 'agent-sub',
    agentName: '子Agent',
    agentLevel: 2,
    parentRecordId: 'rec-sub-x',
    pipelineRunId: subPid,
    path: ['主管道', '子Agent'],
    status: 'running',
    hasUnread: false,
    canClose: true,
    messages: [],
    ...overrides,
  })
}

export function makeSubTabInputFactory(makeSubTab: (o: Partial<AgentTab>) => AgentTab) {
  return (overrides: Partial<Omit<AgentTab, 'messages'>> = {}) => {
    const { messages: _messages, ...tab } = makeSubTab(overrides)
    return tab
  }
}
