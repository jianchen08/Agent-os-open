// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/** isPluginConfigConflict 兼容谓词：按 error.name 判定 */
import { describe, expect, it } from 'vitest'
import { isPluginConfigConflict, PluginConfigConflictError } from '@/services/api/pluginConfig'

describe('isPluginConfigConflict', () => {
  it('冲突错误实例 → true；普通 Error/非错误 → false', () => {
    const conflict = new PluginConfigConflictError('etag 冲突', 'W/1')
    expect(isPluginConfigConflict(conflict)).toBe(true)
    if (isPluginConfigConflict(conflict)) {
      expect(conflict.currentEtag).toBe('W/1')
    }
    expect(isPluginConfigConflict(new Error('普通错'))).toBe(false)
    expect(isPluginConfigConflict('str')).toBe(false)
  })
})
