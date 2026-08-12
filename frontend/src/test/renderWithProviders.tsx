/**
 * renderWithProviders —— 统一测试渲染器
 *
 * 包装 react-router MemoryRouter，供使用 PageShell（内部用 <Link>）的页面/组件测试。
 * 不用真实 antd ConfigProvider 以保持 jsdom 轻量（antd 组件按需 mock）。
 *
 * 返回 RTL render 的全部结果，便于直接 screen 查询。
 */

import { render } from '@testing-library/react'
import type { RenderOptions, RenderResult } from '@testing-library/react'
import type { ReactElement, ReactNode } from 'react'
import { MemoryRouter } from 'react-router-dom'
import type { MemoryRouterProps } from 'react-router-dom'

export interface RenderWithProvidersOptions extends Omit<RenderOptions, 'wrapper'> {
  /** MemoryRouter 初始路由，默认 ['/'] */
  initialEntries?: MemoryRouterProps['initialEntries']
}

/** Router 包装器 */
function Wrapper({
  children,
  initialEntries,
}: {
  children: ReactNode
  initialEntries?: MemoryRouterProps['initialEntries']
}) {
  return <MemoryRouter initialEntries={initialEntries ?? ['/']}>{children}</MemoryRouter>
}

export function renderWithProviders(
  ui: ReactElement,
  { initialEntries, ...rest }: RenderWithProvidersOptions = {},
): RenderResult {
  return render(ui, { wrapper: (props) => <Wrapper {...props} initialEntries={initialEntries} />, ...rest })
}
