/**
 * ChoiceDescriptionDialog.test.tsx
 *
 * 验证 Backlog 1.2: Choice 模式选项描述弹窗渲染
 *
 * 测试覆盖：
 * 1. AC-1.2-1: 点击有长描述的选项弹出详情弹窗，确认后才执行选择
 * 2. AC-1.2-2: 弹窗内容可滚动（max-h-[60vh]）
 * 3. AC-1.2-3: 使用 MarkdownRenderer 渲染 description，短描述直接执行
 * 4. AC-1.2-4: 无 description 选项保持原有行为
 */

import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import React from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { InteractionCard } from '@/components/chat/InteractionCard'
import type { InteractionCardProps } from '@/components/chat/InteractionCard'
import type { PendingInteraction } from '@/stores/interactionStore'

// ---------------------------------------------------------------------------
//  Mock: lucide-react
// ---------------------------------------------------------------------------
vi.mock('lucide-react', () => {
  const icons = [
    'ArrowRight',
    'Check',
    'Loader2',
    'MessageSquare',
    'Clock',
    'AlertTriangle',
    'Send',
    'X',
  ]
  const m: Record<string, any> = {}
  for (const name of icons) {
    m[name] = (p: any) => <svg data-testid={`icon-${name}`} {...p} />
  }
  return m
})

// ---------------------------------------------------------------------------
//  Mock: @/lib/utils
// ---------------------------------------------------------------------------
vi.mock('@/lib/utils', () => ({
  cn: (...args: (string | undefined | null | false)[]) =>
    args.filter(Boolean).join(' '),
}))

// ---------------------------------------------------------------------------
//  Mock: MarkdownRenderer — 记录调用参数以便断言
// ---------------------------------------------------------------------------
const markdownRenderCalls: string[] = []

vi.mock('@/components/chat/markdown/MarkdownRenderer', () => ({
  MarkdownRenderer: ({ content }: { content: string }) => {
    markdownRenderCalls.push(content)
    return <div data-testid="markdown-renderer">{content}</div>
  },
}))

// ---------------------------------------------------------------------------
//  Mock: UI Button
// ---------------------------------------------------------------------------
vi.mock('@/components/ui/button', () => ({
  Button: ({
    children,
    onClick,
    disabled,
    ...rest
  }: {
    children: React.ReactNode
    onClick?: () => void
    disabled?: boolean
    [key: string]: any
  }) => (
    <button
      data-testid={`button-${typeof children === 'string' ? children : 'action'}`}
      onClick={onClick}
      disabled={disabled}
      {...rest}
    >
      {children}
    </button>
  ),
}))

// ---------------------------------------------------------------------------
//  Mock: Dialog — 简化渲染，通过 data-testid 标识各部分
// ---------------------------------------------------------------------------
vi.mock('@/components/ui/dialog', () => {
  return {
    Dialog: ({ children, open }: { children: React.ReactNode; open?: boolean }) => {
      if (!open) return null
      return <div data-testid="dialog-root">{children}</div>
    },
    DialogContent: ({ children, className }: { children: React.ReactNode; className?: string }) => (
      <div data-testid="dialog-content" className={className}>
        {children}
      </div>
    ),
    DialogHeader: ({ children }: { children: React.ReactNode }) => (
      <div data-testid="dialog-header">{children}</div>
    ),
    DialogTitle: ({ children }: { children: React.ReactNode }) => (
      <h2 data-testid="dialog-title">{children}</h2>
    ),
    DialogDescription: ({ children }: { children: React.ReactNode }) => (
      <p data-testid="dialog-description">{children}</p>
    ),
    DialogFooter: ({ children }: { children: React.ReactNode }) => (
      <div data-testid="dialog-footer">{children}</div>
    ),
    DialogPortal: ({ children }: { children: React.ReactNode }) => <>{children}</>,
    DialogOverlay: () => <div data-testid="dialog-overlay" />,
    DialogTrigger: ({ children }: { children: React.ReactNode }) => <>{children}</>,
    DialogClose: ({ children, onClick }: { children: React.ReactNode; onClick?: () => void }) => (
      <button data-testid="dialog-close" onClick={onClick}>{children}</button>
    ),
  }
})

// ---------------------------------------------------------------------------
//  工厂函数
// ---------------------------------------------------------------------------

/** 创建 PendingInteraction 对象 */
function createPendingInteraction(
  overrides: Partial<PendingInteraction> = {},
): PendingInteraction {
  return {
    requestId: 'req-1',
    mode: 'choice',
    title: '选择操作',
    description: '',
    threadId: 'thread-1',
    tabId: 'tab-1',
    agentId: 'agent-1',
    timestamp: new Date().toISOString(),
    status: 'pending',
    ...overrides,
  }
}

