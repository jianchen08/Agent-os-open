// @feature: FP-T12 补测 | @ci: frontend-test
/**
 * ImageGallery 补缺测试（批九覆盖率冲刺）
 *
 * 既有 ImageGallery.test.tsx 已覆盖：网格渲染/打开与关闭 Lightbox/卡片下载/
 * 空态/className。本文件补齐其余行为面：
 * - 前后导航越界回绕（第一张向前 → 末张；末张向后 → 首张）
 * - 键盘导航（ArrowLeft/ArrowRight/Escape）
 * - 滚轮缩放（放大/缩小、MIN/MAX 钳制、回到 1 重置平移、百分比显示）
 * - 双击切换缩放（进/出）、scale>1 才可拖拽平移、光标三态
 * - 遮罩空白点击关闭、Lightbox 内部点击不误关
 * - 生成参数面板开合（prompt/size/seed/metadata）；无 prompt 无入口
 * - 大图 onError 回退缩略图（无缩略图、二次失败不循环）
 * - 下载标题回退（title 空串 → image-{id}）、Lightbox 内下载按钮
 * - columns=2/4 网格类切换、thumbnailUrl 缺省回退
 */
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ImageGallery, type ImageItem } from '../ImageGallery'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

function makeImages(count: number): ImageItem[] {
  return Array.from({ length: count }, (_, i) => ({
    id: `img-${i}`,
    url: `https://example.com/image-${i}.png`,
    thumbnailUrl: `https://example.com/thumb-${i}.png`,
    title: `图像 ${i + 1}`,
    prompt: `提示词 ${i + 1}`,
    size: '1024x1024',
    seed: 100 + i,
    metadata: { model: 'xl', style: 'vivid' },
  }))
}

/** 点击网格中第 i 张卡片打开 Lightbox，返回 Lightbox 内的大图元素 */
function openLightbox(images: ImageItem[], i: number): HTMLImageElement {
  fireEvent.click(
    within(screen.getByTestId('gallery-grid')).getByAltText(images[i].title),
  )
  return within(screen.getByTestId('lightbox')).getByAltText(images[i].title)
}

/** Lightbox 中承接滚轮缩放/拖拽的舞台容器（大图的父元素） */
function stageOf(lbImg: HTMLElement): HTMLElement {
  return lbImg.parentElement as HTMLElement
}

describe('ImageGallery 导航回绕与键盘', () => {
  it('导航越界回绕：第一张向前到末张、末张向后回首张', () => {
    const images = makeImages(3)
    render(<ImageGallery images={images} />)
    openLightbox(images, 0)
    expect(screen.getByText('1 / 3')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /上一张/ }))
    expect(screen.getByText('3 / 3')).toBeInTheDocument()
    expect(
      within(screen.getByTestId('lightbox')).getByAltText('图像 3'),
    ).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /下一张/ }))
    expect(screen.getByText('1 / 3')).toBeInTheDocument()
  })

  it('键盘 ArrowLeft/ArrowRight/Escape 导航', () => {
    const images = makeImages(3)
    render(<ImageGallery images={images} />)
    openLightbox(images, 1)
    expect(screen.getByText('2 / 3')).toBeInTheDocument()

    fireEvent.keyDown(document, { key: 'ArrowRight' })
    expect(screen.getByText('3 / 3')).toBeInTheDocument()
    fireEvent.keyDown(document, { key: 'ArrowLeft' })
    expect(screen.getByText('2 / 3')).toBeInTheDocument()
    fireEvent.keyDown(document, { key: 'ArrowLeft' })
    expect(screen.getByText('1 / 3')).toBeInTheDocument()
    fireEvent.keyDown(document, { key: 'ArrowLeft' })
    expect(screen.getByText('3 / 3')).toBeInTheDocument()

    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByTestId('lightbox')).not.toBeInTheDocument()
  })
})

