// @feature: FP-T12 补测 | @ci: frontend-test
/**
 * GalleryWidget 组件测试（批九覆盖率冲刺，此前 0% 无测试）
 *
 * 行为面：
 * - 空数据/非数组 items → 空态提示
 * - 非法项过滤（非对象/缺字符串 src 的项不渲染）
 * - columns 阈值映射网格列类（默认 3 / ≤2 / ≤4 / >4）
 * - 信息区按 title/description 有无渲染
 * - 点击卡片打开放大预览：计数、预览图切换、首末张导航按钮显隐
 * - 预览信息浮层按 title/description 有无渲染
 * - 预览关闭：✕ 按钮、遮罩空白点击；内部点击不误关
 * - 缩略图加载失败 → 隐藏原图、显示占位
 */
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { GalleryWidget } from '../GalleryWidget'

afterEach(cleanup)

const ITEMS = [
  { src: 'https://x/1.png', alt: '图一', title: '标题一', description: '描述一' },
  { src: 'https://x/2.png', alt: '图二' },
  { src: 'https://x/3.png', alt: '图三', title: '标题三' },
]

function gridEl(): HTMLElement {
  return document.querySelector('.grid') as HTMLElement
}

function overlayEl(): HTMLElement | null {
  return document.querySelector('.fixed')
}

describe('GalleryWidget 数据面', () => {
  it('items 缺省/非数组/空数组 → 空态提示', () => {
    render(<GalleryWidget />)
    expect(screen.getByText('暂无图片')).toBeInTheDocument()
    cleanup()

    render(<GalleryWidget items={'不是数组'} />)
    expect(screen.getByText('暂无图片')).toBeInTheDocument()
    cleanup()

    render(<GalleryWidget items={[]} />)
    expect(screen.getByText('暂无图片')).toBeInTheDocument()
  })

  it('过滤非法项，只渲染含字符串 src 的项', () => {
    render(
      <GalleryWidget
        items={[
          null,
          42,
          'x',
          { alt: '缺src' },
          { src: 'ok.png', alt: '好图', title: '唯一' },
        ]}
      />,
    )
    expect(screen.getByAltText('好图')).toBeInTheDocument()
    expect(screen.queryByAltText('缺src')).not.toBeInTheDocument()
    expect(screen.getAllByRole('img')).toHaveLength(1)
    expect(screen.getByText('唯一')).toBeInTheDocument()
  })

  it('信息区按 title/description 有无渲染', () => {
    render(<GalleryWidget items={ITEMS} />)
    expect(screen.getByText('标题一')).toBeInTheDocument()
    expect(screen.getByText('描述一')).toBeInTheDocument()
    expect(screen.getByText('标题三')).toBeInTheDocument()
    // 图二无 title/description → 不渲染信息块（图片容器的下一个兄弟为空）
    const img2 = screen.getByAltText('图二')
    expect(img2.parentElement?.nextElementSibling).toBeNull()
    // 图一有信息块
    expect(
      screen.getByAltText('图一').parentElement?.nextElementSibling,
    ).not.toBeNull()
  })
})

describe('GalleryWidget 布局', () => {
  it('columns 阈值映射网格列类：默认3 / ≤2 / ≤4 / >4', () => {
    render(<GalleryWidget items={ITEMS} />)
    expect(gridEl().classList.contains('grid-cols-3')).toBe(true)
    cleanup()

    render(<GalleryWidget items={ITEMS} columns={2} />)
    expect(gridEl().classList.contains('grid-cols-2')).toBe(true)
    cleanup()

    render(<GalleryWidget items={ITEMS} columns={4} />)
    expect(gridEl().classList.contains('grid-cols-4')).toBe(true)
    cleanup()

    render(<GalleryWidget items={ITEMS} columns={6} />)
    expect(gridEl().className).toContain('sm:grid-cols-2')
    expect(gridEl().className).toContain('lg:grid-cols-4')
  })
})

describe('GalleryWidget 放大预览', () => {
  it('点击卡片打开预览：计数与预览图随导航切换，首末张按钮显隐', () => {
    render(<GalleryWidget items={ITEMS} />)
    fireEvent.click(screen.getByAltText('图一'))

    const ov = overlayEl()
    expect(ov).not.toBeNull()
    expect(
      within(ov as HTMLElement).getByAltText('图一').getAttribute('src'),
    ).toBe('https://x/1.png')
    expect(screen.getByText('1 / 3')).toBeInTheDocument()
    // 首张无 ‹，有 ›
    expect(screen.queryByRole('button', { name: '‹' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '›' }))
    expect(screen.getByText('2 / 3')).toBeInTheDocument()
    expect(within(ov as HTMLElement).getByAltText('图二')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '›' }))
    expect(screen.getByText('3 / 3')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '›' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '‹' }))
    expect(screen.getByText('2 / 3')).toBeInTheDocument()
  })

  it('预览信息浮层按 title/description 有无渲染', () => {
    render(<GalleryWidget items={ITEMS} />)
    fireEvent.click(screen.getByAltText('图一'))
    // 卡片与预览浮层各渲染一份标题
    expect(screen.getAllByText('标题一')).toHaveLength(2)
    expect(screen.getAllByText('描述一')).toHaveLength(2)

    fireEvent.click(screen.getByRole('button', { name: '›' }))
    // 图二无 title/description → 浮层无信息，仅计数
    const ov = overlayEl() as HTMLElement
    expect(within(ov).queryByText('标题二')).toBeNull()
    expect(within(ov).getByText('2 / 3')).toBeInTheDocument()
  })

  it('预览关闭：✕ 按钮与遮罩空白点击生效，内部点击不误关', () => {
    render(<GalleryWidget items={ITEMS} />)
    fireEvent.click(screen.getByAltText('图一'))

    // 内部容器（预览图所在）点击 stopPropagation → 不关闭
    fireEvent.click(within(overlayEl() as HTMLElement).getByAltText('图一'))
    expect(overlayEl()).not.toBeNull()

    fireEvent.click(screen.getByRole('button', { name: '✕' }))
    expect(overlayEl()).toBeNull()

    // 遮罩自身（target === currentTarget）点击 → 关闭
    fireEvent.click(screen.getByAltText('图二'))
    fireEvent.click(overlayEl() as HTMLElement)
    expect(overlayEl()).toBeNull()
  })
})

describe('GalleryWidget 加载失败占位', () => {
  it('缩略图 onError：隐藏原图并显示加载失败占位', () => {
    render(
      <GalleryWidget items={[{ src: 'bad.png', alt: '坏图', title: 't' }]} />,
    )
    const img = screen.getByAltText('坏图')
    fireEvent.error(img)
    expect(img.style.display).toBe('none')
    const placeholder = img.nextElementSibling as HTMLElement
    expect(placeholder.style.display).toBe('flex')
    expect(placeholder.textContent).toContain('加载失败')
  })
})
