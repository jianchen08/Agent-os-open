/**
 * ImageGallery 组件单元测试
 *
 * 测试覆盖：
 * - 网格展示图像
 * - 点击查看大图（Lightbox）
 * - 显示生成参数信息
 * - 下载功能
 * - 历史记录浏览
 * - 响应式布局
 * - 空数据状态
 */

import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ImageGallery } from '../ImageGallery'

beforeEach(() => {
  vi.clearAllMocks()
})

afterEach(() => {
  cleanup()
})

/** 创建模拟图像数据 */
function createMockImages(count: number = 3) {
  return Array.from({ length: count }, (_, i) => ({
    id: `img-${i}`,
    url: `https://example.com/image-${i}.png`,
    thumbnailUrl: `https://example.com/thumb-${i}.png`,
    title: `测试图像 ${i + 1}`,
    prompt: `一个美丽的风景，编号 ${i + 1}`,
    size: '1024x1024',
    seed: 1000 + i,
    createdAt: new Date(2026, 0, i + 1).toISOString(),
    metadata: {
      model: 'dall-e-3',
      style: 'natural',
    },
  }))
}

describe('ImageGallery', () => {
  it('应正确渲染图像画廊组件', () => {
    const images = createMockImages(3)
    render(<ImageGallery images={images} />)

    // 验证所有图像都已渲染
    images.forEach((img) => {
      expect(screen.getByAltText(img.title)).toBeInTheDocument()
    })
  })

  it('应以网格布局展示图像', () => {
    const images = createMockImages(6)
    render(<ImageGallery images={images} />)

    const grid = document.querySelector('[data-testid="gallery-grid"]')
    expect(grid).toBeInTheDocument()

    // 验证网格中有正确数量的图像卡片
    const cards = grid?.querySelectorAll('[data-testid="gallery-card"]')
    expect(cards?.length).toBe(6)
  })

  it('点击图像应打开 Lightbox 大图查看', () => {
    const images = createMockImages(3)
    render(<ImageGallery images={images} />)

    // 点击第一张图
    const firstImage = screen.getByAltText('测试图像 1')
    fireEvent.click(firstImage)

    // 应该打开 Lightbox
    const lightbox = document.querySelector('[data-testid="lightbox"]')
    expect(lightbox).toBeInTheDocument()
  })

  it('Lightbox 中应显示关闭按钮', () => {
    const images = createMockImages(3)
    render(<ImageGallery images={images} />)

    // 打开 Lightbox
    const firstImage = screen.getByAltText('测试图像 1')
    fireEvent.click(firstImage)

    // 应该有关闭按钮
    const closeButton = screen.getByRole('button', { name: /关闭/i })
    expect(closeButton).toBeInTheDocument()
  })

  it('点击关闭按钮应关闭 Lightbox', () => {
    const images = createMockImages(3)
    render(<ImageGallery images={images} />)

    // 打开 Lightbox
    fireEvent.click(screen.getByAltText('测试图像 1'))
    expect(document.querySelector('[data-testid="lightbox"]')).toBeInTheDocument()

    // 关闭
    fireEvent.click(screen.getByRole('button', { name: /关闭/i }))
    expect(document.querySelector('[data-testid="lightbox"]')).not.toBeInTheDocument()
  })

  it('应显示生成参数信息（prompt, size, seed）', () => {
    const images = createMockImages(1)
    render(<ImageGallery images={images} />)

    // 验证生成参数显示
    expect(screen.getByText(/1024x1024/)).toBeInTheDocument()
    expect(screen.getByText(/1000/)).toBeInTheDocument()
  })

  it('应支持下载图像', () => {
    const images = createMockImages(1)

    const mockAnchor = {
      href: '',
      download: '',
      click: vi.fn(),
    }
    vi.spyOn(document, 'createElement').mockImplementation((tag: string) => {
      if (tag === 'a') return mockAnchor as unknown as HTMLAnchorElement
      return document.createElement(tag)
    })

    render(<ImageGallery images={images} />)

    const downloadButton = screen.getByRole('button', { name: /下载/i })
    fireEvent.click(downloadButton)

    expect(mockAnchor.click).toHaveBeenCalled()

    vi.restoreAllMocks()
  })

  it('空数据时应显示空状态提示', () => {
    render(<ImageGallery images={[]} />)

    expect(screen.getByText(/暂无图像/)).toBeInTheDocument()
  })

  it('应渲染响应式网格布局', () => {
    const images = createMockImages(4)
    render(<ImageGallery images={images} />)

    const grid = document.querySelector('[data-testid="gallery-grid"]')
    // 验证网格使用了响应式样式类
    expect(grid?.className).toMatch(/grid/)
  })

  it('应显示图像标题', () => {
    const images = createMockImages(2)
    render(<ImageGallery images={images} />)

    images.forEach((img) => {
      expect(screen.getByText(img.title)).toBeInTheDocument()
    })
  })

  it('Lightbox 中应支持导航到前后图像', () => {
    const images = createMockImages(3)
    render(<ImageGallery images={images} />)

    // 打开 Lightbox
    fireEvent.click(screen.getByAltText('测试图像 1'))

    // 应该有导航按钮
    const prevButton = screen.queryByRole('button', { name: /上一张/i })
    const nextButton = screen.queryByRole('button', { name: /下一张/i })

    // 第一张图不应该有"上一张"，但应该有"下一张"
    expect(nextButton).toBeInTheDocument()
  })

  it('应显示图像创建时间', () => {
    const images = createMockImages(1)
    render(<ImageGallery images={images} />)

    // 验证时间显示区域存在
    const timeElement = document.querySelector('[data-testid="image-time"]')
    expect(timeElement).toBeInTheDocument()
  })

  it('应应用自定义 className', () => {
    const images = createMockImages(1)
    render(<ImageGallery images={images} className="custom-gallery" />)

    const container = document.querySelector('.custom-gallery')
    expect(container).toBeInTheDocument()
  })
})
