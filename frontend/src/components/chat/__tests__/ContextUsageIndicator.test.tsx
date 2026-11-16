/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * 功能测试：ContextUsageIndicator 上下文用量指示器（圈型进度 + 用量明细浮窗）
 *
 * 主条头部：圆环本身作模型名前的图标槽位（无 Database 图标）。
 * 语义沿用：>=90% error 红 / >=70% warning 黄 / 其余 success 绿；
 * maxTokens<=0 时圆环仍在但不带进度语义（无 progressbar role/aria、弧为空）
 * 且不显示数字（不展示假进度）；compact（小尺寸）同样渲染圆环。
 *
 * 浮窗：悬停或点击主条弹出（上下文使用量 + 本轮明细 + 管道累计明细），
 * 移入浮窗保持打开，移出关闭；无数据的明细行/段不渲染。
 */

import { act, fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ContextUsageIndicator } from '../ContextUsageIndicator'

/** 打开浮窗（悬停主条） */
const openPopover = () => {
  fireEvent.mouseEnter(screen.getByTestId('context-usage-indicator'))
}

describe('ContextUsageIndicator — 圈型进度', () => {
  it('maxTokens>0 时渲染圆环进度（role=progressbar，值正确），主条显示紧凑数字', () => {
    render(
      <ContextUsageIndicator modelName="deepseek-v3" currentTokenUsage={4000} maxTokens={10000} />,
    )

    const ring = screen.getByRole('progressbar')
    expect(ring).toBeInTheDocument()
    expect(ring).toHaveAttribute('aria-valuenow', '40')
    expect(ring).toHaveAttribute('aria-valuemin', '0')
    expect(ring).toHaveAttribute('aria-valuemax', '100')
    // 旧横向进度条结构已移除
    expect(screen.queryByTestId('context-usage-bar')).not.toBeInTheDocument()
    // 圆环是主条头部第一个元素（原 Database 图标槽位），Database 图标已移除
    const indicator = screen.getByTestId('context-usage-indicator')
    expect(indicator.firstElementChild).toBe(ring)
    expect(document.querySelector('.database-icon')).not.toBeInTheDocument()
    // 主条显示紧凑数字（千分位完整数字只出现在浮窗）
    expect(indicator).toHaveTextContent('4.0k / 10.0k')
  })

  it('紧凑数字格式：十万级无小数、百万级 M、千以下原样', () => {
    const { rerender } = render(
      <ContextUsageIndicator modelName="m" currentTokenUsage={123456} maxTokens={128000} />,
    )
    expect(screen.getByTestId('context-usage-indicator')).toHaveTextContent('123k / 128k')

    rerender(<ContextUsageIndicator modelName="m" currentTokenUsage={2500000} maxTokens={4000000} />)
    expect(screen.getByTestId('context-usage-indicator')).toHaveTextContent('2.5M / 4.0M')

    rerender(<ContextUsageIndicator modelName="m" currentTokenUsage={900} maxTokens={2000} />)
    expect(screen.getByTestId('context-usage-indicator')).toHaveTextContent('900 / 2.0k')
  })

  it('maxTokens<=0：圆环仍在作图标槽位，但不带进度语义（无 progressbar role/aria 值）、主条无数字', () => {
    render(<ContextUsageIndicator modelName="deepseek-v3" currentTokenUsage={0} maxTokens={0} />)
    // 窗口未知圆环仍渲染（模型名前的图标槽位），只是不假装有进度
    const ring = screen.getByTestId('context-usage-ring')
    expect(ring).toBeInTheDocument()
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
    expect(ring).not.toHaveAttribute('role')
    expect(ring).not.toHaveAttribute('aria-valuenow')
    const indicator = screen.getByTestId('context-usage-indicator')
    expect(indicator).not.toHaveTextContent('/ ')
    // Database 图标不再出现（圆环即图标槽位）
    expect(document.querySelector('.database-icon')).not.toBeInTheDocument()
  })
})

