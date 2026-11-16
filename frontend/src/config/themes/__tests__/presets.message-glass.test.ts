// @feature FP-0.2.四 前端主题 | @ci: frontend-test
/**
 * 预设主题消息气泡毛玻璃开关契约（effects.messageGlass）
 *
 * 主题级开关必须显式表态（缺省开只兜底皮肤/旧数据，不允许内置预设静默
 * 漏声明）；开/关取值是各主题的风格决策（记录于 E轨迹台账 R36），关档
 * 主题（纯浅实色/像素/无障碍）被误翻开会破坏其风格契约，故取值同锁。
 */

import { describe, it, expect } from 'vitest'
import { presetThemes } from '@/config/themes'

describe('预设主题 messageGlass 开关契约', () => {
  it('每个内置预设显式声明 effects.messageGlass 且为 boolean', () => {
    for (const theme of Object.values(presetThemes)) {
      expect(theme.effects.messageGlass, `${theme.id} 缺 messageGlass 声明`).toBeTypeOf('boolean')
    }
  })

  it('按风格定值：纯浅实色/像素/无障碍关，深色/层次背景开', () => {
    const offIds = new Set(['light', 'pixel-art', 'high-contrast'])
    for (const theme of Object.values(presetThemes)) {
      if (offIds.has(theme.id)) {
        expect(theme.effects.messageGlass, `${theme.id} 风格应为关`).toBe(false)
      } else {
        expect(theme.effects.messageGlass, `${theme.id} 风格应为开`).toBe(true)
      }
    }
  })
})