describe('ImageGallery 缩放与拖拽', () => {
  it('滚轮缩放到 MAX/MIN 钳制并显示百分比；单图无前后导航按钮', () => {
    const images = makeImages(1)
    render(<ImageGallery images={images} />)
    const lbImg = openLightbox(images, 0)
    expect(
      screen.queryByRole('button', { name: /上一张/ }),
    ).not.toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: /下一张/ }),
    ).not.toBeInTheDocument()

    const stage = stageOf(lbImg)
    for (let i = 0; i < 25; i++) fireEvent.wheel(stage, { deltaY: -100 })
    expect(screen.getByText('500%')).toBeInTheDocument()

    for (let i = 0; i < 30; i++) fireEvent.wheel(stage, { deltaY: 100 })
    expect(screen.getByText('50%')).toBeInTheDocument()
  })

  it('滚轮放大到 1.2 后降一步恰回 1（IEEE754 精确），百分比隐藏', () => {
    const images = makeImages(1)
    render(<ImageGallery images={images} />)
    const lbImg = openLightbox(images, 0)
    const stage = stageOf(lbImg)

    // 1 + 0.2 = 1.2 精确；1.2 - 0.2 = 1 精确 → 命中「回到 1 重置平移」分支
    fireEvent.wheel(stage, { deltaY: -100 })
    expect(screen.getByText('120%')).toBeInTheDocument()
    fireEvent.wheel(stage, { deltaY: 100 })
    expect(screen.queryByText(/%/)).not.toBeInTheDocument()
  })

  it('scale≤1 拖拽不生效；双击放大后可拖拽平移，再双击复原', () => {
    const images = makeImages(1)
    render(<ImageGallery images={images} />)
    const lbImg = openLightbox(images, 0)
    const stage = stageOf(lbImg)
    expect(stage.style.cursor).toBe('zoom-in')

    // scale≤1 时 mouseDown 直接返回，拖不动
    fireEvent.mouseDown(stage, { clientX: 100, clientY: 100 })
    fireEvent.mouseMove(stage, { clientX: 150, clientY: 120 })
    expect(lbImg.style.transform).toBe('translate(0px, 0px) scale(1)')

    // 双击放大 → 光标 grab → 拖拽平移（50, 20），拖拽中 grabbing + 无过渡
    fireEvent.dblClick(stage)
    expect(screen.getByText('200%')).toBeInTheDocument()
    expect(stage.style.cursor).toBe('grab')
    fireEvent.mouseDown(stage, { clientX: 100, clientY: 100 })
    fireEvent.mouseMove(stage, { clientX: 150, clientY: 120 })
    expect(lbImg.style.transform).toBe('translate(50px, 20px) scale(2)')
    expect(stage.style.cursor).toBe('grabbing')
    expect(lbImg.style.transition).toBe('none')

    // 再双击 → 复原（缩放回 1、平移清零、光标回 zoom-in）
    fireEvent.mouseUp(stage)
    fireEvent.dblClick(stage)
    expect(lbImg.style.transform).toBe('translate(0px, 0px) scale(1)')
    expect(stage.style.cursor).toBe('zoom-in')
    expect(lbImg.style.transition).toBe('transform 0.2s ease-out')
  })
})

