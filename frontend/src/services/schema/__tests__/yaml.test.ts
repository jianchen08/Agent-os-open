// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/** parseYamlObject 容错：非法 yaml / 非对象顶层 → 空对象（表单按空值渲染）；
 * parseYamlStrict fail-closed：契约单源文件解析失败必须抛错（ADR 2026-10-10） */
import { describe, expect, it } from 'vitest'
import { parseYamlObject, parseYamlStrict } from '../yaml'

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

describe('parseYamlStrict（fail-closed 严格版）', () => {
  it('合法文档返回解析值（对象/数组两形）', () => {
    expect(parseYamlStrict('a: 1')).toEqual({ a: 1 })
    expect(parseYamlStrict('- x')).toEqual(['x'])
  })

  it('非法 yaml 语法 → 抛错（不吞异常）', () => {
    expect(() => parseYamlStrict('a: [unclosed')).toThrow()
  })

  it('空文档 / null 文档 → 抛错（契约文件不得为空）', () => {
    expect(() => parseYamlStrict('')).toThrow(/不得为空/)
    expect(() => parseYamlStrict('~')).toThrow(/不得为空/)
  })
})
