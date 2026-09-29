/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * modeOptions — withPossession 附身态并入测试（批 G② 通用化）
 *
 * 契约（纯函数，无 store 依赖）：附身存在且注入键已解析 → 浅合并出
 * `[personaKey]: personaText`（注入键随附身档钉住，registry decl.persona.from
 * 派生，零硬编码）+ mode=<附身模式键>（覆写既有 mode 键）的新
 * execution_context；无附身或键未解析（registry 不可达诚实降级）原样返回。
 * 主 agent 身份不变（工具面天然全量），不走 agent_id 身份切换。
 *
 * 防拟合：正常/边界（undefined context、既有 mode 键、空串人设、键未解析）
 * 多组区分度输入 + 性质断言（浅合并纯度：入参对象零改动）。
 */

import { describe, expect, it } from 'vitest'
import { withPossession, type PersonaPossession } from '@/services/schema/modeOptions'

const possessed: PersonaPossession = {
  mode: 'roleplay',
  card_id: 'card_luna',
  name: '月见',
  avatar: '🌙',
  personaText: '银发碧眼的月精灵法师，月光神殿的最后一位守望者。',
  personaKey: 'roleplay_persona',
}

/** 换模式即换键：注入键随声明派生（groupchat 模式声明 from=groupchat_persona） */
const groupPossessed: PersonaPossession = {
  ...possessed,
  mode: 'groupchat',
  personaKey: 'groupchat_persona',
}

describe('withPossession — 附身态并入 execution_context（注入键随声明）', () => {
  it('附身存在 → 并入 [personaKey] 人设 + mode=附身模式键，浅合并出新对象', () => {
    const ctx: Record<string, unknown> = { workspace: { source_path: '/w' } }
    const merged = withPossession(ctx, possessed)
    expect(merged).toEqual({
      workspace: { source_path: '/w' },
      roleplay_persona: '银发碧眼的月精灵法师，月光神殿的最后一位守望者。',
      mode: 'roleplay',
    })
    expect(merged).not.toBe(ctx)
    expect(ctx).not.toHaveProperty('roleplay_persona')
    expect(ctx).not.toHaveProperty('mode')
  })

  it('注入键随模式声明派生（不同模式不同键，非硬编码 roleplay_persona）', () => {
    const merged = withPossession(undefined, groupPossessed)
    expect(merged).toEqual({
      groupchat_persona: possessed.personaText,
      mode: 'groupchat',
    })
    expect(merged).not.toHaveProperty('roleplay_persona')
  })

  it('无附身（null）→ 原 context 原样返回（含 undefined）', () => {
    const ctx: Record<string, unknown> = { isolation: { level: 'high' } }
    expect(withPossession(ctx, null)).toBe(ctx)
    expect(withPossession(undefined, null)).toBeUndefined()
  })

  it('注入键未解析（personaKey=null，registry 不可达）→ 诚实降级不注入附身', () => {
    const ctx: Record<string, unknown> = { mode: 'coding', workspace: { source_path: '/w' } }
    expect(withPossession(ctx, { ...possessed, personaKey: null })).toBe(ctx)
    expect(withPossession(undefined, { ...possessed, personaKey: null })).toBeUndefined()
  })

  it('既有 mode 键被附身覆写为附身模式键（附身即激活模式物料，不留旧值）', () => {
    const ctx: Record<string, unknown> = { mode: 'coding', workspace: { source_path: '/w' } }
    expect(withPossession(ctx, possessed)).toMatchObject({ mode: 'roleplay' })
  })

  it('personaText 空串照样并入（空串容许：物料侧零人设注入，模式键仍生效）', () => {
    const merged = withPossession(undefined, { ...possessed, personaText: '' })
    expect(merged).toEqual({ roleplay_persona: '', mode: 'roleplay' })
  })
})
