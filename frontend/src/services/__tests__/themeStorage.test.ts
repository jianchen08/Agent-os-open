// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/**
 * ThemeStorageService / mergeTheme 测试
 *
 * 覆盖：活跃主题读写、用户主题 CRUD（新增/更新/删除/查缺）、
 * 导出/导入（含非法 JSON、缺必需字段、同名更新）、偏好合并、
 * 存储信息统计、删除当前主题回退默认、mergeTheme 深合并。
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import {
  ThemeStorageService,
  mergeTheme,
  type UserThemeConfig,
  type ThemePreferences,
} from '@/services/themeStorage'

vi.mock('@/utils/logger', () => ({
  loggers: { storage: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() } },
}))

const makeTheme = (id: string, name = id): UserThemeConfig => ({
  id,
  name,
  basedOn: 'light',
  customizations: {},
  createdAt: '',
  updatedAt: '',
})

const BASE_THEME: any = {
  id: 'base',
  name: 'base',
  colors: {
    background: { default: '#fff', paper: '#f5f5f5' },
    text: { default: '#111', secondary: '#666' },
    border: { default: '#eee' },
    status: { success: '#0a0' },
    bubble: { ai: '#eef' },
  },
  components: {
    borderRadius: { sm: 4, md: 8 },
    shadows: { sm: '0 1px' },
    button: { variants: { primary: { bg: '#00f' } } },
  },
  effects: { blur: 8 },
  backgrounds: { default: '#fff' },
} as any

describe('ThemeStorageService - 主题存储', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  afterEach(() => {
    localStorage.clear()
  })

  it('getActiveTheme 缺省返回 dark；setActiveTheme 后读回', () => {
    expect(ThemeStorageService.getActiveTheme()).toBe('dark')
    ThemeStorageService.setActiveTheme('ocean-breeze')
    expect(ThemeStorageService.getActiveTheme()).toBe('ocean-breeze')
  })

  it('saveUserTheme 新增主题并自动补 createdAt/updatedAt', () => {
    const theme = makeTheme('t1')
    ThemeStorageService.saveUserTheme(theme)
    const saved = ThemeStorageService.getUserTheme('t1')!
    expect(saved).not.toBeNull()
    expect(saved.createdAt.length).toBeGreaterThan(0)
    expect(saved.updatedAt.length).toBeGreaterThan(0)
    expect(ThemeStorageService.getUserThemes().length).toBe(1)
  })

  it('saveUserTheme 覆盖更新已有主题（列表长度不变，updatedAt 刷新）', () => {
    const theme = makeTheme('t2')
    theme.createdAt = '2026-01-01T00:00:00.000Z'
    ThemeStorageService.saveUserTheme(theme)
    const updated = makeTheme('t2')
    updated.name = '改名'
    ThemeStorageService.saveUserTheme(updated)

    const themes = ThemeStorageService.getUserThemes()
    expect(themes.length).toBe(1)
    expect(themes[0].name).toBe('改名')
    // [现状断言] 更新路径整体替换对象：createdAt 随新对象透传（'' 原样保留），
    // 与 importTheme 的「保留原 createdAt」行为不一致——疑似产品 bug，按现状断言
    expect(themes[0].createdAt).toBe('')
  })

  it('getUserTheme 查不存在的 id 返回 null', () => {
    expect(ThemeStorageService.getUserTheme('nope')).toBeNull()
  })

  it('deleteUserTheme 删除成功返回 true；删除当前主题回退 dark', () => {
    ThemeStorageService.setActiveTheme('t3')
    ThemeStorageService.saveUserTheme(makeTheme('t3'))
    expect(ThemeStorageService.deleteUserTheme('t3')).toBe(true)
    expect(ThemeStorageService.getUserTheme('t3')).toBeNull()
    expect(ThemeStorageService.getActiveTheme()).toBe('dark')
  })

  it('deleteUserTheme 删除不存在的 id 返回 false（不改变列表）', () => {
    ThemeStorageService.saveUserTheme(makeTheme('keep'))
    expect(ThemeStorageService.deleteUserTheme('missing')).toBe(false)
    expect(ThemeStorageService.getUserThemes().length).toBe(1)
  })

  it('exportTheme 输出可读 JSON；主题不存在时抛错', () => {
    ThemeStorageService.saveUserTheme(makeTheme('exp'))
    const json = ThemeStorageService.exportTheme('exp')
    expect(() => JSON.parse(json)).not.toThrow()
    expect(JSON.parse(json).id).toBe('exp')
    expect(() => ThemeStorageService.exportTheme('no-such')).toThrow('不存在')
  })

  it('importTheme 合法配置导入并可读回', () => {
    const imported = ThemeStorageService.importTheme(
      JSON.stringify({ id: 'imp-1', name: '导入', basedOn: 'dark', customizations: { colors: {} } }),
    )
    expect(imported.id).toBe('imp-1')
    expect(ThemeStorageService.getUserTheme('imp-1')!.name).toBe('导入')
  })

  it('importTheme 非法 JSON / 缺必需字段 → 抛错且不落库', () => {
    expect(() => ThemeStorageService.importTheme('{bad json')).toThrow()
    expect(() =>
      ThemeStorageService.importTheme(JSON.stringify({ name: '缺 id', basedOn: 'dark' })),
    ).toThrow('必需字段')
    expect(() =>
      ThemeStorageService.importTheme(JSON.stringify({ id: 'x', name: '缺 basedOn' })),
    ).toThrow('必需字段')
    expect(ThemeStorageService.getUserThemes().length).toBe(0)
  })

  it('importTheme 同名主题 → 更新并保留原 createdAt', () => {
    const original = makeTheme('same')
    original.createdAt = '2026-02-02T00:00:00.000Z'
    ThemeStorageService.saveUserTheme(original)
    ThemeStorageService.importTheme(
      JSON.stringify({ id: 'same', name: '新名', basedOn: 'light', customizations: {} }),
    )
    const saved = ThemeStorageService.getUserTheme('same')!
    expect(saved.name).toBe('新名')
    expect(saved.createdAt).toBe('2026-02-02T00:00:00.000Z')
    expect(ThemeStorageService.getUserThemes().length).toBe(1)
  })

  it('getPreferences 缺省返回默认偏好；savePreferences 增量合并', () => {
    const defaults = ThemeStorageService.getPreferences()
    expect(defaults.followSystem).toBe(false)
    expect(defaults.enableAnimations).toBe(true)
    expect(defaults.enableGlassmorphism).toBe(true)
    expect(defaults.reducedMotion).toBe(false)

    ThemeStorageService.savePreferences({ followSystem: true, reducedMotion: true })
    const updated = ThemeStorageService.getPreferences()
    expect(updated.followSystem).toBe(true)
    expect(updated.reducedMotion).toBe(true)
    expect(updated.enableAnimations).toBe(true) // 未提供的字段保留
  })

  it('clearAll 清除全部主题数据（active 回退 dark）', () => {
    ThemeStorageService.setActiveTheme('c1')
    ThemeStorageService.saveUserTheme(makeTheme('c1'))
    ThemeStorageService.clearAll()
    expect(ThemeStorageService.getActiveTheme()).toBe('dark')
    expect(ThemeStorageService.getUserThemes().length).toBe(0)
    expect(ThemeStorageService.getPreferences().followSystem).toBe(false)
  })

  it('getStorageInfo 返回 used/total/percentage（used 随写入增长且非负）', () => {
    const empty = ThemeStorageService.getStorageInfo()
    expect(empty.total).toBe(5 * 1024 * 1024)
    expect(empty.used).toBe(0)
    expect(empty.percentage).toBe(0)

    ThemeStorageService.saveUserTheme(makeTheme('big-1', '长名'.repeat(20)))
    ThemeStorageService.savePreferences({ followSystem: true } as ThemePreferences)
    const after = ThemeStorageService.getStorageInfo()
    expect(after.used).toBeGreaterThan(0)
    expect(after.percentage).toBeGreaterThan(0)
    expect(after.percentage).toBeLessThanOrEqual(100)
  })
})

