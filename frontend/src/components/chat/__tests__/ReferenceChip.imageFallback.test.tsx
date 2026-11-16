/** @feature FP-T12 前端组件补测 | @ci: frontend-test */
/**
 * ReferenceChip 预览图降级补测：预览图加载失败时隐藏破图（不留裂图占位，
 * 标题与副标题照常可读）。
 */

import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ReferenceChip } from '../ReferenceChip'

describe('ReferenceChip — 预览图加载失败降级', () => {
  it('图片 onError → 该图隐藏，文本信息不受影响', () => {
    render(
      <ReferenceChip
        data={{
          kind: 'image',
          title: '设计稿.png',
          subtitle: 'image @ /assets/design.png',
          previewUrl: 'blob:broken-image',
        }}
      />,
    )

    const img = screen.getByRole('img', { name: '设计稿.png' })
    fireEvent.error(img)

    expect((img as HTMLImageElement).style.display).toBe('none')
    expect(screen.getByText('设计稿.png')).toBeInTheDocument()
    expect(screen.getByText(/image @ /)).toBeInTheDocument()
  })

  it('无预览图时渲染占位图标（不发图片请求）', () => {
    render(<ReferenceChip data={{ kind: 'file', title: 'README.md' }} />)

    expect(screen.queryByRole('img')).not.toBeInTheDocument()
    expect(screen.getByText('README.md')).toBeInTheDocument()
  })
})
