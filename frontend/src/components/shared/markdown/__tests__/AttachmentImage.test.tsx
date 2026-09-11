/** @ci frontend-test */
/**
 * markdown 图片附件失败态测试（2026-09-08 附件解析失败显式化）。
 *
 * 消息 content 里的附件图片引用（![f](/uploads/x.png)）加载失败（404 等）
 * 不得停留为无说明的占位图——必须显式化为附件失败卡（用户裁定：前端要说明）；
 * 成功路径渲染普通 <img>（行为不变）。
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { AttachmentImage } from '../AttachmentImage'

// @lobehub/ui barrel 在 vitest 下无法解析（fluent-emoji 目录式 ESM import，
// 全仓既有测试一律 mock 该库）——以最小 <img> 替身承担其成功路径渲染契约。
vi.mock('@lobehub/ui', () => ({
  Image: ({
    src,
    alt,
    onError,
  }: {
    src?: string
    alt?: string
    onError?: (e: Event) => void
  }) => <img src={src} alt={alt} onError={onError} data-testid="lobehub-image" />,
}))

describe('AttachmentImage 附件图片失败态', () => {
  it('成功路径：渲染普通 img（src/alt 透传，行为不变）', () => {
    render(<AttachmentImage src="/uploads/cat.png" alt="cat.png" />)
    const img = screen.getByRole('img')
    expect(img).toHaveAttribute('src', '/uploads/cat.png')
    expect(img).toHaveAttribute('alt', 'cat.png')
  })

  it('加载失败（onError）→ 显式失败卡：失败说明 + 附件名，不再渲染 img', () => {
    render(<AttachmentImage src="/uploads/gone.png" alt="gone.png" />)
    fireEvent.error(screen.getByRole('img'))
    expect(screen.queryByRole('img')).toBeNull()
    expect(screen.getByTestId('attachment-image-failed')).toBeInTheDocument()
    expect(screen.getByText('附件加载失败')).toBeInTheDocument()
    expect(screen.getByText('gone.png')).toBeInTheDocument()
  })

  it('无 alt 时失败卡回退展示 src（来源仍可辨识）', () => {
    render(<AttachmentImage src="/uploads/anonymous.png" />)
    fireEvent.error(screen.getByRole('img'))
    expect(screen.getByText('/uploads/anonymous.png')).toBeInTheDocument()
  })

  it('src 变化后失败态自动复位（同组件渲染新附件不再残留失败卡）', () => {
    const { rerender } = render(<AttachmentImage src="/uploads/a.png" alt="a.png" />)
    fireEvent.error(screen.getByRole('img'))
    expect(screen.getByTestId('attachment-image-failed')).toBeInTheDocument()
    rerender(<AttachmentImage src="/uploads/b.png" alt="b.png" />)
    const img = screen.getByRole('img')
    expect(img).toHaveAttribute('src', '/uploads/b.png')
  })
})
