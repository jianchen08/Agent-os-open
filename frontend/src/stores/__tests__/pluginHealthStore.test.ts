// @feature: FP-0.2.可观测性 插件不可用系统通知（卡3 前端） | @ci: frontend-test
/**
 * pluginHealthStore 测试：降级项登记/去重、清账、契约读面对账清除。
 *
 * 对账语义（卡3 前端工单定案）：后端恢复侧无事件（转正只清内核内存账本），
 * 清除消费既有只读端点 GET /api/v1/plugins/contract-status 的解析结果——
 * 账本非空且无任何 verify_failed → 清全部降级项（保守）；账本为空/读不到 →
 * 保留（不能确认恢复就不撤条目）。
 */
import { beforeEach, describe, expect, it } from 'vitest'
import { usePluginHealthStore, type ContractStatusLite } from '../pluginHealthStore'

function status(g2: string | undefined): ContractStatusLite {
  return { plugin_id: 'p', gates: { g2_consistency: g2 } }
}

beforeEach(() => {
  localStorage.clear()
  usePluginHealthStore.setState({ unavailable: [] })
})

describe('reportUnavailable 降级项登记', () => {
  it('新键入列，字段如实承载', () => {
    usePluginHealthStore.getState().reportUnavailable({
      key: 'cohost_member_offline|插件组 light 成员 task_submit',
      kind: 'cohost_member_offline',
      subject: '插件组 light 成员 task_submit',
      content: '插件组 light 成员 task_submit当前不可用（load: ImportError x）；修复插件后将自动恢复，详见插件管理页。',
      level: 'error',
    })

    const list = usePluginHealthStore.getState().unavailable
    expect(list).toHaveLength(1)
    expect(list[0].kind).toBe('cohost_member_offline')
    expect(list[0].subject).toBe('插件组 light 成员 task_submit')
    expect(list[0].level).toBe('error')
    expect(list[0].content).toContain('ImportError')
    expect(list[0].firstSeenAt).toBeTruthy()
  })

  it('同键重报（同类同成员重发）upsert 不重复，firstSeenAt 保持首见', () => {
    const key = 'verify_failed|插件 fs_tools'
    usePluginHealthStore.getState().reportUnavailable({
      key, kind: 'verify_failed', subject: '插件 fs_tools', content: '旧正文', level: 'error',
    })
    const firstSeenAt = usePluginHealthStore.getState().unavailable[0].firstSeenAt

    usePluginHealthStore.getState().reportUnavailable({
      key, kind: 'verify_failed', subject: '插件 fs_tools', content: '新正文', level: 'error',
    })

    const list = usePluginHealthStore.getState().unavailable
    expect(list).toHaveLength(1)
    expect(list[0].content).toBe('新正文')
    expect(list[0].firstSeenAt).toBe(firstSeenAt)
  })

  it('不同键（不同成员/不同类）各自成行', () => {
    usePluginHealthStore.getState().reportUnavailable({
      key: 'k1', kind: 'spawn_breaker', subject: '插件 a', content: 'a', level: 'error',
    })
    usePluginHealthStore.getState().reportUnavailable({
      key: 'k2', kind: 'cohost_member_offline', subject: '插件组 g 成员 b', content: 'b', level: 'error',
    })

    expect(usePluginHealthStore.getState().unavailable).toHaveLength(2)
  })

  it('多行场景下 upsert 只动同键行，他行原样保留', () => {
    usePluginHealthStore.getState().reportUnavailable({
      key: 'k1', kind: 'spawn_breaker', subject: '插件 a', content: 'a 旧', level: 'error',
    })
    usePluginHealthStore.getState().reportUnavailable({
      key: 'k2', kind: 'verify_failed', subject: '插件 b', content: 'b', level: 'error',
    })

    usePluginHealthStore.getState().reportUnavailable({
      key: 'k1', kind: 'spawn_breaker', subject: '插件 a', content: 'a 新', level: 'error',
    })

    const list = usePluginHealthStore.getState().unavailable
    expect(list).toHaveLength(2)
    expect(list.find((e) => e.key === 'k1')?.content).toBe('a 新')
    expect(list.find((e) => e.key === 'k2')?.content).toBe('b')
  })

  it('登记即持久化（刷新不丢降级条目账本）', () => {
    usePluginHealthStore.getState().reportUnavailable({
      key: 'k1', kind: 'spawn_breaker', subject: '插件 a', content: 'a', level: 'error',
    })

    const raw = localStorage.getItem('agentos-plugin-health')
    expect(raw).toBeTruthy()
    const parsed = JSON.parse(raw ?? '{}') as { state?: { unavailable?: unknown[] } }
    expect(parsed.state?.unavailable).toHaveLength(1)
  })
})

describe('clearAllDegraded / reconcileWithContractStatuses 恢复对账', () => {
  it('clearAllDegraded 清空全部降级项', () => {
    usePluginHealthStore.setState({
      unavailable: [
        { key: 'k1', kind: 'spawn_breaker', subject: 'a', content: 'a', level: 'error', firstSeenAt: 't' },
        { key: 'k2', kind: 'verify_failed', subject: 'b', content: 'b', level: 'error', firstSeenAt: 't' },
      ],
    })

    usePluginHealthStore.getState().clearAllDegraded()

    expect(usePluginHealthStore.getState().unavailable).toHaveLength(0)
  })

  it('账本仍有 verify_failed：保留降级项（未恢复不撤条目）', () => {
    usePluginHealthStore.getState().reportUnavailable({
      key: 'k1', kind: 'verify_failed', subject: '插件 a', content: 'a', level: 'error',
    })

    usePluginHealthStore.getState().reconcileWithContractStatuses([
      status('ok'),
      status('verify_failed'),
      status('not_covered'),
    ])

    expect(usePluginHealthStore.getState().unavailable).toHaveLength(1)
  })

  it('账本非空且无 verify_failed（转正）：清全部降级项', () => {
    usePluginHealthStore.setState({
      unavailable: [
        { key: 'k1', kind: 'verify_failed', subject: 'a', content: 'a', level: 'error', firstSeenAt: 't' },
        { key: 'k2', kind: 'spawn_breaker', subject: 'b', content: 'b', level: 'error', firstSeenAt: 't' },
      ],
    })

    usePluginHealthStore.getState().reconcileWithContractStatuses([
      status('ok'),
      status('not_covered'),
      status('verify_incomplete'),
      status('drift'),
      status(undefined),
    ])

    expect(usePluginHealthStore.getState().unavailable).toHaveLength(0)
  })

  it('账本为空（读不到/未登记任何插件）：保守保留', () => {
    usePluginHealthStore.getState().reportUnavailable({
      key: 'k1', kind: 'verify_failed', subject: 'a', content: 'a', level: 'error',
    })

    usePluginHealthStore.getState().reconcileWithContractStatuses([])

    expect(usePluginHealthStore.getState().unavailable).toHaveLength(1)
  })
})

