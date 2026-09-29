/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * modeOptions — 任务模式键契约与发送链并入测试
 *
 * 核验（纯函数，无 store 依赖）：
 * - mode 键 = 开放标签（D1 标签裁定）：isModeIdShape 形态守卫正负例；
 * - withTaskMode：显式选择并入 mode 键；「默认」原样透传（含 undefined）；
 * - 选择器选项构建函数从 registry 派生（对账闸升级承接：合成 registry
 *   多模式/空两态，断言派生无损——冻结集退役后选项不可能领先/滞后 registry）。
 *
 * 选项面（UI 渲染）：任务模式选择器由 task_form 的 task_mode form 声明 +
 * registry 派生选项经宿主桥注入（见 ChatInput.taskModeSelector 车道）。
 */

import { describe, expect, it } from 'vitest'
import { taskModeOptionsFromModes, type ModesRegistryResponse } from '@/services/api/modes'
import { isModeIdShape, withTaskMode } from '@/services/schema/modeOptions'

describe('isModeIdShape — mode 键形态守卫（与后端 MODE_ID_RE 同口径）', () => {
  it('合法形态：小写字母起头 + 小写字母/数字/下划线，≤64 字符', () => {
    expect(isModeIdShape('coding')).toBe(true)
    expect(isModeIdShape('roleplay')).toBe(true)
    expect(isModeIdShape('mode2')).toBe(true)
    expect(isModeIdShape('a'.repeat(64))).toBe(true)
  })

  it('非法形态：大写/连字符起头非法/超长/非字符串', () => {
    expect(isModeIdShape('Coding')).toBe(false)
    expect(isModeIdShape('2mode')).toBe(false)
    expect(isModeIdShape('mode-x')).toBe(false)
    expect(isModeIdShape('a'.repeat(65))).toBe(false)
    expect(isModeIdShape('')).toBe(false)
    expect(isModeIdShape(42)).toBe(false)
    expect(isModeIdShape(null)).toBe(false)
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

  it('「默认」不带键 → 原 context 原样返回（含 undefined）', () => {
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

describe('taskModeOptionsFromModes — 选择器选项 registry 派生（对账闸升级承接）', () => {
  /** 合成 registry（多模式态）：icon/description 声明有无各半 */
  const multiRegistry: ModesRegistryResponse = {
    modes: [
      {
        mode: 'roleplay',
        name: '角色扮演模式',
        description: '卡驱动的沉浸式对话',
        pipelines: [{ name: 'roleplay', context: 'conversation' }],
        presenter: { source: 'data_cards' },
        tool_card: 'collapse',
        icon: '🎭',
        theme: null,
        persona: { replace: true, from: 'roleplay_persona' },
        plugin_id: 'mode_roleplay',
      },
      {
        mode: 'godot',
        name: 'Godot 模式',
        pipelines: [],
        presenter: { source: 'none' },
        tool_card: 'native',
        icon: null,
        plugin_id: 'mode_godot',
      },
    ],
    total: 2,
    errors: [],
  }

  it('多模式态：逐模式派生 label=decl.name、value=decl.mode、icon/description 随声明', () => {
    const options = taskModeOptionsFromModes(multiRegistry)
    // 性质断言：值集 ≡ registry mode 键集（派生无损，无领先/滞后）
    expect(options.map((o) => o.value)).toEqual(['roleplay', 'godot'])
    expect(options[0]).toEqual({
      label: '角色扮演模式',
      value: 'roleplay',
      icon: '🎭',
      description: '卡驱动的沉浸式对话',
    })
    // 未声明 icon/description → 键缺省（不给选项挂空串）
    expect(options[1]).toEqual({ label: 'Godot 模式', value: 'godot' })
    expect(options[1]).not.toHaveProperty('icon')
    expect(options[1]).not.toHaveProperty('description')
  })

  it('空态：registry 未就绪/空 modes → 空数组（选择器只剩声明面「默认」档，不阻断）', () => {
    expect(taskModeOptionsFromModes(undefined)).toEqual([])
    expect(
      taskModeOptionsFromModes({ modes: [], total: 0, errors: [] }),
    ).toEqual([])
  })
})
