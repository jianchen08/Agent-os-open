/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * ReviewDiff 覆盖缺口补测
 *
 * 覆盖契约（均以渲染文本/行号断言，不断言内部算法细节）：
 * - LCS diff：新增/删除/未变三类行在左右视图分别呈现，统计 +/- 计数
 * - 完全相同内容：两侧行都渲染且无增删计数
 * - 一侧为空：全部为新增（或全部为删除）
 * - 模式切换按钮：左右 ↔ 统一，统一视图合并两列行号（旧/新）
 * - 统一视图中只有一侧存在的行（删除行无新行号、新增行无旧行号）
 * - 内容为空字符串 → split('\n') 得单个空行参与 diff（非零行）
 * - 模式切换后仍保留计数
 *
 * 不可达说明（逐条）：
 * 1. `DiffLineList` 的 `lines.length === 0 → 无内容` 与 `UnifiedDiffView` 的
 *    `unified.length === 0 → 两个版本内容相同` 两个占位分支不可达：`split('\n')`
 *    对任意字符串（含空串）至少产出 1 行，回溯循环的 i>0 分支强制产出 removed、
 *    j>0 分支强制产出 added，故 oldLines/newLines 恒非空，unified 亦恒非空。
 *    两处均为防御性占位，保留待未来支持「零行」输入（如按行数组直接入参）。
 *
 * 测试策略：真实组件（纯展示 + LCS 内部实现），不 mock。
 */

import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ReviewDiff } from '@/components/review/ReviewDiff'

/** 左右视图下取某一列（旧版本 / 新版本）的行文本集合 */
function columnRows(label: string): string[] {
  const header = screen.getByText(label)
  const column = header.parentElement as HTMLElement
  return Array.from(column.querySelectorAll('.whitespace-pre-wrap')).map((n) => n.textContent ?? '')
}

describe('ReviewDiff — 左右对比', () => {
  it('新增与删除分别落到新版/旧版列，统计 +N/-M', () => {
    render(<ReviewDiff oldContent={'a\nold'} newContent={'a\nnew'} />)
    expect(columnRows('旧版本')).toEqual(['a', 'old'])
    expect(columnRows('新版本')).toEqual(['a', 'new'])
    expect(screen.getByText('+1')).toBeInTheDocument()
    expect(screen.getByText('-1')).toBeInTheDocument()
  })

  it('内容完全相同：无增删计数，两侧逐行一致', () => {
    render(<ReviewDiff oldContent={'same\nlines'} newContent={'same\nlines'} />)
    expect(screen.getByText('+0')).toBeInTheDocument()
    expect(screen.getByText('-0')).toBeInTheDocument()
    expect(columnRows('旧版本')).toEqual(['same', 'lines'])
    expect(columnRows('新版本')).toEqual(['same', 'lines'])
  })

  it('旧版为空字符串：新版两行新增，空串旧版自身计为 1 行删除', () => {
    // 契约：split('\n') 对空串得 ['']（1 个空行），该空行走删除侧
    render(<ReviewDiff oldContent="" newContent={'x\ny'} />)
    expect(screen.getByText('+2')).toBeInTheDocument()
    expect(screen.getByText('-1')).toBeInTheDocument()
    expect(columnRows('旧版本')).toEqual([''])
    expect(columnRows('新版本')).toEqual(['x', 'y'])
  })

  it('新版为空字符串：旧版三行全删，空串新版计为 1 行新增', () => {
    render(<ReviewDiff oldContent={'x\ny\nz'} newContent="" />)
    expect(screen.getByText('-3')).toBeInTheDocument()
    expect(screen.getByText('+1')).toBeInTheDocument()
    expect(columnRows('旧版本')).toEqual(['x', 'y', 'z'])
  })

  it('两侧输入均非空且完全不同：一侧全删一侧全增', () => {
    render(<ReviewDiff oldContent={'keep'} newContent={'other'} />)
    expect(screen.getByText('+1')).toBeInTheDocument()
    expect(screen.getByText('-1')).toBeInTheDocument()
    expect(columnRows('旧版本')).toEqual(['keep'])
    expect(columnRows('新版本')).toEqual(['other'])
  })

  it('初始 mode 可由 props 指定为 unified', () => {
    render(<ReviewDiff oldContent={'a'} newContent={'b'} mode="unified" />)
    // 统一视图有两列行号，不再出现「旧版本/新版本」列头
    expect(screen.queryByText('旧版本')).toBeNull()
    expect(screen.queryByText('新版本')).toBeNull()
  })
})

describe('ReviewDiff — 统一视图', () => {
  it('点击「统一」切换视图，未变行在前、增删成对呈现', () => {
    render(<ReviewDiff oldContent={'a\nold'} newContent={'a\nnew'} />)
    fireEvent.click(screen.getByTitle('统一视图'))
    expect(screen.queryByText('旧版本')).toBeNull()

    const rows = document
      .querySelector('.review-diff')!
      .querySelectorAll('.whitespace-pre-wrap')
    const contents = Array.from(rows).map((n) => n.textContent)
    // 未变行先输出，随后删除行与新增行成对交错
    expect(contents).toEqual(['a', 'old', 'new'])
  })

  it('删除行缺新行号、新增行缺旧行号（行号列留空）', () => {
    render(<ReviewDiff oldContent={'gone'} newContent={'fresh'} mode="unified" />)
    const container = document.querySelector('.review-diff') as HTMLElement
    const rows = Array.from(container.querySelectorAll('.whitespace-pre-wrap')).map(
      (n) => n.parentElement as HTMLElement,
    )
    // 第一行是删除（旧行号 1，新行号空），第二行是新增（旧行号空，新行号 1）
    const deletedNums = Array.from(rows[0].querySelectorAll('span')).slice(0, 2).map((s) => s.textContent)
    const addedNums = Array.from(rows[1].querySelectorAll('span')).slice(0, 2).map((s) => s.textContent)
    expect(deletedNums).toEqual(['1', ''])
    expect(addedNums).toEqual(['', '1'])
  })

  it('切回左右视图恢复两列布局', () => {
    render(<ReviewDiff oldContent={'a'} newContent={'b'} mode="unified" />)
    fireEvent.click(screen.getByTitle('左右对比'))
    expect(screen.getByText('旧版本')).toBeInTheDocument()
    expect(screen.getByText('新版本')).toBeInTheDocument()
  })

  it('两侧都为空字符串：视为一行未变（split 空串得空行）', () => {
    render(<ReviewDiff oldContent="" newContent="" mode="unified" />)
    // split('\n') 对空串返回 ['']，LCS 命中为未变行 —— 不会命中「两个版本内容相同」占位
    expect(screen.queryByText('两个版本内容相同')).toBeNull()
    const rows = document.querySelectorAll('.review-diff .whitespace-pre-wrap')
    expect(rows).toHaveLength(1)
  })

  it('模式切换不丢失增删统计', () => {
    render(<ReviewDiff oldContent={'a\nb'} newContent={'a'} />)
    expect(screen.getByText('-1')).toBeInTheDocument()
    fireEvent.click(screen.getByTitle('统一视图'))
    expect(screen.getByText('-1')).toBeInTheDocument()
    expect(screen.getByText('+0')).toBeInTheDocument()
  })
})
