/** @feature FP-0.2.九 会话工作空间与会话列表（自定义分组） @vision V6 可即用 @ci frontend-test */
/**
 * sessionGroups 契约测试
 *
 * 契约：
 * - createSessionGroup 建空组，名净化（trim+截断），空白名拒绝；
 * - assignSessionToGroup 单归属幂等：同会话再分配先移出旧组；组不存在拒绝；
 * - unassignSession 移出所有组，不在任何组时无变化不落盘；
 * - deleteSessionGroup 删组不删会话（成员 id 随组消失，渲染侧回落平铺）；
 * - 损坏载荷按空表处理；超上限丢最旧。
 */

import { beforeEach, describe, expect, it } from 'vitest'
import {
  GROUP_NAME_MAX,
  assignSessionToGroup,
  createSessionGroup,
  deleteSessionGroup,
  listSessionGroups,
  sanitizeGroupName,
  unassignSession,
} from '@/services/sessionGroups'
import { uiStorage } from '@/utils/storage'

beforeEach(() => {
  uiStorage.setSessionCustomGroups([])
})

describe('sessionGroups — 建组与组名', () => {
  it('建组返回新空组并落盘，组名去首尾空白', () => {
    const g = createSessionGroup('  工作  ')
    expect(g.name).toBe('工作')
    expect(g.sessionIds).toEqual([])
    expect(listSessionGroups()).toHaveLength(1)
    expect(listSessionGroups()[0].name).toBe('工作')
  })

  it('组名超长截断到上限', () => {
    expect(sanitizeGroupName('长'.repeat(GROUP_NAME_MAX + 10))).toHaveLength(GROUP_NAME_MAX)
  })

  it('空白名建组拒绝（抛错不落盘）', () => {
    expect(() => createSessionGroup('   ')).toThrow()
    expect(listSessionGroups()).toHaveLength(0)
  })

  it('损坏载荷按空表处理（读侧形状过滤）', () => {
    uiStorage.setSessionCustomGroups([null, { id: 1 }, { id: 'g', name: 'x' }, 'junk'])
    expect(listSessionGroups()).toHaveLength(0)
  })
})

describe('sessionGroups — 分配语义', () => {
  it('分配后再分配到别的组：单归属，旧组不再含该会话', () => {
    const a = createSessionGroup('甲')
    const b = createSessionGroup('乙')
    assignSessionToGroup(a.id, 's1')
    assignSessionToGroup(b.id, 's1')

    const groups = listSessionGroups()
    const ga = groups.find((g) => g.id === a.id)
    const gb = groups.find((g) => g.id === b.id)
    expect(ga?.sessionIds).toEqual([])
    expect(gb?.sessionIds).toEqual(['s1'])
  })

  it('同组重复分配幂等（不产生重复 id）', () => {
    const g = createSessionGroup('工作')
    assignSessionToGroup(g.id, 's1')
    assignSessionToGroup(g.id, 's1')
    expect(listSessionGroups()[0].sessionIds).toEqual(['s1'])
  })

  it('分配到不存在的组抛错且不落盘', () => {
    createSessionGroup('工作')
    expect(() => assignSessionToGroup('grp-nope', 's1')).toThrow('分组不存在')
    expect(listSessionGroups()[0].sessionIds).toEqual([])
  })

  it('移出会话后所有组都不含它；无变化时不写存储', () => {
    const g = createSessionGroup('工作')
    assignSessionToGroup(g.id, 's1')
    unassignSession('s1')
    expect(listSessionGroups()[0].sessionIds).toEqual([])

    const before = JSON.stringify(listSessionGroups())
    unassignSession('s1') // 已不在任何组，再移出无变化
    expect(JSON.stringify(listSessionGroups())).toBe(before)
  })
})

describe('sessionGroups — 删组与容量', () => {
  it('删组只删组，其他组与会话 id 不受影响', () => {
    const a = createSessionGroup('甲')
    const b = createSessionGroup('乙')
    assignSessionToGroup(a.id, 's1')
    assignSessionToGroup(b.id, 's2')

    deleteSessionGroup(a.id)
    const groups = listSessionGroups()
    expect(groups).toHaveLength(1)
    expect(groups[0].id).toBe(b.id)
    expect(groups[0].sessionIds).toEqual(['s2'])
  })

  it('超上限丢最旧（性质：总量恒有界）', () => {
    for (let i = 0; i < 52; i++) createSessionGroup(`组${i}`)
    const groups = listSessionGroups()
    expect(groups.length).toBeLessThanOrEqual(50)
    expect(groups[groups.length - 1].name).toBe('组51')
    expect(groups[0].name).toBe('组2')
  })
})
