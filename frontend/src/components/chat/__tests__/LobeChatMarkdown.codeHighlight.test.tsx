/**
 * LobeChatMarkdown · 代码块高亮行为测试（shiki 懒化 2026-10-15）
 *
 * 契约：消息里的 fenced code 经真实渲染链路（@lobehub/ui Markdown → shiki 动态导入）
 * 最终呈现语法高亮——高亮引擎为按需加载（shiki 不在首屏 chunk），因此高亮结果异步
 * 到达：mount 后首帧无高亮，等待后出现带 token 配色的代码块。断用户可见效果
 * （高亮 DOM 出现），不断言 import 细节。
 */
import { render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { LobeChatMarkdown } from '../LobeChatMarkdown'

const CODE_SAMPLE = 'const answer = 42'

describe('LobeChatMarkdown 代码块高亮（shiki 动态导入路径）', () => {
  it('fenced code 首帧无高亮，异步完成后呈现语法高亮 DOM', async () => {
    const { container } = render(
      <LobeChatMarkdown content={'```js\n' + CODE_SAMPLE + '\n```'} />,
    )

    // shiki 引擎按需加载：首帧（同步 render 后）不应已有 token 配色
    const pre = container.querySelector('pre')
    if (pre) {
      expect(pre.querySelectorAll('span[style*="color"]').length).toBe(0)
    }

    // 异步高亮到达：代码文本渲染出来，且带语法着色（用户可见的高亮效果）
    await waitFor(
      () => {
        expect(screen.getByText(/answer/)).toBeInTheDocument()
        const highlighted = container.querySelectorAll('span[style*="color"]')
        expect(highlighted.length, '应出现带内联配色的语法 token').toBeGreaterThanOrEqual(2)
      },
      { timeout: 8000 },
    )
  })
})
