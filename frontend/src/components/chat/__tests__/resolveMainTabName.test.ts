/**
 * resolveMainTabName 回退显示回归测试。
 *
 * BUG-FIX-20260713_main_tab_default_agent:
 * resolveMainTabName 此前匹配不到 agent 时回退显示 "主Agent"（无信息掩盖）。
 * 当 agents 列表加载竞态（fetchAgents 与 fetchSessions 并行，ChatContainer.tsx:46-47
 * 注释自承）或后端返回空时，主 Tab 无声显示 "主Agent"，看起来像"默认 agent"。
 *
 * 正确语义：匹配不到时显示真实 agentId（如 "lingxi"），保留可追溯信息；
 * 只有 agentId 本身为空时才回退 "主Agent"。
 */
import { describe, it, expect } from 'vitest'
import { resolveMainTabName } from '@/components/chat/tabNameResolver'
import type { Agent } from '@/types/models'

function makeAgent(overrides: Partial<Agent> = {}): Agent {
  return {
    id: 'lingxi',
    configId: 'lingxi',
    name: '灵汐',
    description: '',
    type: 'main',
    status: 'active',
    ...overrides,
  }
}

describe('resolveMainTabName', () => {
  it('匹配到 agent 时显示真实名称', () => {
    const agents = [makeAgent({ name: '灵汐' })]
    expect(resolveMainTabName('lingxi', agents)).toBe('灵汐')
  })

  it('agents 列表为空（加载竞态）时显示真实 agentId，而非 "主Agent"', () => {
    // 这是本次 bug 的核心场景：agents 还没加载完，find 返回 undefined
    expect(resolveMainTabName('lingxi', [])).toBe('lingxi')
  })

  it('agentId 在列表中匹配不到时显示 agentId 原值', () => {
    const agents = [makeAgent({ id: 'other', configId: 'other', name: '其他' })]
    expect(resolveMainTabName('lingxi', agents)).toBe('lingxi')
  })

  it('agentId 为空时才回退 "主Agent"', () => {
    expect(resolveMainTabName(undefined, [])).toBe('主Agent')
    expect(resolveMainTabName('', [makeAgent()])).toBe('主Agent')
  })

  it('按 configId 匹配（后端 AgentResponse 无 id 字段，前端 id 回退自 config_id）', () => {
    const agents = [makeAgent({ id: undefined, configId: 'lingxi', name: '灵汐' })]
    expect(resolveMainTabName('lingxi', agents)).toBe('灵汐')
  })
})
