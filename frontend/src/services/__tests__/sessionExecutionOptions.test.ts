// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/**
 * 会话插件表单值持久层测试（快照 v2：values + executionContext 成品区）
 *
 * 行为契约：
 * - save/load 整包往返一致（含 executionContext 区）；无记录返回 null；
 *   损坏 JSON / 非法形状（缺 values 区、数组）均按无记录处理；
 * - 本层不感知具体字段名——测试数据形状即契约。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  clearSessionExecutionOptions,
  loadSessionExecutionOptions,
  saveSessionExecutionOptions,
} from '@/services/sessionExecutionOptions'

beforeEach(() => {
  localStorage.clear()
})

describe('sessionExecutionOptions 快照存取（v2 整包）', () => {
  it('save/load 往返一致（values 区 + executionContext 区）', () => {
    const snapshot = {
      values: {
        workspace: 'D:/proj/demo',
        workspace_mode: 'worktree',
        isolation_mode: 'isolated',
        conversation_mode: 'plan',
      },
      executionContext: {
        workspace: { source_path: 'D:/proj/demo', mode: 'worktree' },
        isolation: { level: 'isolated' },
      },
    }
    saveSessionExecutionOptions('th-1', snapshot)
    expect(loadSessionExecutionOptions('th-1')).toEqual(snapshot)
  })

  it('仅 values 区（无执行上下文声明）也是合法快照', () => {
    saveSessionExecutionOptions('th-a', { values: { conversation_mode: 'plan' } })
    const loaded = loadSessionExecutionOptions('th-a')
    expect(loaded?.values).toEqual({ conversation_mode: 'plan' })
    expect(loaded?.executionContext).toBeUndefined()
  })

  it('不同会话键互不串扰；clear 后读回 null', () => {
    saveSessionExecutionOptions('th-a', { values: { x: '1' } })
    saveSessionExecutionOptions('th-b', { values: { y: '2' } })
    expect(loadSessionExecutionOptions('th-a')?.values.x).toBe('1')
    expect(loadSessionExecutionOptions('th-b')?.values.y).toBe('2')

    clearSessionExecutionOptions('th-a')
    expect(loadSessionExecutionOptions('th-a')).toBeNull()
    expect(loadSessionExecutionOptions('th-b')).not.toBeNull()
  })

  it('损坏 JSON / 缺 values 区 / 数组形状均按无记录处理（不抛错、返回 null）', () => {
    localStorage.setItem('session-exec-options:th-x', '{broken json')
    expect(() => loadSessionExecutionOptions('th-x')).not.toThrow()
    expect(loadSessionExecutionOptions('th-x')).toBeNull()

    localStorage.setItem('session-exec-options:th-y', '{"foo": 1}')
    expect(loadSessionExecutionOptions('th-y')).toBeNull()

    localStorage.setItem('session-exec-options:th-z', '[1,2,3]')
    expect(loadSessionExecutionOptions('th-z')).toBeNull()
  })

  it('损坏快照改名 .corrupt 留证后回退（键名+原文摘要 warn，原键移除，不再无声覆写）', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    try {
      // 解析失败（坏 JSON）
      localStorage.setItem('session-exec-options:th-bad', '{broken json')
      expect(loadSessionExecutionOptions('th-bad')).toBeNull()
      // 留证：原键移除，.corrupt 副本保留原文供取证
      expect(localStorage.getItem('session-exec-options:th-bad')).toBeNull()
      expect(localStorage.getItem('session-exec-options:th-bad.corrupt')).toBe('{broken json')
      // warn 带键名与原文摘要
      const warnText = warnSpy.mock.calls.map((c) => c.join(' ')).join('\n')
      expect(warnText).toContain('session-exec-options:th-bad')
      expect(warnText).toContain('{broken json')

      // 形状不符（解析成功但缺 values 区）同样留证回退
      localStorage.setItem('session-exec-options:th-shape', '{"foo": 1}')
      expect(loadSessionExecutionOptions('th-shape')).toBeNull()
      expect(localStorage.getItem('session-exec-options:th-shape')).toBeNull()
      expect(localStorage.getItem('session-exec-options:th-shape.corrupt')).toBe('{"foo": 1}')

      // 正常快照读取不产生 .corrupt、不触发 warn
      saveSessionExecutionOptions('th-ok', { values: { a: '1' } })
      expect(loadSessionExecutionOptions('th-ok')?.values.a).toBe('1')
      expect(localStorage.getItem('session-exec-options:th-ok.corrupt')).toBeNull()
      expect(warnSpy).toHaveBeenCalledTimes(2)
    } finally {
      warnSpy.mockRestore()
    }
  })
})
