import { type ClassValue, clsx } from 'clsx'
import { twMerge } from 'tailwind-merge'

/**
 * 合并 CSS 类名工具函数
 *
 * 使用 clsx 处理条件类名，twMerge 处理 Tailwind CSS 类冲突。
 * 所有 shadcn/ui 组件和项目组件都依赖此函数。
 */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs))
}