describe('ContextUsageIndicator — 用量明细浮窗', () => {
  it('悬停打开：显示上下文使用量（完整数字+百分比+进度条）；移出关闭', async () => {
    render(
      <ContextUsageIndicator
        modelName="deepseek-v3"
        currentTokenUsage={4000}
        maxTokens={10000}
        totalTokens={6500}
        completionTokens={2500}
        cachedTokens={2880}
        hitRatio={0.72}
      />,
    )
    expect(screen.queryByTestId('context-usage-popover')).not.toBeInTheDocument()

    openPopover()
    const popover = screen.getByTestId('context-usage-popover')
    expect(popover).toHaveTextContent('4,000 / 10,000（40%）')
    expect(screen.getByTestId('context-usage-popover-bar')).toBeInTheDocument()

    fireEvent.mouseLeave(screen.getByTestId('context-usage-indicator'))
    expect(screen.queryByTestId('context-usage-popover')).not.toBeInTheDocument()
  })

  it('点击切换开合：再次点击关闭', () => {
    render(<ContextUsageIndicator modelName="m" currentTokenUsage={1000} maxTokens={2000} />)
    const indicator = screen.getByTestId('context-usage-indicator')

    fireEvent.click(indicator)
    expect(screen.getByTestId('context-usage-popover')).toBeInTheDocument()
    expect(indicator).toHaveAttribute('aria-expanded', 'true')

    fireEvent.click(indicator)
    expect(screen.queryByTestId('context-usage-popover')).not.toBeInTheDocument()
  })

  it('本轮明细：输入/输出/总计/缓存命中（含命中率）；移入浮窗不关闭', () => {
    render(
      <ContextUsageIndicator
        modelName="deepseek-v3"
        currentTokenUsage={4000}
        maxTokens={10000}
        totalTokens={6500}
        completionTokens={2500}
        cachedTokens={2880}
        hitRatio={0.72}
      />,
    )

    openPopover()
    // 悬停移入浮窗（先离主条、再入浮窗的物理顺序）。真实浏览器两事件同批处理
    // 不闪关；jsdom 的 fireEvent 逐个刷新渲染，用 act 包成同一批次复现该语义
    act(() => {
      fireEvent.mouseLeave(screen.getByTestId('context-usage-indicator'))
      fireEvent.mouseEnter(screen.getByTestId('context-usage-popover'))
    })
    expect(screen.getByTestId('context-usage-popover')).toBeInTheDocument()

    const popover = screen.getByTestId('context-usage-popover')
    expect(popover).toHaveTextContent('本轮')
    expect(popover).toHaveTextContent('4,000')
    expect(popover).toHaveTextContent('2,500')
    expect(popover).toHaveTextContent('6,500')
    expect(popover).toHaveTextContent('2,880 (72%)')
  })

  it('管道累计段：cumulative 有值才渲染，显示累计输入/输出/总计/缓存', () => {
    render(
      <ContextUsageIndicator
        modelName="deepseek-v3"
        currentTokenUsage={4000}
        maxTokens={10000}
        cumulative={{
          total_input: 120400,
          total_output: 30200,
          total_cached: 80000,
          missed: 40400,
          total_tokens: 150600,
          cache_hit_ratio: 0.66,
        }}
      />,
    )

    // 未悬停时无累计内容
    expect(screen.queryByText('本管道累计')).not.toBeInTheDocument()

    openPopover()
    const popover = screen.getByTestId('context-usage-popover')
    expect(popover).toHaveTextContent('本管道累计')
    expect(popover).toHaveTextContent('120,400')
    expect(popover).toHaveTextContent('30,200')
    expect(popover).toHaveTextContent('150,600')
    expect(popover).toHaveTextContent('80,000 (66%)')
  })

  it('无明细数据：浮窗只有上下文段，不渲染本轮/累计空段', () => {
    render(<ContextUsageIndicator modelName="m" currentTokenUsage={1000} maxTokens={2000} />)
    openPopover()
    const popover = screen.getByTestId('context-usage-popover')
    expect(popover).toHaveTextContent('1,000 / 2,000（50%）')
    expect(popover).not.toHaveTextContent('本轮')
    expect(popover).not.toHaveTextContent('本管道累计')
  })

  it('无明细数据且 maxTokens<=0：无浮窗触发数据，悬停仍打开但只有模型名上下文', () => {
    render(<ContextUsageIndicator modelName="m" currentTokenUsage={0} maxTokens={0} />)
    openPopover()
    const popover = screen.getByTestId('context-usage-popover')
    expect(popover).toHaveTextContent('上下文 · m')
    expect(screen.queryByTestId('context-usage-popover-bar')).not.toBeInTheDocument()
  })
})

