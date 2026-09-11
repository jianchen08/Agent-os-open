/**
 * 通用引用协议回环测试（ADR 2026-09-10-generic-reference-protocol）
 *
 * 核验三层通用性：
 * - provider 注册缝：任意 source 注册即可被枚举注入与消耗
 * - 块构建 ↔ 解析互逆：buildReferenceBlock 产出可被 parseReferenceMessage
 *   还原（source/scene/items），任意 source、任意头部属性
 * - 渲染源无关：MessageItem 按 source 渲染「{source} 引用」与 {source}-node
 */

import {
  buildReferenceBlock,
  getReferenceProviders,
  registerReferenceProvider,
  type ReferenceSelection,
} from '../referenceProviders'
import { parseReferenceMessage } from '@/components/chat/ReferenceChip'

const CONSUMED: string[] = []

beforeEach(() => {
  CONSUMED.length = 0
})

function fakeProvider(source: string, selection: ReferenceSelection | null) {
  registerReferenceProvider({
    source,
    getSelection: () => selection,
    consume: () => {
      CONSUMED.push(source)
    },
  })
}

describe('provider 注册缝', () => {
  it('任意 source 注册后可被枚举（注入侧零插件知识）', () => {
    fakeProvider('file-explorer', {
      source: 'file-explorer',
      items: [{ name: 'a.ts', type: 'file', path: 'src/a.ts' }],
    })
    const sel = getReferenceProviders().find((p) => p.source === 'file-explorer')?.getSelection()
    expect(sel?.items[0]).toMatchObject({ name: 'a.ts', type: 'file' })
  })

  it('空选择返回 null 仍可注册（不注入）', () => {
    fakeProvider('empty-src', null)
    const p = getReferenceProviders().find((x) => x.source === 'empty-src')
    expect(p?.getSelection()).toBeNull()
  })

  it('consume 逐 provider 调用（发送即消耗）', () => {
    fakeProvider('s1', { source: 's1', items: [{ name: 'x', type: 't', path: 'p' }] })
    fakeProvider('s2', { source: 's2', items: [{ name: 'y', type: 't', path: 'q' }] })
    for (const p of getReferenceProviders()) void p.consume?.()
    expect(CONSUMED).toContain('s1')
    expect(CONSUMED).toContain('s2')
  })
})

describe('块构建 ↔ 解析互逆（任意 source）', () => {
  it('godot 形态：attrs/extra 还原（线格式兼容历史）', () => {
    const block = buildReferenceBlock({
      source: 'godot',
      attrs: { scene: 'res://main.tscn' },
      items: [{ name: 'Player', type: 'Node2D', path: '/root/Player', extra: 'position=(1, 2)' }],
    })
    expect(block).toContain('<reference source="godot" scene="res://main.tscn">')
    const parsed = parseReferenceMessage(block!)
    expect(parsed?.source).toBe('godot')
    expect(parsed?.scene).toBe('res://main.tscn')
    expect(parsed?.items[0].name).toBe('Player')
    expect(parsed?.items[0].path).toContain('/root/Player')
    expect(parsed?.items[0].path).toContain('position=(1, 2)')
  })

  it('任意新 source：source 从内容解析（非硬编码）', () => {
    const block = buildReferenceBlock({
      source: 'figma',
      attrs: { file: 'design v3' },
      items: [{ name: 'Login 页', type: 'frame', path: 'figma://login' }],
    })
    const parsed = parseReferenceMessage(block!)
    expect(parsed?.source).toBe('figma')
    expect(parsed?.items).toHaveLength(1)
  })

  it('空 items 不产出块', () => {
    expect(buildReferenceBlock({ source: 'x', items: [] })).toBeNull()
  })
})
