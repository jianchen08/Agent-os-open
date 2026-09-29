/** @feature FP-T12 前端适配(管道模型数组操作) | @ci: frontend-test */
/**
 * pipeline/model 数组不可变操作——deleteAtPath/insertAtPath/moveArrayItem
 * 的边界与性质：原对象不动（不可变性）、index 钳制、越界/非数组早退。
 */
import { describe, expect, it } from 'vitest'
import { deleteAtPath, insertAtPath, moveArrayItem, setAtPath } from '../model'

describe('deleteAtPath — 不可变删除', () => {
  it('数组按索引删除 + 对象按键删除（双形态），原对象不动', () => {
    const root = { list: [1, 2, 3], meta: { keep: 1, drop: 'x' } }
    const r1 = deleteAtPath(root, ['list', 1])
    expect(r1.list).toEqual([1, 3])
    expect(root.list).toEqual([1, 2, 3])
    const r2 = deleteAtPath(r1, ['meta', 'drop'])
    expect(r2.meta).toEqual({ keep: 1 })
  })

  it('中途非对象路径原样返回；数组越界/非整数索引不动作', () => {
    const root = { a: null, list: [1] }
    expect(deleteAtPath(root, ['a', 'b', 'c'])).toEqual(root)
    expect(deleteAtPath(root, ['list', 9]).list).toEqual([1])
    expect(deleteAtPath(root, ['list', 'x']).list).toEqual([1])
  })
})

describe('insertAtPath — 不可变插入（index 钳制）', () => {
  it('命中数组按位插入（负/超界钳制到两端），原数组不动', () => {
    const root = { list: ['a', 'c'] }
    expect(insertAtPath(root, ['list'], 1, 'b').list).toEqual(['a', 'b', 'c'])
    expect(insertAtPath(root, ['list'], -5, 'z').list).toEqual(['z', 'a', 'c'])
    expect(insertAtPath(root, ['list'], 99, 'z').list).toEqual(['a', 'c', 'z'])
    expect(root.list).toEqual(['a', 'c'])
  })

  it('目标非数组 → 以新值建单元素数组（setAtPath 语义兜底）', () => {
    expect(insertAtPath({ x: 1 }, ['x'], 0, 'v').x).toEqual(['v'])
  })
})

describe('moveArrayItem — 不可变移动（-1/+1）', () => {
  it('合法移动换位且原对象不动；非法（越界/非数组/出界目标）原样返回', () => {
    const root = { list: ['a', 'b', 'c'] }
    expect(moveArrayItem(root, ['list'], 0, 1).list).toEqual(['b', 'a', 'c'])
    expect(moveArrayItem(root, ['list'], 2, -1).list).toEqual(['a', 'c', 'b'])
    expect(root.list).toEqual(['a', 'b', 'c'])
    expect(moveArrayItem(root, ['list'], 0, -1)).toEqual(root)
    expect(moveArrayItem(root, ['list'], 2, 1)).toEqual(root)
    expect(moveArrayItem(root, ['list'], 9, 1)).toEqual(root)
    expect(moveArrayItem({ x: 1 }, ['x'], 0, 1)).toEqual({ x: 1 })
  })
})

describe('深路径走穿（多段/中途缺失形态）', () => {
  const deep = { a: { b: { list: [1, 2, 3] } } }

  it('三段路径的删/插/移全操作，原树不动', () => {
    const path = ['a', 'b', 'list'] as const
    expect(deleteAtPath(deep, [...path, 0]).a.b.list).toEqual([2, 3])
    expect(insertAtPath(deep, [...path], 1, 9).a.b.list).toEqual([1, 9, 2, 3])
    expect(moveArrayItem(deep, [...path], 0, 1).a.b.list).toEqual([2, 1, 3])
    expect(deep.a.b.list).toEqual([1, 2, 3])
  })

  it('中途缺失段：getAtPath 归 undefined 走早退（删/移原样、插走 set 兜底建层）', () => {
    const broken = { a: null }
    expect(deleteAtPath(broken, ['a', 'b', 'list', 0])).toEqual(broken)
    expect(moveArrayItem(broken, ['a', 'b', 'list'], 0, 1)).toEqual(broken)
    expect(insertAtPath(broken, ['a', 'b', 'list'], 0, 'v')).toEqual({ a: { b: { list: ['v'] } } })
  })
})

describe('setAtPath — 深路径写入保真（回归锚）', () => {
  it('多级写入；中途缺失/非对象中间层创建空对象续写（docstring 契约）', () => {
    expect(setAtPath({ a: { b: 1 } }, ['a', 'b'], 2).a.b).toBe(2)
    expect(setAtPath({ a: null }, ['a', 'b'], 1)).toEqual({ a: { b: 1 } })
    expect(setAtPath({}, ['x', 'y'], 9)).toEqual({ x: { y: 9 } })
  })
})
