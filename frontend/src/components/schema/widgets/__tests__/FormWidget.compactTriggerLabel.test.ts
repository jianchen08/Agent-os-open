/**
 * 触发器文案派生单测：compactTriggerLabel（窄屏 ≤2 字收口）+ triggerVisibleLabel
 * （R10③ 桌面去括注——用户原话"显示的描述太长了"）
 *
 * 契约（FormWidget.compactTriggerLabel）：去括注 → 去「模式」尾缀 → 仍超
 * 2 字取末 2 字；否定前缀「免」的 3 字词保留首 2 字（语义不反转）。
 * 契约（FormWidget.triggerVisibleLabel）：仅去括注留裸档名。
 * 全称承载在菜单项/可访问名，两者只服务触发器可见文案。
 */
import { compactTriggerLabel, triggerVisibleLabel } from '@/components/schema/widgets/FormWidget'

describe('compactTriggerLabel 窄屏收口文案', () => {
  it.each([
    ['默认（命中规则才确认）', '默认'], // 去括注
    ['接受编辑（文件自动）', '编辑'], // 去括注后仍超 2 字取末 2
    ['个人助手', '助手'], // 超 2 字取末 2
    ['个人助理模式', '助理'], // 去「模式」尾缀后再取末 2
    ['编码模式', '编码'], // 去「模式」尾缀后恰 2 字
    ['免审批（隔离默认）', '免审'], // 否定前缀不反转（截成「审批」= 语义取反）
    ['旁路（跳过审批）', '旁路'], // 去括注后恰 2 字
    ['低', '低'], // 单字原样
    ['高', '高'],
    ['', ''], // 空串零崩溃
  ])('%s → %s', (input, expected) => {
    expect(compactTriggerLabel(input)).toBe(expected)
  })

  it('性质：任意输入产物不超过 2 字', () => {
    const samples = [
      '默认（命中规则才确认）',
      '个人助理模式',
      'Godot 游戏开发模式',
      '免审批',
      'x'.repeat(50),
      '自进化模式',
    ]
    for (const s of samples) {
      expect(compactTriggerLabel(s).length).toBeLessThanOrEqual(2)
    }
  })
})

describe('triggerVisibleLabel 桌面触发器去括注（R10③）', () => {
  it.each([
    ['默认（命中规则才确认）', '默认'],
    ['接受编辑（文件自动）', '接受编辑'], // 括注去掉，主体保留（非窄屏截断）
    ['自动（block 不打扰）', '自动'],
    ['旁路（跳过审批）', '旁路'],
    ['免审批（隔离默认）', '免审批'],
    ['中', '中'], // 无括注原样
    ['', ''], // 空串零崩溃
  ])('%s → %s', (input, expected) => {
    expect(triggerVisibleLabel(input)).toBe(expected)
  })

  it('全括注标签回退原文（不产生空可见文案）', () => {
    expect(triggerVisibleLabel('（仅括注）')).toBe('（仅括注）')
  })

  it('性质：去括注产物长度 ≤ 原文长度（只减不增）', () => {
    const samples = [
      '默认（命中规则才确认）',
      '免审批（隔离默认）',
      '个人助理模式',
      'plain',
      '',
    ]
    for (const s of samples) {
      expect(triggerVisibleLabel(s).length).toBeLessThanOrEqual(s.length)
    }
  })
})
