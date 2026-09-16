/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * modeOptions — 任务模式键契约与发送链并入测试
 *
 * 核验（纯函数，无 registry 依赖）：
 * - TASK_MODES 契约值集合冻结（coding|writing|roleplay|research）
 * - withTaskMode：显式选择并入 mode 键；「自动」原样透传（含 undefined）
 *
 * 选项面（UI 聚合/渲染）不在本模块：任务模式选择器由 task_form 的 task_mode
 * form 声明 + 各模式插件 select-option 追加声明 + DeclaredWidgetLayer 合并
 * 渲染（见 ChatInput.taskModeSelector / DeclaredWidgetLayer 车道）。
 */

import { describe, expect, it } from 'vitest'
import {
  TASK_MODES,
  withTaskMode,
} from '@/services/schema/modeOptions'

describe('TASK_MODES — 契约值集合冻结', () => {
  it('四键且仅四键（godot 等家族模式键不入任务模式契约）', () => {
    expect([...TASK_MODES]).toEqual(['coding', 'writing', 'roleplay', 'research'])
  })
})

describe('withTaskMode — mode 键并入 execution_context', () => {
  it('显式选择 → 浅合并出带 mode 键的新 context（不改入参对象）', () => {
    const session: Record<string, unknown> = { workspace: { mode: 'isolated' } }
    const merged = withTaskMode(session, 'coding')
    expect(merged).toEqual({ workspace: { mode: 'isolated' }, mode: 'coding' })
    expect(merged).not.toBe(session)
    expect(session).not.toHaveProperty('mode')
  })

  it('「自动」不带键 → 原 context 原样返回（含 undefined）', () => {
    const session: Record<string, unknown> = { mode: 'stale', isolation: { level: 'high' } }
    expect(withTaskMode(session, undefined)).toBe(session)
    expect(withTaskMode(undefined, undefined)).toBeUndefined()
  })

  it('显式选择覆盖 context 中既有顶层 mode 键（选择即生效，不留旧值）', () => {
    const session: Record<string, unknown> = { mode: 'stale', isolation: { level: 'high' } }
    expect(withTaskMode(session, 'writing')).toEqual({
      mode: 'writing',
      isolation: { level: 'high' },
    })
  })
})
