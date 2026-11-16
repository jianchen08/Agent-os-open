/** @feature FP-RP面板v3.1刀5 | @ci: frontend-test */
/**
 * parseCsv — CSV 解析测试（content_cards csv_table 数据面，RFC4180 简化）。
 * 同一行为 ≥2 组有区分度输入（正常/边界/引号形态相反）。
 */
import { describe, expect, it } from 'vitest'
import { parseCsv } from '../parseCsv'

describe('parseCsv — 基础形态', () => {
  it('普通逗号分隔：多行多列', () => {
    expect(parseCsv('a,b,c\n1,2,3')).toEqual([
      ['a', 'b', 'c'],
      ['1', '2', '3'],
    ])
  })

  it('单行无换行符：末行照常成行', () => {
    expect(parseCsv('a,b')).toEqual([['a', 'b']])
  })

  it('CRLF / CR / LF 三种行界归一', () => {
    expect(parseCsv('a,b\r\n1,2')).toEqual([
      ['a', 'b'],
      ['1', '2'],
    ])
    expect(parseCsv('a,b\r1,2')).toEqual([
      ['a', 'b'],
      ['1', '2'],
    ])
  })

  it('空输入 → 空数组；纯换行 → 单空字段行', () => {
    expect(parseCsv('')).toEqual([])
    expect(parseCsv('\n')).toEqual([['']])
  })
})

describe('parseCsv — 引号形态（RFC4180）', () => {
  it('引号包裹字段内逗号不切分', () => {
    expect(parseCsv('"a,b",c')).toEqual([['a,b', 'c']])
  })

  it('双引号转义："" → 字面引号', () => {
    expect(parseCsv('"他说""好"""，x'.replace('，', ','))).toEqual([['他说"好"', 'x']])
  })

  it('引号字段内换行不分行（两组：行中/行尾）', () => {
    expect(parseCsv('"l1\nl2",b')).toEqual([['l1\nl2', 'b']])
    expect(parseCsv('"x\n",y')).toEqual([['x\n', 'y']])
  })

  it('混合形态：引号列与裸列并存（符号量级相反组）', () => {
    expect(parseCsv('name,note\n"列1,列2",plain\n"q""q",x')).toEqual([
      ['name', 'note'],
      ['列1,列2', 'plain'],
      ['q"q', 'x'],
    ])
  })
})
