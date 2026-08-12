/**
 * PageShell 测试 —— 统一页面外壳契约
 *
 * 意图：统一审查 §一/§4.2。PageShell 是页面统一的最小承载，必须：
 * - 返回用 SPA 导航（Link），不产生整页刷新（修 N1）
 * - 三态插槽：loading/error/empty（修 P4，状态覆盖维度二 Must Fix）
 * - embedded 模式不渲染 header/back（吸收 settings 子页 fork 差异）
 * - density 档位控制 header 密度（compact 36px / comfortable 40px）
 */

import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { PageShell } from '../PageShell'

function wrap(node: React.ReactNode) {
  return render(<MemoryRouter>{node}</MemoryRouter>)
}

describe('PageShell', () => {
  it('渲染标题、描述、操作区', () => {
    wrap(
      <PageShell title="工具管理" description="配置工具" actions={<button>新增</button>}>
        <div>内容</div>
      </PageShell>,
    )
    expect(screen.getByText('工具管理')).toBeInTheDocument()
    expect(screen.getByText('配置工具')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '新增' })).toBeInTheDocument()
  })

  it('返回是 react-router Link（SPA 导航，修 N1 整页刷新）', () => {
    // 源码层用 <Link>（非裸 <a href>）由 lint no-restricted-syntax + no-drift 契约保证；
    // 此处验证返回元素存在且为链接。Link 在 MemoryRouter 下渲染为 <a to>。
    wrap(<PageShell title="t">x</PageShell>)
    const back = screen.getByText(/返回/)
    expect(back.tagName).toBe('A')
  })

  it('backHref 透传到 Link', () => {
    wrap(<PageShell title="t" backHref="/settings">x</PageShell>)
    expect(screen.getByText(/返回/).closest('a')).toHaveAttribute('href', '/settings')
  })

  it('loading 态渲染骨架占位（不渲染 children）', () => {
    wrap(
      <PageShell title="t" loading>
        <div>真实内容</div>
      </PageShell>,
    )
    expect(screen.queryByText('真实内容')).not.toBeInTheDocument()
  })

  it('error 态渲染错误占位', () => {
    wrap(
      <PageShell title="t" error="加载失败">
        <div>真实内容</div>
      </PageShell>,
    )
    expect(screen.getByText('加载失败')).toBeInTheDocument()
    expect(screen.queryByText('真实内容')).not.toBeInTheDocument()
  })

  it('isEmpty + empty 渲染空节点（不渲染 children）', () => {
    wrap(
      <PageShell title="t" isEmpty empty={<div>暂无数据</div>}>
        <div>真实内容</div>
      </PageShell>,
    )
    expect(screen.getByText('暂无数据')).toBeInTheDocument()
    expect(screen.queryByText('真实内容')).not.toBeInTheDocument()
  })

  it('embedded 模式不渲染 header/back（吸收 settings 子页 fork）', () => {
    const { container } = wrap(
      <PageShell title="不应出现的标题" embedded>
        <div>内容</div>
      </PageShell>,
    )
    expect(screen.queryByText(/不应出现的标题/)).not.toBeInTheDocument()
    expect(screen.queryByText(/返回/)).not.toBeInTheDocument()
    expect(screen.getByText('内容')).toBeInTheDocument()
  })
})
