/**
 * 回归测试：YAML 等缩进敏感语言在文件标签页中的渲染
 *
 * 背景：react-syntax-highlighter@16 在同时开启 showLineNumbers + wrapLongLines 时，
 * 会在每行 span 上强制注入 `display: flex`（见其 highlight.js 的
 * `wrapLongLines & showLineNumbers` 分支）。flex 容器打破 inline 文本流并折叠前导
 * 空格（缩进），导致 yaml 渲染成"每行只剩几个字、像竖排"的不可读状态。
 *
 * 修复：通过 lineProps 注入 `display: block` 覆盖库的 `display: flex`，
 * 并用 `whiteSpace: pre-wrap` 保留缩进与换行。
 */
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { FilePreview } from '../FilePreview'

/** 含典型 YAML 缩进的示例内容 */
const YAML_CONTENT = [
  'agents:',
  '  main:',
  '    name: 灵犀',
  '    model:',
  '      provider: glm',
  '      name: glm-5.2',
].join('\n')

describe('FilePreview — YAML 渲染不塌缩为竖排', () => {
  it('行容器使用 display: block 而非库注入的 display: flex', () => {
    const { container } = render(
      <FilePreview
        filePath="config/agents.yaml"
        content={YAML_CONTENT}
        containerTaskId="task-1"
      />,
    )

    // SyntaxHighlighter 把每个代码行包成一个 span（含行号 + 代码内容）。
    // 修复前：这些行 span 被库强制设为 display: flex → 文本流塌缩、缩进丢失、呈竖排。
    // 修复后：lineProps 注入 display: block 覆盖了库的 flex。
    const codeRoot = container.querySelector('pre code')
    expect(codeRoot).not.toBeNull()

    const lineSpans = codeRoot!.querySelectorAll(':scope > span')
    expect(lineSpans.length).toBeGreaterThan(0)

    const flexLines = Array.from(lineSpans).filter(
      (el) => (el as HTMLElement).style.display === 'flex',
    )
    const blockLines = Array.from(lineSpans).filter(
      (el) => (el as HTMLElement).style.display === 'block',
    )

    expect(flexLines.length).toBe(0)
    expect(blockLines.length).toBe(lineSpans.length)
  })

  it('保留 YAML 缩进内容，可在 DOM 中完整读到嵌套键', () => {
    render(
      <FilePreview
        filePath="config/agents.yaml"
        content={YAML_CONTENT}
        containerTaskId="task-1"
      />,
    )

    // 缩进文本必须完整保留在 DOM 中（修复前 flex 折叠前导空格会破坏可读性）。
    const codeRoot = screen
      .getByText((_, node) => node?.tagName === 'CODE')
      .closest('pre')
    expect(codeRoot).not.toBeNull()
    expect(codeRoot!.textContent).toContain('agents:')
    expect(codeRoot!.textContent).toContain('name: 灵犀')
    expect(codeRoot!.textContent).toContain('provider: glm')
    expect(codeRoot!.textContent).toContain('name: glm-5.2')
  })
})
