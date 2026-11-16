/** @feature 双端优化轮 R25 | @ci: frontend-test */
/**
 * ImageGallery — WebView 110（Chromium <111）防回归测试（R25 系统性收口）
 *
 * 空态 tint 与弱化文字：R22 期为手写 hsl(var(--x)/alpha) 包装（当时现役主题
 * 三元组合法）；R25 审计确认 deep-space --muted / pixel-art --muted-foreground
 * 值自带声明级 alpha，包装展开双 alpha 非法（全引擎 IACVT：底透明、文字继承
 * 全前景）——一律语义 alpha 修饰类：桌面走 @supports 门内 color-mix（与包装
 * 数学等值），110 legacy 路径走 compat 因子化规则（-t/-a 镜像 alpha 乘积）。
 * 契约：bg-muted/20、text-muted-foreground/60；手写两段式包装禁入。
 */

import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ImageGallery } from '../ImageGallery'

const { useAuthedAttachmentUrl } = vi.hoisted(() => ({
  useAuthedAttachmentUrl: vi.fn(),
}))
vi.mock('@/services/fileLoaderRegistry', () => ({
  downloadAttachment: vi.fn(),
  useAuthedAttachmentUrl,
}))

afterEach(() => {
  cleanup()
  useAuthedAttachmentUrl.mockReset()
})

function mockThumbPassthrough() {
  useAuthedAttachmentUrl.mockImplementation((src?: string) => ({
    url: src?.startsWith('/uploads') ? undefined : src,
    failed: false,
  }))
}

const sampleImage = {
  id: 'img-1',
  url: 'https://example.com/image-1.png',
  thumbnailUrl: 'https://example.com/thumb-1.png',
  title: '测试图像',
  prompt: '风景',
  size: '1024x1024',
  seed: 1000,
  createdAt: new Date(2026, 0, 1).toISOString(),
  metadata: { model: 'dall-e-3', style: 'natural' },
}

describe('ImageGallery · WebView 110 兼容（语义 alpha 修饰类；手写 hsl 包装禁入）', () => {
  it('空态容器 tint 为 bg-muted/20，提示文字为 text-muted-foreground/60', () => {
    mockThumbPassthrough()
    const { container } = render(<ImageGallery images={[]} />)
    expect(container.innerHTML).toContain('bg-muted/20')
    expect(container.innerHTML).toContain('text-muted-foreground/60')
    expect(screen.getByText('暂无图像')).toBeInTheDocument()
    expect(container.innerHTML).not.toContain('color-mix(')
  })

  it('手写 hsl(var(--x)/alpha) 两段式包装禁入（带 alpha 主题下双 alpha 非法）', () => {
    mockThumbPassthrough()
    const { container } = render(<ImageGallery images={[]} />)
    expect(container.innerHTML).not.toContain('hsl(var(--muted)/')
    expect(container.innerHTML).not.toContain('hsl(var(--muted-foreground)/')
  })

  it('缩略图取流失败占位图标为弱化 alpha 语义类（同契约）', () => {
    useAuthedAttachmentUrl.mockReturnValue({ url: undefined, failed: true })
    const { container } = render(<ImageGallery images={[sampleImage]} />)
    expect(container.innerHTML).toContain('text-muted-foreground/60')
    expect(container.innerHTML).not.toContain('color-mix(')
  })

  it('列表时间戳弱化文字为弱化 alpha 语义类（同契约）', () => {
    mockThumbPassthrough()
    const { container } = render(<ImageGallery images={[sampleImage]} />)
    expect(screen.getByTestId('image-time').className).toContain('text-muted-foreground/60')
    expect(container.innerHTML).not.toContain('color-mix(')
  })
})
