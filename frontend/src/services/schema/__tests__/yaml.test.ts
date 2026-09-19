// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/** parseYamlObject 容错：非法 yaml / 非对象顶层 → 空对象（表单按空值渲染） */
import { describe, expect, it } from 'vitest'
import { parseYamlObject } from '../yaml'

describe('parseYamlObject', () => {
  it('非法 yaml（未闭合括号）→ 空对象不抛错', () => {
    expect(parseYamlObject('a: [unclosed')).toEqual({})
  })

  it('顶层为数组 → 空对象（仅收对象形态）', () => {
    expect(parseYamlObject('- a\n- b')).toEqual({})
  })

  it('合法对象原样解析', () => {
    expect(parseYamlObject('a: 1\nb: x')).toEqual({ a: 1, b: 'x' })
  })
})