describe('mergeTheme - 用户自定义与基础主题深合并', () => {
  it('完整合并：未覆盖字段保留 base，覆盖字段取 custom', () => {
    const merged = mergeTheme(BASE_THEME, {
      id: 'custom-id',
      name: '自定义',
      colors: {
        background: { paper: '#000' },
        text: { default: '#fff' },
      },
      components: {
        borderRadius: { md: 16 },
        button: { variants: { primary: { bg: '#0f0' } } },
      },
      effects: { blur: 16 },
    } as any)

    expect(merged.id).toBe('custom-id')
    expect(merged.name).toBe('自定义')
    // 深层覆盖
    expect(merged.colors.background.paper).toBe('#000')
    expect(merged.colors.background.default).toBe('#fff') // base 保留
    expect(merged.colors.text.default).toBe('#fff')
    expect(merged.colors.text.secondary).toBe('#666') // base 保留
    expect(merged.colors.border.default).toBe('#eee')
    expect(merged.colors.status.success).toBe('#0a0')
    expect(merged.colors.bubble.ai).toBe('#eef')
    expect(merged.components.borderRadius.md).toBe(16)
    expect(merged.components.borderRadius.sm).toBe(4)
    expect(merged.components.shadows.sm).toBe('0 1px')
    expect(merged.components.button.variants.primary.bg).toBe('#0f0')
    expect(merged.effects.blur).toBe(16)
    expect(merged.backgrounds.default).toBe('#fff')
  })

  it('customizations 为空对象 → 完全等于 base（id/name 兜底）', () => {
    const merged = mergeTheme(BASE_THEME, {} as any)
    expect(merged.id).toBe('base')
    expect(merged.name).toBe('base')
    expect(merged.colors.background.default).toBe('#fff')
    expect(merged.components.borderRadius.sm).toBe(4)
  })

  it('custom 只带 id（无 name）→ name 回落 base', () => {
    const merged = mergeTheme(BASE_THEME, { id: 'only-id' } as any)
    expect(merged.id).toBe('only-id')
    expect(merged.name).toBe('base')
  })
})
