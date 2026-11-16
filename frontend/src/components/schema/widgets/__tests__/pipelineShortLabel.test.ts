/**
 * resolveAgentLabel 单测（任务管理面板行首 agent 名徽标解析，用户裁定
 * 2026-11-06：显 agent 名不显代号 ID）。锁三点：注册表命中显配置名、
 * 未命中回退 stem 短名、空值不渲染（undefined）。
 */
import { describe, expect, it } from 'vitest'
import { resolveAgentLabel } from '../pipelineShortLabel'

/** agents 注册表夹具：系统主 agent + 模式包 agent（id/config_id 双键同形） */
const AGENTS = [
  { id: 'agentos', configId: 'agentos', name: '灵汐' },
  {
    id: 'mode_coding/programming_orchestrator_agent_v2',
    configId: 'programming_orchestrator_agent_v2',
    name: '编程编排者',
  },
]

describe('resolveAgentLabel', () => {
  it('注册表命中 → 配置显示名（主管道 agentos → 灵汐；模式完整键同）', () => {
    expect(resolveAgentLabel('agentos', AGENTS)).toBe('灵汐')
    expect(
      resolveAgentLabel('mode_coding/programming_orchestrator_agent_v2', AGENTS),
    ).toBe('编程编排者')
  })

  it('未命中 → stem 短名回退（mode_ 前缀不进行内）；空值 → undefined', () => {
    expect(resolveAgentLabel('mode_writing/novel_writer', [])).toBe('novel_writer')
    expect(resolveAgentLabel('', AGENTS)).toBeUndefined()
    expect(resolveAgentLabel(undefined, AGENTS)).toBeUndefined()
  })
})
