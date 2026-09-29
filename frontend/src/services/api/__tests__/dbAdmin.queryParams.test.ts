/** @feature FP-T12 前端适配(小件缺口补测) | @ci: frontend-test */
/**
 * dbAdmin 查询参数序列化——导出面 docstring 自述「供单元测试断言序列化行为
 * （回归测试防 DEF-2 复发）」，本文件补齐该断言面。
 */
import { describe, expect, it } from 'vitest'
import { serializeDbQueryParams } from '@/services/api/dbAdmin'

describe('serializeDbQueryParams — 查询串序列化', () => {
  it('全量参数形态（limit/offset/filter 多值/sort）', () => {
    expect(
      serializeDbQueryParams({ limit: 10, offset: 5, filter: ['a=1', 'b=2'], sort: 'id desc' }),
    ).toBe('limit=10&offset=5&filter=a%3D1&filter=b%3D2&sort=id+desc')
  })

  it('空对象与单键：零参数零追加，不发明默认值（性质断言）', () => {
    expect(serializeDbQueryParams({})).toBe('')
    expect(serializeDbQueryParams({ limit: 1 })).toBe('limit=1')
    // filter 非数组形态（null/undefined）不追加键
    expect(serializeDbQueryParams({ filter: null as unknown as string[] })).toBe('')
  })
})
