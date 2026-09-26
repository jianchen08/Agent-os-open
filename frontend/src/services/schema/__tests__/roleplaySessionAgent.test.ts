/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * modeOptions — withSessionAgent 扮演会话绑定 + 发送链身份归一测试
 *
 * 契约（纯函数，无 store 依赖）：
 * - withSessionAgent：会话执行选项绑定 agentId 非空 → 浅合并出 mode=roleplay
 *   的新 execution_context（agent_id 本身走 WS 帧，不入 context）；无绑定原样；
 *   开演档键（roleplay_greeting/roleplay_user_persona）非空白时一并并入。
 * - composeRoleplaySendIdentity 三优先级：附身 > 扮演会话 > 不带；附身与扮演
 *   会话并存时附身独占（roleplay_persona 与 WS agent_id 两路不同时注入——卡键
 *   优先序已在后端 material.py 定死，前端只走单路），开演档键仅扮演会话路消费。
 *
 * 防拟合：正常/边界（undefined context、既有 mode 键、空串绑定）多组区分度输入
 * + 性质断言（浅合并纯度：入参对象零改动）。
 */

import { describe, expect, it } from 'vitest'
import {
  composeRoleplaySendIdentity,
  withSessionAgent,
  type RoleplayPossession,
} from '@/services/schema/modeOptions'

const possessed: RoleplayPossession = {
  card_id: 'card_luna',
  name: '月见',
  avatar: '🌙',
  personaText: '银发碧眼的月精灵法师。',
}

describe('withSessionAgent — 扮演会话绑定并入 execution_context', () => {
  it('绑定非空 → 浅合并出 mode=roleplay 的新对象（不改入参）', () => {
    const ctx: Record<string, unknown> = { workspace: { source_path: '/w' } }
    const merged = withSessionAgent(ctx, 'mode_roleplay/card_luna')
    expect(merged).toEqual({ workspace: { source_path: '/w' }, mode: 'roleplay' })
    expect(merged).not.toBe(ctx)
    expect(ctx).not.toHaveProperty('mode')
  })

  it('无绑定（undefined/空串）→ 原 context 原样返回（含 undefined）', () => {
    const ctx: Record<string, unknown> = { mode: 'coding', isolation: { level: 'high' } }
    expect(withSessionAgent(ctx, undefined)).toBe(ctx)
    expect(withSessionAgent(ctx, '')).toBe(ctx)
    expect(withSessionAgent(undefined, 'mode_roleplay/card_luna')).toEqual({ mode: 'roleplay' })
    expect(withSessionAgent(undefined, undefined)).toBeUndefined()
  })

  it('既有 mode 键被覆写为 roleplay（扮演会话即激活模式物料，不留旧值）', () => {
    const ctx: Record<string, unknown> = { mode: 'coding' }
    expect(withSessionAgent(ctx, 'mode_roleplay/card_rin')).toEqual({ mode: 'roleplay' })
  })

  it('开演档键（开场白/用户设定）非空并入 roleplay_greeting/roleplay_user_persona', () => {
    const merged = withSessionAgent(
      undefined,
      'mode_roleplay/card_luna',
      '*提灯的光圈里……*「欢迎光临！」',
      '北地来的佣兵，沉默寡言。',
    )
    expect(merged).toEqual({
      mode: 'roleplay',
      roleplay_greeting: '*提灯的光圈里……*「欢迎光临！」',
      roleplay_user_persona: '北地来的佣兵，沉默寡言。',
    })
    // 既有 context 之上浅合并（开演档不挤掉其余键）
    const ctx: Record<string, unknown> = { isolation: { level: 'high' } }
    expect(withSessionAgent(ctx, 'mode_roleplay/card_luna', '开场白', undefined)).toEqual({
      isolation: { level: 'high' },
      mode: 'roleplay',
      roleplay_greeting: '开场白',
    })
  })

  it('开演档键空串/缺席 → 键缺省（零注入，material.py 无键零段）', () => {
    expect(withSessionAgent(undefined, 'mode_roleplay/card_luna', '', '  ')).toEqual({ mode: 'roleplay' })
    expect(withSessionAgent(undefined, 'mode_roleplay/card_luna', undefined, undefined)).toEqual({
      mode: 'roleplay',
    })
  })
})

describe('composeRoleplaySendIdentity — 发送链三优先级', () => {
  it('附身独占：并存的会话绑定两路全让位（agentId 不带 + 只注入 roleplay_persona）', () => {
    const ctx: Record<string, unknown> = { workspace: { source_path: '/w' } }
    const { executionContext, agentId } = composeRoleplaySendIdentity(ctx, possessed, 'mode_roleplay/card_rin')
    expect(executionContext).toEqual({
      workspace: { source_path: '/w' },
      roleplay_persona: '银发碧眼的月精灵法师。',
      mode: 'roleplay',
    })
    expect(agentId).toBeUndefined()
  })

  it('仅扮演会话绑定：agentId 原样透传 + context 并入 mode=roleplay', () => {
    const ctx: Record<string, unknown> = { workspace: { source_path: '/w' } }
    const { executionContext, agentId } = composeRoleplaySendIdentity(ctx, null, 'mode_roleplay/card_rin')
    expect(executionContext).toEqual({ workspace: { source_path: '/w' }, mode: 'roleplay' })
    expect(agentId).toBe('mode_roleplay/card_rin')
  })

  it('开演档键仅在扮演会话路透传（附身路不消费）', () => {
    const ctx: Record<string, unknown> = { workspace: { source_path: '/w' } }
    const session = composeRoleplaySendIdentity(ctx, null, 'mode_roleplay/card_rin', '开场白', '用户设定')
    expect(session.executionContext).toEqual({
      workspace: { source_path: '/w' },
      mode: 'roleplay',
      roleplay_greeting: '开场白',
      roleplay_user_persona: '用户设定',
    })
    expect(session.agentId).toBe('mode_roleplay/card_rin')
    const possess = composeRoleplaySendIdentity(ctx, possessed, 'mode_roleplay/card_rin', '开场白', '用户设定')
    expect(possess.executionContext).toEqual({
      workspace: { source_path: '/w' },
      roleplay_persona: '银发碧眼的月精灵法师。',
      mode: 'roleplay',
    })
    expect(possess.agentId).toBeUndefined()
  })

  it('都无：context 原样（含 undefined）+ 不带 agentId', () => {
    const ctx: Record<string, unknown> = { isolation: { level: 'high' } }
    expect(composeRoleplaySendIdentity(ctx, null, undefined)).toEqual({
      executionContext: ctx,
      agentId: undefined,
    })
    expect(composeRoleplaySendIdentity(undefined, null, undefined)).toEqual({
      executionContext: undefined,
      agentId: undefined,
    })
  })

  it('附身空人设档仍独占（存在即优先，与 personaText 内容无关）', () => {
    const { executionContext, agentId } = composeRoleplaySendIdentity(
      undefined,
      { ...possessed, personaText: '' },
      'mode_roleplay/card_rin',
    )
    expect(executionContext).toEqual({ roleplay_persona: '', mode: 'roleplay' })
    expect(agentId).toBeUndefined()
  })
})