describe('ImageGallery Lightbox 交互', () => {
  it('遮罩空白点击关闭；Lightbox 内部点击不冒泡误关', () => {
    const images = makeImages(2)
    render(<ImageGallery images={images} />)
    const lbImg = openLightbox(images, 0)

    fireEvent.click(stageOf(lbImg))
    expect(screen.getByTestId('lightbox')).toBeInTheDocument()

    fireEvent.click(screen.getByTestId('lightbox'))
    expect(screen.queryByTestId('lightbox')).not.toBeInTheDocument()
  })

  it('参数面板：打开显示 prompt/尺寸/种子/metadata，再点收起', () => {
    const images = makeImages(1)
    render(<ImageGallery images={images} />)
    openLightbox(images, 0)
    expect(screen.queryByText('生成参数')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /查看参数/ }))
    expect(screen.getByText('生成参数')).toBeInTheDocument()
    expect(screen.getByText('提示词 1')).toBeInTheDocument()
    // metadata 行由「span 键 + 裸文本值」拼成，getByText 不跨元素匹配，断面板全文
    const panel = screen.getByText('生成参数').parentElement as HTMLElement
    expect(panel.textContent).toContain('model: xl')
    expect(panel.textContent).toContain('style: vivid')
    expect(panel.textContent).toContain('Prompt: ')
    // 尺寸/种子在卡片与面板重复出现，断言至少各一处
    expect(screen.getAllByText('1024x1024').length).toBeGreaterThan(0)
    expect(screen.getAllByText('种子: 100').length).toBeGreaterThan(0)

    fireEvent.click(screen.getByRole('button', { name: /查看参数/ }))
    expect(screen.queryByText('生成参数')).not.toBeInTheDocument()
  })

  it('无 prompt 的图像不渲染参数入口', () => {
    const images = [{ ...makeImages(1)[0], prompt: undefined }]
    render(<ImageGallery images={images} />)
    fireEvent.click(screen.getByAltText('图像 1'))
    expect(
      screen.queryByRole('button', { name: /查看参数/ }),
    ).not.toBeInTheDocument()
  })

  it('大图加载失败回退缩略图；二次失败不再回退', () => {
    const images = makeImages(1)
    render(<ImageGallery images={images} />)
    const lbImg = openLightbox(images, 0)
    expect(lbImg.getAttribute('src')).toBe(images[0].url)

    fireEvent.error(lbImg)
    expect(lbImg.getAttribute('src')).toBe(images[0].thumbnailUrl)
    fireEvent.error(lbImg)
    expect(lbImg.getAttribute('src')).toBe(images[0].thumbnailUrl)
  })

  it('无缩略图时大图失败不切换 src', () => {
    const images = [{ id: 'a', url: 'https://x/a.png', title: '仅原图' }]
    render(<ImageGallery images={images} />)
    const lbImg = openLightbox(images, 0)
    fireEvent.error(lbImg)
    expect(lbImg.getAttribute('src')).toBe('https://x/a.png')
  })

  it('下载：title 空串回退 image-{id}，Lightbox 内下载按钮可用', () => {
    const images = [{ id: 'xyz', url: 'https://x/1.png', title: '' }]
    // 真 anchor 承接 appendChild/removeChild（普通对象会被 jsdom 拒收），
    // 仅 spy click 阻止导航；createElement 间谍经原实现转发防自递归
    const realAnchor = document.createElement('a')
    const clickSpy = vi.spyOn(realAnchor, 'click').mockImplementation(() => {})
    const origCreateElement = document.createElement.bind(document)
    vi.spyOn(document, 'createElement').mockImplementation(
      (tag: string) =>
        tag === 'a'
          ? realAnchor
          : origCreateElement(tag as keyof HTMLElementTagNameMap),
    )

    render(<ImageGallery images={images} />)
    fireEvent.click(screen.getByAltText(''))

    const lbButtons = within(screen.getByTestId('lightbox')).getAllByRole(
      'button',
      { name: /下载/ },
    )
    expect(lbButtons).toHaveLength(1)
    fireEvent.click(lbButtons[0])
    expect(clickSpy).toHaveBeenCalled()
    expect(realAnchor.href).toBe('https://x/1.png')
    expect(realAnchor.download).toBe('image-xyz')
  })
})

describe('ImageGallery 布局与图源', () => {
  it('columns 2/4/默认 对应不同网格列类', () => {
    const images = makeImages(2)
    render(<ImageGallery images={images} columns={2} />)
    const grid = screen.getByTestId('gallery-grid')
    expect(grid.className).toContain('sm:grid-cols-2')
    expect(grid.className).not.toContain('lg:grid-cols-4')
    cleanup()

    render(<ImageGallery images={images} columns={4} />)
    expect(screen.getByTestId('gallery-grid').className).toContain(
      'lg:grid-cols-4',
    )
    cleanup()

    render(<ImageGallery images={images} />)
    expect(screen.getByTestId('gallery-grid').className).toContain(
      'lg:grid-cols-3',
    )
  })

  it('网格缩略图优先 thumbnailUrl，缺省回退 url', () => {
    const withThumb = makeImages(1)
    const { unmount } = render(<ImageGallery images={withThumb} />)
    expect(screen.getByAltText('图像 1').getAttribute('src')).toBe(
      withThumb[0].thumbnailUrl,
    )
    unmount()

    const noThumb = [{ id: 'b', url: 'https://x/b.png', title: '图像 1' }]
    render(<ImageGallery images={noThumb} />)
    expect(screen.getByAltText('图像 1').getAttribute('src')).toBe(
      'https://x/b.png',
    )
  })

  it('大图 url 为空串时回退缩略图', () => {
    const images = [
      { id: 'c', url: '', thumbnailUrl: 'https://x/t.png', title: '空url' },
    ]
    render(<ImageGallery images={images} />)
    const lbImg = openLightbox(images, 0)
    expect(lbImg.getAttribute('src')).toBe('https://x/t.png')
  })
})
