/**
 * ActivityCard 侧「工具卡片 UI 优化」补充验证测试（验证用，非正式回归套件）
 *
 * 覆盖场景5（ActivityCard 侧）：
 * - status='failed' 的活动卡片默认折叠（不再自动展开），点击头部才展开查看错误详情
 * - json 详情块含换行类（break-all whitespace-pre-wrap）+ 纵向滚动（max-h-40 overflow-y-auto）
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import ActivityCard from '../ActivityCard'
import type { ActivityData } from '@/types/activity'

// Mock 外部依赖（ActivityCard 依赖较重，仅保留被测行为需要的最小依赖）
vi.mock('@/components/approval', () => ({
  TextDiffView: () => null,
}))

vi.mock('@/components/chat/markdown/MarkdownRenderer', () => ({
  MarkdownRenderer: () => null,
}))

vi.mock('@/utils/toolCardRegistry', () => ({
  getGlobalOpenFileCallback: () => () => {},
}))

function makeActivity(overrides: Partial<ActivityData> = {}): ActivityData {
  return {
    type: 'tool_call',
    id: 'act-1',
    title: 'search 工具调用',
    status: 'failed',
    ...overrides,
  }
}

describe('AC-工具卡片UI-场景5: ActivityCard 侧（失败默认折叠 + json 详情块滚动样式）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('status=failed 的活动卡片默认折叠：错误详情不可见，点击头部后才可见', () => {
    render(
      <ActivityCard
        activity={makeActivity({
          status: 'failed',
          error: '上游服务不可达：连接超时',
        })}
      />,
    )

    // 默认折叠：错误详情不可见
    expect(screen.queryByText(/上游服务不可达/)).not.toBeInTheDocument()

    // 点击卡片头部（标题所在区域）→ 展开，错误详情可见
    fireEvent.click(screen.getByText('search 工具调用'))
    expect(screen.getByText(/上游服务不可达/)).toBeInTheDocument()
  })

  it('json 详情块应含语义换行类（break-words whitespace-pre-wrap）与纵向滚动（max-h-40 overflow-y-auto），不含 break-all', () => {
    render(
      <ActivityCard
        activity={makeActivity({
          status: 'completed',
          details: [
            {
              id: 'd1',
              label: '返回数据',
              contentType: 'json',
              content: JSON.stringify({ key: 'x'.repeat(3000) }),
            },
          ],
        })}
      />,
    )

    // 展开卡片
    fireEvent.click(screen.getByText('search 工具调用'))

    // 找到详情块 pre 元素并断言样式契约
    const pre = document.querySelector('pre')
    expect(pre).toBeInTheDocument()
    expect(pre!.className).toContain('max-h-40')
    expect(pre!.className).toContain('overflow-y-auto')
    // break-words（overflow-wrap: break-word）：中文按语义换行，仅超长词断词
    expect(pre!.className).toContain('break-words')
    // 禁止 break-all（word-break: break-all 强制每字符断行 → 中文每字一行）
    expect(pre!.className).not.toContain('break-all')
    expect(pre!.className).toContain('whitespace-pre-wrap')
  })

  it('text 类型详情块（执行输出）应含纵向滚动（max-h-40 overflow-y-auto）与语义换行（break-words）', () => {
    render(
      <ActivityCard
        activity={makeActivity({
          status: 'completed',
          details: [
            {
              id: 'd3',
              label: '执行输出',
              contentType: 'text',
              content: 'line\n'.repeat(500),
            },
          ],
        })}
      />,
    )

    // 展开卡片
    fireEvent.click(screen.getByText('search 工具调用'))

    // 找到 text 类型详情块 pre 元素并断言样式契约
    const pre = document.querySelector('pre')
    expect(pre).toBeInTheDocument()
    expect(pre!.className).toContain('max-h-40')
    expect(pre!.className).toContain('overflow-y-auto')
    expect(pre!.className).toContain('break-words')
    expect(pre!.className).not.toContain('break-all')
  })

  it('status=failed 的活动卡片不因失败而自动展开（defaultExpanded 缺省 false）', () => {
    render(
      <ActivityCard
        activity={makeActivity({
          status: 'failed',
          error: '执行失败',
          details: [
            { id: 'd2', label: '详情', contentType: 'text', content: '失败明细' },
          ],
        })}
      />,
    )

    // 失败状态但默认折叠：错误与详情均不可见
    expect(screen.queryByText(/执行失败/)).not.toBeInTheDocument()
    expect(screen.queryByText(/失败明细/)).not.toBeInTheDocument()
  })

  it('展开后 error 错误信息 pre 应含滚动类（max-h + overflow-y-auto + break-words），不含 break-all（三轮修复）', () => {
    render(
      <ActivityCard
        activity={makeActivity({
          status: 'failed',
          error: '上游服务不可达：' + 'x'.repeat(2000),
        })}
      />,
    )

    // 展开卡片显示错误详情
    fireEvent.click(screen.getByText('search 工具调用'))

    const errorPre = document.querySelector('pre.text-status-error')
    expect(errorPre).toBeInTheDocument()
    // 超长错误信息统一滚动（任何层级展开都生效）
    expect(errorPre!.className).toMatch(/max-h-/)
    expect(errorPre!.className).toContain('overflow-y-auto')
    expect(errorPre!.className).toContain('break-words')
    expect(errorPre!.className).not.toContain('break-all')
  })

  it('markdown 详情块容器应含滚动类（max-h + overflow-y-auto），超长内容可滚动浏览（三轮修复）', () => {
    render(
      <ActivityCard
        activity={makeActivity({
          status: 'completed',
          details: [
            {
              id: 'd-md',
              label: 'Markdown 结果',
              contentType: 'markdown',
              content: '# 标题\n\n' + '长内容'.repeat(500),
            },
          ],
        })}
      />,
    )

    // 展开卡片显示 markdown 详情
    fireEvent.click(screen.getByText('search 工具调用'))

    // 找到 markdown 分支的容器（max-w-none 是 markdown 分支特征）
    const mdContainer = Array.from(document.querySelectorAll('div')).find(
      (el) => el.className.includes('max-w-none'),
    )
    expect(mdContainer).toBeInTheDocument()
    expect(mdContainer!.className).toMatch(/max-h-/)
    expect(mdContainer!.className).toContain('overflow-y-auto')
  })
})
