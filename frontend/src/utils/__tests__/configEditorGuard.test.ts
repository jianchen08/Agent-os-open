/** @feature FP-兜底反模式修复.FE1/FE2 配置编辑器禁存守卫 @ci frontend-test */
/**
 * shouldDisableConfigSave：配置编辑器「加载失败 = 只读/禁存」共用守卫。
 *
 * 约束来源：加载失败后的空态一旦可保存，会把空对象覆盖写回
 * autonomous.yaml / 插件配置文件（兜底反模式全库审查 FE1/FE2）。
 */
import { describe, expect, it } from 'vitest'
import { shouldDisableConfigSave } from '../configEditorGuard'

describe('shouldDisableConfigSave', () => {
  it('正常态（已加载、无错误、非保存中）可保存', () => {
    expect(shouldDisableConfigSave(false, null, { a: 1 })).toBe(false)
  })

  it('保存中禁存', () => {
    expect(shouldDisableConfigSave(true, null, { a: 1 })).toBe(true)
  })

  it('加载失败禁存（即使 config 仍有旧值）', () => {
    expect(shouldDisableConfigSave(false, '无法加载配置', { a: 1 })).toBe(true)
  })

  it('配置未加载（null）禁存——加载失败落空态不可保存', () => {
    expect(shouldDisableConfigSave(false, null, null)).toBe(true)
  })
})
