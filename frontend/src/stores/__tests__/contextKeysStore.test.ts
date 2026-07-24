/**
 * ContextKeysStore 测试（ADR §3.4）
 *
 * 前端维护 context keys 状态（Zustand store），各事件更新它。
 * contributes 的 when 命中 → 可见，失配 → 隐藏。
 *
 * 基础集：pipeline.running/pipeline.idle、workspace.focus/chat.focus、
 * resource.isFile/resource.extname、interaction.pending
 */

import { describe, it, expect, beforeEach } from 'vitest'
import { useContextKeys } from '@/stores/contextKeysStore'
import { evaluateWhen } from '@/services/schema/whenExpression'

describe('ContextKeysStore — 基础读写', () => {
  beforeEach(() => {
    // 每个测试前重置到默认状态
    useContextKeys.getState().reset()
  })

  it('初始状态：pipeline.idle=true（空闲），其余 focus/isFile 默认 false', () => {
    const ctx = useContextKeys.getState().keys
    expect(ctx['pipeline.idle']).toBe(true)
    expect(ctx['pipeline.running']).toBe(false)
    expect(ctx['workspace.focus']).toBe(false)
    expect(ctx['chat.focus']).toBe(false)
    expect(ctx['interaction.pending']).toBe(false)
  })

  it('setKey 更新单个 key', () => {
    useContextKeys.getState().setKey('pipeline.running', true)
    expect(useContextKeys.getState().keys['pipeline.running']).toBe(true)
  })

  it('setKeys 批量更新', () => {
    useContextKeys.getState().setKeys({ 'pipeline.running': true, 'chat.focus': true })
    expect(useContextKeys.getState().keys['pipeline.running']).toBe(true)
    expect(useContextKeys.getState().keys['chat.focus']).toBe(true)
  })

  it('setPipelineRunning 同时设置 running=true 和 idle=false', () => {
    useContextKeys.getState().setPipelineRunning(true)
    const ctx = useContextKeys.getState().keys
    expect(ctx['pipeline.running']).toBe(true)
    expect(ctx['pipeline.idle']).toBe(false)
  })

  it('setPipelineRunning(false) 同时设置 running=false 和 idle=true', () => {
    useContextKeys.getState().setPipelineRunning(false)
    const ctx = useContextKeys.getState().keys
    expect(ctx['pipeline.running']).toBe(false)
    expect(ctx['pipeline.idle']).toBe(true)
  })

  it('setResource 更新 isFile + extname', () => {
    useContextKeys.getState().setResource({ isFile: true, extname: '.py' })
    const ctx = useContextKeys.getState().keys
    expect(ctx['resource.isFile']).toBe(true)
    expect(ctx['resource.extname']).toBe('.py')
  })

  it('reset 恢复默认状态', () => {
    useContextKeys.getState().setKeys({ 'pipeline.running': true, 'chat.focus': true })
    useContextKeys.getState().reset()
    const ctx = useContextKeys.getState().keys
    expect(ctx['pipeline.running']).toBe(false)
    expect(ctx['pipeline.idle']).toBe(true)
  })
})

describe('ContextKeysStore — 配合 evaluateWhen', () => {
  beforeEach(() => {
    useContextKeys.getState().reset()
  })

  it('默认状态下 when="pipeline.running" 不可见', () => {
    const ctx = useContextKeys.getState().keys
    expect(evaluateWhen('pipeline.running', ctx)).toBe(false)
  })

  it('启动流水线后 when="pipeline.running" 可见', () => {
    useContextKeys.getState().setPipelineRunning(true)
    const ctx = useContextKeys.getState().keys
    expect(evaluateWhen('pipeline.running', ctx)).toBe(true)
  })

  it('when="pipeline.idle" 默认可见（空闲态）', () => {
    const ctx = useContextKeys.getState().keys
    expect(evaluateWhen('pipeline.idle', ctx)).toBe(true)
  })

  it('设置 Python 资源后 when="resource.extname == \'.py\'" 可见', () => {
    useContextKeys.getState().setResource({ isFile: true, extname: '.py' })
    const ctx = useContextKeys.getState().keys
    expect(evaluateWhen("resource.extname == '.py'", ctx)).toBe(true)
  })
})

describe('ContextKeysStore — 订阅响应式', () => {
  beforeEach(() => {
    useContextKeys.getState().reset()
  })

  it('getKey 读取单个 key 值', () => {
    useContextKeys.getState().setKey('custom.key', 'hello')
    expect(useContextKeys.getState().getKey('custom.key')).toBe('hello')
  })

  it('getKey 未声明返回 undefined', () => {
    expect(useContextKeys.getState().getKey('nonexistent')).toBeUndefined()
  })
})
