/** @feature FP-T12 前端适配（工具结果截断预览+展开回填） @vision V6 可即用 @ci frontend-test */
// @ci: frontend-test
/**
 * DetailBlock 截断回填展开交互测试（数据面瘦身批次 1.2）：
 * 带 onExpand 的块首次展开触发回取，加载中/失败态如实显示（保留预览），
 * 成功后不再显示错误；onExpand 缺省的普通块无副作用。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { DetailBlock } from '../ActivityBlockViews'
import type { ActivityDetailBlock } from '@/types/activity'

vi.mock('@/components/approval', () => ({ TextDiffView: () => null }))
vi.mock('@/components/shared/markdown/MarkdownRenderer', () => ({ MarkdownRenderer: () => null }))

function blockWith(onExpand: () => Promise<boolean>): ActivityDetailBlock {
  return {
    id: 'result-truncated',
    label: '结果已截断（共 9.9 KB），展开加载全文',
    content: '截断预览内容',
    contentType: 'text',
    collapsible: true,
    defaultExpanded: false,
    onExpand,
  }
}

describe('DetailBlock 截断回填展开', () => {
  it('首次展开触发 onExpand，加载中提示可见，成功后提示消失、预览保留', async () => {
    let resolveExpand: (ok: boolean) => void = () => {}
    const onExpand = vi.fn(
      () =>
        new Promise<boolean>((resolve) => {
          resolveExpand = resolve
        }),
    )
    render(<DetailBlock block={blockWith(onExpand)} />)

    // 未展开不触发
    expect(onExpand).not.toHaveBeenCalled()
    fireEvent.click(screen.getByText('结果已截断（共 9.9 KB），展开加载全文'))
    expect(onExpand).toHaveBeenCalledTimes(1)
    expect(screen.getByText('截断预览内容')).toBeTruthy()

    // 加载中如实提示
    expect(await screen.findByText('正在加载完整结果…')).toBeTruthy()
    resolveExpand(true)
    await waitFor(() => expect(screen.queryByText('正在加载完整结果…')).toBeNull())
    // 预览保留（回填成功后本块由全量结果块替换是 store 驱动，块内仅保证不报错）
    expect(screen.getByText('截断预览内容')).toBeTruthy()
  })

  it('失败 → 如实显示失败态并保留预览（禁止假装已全文）', async () => {
    render(<DetailBlock block={blockWith(vi.fn(async () => false))} />)
    fireEvent.click(screen.getByText('结果已截断（共 9.9 KB），展开加载全文'))
    expect(await screen.findByText('完整结果加载失败，当前显示预览')).toBeTruthy()
    expect(screen.getByText('截断预览内容')).toBeTruthy()
  })

  it('onExpand 拒绝（异常）同样落失败态', async () => {
    render(<DetailBlock block={blockWith(vi.fn(async () => { throw new Error('boom') }))} />)
    fireEvent.click(screen.getByText('结果已截断（共 9.9 KB），展开加载全文'))
    expect(await screen.findByText('完整结果加载失败，当前显示预览')).toBeTruthy()
  })

  it('普通块（无 onExpand）展开无副作用、无加载/失败提示', () => {
    render(
      <DetailBlock
        block={{
          label: '参数',
          content: { a: 1 },
          contentType: 'json',
          collapsible: true,
          defaultExpanded: false,
        }}
      />,
    )
    fireEvent.click(screen.getByText('参数'))
    expect(screen.queryByText('正在加载完整结果…')).toBeNull()
    expect(screen.queryByText('完整结果加载失败，当前显示预览')).toBeNull()
  })
})
