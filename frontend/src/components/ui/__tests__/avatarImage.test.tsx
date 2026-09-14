/**
 * Avatar 原语补测（簇3）
 *
 * 覆盖 avatar.tsx 的三组件渲染与 AvatarImage 的三条状态分支：
 * - Avatar / AvatarFallback：className 合并与透传属性
 * - AvatarImage：src 存在 → 渲染 img；src 缺省 → 不渲染（return null）；
 *   onError → 错误态不渲染（图片破损回落 fallback）；src 变化 → 重置错误态
 *
 * 不可达/未覆盖说明（本文件 docstring 存证）：
 * - AvatarImage 的 `hasError || !src` 是两条件的守卫，两面均已覆盖
 *   （hasError 由 onError 用例、!src 由缺省用例）。
 * - useEffect 重置错误态依赖 src 变化：若父组件始终传同一 src，重置分支
 *   不会触发——属正常依赖语义（src 不变则错误态应保持），非死代码；
 *   本文件以 src 变更驱动该分支。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { Avatar, AvatarFallback, AvatarImage } from '@/components/ui/avatar'

describe('Avatar 容器', () => {
  it('渲染 span 容器并合并自定义 className', () => {
    const { container } = render(
      <Avatar className="custom-avatar" data-testid="avatar-root">
        <AvatarFallback>AB</AvatarFallback>
      </Avatar>,
    )
    const root = screen.getByTestId('avatar-root')
    expect(root.tagName).toBe('SPAN')
    expect(root.className).toContain('custom-avatar')
    expect(root.className).toContain('rounded-full')
    expect(container.querySelector('.custom-avatar')).not.toBeNull()
  })

  it('forwardRef 透传 ref 到 DOM 节点', () => {
    let node: HTMLSpanElement | null = null
    render(
      <Avatar
        ref={(el) => {
          node = el
        }}
      />,
    )
    expect(node).not.toBeNull()
    expect((node as unknown as HTMLSpanElement).tagName).toBe('SPAN')
  })
})

describe('AvatarFallback', () => {
  it('渲染子文本并合并 className', () => {
    render(<AvatarFallback className="fb-x">兜底文字</AvatarFallback>)
    const fb = screen.getByText('兜底文字')
    expect(fb.tagName).toBe('SPAN')
    expect(fb.className).toContain('fb-x')
    expect(fb.className).toContain('bg-muted')
  })
})

describe('AvatarImage', () => {
  it.each([
    ['绝对 URL', 'https://cdn.example.com/a.png'],
    ['应用内相对路径', '/assets/avatar.png'],
  ])('%s → 渲染 img（src/alt 透传）', (_name, src) => {
    render(<AvatarImage src={src} alt="头像" data-testid="img" />)
    const img = screen.getByTestId('img')
    expect(img.tagName).toBe('IMG')
    expect(img).toHaveAttribute('src', src)
    expect(img).toHaveAttribute('alt', '头像')
    expect(img.className).toContain('object-cover')
  })

  it('src 缺省 → 不渲染任何元素（由 Fallback 接管）', () => {
    const { container } = render(
      <Avatar>
        <AvatarImage src="" alt="空" />
        <AvatarFallback>FB</AvatarFallback>
      </Avatar>,
    )
    expect(container.querySelector('img')).toBeNull()
    expect(screen.getByText('FB')).toBeInTheDocument()
  })

  it('图片加载失败（onError）→ 转为不渲染，回落 Fallback', async () => {
    const src = 'https://cdn.example.com/broken.png'
    const { container } = render(
      <Avatar>
        <AvatarImage src={src} alt="破损" data-testid="img" />
        <AvatarFallback>FB</AvatarFallback>
      </Avatar>,
    )
    expect(screen.getByTestId('img')).toBeInTheDocument()

    // 触发 React 的 onError（真实 error 事件不冒泡，react 在根容器上代理捕获）
    fireEvent.error(screen.getByTestId('img'))

    // 错误态经 state 更新异步生效
    await waitFor(() => expect(container.querySelector('img')).toBeNull())
    expect(screen.getByText('FB')).toBeInTheDocument()
  })

  it('同 src 重渲染保持错误态（依赖未变更不重置）', async () => {
    const src = 'https://cdn.example.com/broken.png'
    const { container, rerender } = render(
      <AvatarImage src={src} alt="破损" data-testid="img" />,
    )
    fireEvent.error(screen.getByTestId('img'))
    await waitFor(() => expect(container.querySelector('img')).toBeNull())

    rerender(<AvatarImage src={src} alt="破损" data-testid="img" />)
    expect(container.querySelector('img')).toBeNull()
  })

  it('src 变化 → 重置错误态重新渲染 img（换头像后旧错误不粘）', async () => {
    const { container, rerender } = render(
      <AvatarImage src="https://cdn.example.com/old.png" alt="旧" data-testid="img" />,
    )
    fireEvent.error(screen.getByTestId('img'))
    await waitFor(() => expect(container.querySelector('img')).toBeNull())

    rerender(<AvatarImage src="https://cdn.example.com/new.png" alt="新" data-testid="img" />)
    await waitFor(() => expect(container.querySelector('img')).not.toBeNull())
    expect(screen.getByTestId('img')).toHaveAttribute('src', 'https://cdn.example.com/new.png')
  })

  it('forwardRef 透传 ref 到 img 节点', () => {
    let node: HTMLImageElement | null = null
    render(
      <AvatarImage
        src="/a.png"
        alt="x"
        ref={(el) => {
          node = el
        }}
      />,
    )
    expect(node).not.toBeNull()
    expect((node as unknown as HTMLImageElement).tagName).toBe('IMG')
  })
})
