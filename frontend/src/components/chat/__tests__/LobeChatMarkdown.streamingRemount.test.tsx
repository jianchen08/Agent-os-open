/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * LobeChatMarkdown · 流式输出代码块不重挂（2026-11-10 代码块闪烁根修）
 *
 * 缺陷：流式输出时每来一个 token，代码块就整棵重挂一次 → @lobehub/ui
 * useHighlight 的高亮状态（data）随卸载清零，而新内容（新缓存键）要走异步
 * 高亮 → 每 token 闪一帧无高亮纯文本 fallback，用户可见闪烁。
 *
 * 两条重挂来源，本文件各锁定一条：
 * 1. `components.pre` 内联箭头函数：每次渲染新身份，react-markdown 按组件类型
 *    引用协调元素 → pre 子树卸载重挂。修复 = 模块级稳定组件 + isStreaming 经
 *    context 下发（MarkdownPreGate），components 仅在流式门翻转时换新。
 * 2. 片段 key 含内容前 16 字（makeStablePartKey）：流式追加内容时 key 逐 token
 *    变化 → 消息级 markdown 整棵重挂（前 16 字内每 token 一次）。修复 = 键只含
 *    类型 + seq。
 *
 * 断言形态：以代码块容器 DOM 节点身份（[data-code-type="highlighter"]）是否
 * 被替换为判据——节点换了就是重挂，重挂必然清空高亮状态。
 */
import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { buildFragmentsFromParts } from '../hooks/useMessageRender'
import { LobeChatMarkdown } from '../LobeChatMarkdown'
import { MessageContentRenderer } from '../MessageContentRenderer'
import type { MessagePart } from '@/types/messageParts'
import type { Message } from '@/types/models'

const BASE = '```python\ndef greet():\n    return "hi"\n```'

/** 单 text part 的流式消息 → 渲染片段 */
function streamingFragments(content: string) {
  const message: Message = {
    id: 'msg-stream-remount-1',
    sessionId: 'session-remount',
    role: 'assistant',
    content: '',
    timestamp: '2026-01-01T00:00:00.000Z',
    parentId: null,
    status: 'streaming',
    parts: [{ type: 'text', content, state: 'streaming' } as MessagePart],
  }
  return buildFragmentsFromParts(message, undefined, { streaming: true })
}

describe('LobeChatMarkdown 流式代码块不重挂', () => {
  it('内容追加（下一个 token）不替换代码块 DOM 节点', () => {
    const { container, rerender } = render(<LobeChatMarkdown content={BASE} isStreaming />)
    const before = container.querySelector('[data-code-type="highlighter"]')
    expect(before, '首帧应有代码块容器').toBeTruthy()

    rerender(<LobeChatMarkdown content={BASE.replace('\n```', 'x\n```')} isStreaming />)

    const after = container.querySelector('[data-code-type="highlighter"]')
    expect(after, '重渲染后应仍有代码块容器').toBeTruthy()
    expect(after, '内容追加不应替换代码块节点（重挂 = 高亮状态清零 = 闪烁）').toBe(before)
  })

  it('消息级：围栏内容增长越过 16 字不替换代码块 DOM 节点', () => {
    const { container, rerender } = render(
      <MessageContentRenderer fragments={streamingFragments('```js\nx\n```')} isStreaming />,
    )
    const before = container.querySelector('[data-code-type="highlighter"]')
    expect(before, '首帧应有代码块容器').toBeTruthy()

    rerender(
      <MessageContentRenderer
        fragments={streamingFragments('```js\nx1234567890abc\n```')}
        isStreaming
      />,
    )

    const after = container.querySelector('[data-code-type="highlighter"]')
    expect(after, '重渲染后应仍有代码块容器').toBeTruthy()
    expect(after, '片段 key 不应随内容变化（否则消息级整棵重挂 → 闪烁）').toBe(before)
  })

  it('流式结束（流式门翻转）不替换代码块 DOM 节点', () => {
    const { container, rerender } = render(<LobeChatMarkdown content={BASE} isStreaming />)
    const before = container.querySelector('[data-code-type="highlighter"]')
    expect(before, '首帧应有代码块容器').toBeTruthy()

    // 流式门翻转时 components 对象换新（task-list 门键出现），但 pre 组件身份
    // 不变——代码块不重挂，完成时刻无「纯文本→高亮」跳变
    rerender(<LobeChatMarkdown content={BASE} isStreaming={false} />)

    const after = container.querySelector('[data-code-type="highlighter"]')
    expect(after, '重渲染后应仍有代码块容器').toBeTruthy()
    expect(after, '完成态切换不应替换代码块节点').toBe(before)
  })
})