describe('ContextUsageIndicator — 基础语义', () => {
  it('圆环颜色语义：>=90% error / >=70% warning / 其余 success', () => {
    // 进度圆环 = 第二个 circle（第一个是轨道）；SVG className 为 SVGAnimatedString，用 class 属性断言
    const progressCircleClass = () =>
      screen.getByRole('progressbar').querySelectorAll('circle')[1].getAttribute('class') ?? ''

    const { rerender } = render(
      <ContextUsageIndicator modelName="m" currentTokenUsage={9500} maxTokens={10000} />,
    )
    expect(progressCircleClass()).toContain('text-status-error')

    rerender(<ContextUsageIndicator modelName="m" currentTokenUsage={7500} maxTokens={10000} />)
    expect(progressCircleClass()).toContain('text-status-warning')

    rerender(<ContextUsageIndicator modelName="m" currentTokenUsage={3000} maxTokens={10000} />)
    expect(progressCircleClass()).toContain('text-status-success')
  })

  it('模型无效时显示占位，不渲染圆环与浮窗', () => {
    render(<ContextUsageIndicator modelName="unknown" />)
    expect(screen.getByTestId('context-usage-invalid')).toBeInTheDocument()
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
    expect(screen.queryByTestId('context-usage-ring')).not.toBeInTheDocument()
    expect(screen.queryByTestId('context-usage-indicator')).not.toBeInTheDocument()
  })

  it('compact 模式（小尺寸）同样渲染圆环', () => {
    render(
      <ContextUsageIndicator compact modelName="m" currentTokenUsage={1000} maxTokens={2000} />,
    )
    expect(screen.getByRole('progressbar')).toBeInTheDocument()
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '50')
  })
})

describe('ContextUsageIndicator — 上下文压缩终态（用户裁定 2026-10-28）', () => {
  it('压缩失败：主条常驻「压缩失败」标记（不依赖一次性 toast）', () => {
    render(
      <ContextUsageIndicator
        modelName="glm-5.3-flash"
        currentTokenUsage={654479}
        maxTokens={1000000}
        compressionStatus={{ status: 'failed', reason: '压缩未产生有效收缩' }}
      />,
    )
    expect(screen.getByTestId('context-compression-failed')).toHaveTextContent('压缩失败')
  })

  it('压缩失败：纯圈模式用红点作常驻信号（无文案位）', () => {
    render(
      <ContextUsageIndicator
        modelName="glm-5.3-flash"
        currentTokenUsage={654479}
        maxTokens={1000000}
        ringOnly
        compressionStatus={{ status: 'failed' }}
      />,
    )
    expect(screen.getByTestId('context-compression-failed-dot')).toBeInTheDocument()
    expect(screen.queryByTestId('context-compression-failed')).not.toBeInTheDocument()
  })

  it('压缩失败：浮窗展示失败原因', () => {
    render(
      <ContextUsageIndicator
        modelName="glm-5.3-flash"
        currentTokenUsage={654479}
        maxTokens={1000000}
        compressionStatus={{ status: 'failed', reason: 'no healthy deployments' }}
      />,
    )
    openPopover()
    const row = screen.getByTestId('context-compression-status')
    expect(row).toHaveTextContent('失败')
    expect(row).toHaveTextContent('no healthy deployments')
  })

  it('压缩成功（applied）：不显示失败标记，浮窗显示正常', () => {
    render(
      <ContextUsageIndicator
        modelName="glm-5.3-flash"
        currentTokenUsage={200000}
        maxTokens={1000000}
        compressionStatus={{ status: 'applied' }}
      />,
    )
    expect(screen.queryByTestId('context-compression-failed')).not.toBeInTheDocument()
    expect(screen.queryByTestId('context-compression-failed-dot')).not.toBeInTheDocument()
    openPopover()
    expect(screen.getByTestId('context-compression-status')).toHaveTextContent('正常')
  })

  it('无压缩状态：不渲染压缩段（不发明状态）', () => {
    render(
      <ContextUsageIndicator modelName="m" currentTokenUsage={1000} maxTokens={10000} />,
    )
    expect(screen.queryByTestId('context-compression-failed')).not.toBeInTheDocument()
    openPopover()
    expect(screen.queryByTestId('context-compression-status')).not.toBeInTheDocument()
  })
})
