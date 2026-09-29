/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * modeOptions — withSessionBinding 会话绑定 + composeSendIdentity 身份归一测试
 * （批 G② 通用化）
 *
 * 契约（纯函数，无 store 依赖）：
 * - withSessionBinding：会话绑定在场（modeBinding/agentId/extraContext 任一）→
 *   浅合并出 mode=<绑定模式键>（空串不带）+ 扩展上下文键的新 execution_context
 *   （agent_id 本身走 WS 帧，不入 context）；无绑定原样。
 * - composeSendIdentity 三优先级：附身 > 会话绑定 > 不带；附身与会话绑定并存
 *   时附身独占（人设键与 WS agent_id 两路不同时注入——卡键优先序已在后端
 *   material.py 定死，前端只走单路），扩展上下文键仅会话绑定路消费。
 *
 * 防拟合：正常/边界（undefined context、既有 mode 键、空 mode 绑定、附身键
 * 未解析）多组区分度输入 + 性质断言（浅合并纯度：入参对象零改动）。
 */

import { describe, expect, it } from 'vitest'
import {
  composeSendIdentity,
  withSessionBinding,
  type PersonaPossession,
  type SessionBinding,
} from '@/services/schema/modeOptions'

const possessed: PersonaPossession = {
  mode: 'roleplay',
  card_id: 'card_luna',
  name: '月见',
  avatar: '🌙',
  personaText: '银发碧眼的月精灵法师。',
  personaKey: 'roleplay_persona',
}

const binding: SessionBinding = {
  mode: 'roleplay',
  agentId: 'mode_roleplay/card_luna',
  extraContext: { roleplay_greeting: '「欢迎光临！」', roleplay_user_persona: '北地来的佣兵。' },
}

describe('withSessionBinding — 会话绑定并入 execution_context', () => {
  it('绑定在场 → 浅合并出 mode 键 + 扩展上下文键的新对象（不改入参）', () => {
    const ctx: Record<string, unknown> = { workspace: { source_path: '/w' } }
    const merged = withSessionBinding(ctx, binding)
    expect(merged).toEqual({
      workspace: { source_path: '/w' },
      mode: 'roleplay',
      roleplay_greeting: '「欢迎光临！」',
      roleplay_user_persona: '北地来的佣兵。',
    })
    expect(merged).not.toBe(ctx)
    expect(ctx).not.toHaveProperty('mode')
  })

  it('无绑定（undefined）→ 原 context 原样返回（含 undefined）', () => {
    const ctx: Record<string, unknown> = { mode: 'stale', isolation: { level: 'high' } }
    expect(withSessionBinding(ctx, undefined)).toBe(ctx)
    expect(withSessionBinding(undefined, undefined)).toBeUndefined()
  })

  it('既有 mode 键被覆写为绑定模式键（会话身份出生即定，不留旧值）', () => {
    const ctx: Record<string, unknown> = { mode: 'coding' }
    expect(withSessionBinding(ctx, { mode: 'writing' })).toEqual({ mode: 'writing' })
  })

  it('mode 空串（agent 绑定而模式缺席，如遗留快照）→ 不带 mode 键，仅并入扩展键', () => {
    const ctx: Record<string, unknown> = { mode: 'coding', isolation: { level: 'high' } }
    expect(withSessionBinding(ctx, { mode: '', extraContext: { k1: 'v1' } })).toEqual({
      mode: 'coding',
      isolation: { level: 'high' },
      k1: 'v1',
    })
  })

  it('空扩展上下文（缺省）→ 仅 mode 键', () => {
    expect(withSessionBinding(undefined, { mode: 'writing' })).toEqual({ mode: 'writing' })
  })
})

describe('composeSendIdentity — 发送链三优先级', () => {
  it('附身独占：并存的会话绑定两路全让位（agentId 不带 + 只注入人设键）', () => {
    const ctx: Record<string, unknown> = { workspace: { source_path: '/w' } }
    const { executionContext, agentId } = composeSendIdentity(ctx, possessed, binding)
    expect(executionContext).toEqual({
      workspace: { source_path: '/w' },
      roleplay_persona: '银发碧眼的月精灵法师。',
      mode: 'roleplay',
    })
    expect(executionContext).not.toHaveProperty('roleplay_greeting')
    expect(agentId).toBeUndefined()
  })

  it('仅会话绑定：agentId 原样透传 + context 并入 mode/扩展键', () => {
    const ctx: Record<string, unknown> = { workspace: { source_path: '/w' } }
    const { executionContext, agentId } = composeSendIdentity(ctx, null, binding)
    expect(executionContext).toEqual({
      workspace: { source_path: '/w' },
      mode: 'roleplay',
      roleplay_greeting: '「欢迎光临！」',
      roleplay_user_persona: '北地来的佣兵。',
    })
    expect(agentId).toBe('mode_roleplay/card_luna')
  })

  it('附身键未解析（registry 不可达降级）→ 附身路不注入，回落会话绑定路', () => {
    const degraded = { ...possessed, personaKey: null }
    const { executionContext, agentId } = composeSendIdentity(undefined, degraded, binding)
    expect(executionContext).toEqual({
      mode: 'roleplay',
      roleplay_greeting: '「欢迎光临！」',
      roleplay_user_persona: '北地来的佣兵。',
    })
    expect(agentId).toBe('mode_roleplay/card_luna')
  })

  it('都无：context 原样（含 undefined）+ 不带 agentId', () => {
    const ctx: Record<string, unknown> = { isolation: { level: 'high' } }
    expect(composeSendIdentity(ctx, null, undefined)).toEqual({
      executionContext: ctx,
      agentId: undefined,
    })
    expect(composeSendIdentity(undefined, null, undefined)).toEqual({
      executionContext: undefined,
      agentId: undefined,
    })
  })

  it('附身空人设档仍独占（存在即优先，与 personaText 内容无关）', () => {
    const { executionContext, agentId } = composeSendIdentity(
      undefined,
      { ...possessed, personaText: '' },
      binding,
    )
    expect(executionContext).toEqual({ roleplay_persona: '', mode: 'roleplay' })
    expect(agentId).toBeUndefined()
  })
})
