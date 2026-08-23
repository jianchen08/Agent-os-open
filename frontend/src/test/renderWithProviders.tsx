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
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { QueryClientConfig } from '@tanstack/react-query'

export interface RenderWithProvidersOptions extends Omit<RenderOptions, 'wrapper'> {
  /** MemoryRouter 初始路由，默认 ['/'] */
  initialEntries?: MemoryRouterProps['initialEntries']
  /**
   * 显式传入 QueryClient（默认每次 render 新建测试实例：retry:false、不接
   * persister）。query 化组件普遍需要 Provider，默认自动包裹对不用 query 的
   * 组件无副作用（仅挂 context）。
   */
  queryClient?: QueryClient
}

/** 创建测试专用 QueryClient：无重试、无持久化、不自动刷新 */
export function createTestQueryClient(config?: QueryClientConfig): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        gcTime: Infinity,
        staleTime: 0,
        ...config?.defaultOptions?.queries,
      },
      ...config?.defaultOptions,
    },
    ...config,
  })
}

/** Router + QueryClient 包装器 */
function Wrapper({
  children,
  initialEntries,
  queryClient,
}: {
  children: ReactNode
  initialEntries?: MemoryRouterProps['initialEntries']
  queryClient: QueryClient
}) {
  return (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={initialEntries ?? ['/']}>{children}</MemoryRouter>
    </QueryClientProvider>
  )
}

export function renderWithProviders(
  ui: ReactElement,
  { initialEntries, queryClient, ...rest }: RenderWithProvidersOptions = {},
): RenderResult {
  const client = queryClient ?? createTestQueryClient()
  return render(ui, {
    wrapper: (props) => (
      <Wrapper {...props} initialEntries={initialEntries} queryClient={client} />
    ),
    ...rest,
  })
}
