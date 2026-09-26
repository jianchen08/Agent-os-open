/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * modeOptions — withRoleplayPersona 附身态并入测试
 *
 * 契约（纯函数，无 store 依赖）：附身存在 → 浅合并出带 roleplay_persona（卡人设
 * 文本）+ mode=roleplay（附身即激活模式物料）的新 execution_context；无附身原样
 * 返回。主 agent 身份不变（工具面天然全量），不走 agent_id 身份切换。
 *
 * 防拟合：正常/边界（undefined context、既有 mode 键、空串人设）多组区分度输入
 * + 性质断言（浅合并纯度：入参对象零改动）。
 */

import { describe, expect, it } from 'vitest'
import { withRoleplayPersona, type RoleplayPossession } from '@/services/schema/modeOptions'

const possessed: RoleplayPossession = {
  card_id: 'card_luna',
  name: '月见',
  avatar: '🌙',
  personaText: '银发碧眼的月精灵法师，月光神殿的最后一位守望者。',
}

describe('withRoleplayPersona — 附身态并入 execution_context', () => {
  it('附身存在 → 并入 roleplay_persona + mode=roleplay，浅合并出新对象', () => {
    const ctx: Record<string, unknown> = { workspace: { source_path: '/w' } }
    const merged = withRoleplayPersona(ctx, possessed)
    expect(merged).toEqual({
      workspace: { source_path: '/w' },
      roleplay_persona: '银发碧眼的月精灵法师，月光神殿的最后一位守望者。',
      mode: 'roleplay',
    })
    expect(merged).not.toBe(ctx)
    expect(ctx).not.toHaveProperty('roleplay_persona')
    expect(ctx).not.toHaveProperty('mode')
  })

  it('无附身（null）→ 原 context 原样返回（含 undefined）', () => {
    const ctx: Record<string, unknown> = { isolation: { level: 'high' } }
    expect(withRoleplayPersona(ctx, null)).toBe(ctx)
    expect(withRoleplayPersona(undefined, null)).toBeUndefined()
  })

  it('既有 mode 键被附身覆写为 roleplay（附身即激活模式物料，不留旧值）', () => {
    const ctx: Record<string, unknown> = { mode: 'coding', workspace: { source_path: '/w' } }
    expect(withRoleplayPersona(ctx, possessed)).toMatchObject({ mode: 'roleplay' })
  })

  it('personaText 空串照样并入（空串容许：物料侧零人设注入，模式键仍生效）', () => {
    const merged = withRoleplayPersona(undefined, { ...possessed, personaText: '' })
    expect(merged).toEqual({ roleplay_persona: '', mode: 'roleplay' })
  })
})
