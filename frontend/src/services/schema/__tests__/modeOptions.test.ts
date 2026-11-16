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
import {
  isModeIdShape,
  normalizeModeTags,
  primaryModeTag,
  withModeBinding,
  withTaskMode,
} from '@/services/schema/modeOptions'

/** 测试内空数组字面量便捷（绕开 no-unused 边角） */
function normalizeTagsSafe(v: unknown): string[] {
  return normalizeModeTags(v)
}

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
    expect(merged).toEqual({ workspace: { mode: 'isolated' }, mode: ['coding'] })
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
      mode: ['writing'],
      isolation: { level: 'high' },
    })
  })
})

describe('taskModeOptionsFromModes — 选择器选项 registry 派生（对账闸升级承接）', () => {
  /** 合成 registry（多模式态）：description 单句/多句/未声明各一 */
  const multiRegistry: ModesRegistryResponse = {
    modes: [
      {
        mode: 'roleplay',
        name: '角色扮演模式',
        description: '卡驱动的沉浸式对话（开演/附身/世界书）。对话链模式：main 即执行者不派发任务',
        pipelines: [{ name: 'roleplay', context: 'conversation' }],
        presenter: { source: 'data_cards' },
        tool_card: 'collapse',
        icon: '🎭',
        theme: null,
        persona: { replace: true, from: 'roleplay_persona' },
        plugin_id: 'mode_roleplay',
      },
      {
        mode: 'writing',
        name: '写作模式',
        description: '长文创作与连载',
        pipelines: [],
        presenter: { source: 'none' },
        tool_card: 'native',
        icon: '✍️',
        plugin_id: 'mode_writing',
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
    total: 3,
    errors: [],
  }

  it('多模式态：逐模式派生 label=decl.name、value=decl.mode、icon 随声明、description=声明首句', () => {
    const options = taskModeOptionsFromModes(multiRegistry)
    // 性质断言：值集 ≡ registry mode 键集（派生无损，无领先/滞后）
    expect(options.map((o) => o.value)).toEqual(['roleplay', 'writing', 'godot'])
    // 多句声明只取首句域定义进菜单——路由知识（直派/编排链要点）不进选择菜单
    expect(options[0]).toEqual({
      label: '角色扮演模式',
      value: 'roleplay',
      icon: '🎭',
      description: '卡驱动的沉浸式对话（开演/附身/世界书）',
    })
    // 单句无句号声明 → 全文原样透传
    expect(options[1]).toEqual({
      label: '写作模式',
      value: 'writing',
      icon: '✍️',
      description: '长文创作与连载',
    })
    // 未声明 icon/description → 键缺省（不给选项挂空串）
    expect(options[2]).toEqual({ label: 'Godot 模式', value: 'godot' })
    expect(options[2]).not.toHaveProperty('icon')
    expect(options[2]).not.toHaveProperty('description')
  })

  it('空态：registry 未就绪/空 modes → 空数组（选择器只剩声明面「默认」档，不阻断）', () => {
    expect(taskModeOptionsFromModes(undefined)).toEqual([])
    expect(
      taskModeOptionsFromModes({ modes: [], total: 0, errors: [] }),
    ).toEqual([])
  })

  // R10③ 补充裁定（2026-10-14）：选择面选项按声明过滤——默认模式（兜底无需选）
  // 与后台型模式（自进化等）显式声明 foreground_selectable: false 不进选项；
  // 字段缺省 = 可选（向后兼容既有声明），显式声明才隐藏。
  it('声明过滤：foreground_selectable:false 不进选项，缺省/true 照常出现', () => {
    const registry: ModesRegistryResponse = {
      modes: [
        { mode: 'personal', name: '个人助理模式', pipelines: [], presenter: { source: 'none' }, tool_card: 'native', default: true, foreground_selectable: false, plugin_id: 'mode_personal' },
        { mode: 'evolution', name: '自进化模式', pipelines: [], presenter: { source: 'none' }, tool_card: 'native', foreground_selectable: false, plugin_id: 'mode_evolution' },
        { mode: 'writing', name: '写作模式', pipelines: [], presenter: { source: 'none' }, tool_card: 'native', foreground_selectable: true, plugin_id: 'mode_writing' },
        { mode: 'godot', name: 'Godot 模式', pipelines: [], presenter: { source: 'none' }, tool_card: 'native', plugin_id: 'mode_godot' },
      ],
      total: 4,
      errors: [],
    }
    const options = taskModeOptionsFromModes(registry)
    // 性质：隐藏集与可选集互斥且完备——缺省与显式 true 都出现，仅显式 false 隐藏
    expect(options.map((o) => o.value)).toEqual(['writing', 'godot'])
    expect(options.map((o) => o.value)).not.toContain('personal')
    expect(options.map((o) => o.value)).not.toContain('evolution')
  })

  it('声明过滤对空态与全隐藏态不产生空选项对象', () => {
    const allHidden: ModesRegistryResponse = {
      modes: [
        { mode: 'a', name: 'A', pipelines: [], presenter: { source: 'none' }, tool_card: 'native', foreground_selectable: false, plugin_id: 'mode_a' },
      ],
      total: 1,
      errors: [],
    }
    expect(taskModeOptionsFromModes(allHidden)).toEqual([])
  })
})

describe('normalizeModeTags / primaryModeTag — 标签集归一（唯一兼容面）', () => {
  it('string = 旧形态兼容：单元素集；空串 → 空集', () => {
    expect(normalizeModeTags('coding')).toEqual(['coding'])
    expect(normalizeModeTags('  coding  ')).toEqual(['coding'])
    expect(normalizeModeTags('')).toEqual([])
  })

  it('数组：过滤非字符串、剥空白、保序去重', () => {
    expect(normalizeModeTags(['coding', 'coding', ' writing ', 42, null, ''])).toEqual([
      'coding',
      'writing',
    ])
    expect(normalizeTagsSafe([])).toEqual([])
  })

  it('其他形态 → 空集；主标签 = 末元素，空集 = 空串', () => {
    expect(normalizeModeTags(42)).toEqual([])
    expect(normalizeModeTags(null)).toEqual([])
    expect(primaryModeTag(['writing', 'coding'])).toBe('coding')
    expect(primaryModeTag('personal')).toBe('personal')
    expect(primaryModeTag([])).toBe('')
    expect(primaryModeTag(undefined)).toBe('')
  })
})
