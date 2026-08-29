/** @ci frontend-test */
/**
 * resolveSendTarget 发送目标解析测试（一对一 + 串桶防线）
 *
 * 契约：目标 = 所在标签管道原样透传（主标签=主管道、子标签=子管道，
 * 后端按此值路由）；目标不属于该会话（成员集 = 标签管道映射键 ∪
 * 会话快照 pipelineIds）→ undefined，调用方 fail-closed 终止发送——
 * 绝不改发主管道（子管道视图发送落主管道 = 写错桶）。
 */
import { describe, expect, it } from 'vitest'
import { resolveSendTarget } from '@/utils/mappers'

describe('resolveSendTarget 发送目标解析（一对一）', () => {
  const tabMap = { 'pid-main': 'main-s1', 'pid-sub': 'sub-s1' }
  const session = { pipelineIds: ['pid-main', 'pid-sub'] }

  it('子标签发送 → 子管道原样透传（不落主管道）', () => {
    expect(resolveSendTarget('pid-sub', session, tabMap)).toBe('pid-sub')
  })

  it('主标签发送 → 主管道原样透传（主聊行为不变）', () => {
    expect(resolveSendTarget('pid-main', session, tabMap)).toBe('pid-main')
  })

  it('会话快照成员优先于标签映射：映射缺失窗口仍可发送', () => {
    expect(resolveSendTarget('pid-sub', session, { 'pid-main': 'main-s1' })).toBe('pid-sub')
  })

  it('标签映射成员：会话快照未含（新建子管道未刷新）仍可发送', () => {
    expect(resolveSendTarget('pid-sub', { pipelineIds: ['pid-main'] }, tabMap)).toBe('pid-sub')
  })

  it('无会话快照时以标签映射为准', () => {
    expect(resolveSendTarget('pid-sub', undefined, tabMap)).toBe('pid-sub')
    expect(resolveSendTarget('pid-foreign', undefined, tabMap)).toBeUndefined()
  })

  it('外来管道（其他会话）→ undefined，fail-closed 拒发', () => {
    expect(resolveSendTarget('pid-other-session', { pipelineIds: ['pid-main'] }, tabMap)).toBeUndefined()
  })

  it('目标缺失 → undefined（不猜测回退）', () => {
    expect(resolveSendTarget(undefined, session, tabMap)).toBeUndefined()
    expect(resolveSendTarget('', session, tabMap)).toBeUndefined()
    expect(resolveSendTarget('pid-sub', undefined, {})).toBeUndefined()
  })

  it('性质断言：结果要么 undefined、要么必属当前会话成员集（身份透传，绝无改写）', () => {
    for (const input of ['pid-sub', 'pid-main', 'pid-foreign', '']) {
      const result = resolveSendTarget(input || undefined, session, tabMap)
      expect(result === undefined || ['pid-main', 'pid-sub'].includes(result)).toBe(true)
      if (result !== undefined) expect(result).toBe(input)
    }
  })
})
