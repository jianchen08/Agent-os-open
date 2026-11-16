/** @feature FP-T12 前端槽位扩展机制 | @ci: frontend-test */
/**
 * 槽 key 词表测试（设计真值 = docs/working/DSH生态嫁接通用机制设计_20261104.md §4.4）
 *
 * 契约：
 * - 词表按核心布局实际锚点声明：首批开放 sidebar.block(list)/workspace.tab(keyed)/
 *   shell.overlay(list)/chat.toolview(keyed)
 * - 预留键存在但不开放（titlebar.action 预留、statusbar.item 悬置——区已移除）
 * - isSlotKeyOpen 表外/未开放返回 false（fail-closed 依据）
 */

import { describe, expect, it } from 'vitest'
import {
  SLOT_VOCABULARY,
  isSlotKeyOpen,
  slotKindOf,
} from '../slotVocabulary'

describe('槽 key 词表', () => {
  it('首批开放四个锚点域：sidebar.block/workspace.tab/shell.overlay/chat.toolview', () => {
    expect(isSlotKeyOpen('sidebar.block')).toBe(true)
    expect(isSlotKeyOpen('workspace.tab')).toBe(true)
    expect(isSlotKeyOpen('shell.overlay')).toBe(true)
    expect(isSlotKeyOpen('chat.toolview')).toBe(true)
  })

  it('kind 语义：sidebar.block=list、workspace.tab=keyed、shell.overlay=list、chat.toolview=keyed', () => {
    expect(slotKindOf('sidebar.block')).toBe('list')
    expect(slotKindOf('workspace.tab')).toBe('keyed')
    expect(slotKindOf('shell.overlay')).toBe('list')
    expect(slotKindOf('chat.toolview')).toBe('keyed')
  })

  it('预留键在词表内但不开放（fail-closed：表外不可挂声明）', () => {
    // statusbar.item：区已移除（FiveSpaceLayout StatusBar removed），词表预留不开放
    expect(SLOT_VOCABULARY['statusbar.item']).toBeDefined()
    expect(isSlotKeyOpen('statusbar.item')).toBe(false)
    // titlebar.action / chat.composer.extension / chat.turnTail.action：P2/预留批次
    expect(isSlotKeyOpen('titlebar.action')).toBe(false)
    expect(isSlotKeyOpen('chat.composer.extension')).toBe(false)
    expect(isSlotKeyOpen('chat.turnTail.action')).toBe(false)
  })

  it('表外槽 key：isSlotKeyOpen=false 且 slotKindOf=undefined', () => {
    expect(isSlotKeyOpen('statusbar.item')).toBe(false)
    expect(isSlotKeyOpen('made.up.slot')).toBe(false)
    expect(slotKindOf('made.up.slot')).toBeUndefined()
  })
})