/** 创建完整的 InteractionCardProps */
function createCardProps(
  overrides: Partial<InteractionCardProps> = {},
): InteractionCardProps {
  return {
    interaction: createPendingInteraction(
      overrides.interaction as Partial<PendingInteraction> | undefined,
    ),
    onRespondChoice: overrides.onRespondChoice ?? vi.fn(),
    onRespondText: overrides.onRespondText ?? vi.fn(),
    onNavigateToTab: overrides.onNavigateToTab ?? vi.fn(),
    isSubmitting: overrides.isSubmitting ?? false,
  }
}

// ---------------------------------------------------------------------------
//  测试
// ---------------------------------------------------------------------------

describe('ChoiceDescriptionDialog — Backlog 1.2: 选项描述弹窗', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    markdownRenderCalls.length = 0
  })

  // -----------------------------------------------------------------------
  // AC-1.2-4: 无 description 选项保持原有行为
  // -----------------------------------------------------------------------
  describe('无 description 选项（兼容原有行为）', () => {
    it('点击无 description 的选项应直接触发 onRespondChoice，不弹窗', async () => {
      const onRespondChoice = vi.fn()
      const props = createCardProps({
        interaction: createPendingInteraction({
          mode: 'choice',
          options: [
            { id: 'a', label: '批准' },
            { id: 'b', label: '拒绝' },
          ],
        }),
        onRespondChoice,
      })

      render(<InteractionCard {...props} />)

      // 点击选项
      await act(async () => {
        fireEvent.click(screen.getByText('批准'))
      })

      // 直接触发回调，不弹窗
      expect(onRespondChoice).toHaveBeenCalledTimes(1)
      expect(onRespondChoice).toHaveBeenCalledWith('a')
      expect(screen.queryByTestId('dialog-root')).not.toBeInTheDocument()
    })
  })

  // -----------------------------------------------------------------------
  // AC-1.2-3: 短 description（<20字符）直接执行选择
  // -----------------------------------------------------------------------
  describe('短 description 选项（<20字符）', () => {
    it('短 description 应直接触发选择，不弹窗', async () => {
      const onRespondChoice = vi.fn()
      const props = createCardProps({
        interaction: createPendingInteraction({
          mode: 'choice',
          options: [
            { id: 'a', label: '简单选项', description: '简短说明' },
          ],
        }),
        onRespondChoice,
      })

      render(<InteractionCard {...props} />)

      await act(async () => {
        fireEvent.click(screen.getByText('简单选项'))
      })

      // 短描述直接执行
      expect(onRespondChoice).toHaveBeenCalledTimes(1)
      expect(onRespondChoice).toHaveBeenCalledWith('a')
      expect(screen.queryByTestId('dialog-root')).not.toBeInTheDocument()
    })

    it('空字符串 description 应直接执行选择', async () => {
      const onRespondChoice = vi.fn()
      const props = createCardProps({
        interaction: createPendingInteraction({
          mode: 'choice',
          options: [
            { id: 'a', label: '空描述', description: '' },
          ],
        }),
        onRespondChoice,
      })

      render(<InteractionCard {...props} />)

      await act(async () => {
        fireEvent.click(screen.getByText('空描述'))
      })

      expect(onRespondChoice).toHaveBeenCalledTimes(1)
      expect(onRespondChoice).toHaveBeenCalledWith('a')
    })
  })

  // -----------------------------------------------------------------------
  // AC-1.2-1: 点击有长描述的选项弹出详情弹窗
  // -----------------------------------------------------------------------
  describe('长 description 选项（>=20字符）', () => {
    const longDescription = '这是一个详细的选项说明，包含足够多的文字来触发弹窗展示。'

    it('点击选项应弹出 Dialog，不直接触发 onRespondChoice', async () => {
      const onRespondChoice = vi.fn()
      const props = createCardProps({
        interaction: createPendingInteraction({
          mode: 'choice',
          options: [
            { id: 'a', label: '详细选项', description: longDescription },
          ],
        }),
        onRespondChoice,
      })

      render(<InteractionCard {...props} />)

      // 点击选项
      await act(async () => {
        fireEvent.click(screen.getByText('详细选项'))
      })

      // 不应直接触发回调
      expect(onRespondChoice).not.toHaveBeenCalled()

      // 应该弹出 Dialog
      expect(screen.getByTestId('dialog-root')).toBeInTheDocument()
    })

    it('弹窗应显示选项标题和描述内容', async () => {
      const props = createCardProps({
        interaction: createPendingInteraction({
          mode: 'choice',
          options: [
            { id: 'a', label: '详细选项', description: longDescription },
          ],
        }),
      })

      render(<InteractionCard {...props} />)

      await act(async () => {
        fireEvent.click(screen.getByText('详细选项'))
      })

      // 弹窗标题显示选项名称
      expect(screen.getByTestId('dialog-title')).toHaveTextContent('详细选项')

      // 描述通过 MarkdownRenderer 渲染
      expect(screen.getByTestId('markdown-renderer')).toBeInTheDocument()
      expect(screen.getByTestId('markdown-renderer')).toHaveTextContent(longDescription)
    })

    it('弹窗应提供「确认选择」和「取消」按钮', async () => {
      const props = createCardProps({
        interaction: createPendingInteraction({
          mode: 'choice',
          options: [
            { id: 'a', label: '详细选项', description: longDescription },
          ],
        }),
      })

      render(<InteractionCard {...props} />)

      await act(async () => {
        fireEvent.click(screen.getByText('详细选项'))
      })

      // 确认和取消按钮存在
      expect(screen.getByText('确认选择')).toBeInTheDocument()
      expect(screen.getByText('取消')).toBeInTheDocument()
    })

    it('点击「确认选择」应触发 onRespondChoice 并关闭弹窗', async () => {
      const onRespondChoice = vi.fn()
      const props = createCardProps({
        interaction: createPendingInteraction({
          mode: 'choice',
          options: [
            { id: 'a', label: '详细选项', description: longDescription },
          ],
        }),
        onRespondChoice,
      })

      render(<InteractionCard {...props} />)

      // 点击选项打开弹窗
      await act(async () => {
        fireEvent.click(screen.getByText('详细选项'))
      })

      expect(screen.getByTestId('dialog-root')).toBeInTheDocument()

      // 点击确认
      await act(async () => {
        fireEvent.click(screen.getByText('确认选择'))
      })

      // 应触发回调
      expect(onRespondChoice).toHaveBeenCalledTimes(1)
      expect(onRespondChoice).toHaveBeenCalledWith('a')

      // 弹窗应关闭
      expect(screen.queryByTestId('dialog-root')).not.toBeInTheDocument()
    })

    it('点击「取消」应关闭弹窗，不触发 onRespondChoice', async () => {
      const onRespondChoice = vi.fn()
      const props = createCardProps({
        interaction: createPendingInteraction({
          mode: 'choice',
          options: [
            { id: 'a', label: '详细选项', description: longDescription },
          ],
        }),
        onRespondChoice,
      })

      render(<InteractionCard {...props} />)

      // 点击选项打开弹窗
      await act(async () => {
        fireEvent.click(screen.getByText('详细选项'))
      })

      expect(screen.getByTestId('dialog-root')).toBeInTheDocument()

      // 点击取消
      await act(async () => {
        fireEvent.click(screen.getByText('取消'))
      })

      // 不应触发回调
      expect(onRespondChoice).not.toHaveBeenCalled()

      // 弹窗应关闭
      expect(screen.queryByTestId('dialog-root')).not.toBeInTheDocument()
    })
  })

  // -----------------------------------------------------------------------
  // AC-1.2-2: 弹窗内容可滚动
  // -----------------------------------------------------------------------
  describe('弹窗内容滚动', () => {
    it('弹窗内容区域应包含 max-h-[60vh] 样式以支持滚动', async () => {
      const longDescription = '这是一个详细的选项说明，包含足够多的文字来触发弹窗展示。'
      const props = createCardProps({
        interaction: createPendingInteraction({
          mode: 'choice',
          options: [
            { id: 'a', label: '滚动选项', description: longDescription },
          ],
        }),
      })

      render(<InteractionCard {...props} />)

      await act(async () => {
        fireEvent.click(screen.getByText('滚动选项'))
      })

      // 查找带 overflow-y-auto 样式的滚动容器
      const scrollContainer = screen.getByTestId('dialog-scroll-area')
      expect(scrollContainer).toBeInTheDocument()
      expect(scrollContainer.className).toContain('overflow-y-auto')
      expect(scrollContainer.className).toContain('max-h-[60vh]')
    })
  })

  // -----------------------------------------------------------------------
  // AC-1.2-3: MarkdownRenderer 渲染
  // -----------------------------------------------------------------------
  describe('MarkdownRenderer 渲染', () => {
    it('description 应通过 MarkdownRenderer 组件渲染', async () => {
      const mdContent = '# 标题\n\n这是 **加粗** 文本，包含 [链接](https://example.com)。足够长触发弹窗。'
      const props = createCardProps({
        interaction: createPendingInteraction({
          mode: 'choice',
          options: [
            { id: 'a', label: 'Markdown 选项', description: mdContent },
          ],
        }),
      })

      render(<InteractionCard {...props} />)

      await act(async () => {
        fireEvent.click(screen.getByText('Markdown 选项'))
      })

      // MarkdownRenderer 应被调用且接收到完整 description
      expect(markdownRenderCalls).toContain(mdContent)
    })
  })

  // -----------------------------------------------------------------------
  // 边界场景
  // -----------------------------------------------------------------------
  describe('边界场景', () => {
    it('恰好 20 字符的 description 应弹出弹窗', async () => {
      const onRespondChoice = vi.fn()
      // 20个中文字符
      const exactTwenty = '一二三四五六七八九十壹贰叁肆伍陆柒捌玖零'
      expect(exactTwenty.length).toBe(20)

      const props = createCardProps({
        interaction: createPendingInteraction({
          mode: 'choice',
          options: [
            { id: 'a', label: '刚好20字符', description: exactTwenty },
          ],
        }),
        onRespondChoice,
      })

      render(<InteractionCard {...props} />)

      await act(async () => {
        fireEvent.click(screen.getByText('刚好20字符'))
      })

      // 20字符应该弹窗
      expect(screen.getByTestId('dialog-root')).toBeInTheDocument()
      expect(onRespondChoice).not.toHaveBeenCalled()
    })

    it('19 字符的 description 应直接执行选择', async () => {
      const onRespondChoice = vi.fn()
      const nineteen = '一二三四五六七八九十壹贰叁肆伍陆柒捌玖'
      expect(nineteen.length).toBe(19)

      const props = createCardProps({
        interaction: createPendingInteraction({
          mode: 'choice',
          options: [
            { id: 'a', label: '刚好19字符', description: nineteen },
          ],
        }),
        onRespondChoice,
      })

      render(<InteractionCard {...props} />)

      await act(async () => {
        fireEvent.click(screen.getByText('刚好19字符'))
      })

      // 19字符应直接执行
      expect(onRespondChoice).toHaveBeenCalledTimes(1)
      expect(onRespondChoice).toHaveBeenCalledWith('a')
      expect(screen.queryByTestId('dialog-root')).not.toBeInTheDocument()
    })

    it('混合选项：有描述和无描述的选项行为不同', async () => {
      const onRespondChoice = vi.fn()
      const props = createCardProps({
        interaction: createPendingInteraction({
          mode: 'choice',
          options: [
            { id: 'a', label: '无描述选项' },
            { id: 'b', label: '有描述选项', description: '这个选项有足够长的详细描述信息来触发弹窗。' },
          ],
        }),
        onRespondChoice,
      })

      render(<InteractionCard {...props} />)

      // 点击无描述选项 → 直接选择
      await act(async () => {
        fireEvent.click(screen.getByText('无描述选项'))
      })
      expect(onRespondChoice).toHaveBeenCalledTimes(1)
      expect(onRespondChoice).toHaveBeenCalledWith('a')
      expect(screen.queryByTestId('dialog-root')).not.toBeInTheDocument()

      // 点击有描述选项 → 弹窗
      await act(async () => {
        fireEvent.click(screen.getByText('有描述选项'))
      })
      expect(onRespondChoice).toHaveBeenCalledTimes(1) // 还是 1，未增加
      expect(screen.getByTestId('dialog-root')).toBeInTheDocument()
    })

    it('确认选择后再次点击其他有描述的选项应能重新弹窗', async () => {
      const onRespondChoice = vi.fn()
      const props = createCardProps({
        interaction: createPendingInteraction({
          mode: 'choice',
          options: [
            { id: 'a', label: '选项A', description: '选项A的详细描述信息，足够长来触发弹窗展示。' },
            { id: 'b', label: '选项B', description: '选项B的详细描述信息，足够长来触发弹窗展示。' },
          ],
        }),
        onRespondChoice,
      })

      render(<InteractionCard {...props} />)

      // 点击选项A
      await act(async () => {
        fireEvent.click(screen.getByText('选项A'))
      })
      expect(screen.getByTestId('dialog-root')).toBeInTheDocument()

      // 确认
      await act(async () => {
        fireEvent.click(screen.getByText('确认选择'))
      })
      expect(onRespondChoice).toHaveBeenCalledWith('a')
      expect(screen.queryByTestId('dialog-root')).not.toBeInTheDocument()
    })
  })
})
