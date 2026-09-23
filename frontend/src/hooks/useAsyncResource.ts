/**
 * useAsyncResource —— 页面级异步资源四态统一入口（OBS-R258-1）
 *
 * 用户裁定约定：所有前端页面必须区分「无数据」与「加载失败」——失败不得
 * 伪装成空态（失败 = 显式 ErrorState + 重试；空 = EmptyState）。
 *
 * 本 hook 在 react-query（status: pending/success/error）之上派生穷举四态
 * status 联合，页面按 resource.status 分支渲染；视觉件复用
 * shared/LoadingState · ErrorState · EmptyState。派生口径：
 * - error 优先于旧数据：成功后 refetch 失败 → status='error'（不伪装 ready）
 * - 非 Error 拒绝回退 fallbackErrorText（不产出 [object Object] 类脏文本）
 * - 空判定默认「数组长度 0」，非数组数据须显式 isEmpty
 *
 * 新页面必接：pages 目录裸用 useQuery 由 local-rules/no-bare-usequery-in-pages
 * 拦截（frontend/eslint-rules/local-rules.mjs）。一页纸规范见
 * docs/working/前端空态失败态四态约定_OBS-R258-1.md。
 */

import type { UseQueryResult } from '@tanstack/react-query'

/** 穷举四态：loading 首载 / error 加载失败 / empty 成功但无数据 / ready 有数据 */
export type AsyncResourceStatus = 'loading' | 'error' | 'empty' | 'ready'

/** 未给 fallbackErrorText 时的默认错误文案 */
const DEFAULT_ERROR_TEXT = '加载失败，请稍后重试'

/** useAsyncResource 选项 */
export interface AsyncResourceOptions<TData> {
  /** 空态判定（默认：数组长度 0；非数组数据须显式给） */
  isEmpty?: (data: TData) => boolean
  /** 非 Error 拒绝时的回退文案（默认通用提示） */
  fallbackErrorText?: string
}

/** 四态资源：页面渲染分支的唯一数据面 */
export interface AsyncResource<TData> {
  /** 穷举四态，页面渲染分支的唯一依据 */
  status: AsyncResourceStatus
  /** status === 'loading' */
  isLoading: boolean
  /** status === 'error' */
  isError: boolean
  /** status === 'error' 时的用户可读文案（其余态为 null） */
  error: string | null
  /** status === 'ready' | 'empty' 时的数据（empty 判定交给 isEmpty） */
  data: TData | undefined
  /** 重试（失败态重试钮/刷新钮接此；透传 query.refetch） */
  refetch: () => void
}

function defaultIsEmpty<TData>(data: TData): boolean {
  return Array.isArray(data) && data.length === 0
}

/** 把 react-query 结果包装成穷举四态资源 */
export function useAsyncResource<TData>(
  query: UseQueryResult<TData, Error>,
  options?: AsyncResourceOptions<TData>,
): AsyncResource<TData> {
  const isEmpty = options?.isEmpty ?? defaultIsEmpty
  const fallbackErrorText = options?.fallbackErrorText ?? DEFAULT_ERROR_TEXT

  if (query.status === 'error') {
    const message =
      query.error instanceof Error && query.error.message
        ? query.error.message
        : fallbackErrorText
    return {
      status: 'error',
      isLoading: false,
      isError: true,
      error: message,
      data: undefined,
      refetch: () => {
        void query.refetch()
      },
    }
  }
  if (query.status === 'pending' || query.data === undefined) {
    return {
      status: 'loading',
      isLoading: true,
      isError: false,
      error: null,
      data: undefined,
      refetch: () => {
        void query.refetch()
      },
    }
  }
  return {
    status: isEmpty(query.data) ? 'empty' : 'ready',
    isLoading: false,
    isError: false,
    error: null,
    data: query.data,
    refetch: () => {
      void query.refetch()
    },
  }
}
