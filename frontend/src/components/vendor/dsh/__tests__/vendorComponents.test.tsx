/**
 * DSH vendor 组件渲染冒烟测试（task_dsh_plugin_adapter 任务 3）。
 *
 * 验证移植组件在灵汐环境（jsdom + dsh-tokens.css 经 vite 注入）可挂载渲染：
 * 六张卡 + Pill/StateDot 各出关键 DOM 锚点；CSS Modules 类名注入由 Vite
 * 测试管线保证（.module.css import 不抛即通过）。
 */
import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import {
  CodeBlock,
  DiffBlock,
  JsonTree,
  Pill,
  ReadBlock,
  SearchBlock,
  StateDot,
  TerminalBlock,
  WebBlock,
} from '../index'

describe('DiffBlock', () => {
  it('渲染增删行 + 文件计数 footer', () => {
    render(<DiffBlock diffs={[{ path: 'a.ts', oldText: 'x', newText: 'y\nz' }]} />)
    expect(screen.getByText('a.ts')).toBeTruthy()
    expect(screen.getByText(/└ \+2 -1 · 1 file/)).toBeTruthy()
  })

  it('空 diffs 渲染 null', () => {
    const { container } = render(<DiffBlock diffs={[]} />)
    expect(container.firstChild).toBeNull()
  })
})

describe('ReadBlock', () => {
  it('渲染行号 gutter 与窗口计数', () => {
    render(
      <ReadBlock
        label="src/app.ts"
        lines={[{ number: 41, text: 'const x = 1' }, { number: 42, text: 'export default x' }]}
        totalLines={100}
        lang="ts"
      />,
    )
    expect(screen.getByText('41')).toBeTruthy()
    expect(screen.getByText('export default x')).toBeTruthy()
    expect(screen.getByText('显示 2 / 100 行')).toBeTruthy()
    expect(screen.getByText('ts')).toBeTruthy()
  })

  it('超长内容截断为 head/tail 窗口，不提供卡片内展开全量', () => {
    const lines = Array.from({ length: 40 }, (_, i) => ({ number: i + 1, text: `line-${i + 1}` }))
    render(<ReadBlock label="big.txt" lines={lines} totalLines={40} />)

    // head/tail 窗口行可见
    expect(screen.getByText('line-1')).toBeTruthy()
    expect(screen.getByText('line-40')).toBeTruthy()
    // 中间行被截断
    expect(screen.queryByText('line-20')).toBeNull()
    // 静态省略行提示截断量
    expect(screen.getByText('… 其余 24 行')).toBeTruthy()
    // 完整内容走工具卡片头部的"打开文件"入口，卡片内无展开/收起按钮
    expect(screen.queryByRole('button', { name: /展开其余/ })).toBeNull()
    expect(screen.queryByRole('button', { name: '收起' })).toBeNull()
  })
})

describe('TerminalBlock', () => {
  it('渲染命令行 + ANSI 输出 + 干净退出无状态 pill', () => {
    render(
      <TerminalBlock
        command="echo hi"
        cwd="/home/user/project"
        output={'hi\n'}
        exitCode={0}
      />,
    )
    expect(screen.getByText('echo hi')).toBeTruthy()
    expect(screen.getByText('hi')).toBeTruthy()
    expect(screen.queryByText(/退出码/)).toBeNull()
  })

  it('非零退出渲染状态 pill；无输出渲染占位', () => {
    render(<TerminalBlock command="false" output="" exitCode={1} />)
    expect(screen.getByText('退出码 1')).toBeTruthy()
    expect(screen.getByText('无输出')).toBeTruthy()
  })

  it('ANSI 颜色 run 解析为带 style 的 span（ansiToJson 替代层）', () => {
    const { container } = render(
      <TerminalBlock command="c" output={'\u001b[31mred\u001b[0m plain'} exitCode={0} />,
    )
    const styled = container.querySelector('span[style*="color"]')
    expect(styled?.textContent).toBe('red')
    expect(screen.getByText(/plain/)).toBeTruthy()
  })
})

describe('SearchBlock', () => {
  it('matches 形态：文件头 + 行号匹配 + 汇总', () => {
    render(
      <SearchBlock
        kind="matches"
        files={[{ path: 'a.ts', matches: [{ lineNumber: 3, line: 'hit line' }] }]}
        truncated={false}
        total={1}
      />,
    )
    expect(screen.getByText('a.ts')).toBeTruthy()
    expect(screen.getByText('3:')).toBeTruthy()
    expect(screen.getByText(/1 处匹配 · 1 个文件/)).toBeTruthy()
  })

  it('paths 形态：平铺路径 + 计数', () => {
    render(<SearchBlock kind="paths" paths={['x.ts', 'y.ts']} truncated total={5} />)
    expect(screen.getByText('x.ts')).toBeTruthy()
    expect(screen.getByText(/显示 2 \/ 共 5 个路径/)).toBeTruthy()
  })
})

describe('WebBlock', () => {
  it('search 形态：来源链接 + hostname 兜底标签', () => {
    render(
      <WebBlock
        kind="search"
        answer=""
        sources={[{ url: 'https://example.com/page', snippet: 's' }]}
        truncated={false}
      />,
    )
    const link = screen.getByRole('link', { name: 'example.com' })
    expect(link.getAttribute('href')).toBe('https://example.com/page')
  })

  it('fetch 形态：URL + HTTP 状态', () => {
    render(<WebBlock kind="fetch" url="https://a.dev/x" statusCode={204} truncated />)
    expect(screen.getByText('https://a.dev/x')).toBeTruthy()
    expect(screen.getByText('HTTP 204')).toBeTruthy()
    expect(screen.getByText('内容已截断')).toBeTruthy()
  })

  it('javascript: URL 不落 href（安全白名单）', () => {
    render(<WebBlock kind="fetch" url="javascript:alert(1)" statusCode={200} truncated={false} />)
    expect(screen.queryByRole('link')).toBeNull()
  })
})

describe('JsonTree', () => {
  it('渲染顶层展开 + 原始值着色', () => {
    render(<JsonTree data={{ name: 'x', count: 2, ok: true }} />)
    expect(screen.getByText('name:')).toBeTruthy()
    expect(screen.getByText('"x"')).toBeTruthy()
    expect(screen.getByText('2')).toBeTruthy()
    expect(screen.getByText('true')).toBeTruthy()
  })
})

describe('CodeBlock', () => {
  it('渲染语言横幅 + 代码体', () => {
    const { container } = render(<CodeBlock code={'const a = 1\n'} lang="ts" />)
    expect(screen.getByText('ts')).toBeTruthy()
    // Prism 把代码切成多个 token span，用整体文本断言
    expect(container.textContent).toContain('const')
    expect(container.textContent).toContain('1')
  })
})

describe('Pill / StateDot', () => {
  it('Pill 静态/可交互两态', () => {
    const { container } = render(
      <>
        <Pill>static</Pill>
        <Pill onClick={() => {}}>btn</Pill>
      </>,
    )
    expect(container.querySelector('span')?.textContent).toBe('static')
    expect(container.querySelector('button')?.textContent).toBe('btn')
  })

  it('StateDot 三态 DOM 锚点', () => {
    const { container, rerender } = render(<StateDot state="done" />)
    expect(container.querySelector('[data-state="done"]')).toBeTruthy()
    rerender(<StateDot state="error" />)
    expect(container.querySelector('[data-state="error"]')).toBeTruthy()
    rerender(<StateDot state="ongoing" />)
    expect(container.querySelector('[data-state="ongoing"]')).toBeTruthy()
  })
})
