/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * roleplayPossessStore — 附身档载荷校验与持久化迁移测试
 *
 * - parsePossessPayload：personaText 载荷两形态（携带 → 原样入档 / 缺席 → 容缺
 *   空串，可选字段不触发整包拒绝）；必要项缺失仍 fail-closed。
 * - persist 迁移：v0 旧档（无 personaText）→ v1 迁移补空串（附身档跨升级连贯）。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { parsePossessPayload, useRoleplayPossessStore } from '@/stores/roleplayPossessStore'

describe('parsePossessPayload — personaText 载荷两形态', () => {
  it('携带 personaText → 原样入档', () => {
    expect(
      parsePossessPayload({
        card_id: 'card_luna', name: '月见', avatar: '🌙', personaText: '银发碧眼的月精灵法师。',
      }),
    ).toEqual({
      card_id: 'card_luna', name: '月见', avatar: '🌙', personaText: '银发碧眼的月精灵法师。',
    })
  })

  it('缺席/空串 personaText → 容缺空串（可选字段，整包不拒绝）', () => {
    expect(parsePossessPayload({ card_id: 'card_rin', name: '凛', avatar: '🎭' })).toEqual({
      card_id: 'card_rin', name: '凛', avatar: '🎭', personaText: '',
    })
    expect(parsePossessPayload({ card_id: 'card_rin', name: '凛', avatar: '🎭', personaText: '' })).toEqual({
      card_id: 'card_rin', name: '凛', avatar: '🎭', personaText: '',
    })
  })

  it('必要项缺失仍 fail-closed（personaText 可选不改必要项口径）', () => {
    expect(parsePossessPayload({ card_id: '', name: '月见', avatar: '🌙', personaText: 'x' })).toBeNull()
    expect(parsePossessPayload({ card_id: 'card_x', name: '缺 avatar', personaText: 'x' })).toBeNull()
    expect(parsePossessPayload(null)).toBeNull()
  })
})

describe('persist 迁移 — v0 旧档补 personaText', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.resetModules()
  })

  it('v0 附身档（无 personaText）重水化 → personaText 容缺空串', async () => {
    localStorage.setItem(
      'roleplay.possessed',
      JSON.stringify({
        state: { possessed: { card_id: 'card_old', name: '旧档', avatar: '🌙' } },
        version: 0,
      }),
    )
    const { useRoleplayPossessStore: fresh } = await import('@/stores/roleplayPossessStore')
    expect(fresh.getState().possessed).toEqual({
      card_id: 'card_old', name: '旧档', avatar: '🌙', personaText: '',
    })
  })

  it('v1 新档重水化原样恢复（版本一致不走迁移）', async () => {
    localStorage.setItem(
      'roleplay.possessed',
      JSON.stringify({
        state: { possessed: { card_id: 'card_new', name: '新档', avatar: '🎭', personaText: '北地佣兵。' } },
        version: 1,
      }),
    )
    const { useRoleplayPossessStore: fresh } = await import('@/stores/roleplayPossessStore')
    expect(fresh.getState().possessed).toEqual({
      card_id: 'card_new', name: '新档', avatar: '🎭', personaText: '北地佣兵。',
    })
  })
})

// 模块级持久 store：本文件结束时归零，防用例间/文件间串档
afterEach(() => {
  useRoleplayPossessStore.setState({ possessed: null })
})
