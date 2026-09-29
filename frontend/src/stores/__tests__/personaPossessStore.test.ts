/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * personaPossessStore — 附身档载荷校验、注入键钉住与持久化/迁移测试（批 G②）
 *
 * - parsePossessPayload：mode 形态守卫 + personaText 载荷两形态（携带 → 原样
 *   入档 / 缺席 → 容缺空串，可选字段不触发整包拒绝）；必要项缺失 fail-closed。
 * - setPossessed：personaKey 随档钉住（解析结果原样入档）；空白键拒绝。
 * - 旧 roleplay.possessed 档一次性迁移：补 mode='roleplay' + personaKey 钉旧
 *   契约键 roleplay_persona（历史数据面兼容）；新档在场幂等不覆盖；旧键清除。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { parsePossessPayload, usePersonaPossessStore } from '@/stores/personaPossessStore'

describe('parsePossessPayload — mode 守卫 + personaText 载荷两形态', () => {
  it('携带 mode/personaText → 原样入档（不含注入键——键由 setPossessed 钉住）', () => {
    expect(
      parsePossessPayload({
        mode: 'roleplay', card_id: 'card_luna', name: '月见', avatar: '🌙',
        personaText: '银发碧眼的月精灵法师。',
      }),
    ).toEqual({
      mode: 'roleplay', card_id: 'card_luna', name: '月见', avatar: '🌙',
      personaText: '银发碧眼的月精灵法师。',
    })
  })

  it('缺席/空串 personaText → 容缺空串（可选字段，整包不拒绝）', () => {
    expect(parsePossessPayload({ mode: 'roleplay', card_id: 'card_rin', name: '凛', avatar: '🎭' })).toEqual({
      mode: 'roleplay', card_id: 'card_rin', name: '凛', avatar: '🎭', personaText: '',
    })
    expect(
      parsePossessPayload({ mode: 'roleplay', card_id: 'card_rin', name: '凛', avatar: '🎭', personaText: '' }),
    ).toEqual({ mode: 'roleplay', card_id: 'card_rin', name: '凛', avatar: '🎭', personaText: '' })
  })

  it('mode 形态守卫：缺席/大写/连字符/非字符串 → 整包拒绝（防凭空键）', () => {
    expect(parsePossessPayload({ card_id: 'card_x', name: '月见', avatar: '🌙' })).toBeNull()
    expect(parsePossessPayload({ mode: 'Roleplay', card_id: 'card_x', name: '月见', avatar: '🌙' })).toBeNull()
    expect(parsePossessPayload({ mode: 'mode-x', card_id: 'card_x', name: '月见', avatar: '🌙' })).toBeNull()
    expect(parsePossessPayload({ mode: 42, card_id: 'card_x', name: '月见', avatar: '🌙' })).toBeNull()
  })

  it('必要项缺失仍 fail-closed（personaText/mode 可选性不改必要项口径）', () => {
    expect(parsePossessPayload({ mode: 'roleplay', card_id: '', name: '月见', avatar: '🌙' })).toBeNull()
    expect(parsePossessPayload({ mode: 'roleplay', card_id: 'card_x', name: '缺 avatar' })).toBeNull()
    expect(parsePossessPayload(null)).toBeNull()
  })
})

describe('setPossessed — 注入键随档钉住（registry 解析结果入档）', () => {
  beforeEach(() => {
    localStorage.clear()
    usePersonaPossessStore.setState({ possessed: null })
  })

  it('已解析键原样入档；null（registry 不可达）容许入档（发送链降级不注入）', () => {
    expect(
      usePersonaPossessStore.getState().setPossessed(
        { mode: 'roleplay', card_id: 'card_luna', name: '月见', avatar: '🌙' },
        'roleplay_persona',
      ),
    ).toBe(true)
    expect(usePersonaPossessStore.getState().possessed).toEqual({
      mode: 'roleplay', card_id: 'card_luna', name: '月见', avatar: '🌙',
      personaText: '', personaKey: 'roleplay_persona',
    })
    expect(
      usePersonaPossessStore.getState().setPossessed(
        { mode: 'roleplay', card_id: 'card_rin', name: '凛', avatar: '🎭' },
        null,
      ),
    ).toBe(true)
    expect(usePersonaPossessStore.getState().possessed?.personaKey).toBeNull()
  })

  it('空白注入键拒绝（零状态变更）；非法载荷拒绝', () => {
    expect(
      usePersonaPossessStore.getState().setPossessed(
        { mode: 'roleplay', card_id: 'card_luna', name: '月见', avatar: '🌙' },
        '   ',
      ),
    ).toBe(false)
    expect(usePersonaPossessStore.getState().possessed).toBeNull()
    expect(usePersonaPossessStore.getState().setPossessed({ mode: 'x' }, 'k')).toBe(false)
  })
})

describe('旧 roleplay.possessed 档一次性迁移（数据面兼容）', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.resetModules()
  })

  it('旧档（v1 roleplay 专属形态）重水化 → persona.possessed 补 mode/键，旧键清除', async () => {
    localStorage.setItem(
      'roleplay.possessed',
      JSON.stringify({
        state: { possessed: { card_id: 'card_old', name: '旧档', avatar: '🌙', personaText: '旧人设。' } },
        version: 1,
      }),
    )
    const { usePersonaPossessStore: fresh } = await import('@/stores/personaPossessStore')
    expect(fresh.getState().possessed).toEqual({
      mode: 'roleplay', card_id: 'card_old', name: '旧档', avatar: '🌙',
      personaText: '旧人设。', personaKey: 'roleplay_persona',
    })
    expect(localStorage.getItem('roleplay.possessed')).toBeNull()
    expect(localStorage.getItem('persona.possessed')).toContain('roleplay')
  })

  it('新档已在场 → 幂等不覆盖（旧键清除）', async () => {
    localStorage.setItem(
      'persona.possessed',
      JSON.stringify({
        state: { possessed: { mode: 'groupchat', card_id: 'card_g', name: '群', avatar: '💬', personaText: '', personaKey: 'groupchat_persona' } },
        version: 1,
      }),
    )
    localStorage.setItem(
      'roleplay.possessed',
      JSON.stringify({ state: { possessed: { card_id: 'card_old', name: '旧档', avatar: '🌙' } }, version: 1 }),
    )
    const { usePersonaPossessStore: fresh } = await import('@/stores/personaPossessStore')
    expect(fresh.getState().possessed?.mode).toBe('groupchat')
    expect(localStorage.getItem('roleplay.possessed')).toBeNull()
  })

  it('新档（通用形态）重水化原样恢复', async () => {
    localStorage.setItem(
      'persona.possessed',
      JSON.stringify({
        state: {
          possessed: {
            mode: 'roleplay', card_id: 'card_new', name: '新档', avatar: '🎭',
            personaText: '北地佣兵。', personaKey: 'roleplay_persona',
          },
        },
        version: 1,
      }),
    )
    const { usePersonaPossessStore: fresh } = await import('@/stores/personaPossessStore')
    expect(fresh.getState().possessed).toEqual({
      mode: 'roleplay', card_id: 'card_new', name: '新档', avatar: '🎭',
      personaText: '北地佣兵。', personaKey: 'roleplay_persona',
    })
  })
})

// 模块级持久 store：本文件结束时归零，防用例间/文件间串档
afterEach(() => {
  usePersonaPossessStore.setState({ possessed: null })
})
