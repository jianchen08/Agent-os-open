// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/** typeMatches 非法 type 声明宽容：非 string/非 array 的 type 视为通过（宽松镜像） */
import { describe, expect, it } from 'vitest'
import { validateOutputSubset } from '@/utils/outputSchemaView'

describe('validateOutputSubset - 病态 type 声明', () => {
  it('type 为对象等非法形态 → 宽容通过（不产违规）', () => {
    const schema = {
      type: 'object',
      properties: { weird: { type: { complex: true } } },
    }
    expect(validateOutputSubset(schema as never, { weird: 'anything' })).toEqual([])
  })
})
