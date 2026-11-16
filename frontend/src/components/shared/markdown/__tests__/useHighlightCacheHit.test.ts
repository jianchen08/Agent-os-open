// @feature FP-T12 前端组件补测
/**
 * useHighlight 挂载首帧缓存命中测试(patch-lobehub-ui 行为锚)
 *
 * 缺陷(工作区 MD 预览代码块闪烁根因):
 * @lobehub/ui useHighlight 的高亮状态 data 初值恒为空——即使模块级 highlightCache
 * 命中(promise 已 resolved),setData 也要等 useEffect(paint 之后)→ 下一帧才
 * 切换成高亮 HTML。代码块组件每次挂载都先渲一帧无高亮纯文本 fallback:
 * - 工作区 MD 预览(CodeEditor 懒加载)每次打开/切换文件全部代码块重挂 → 每次可见闪烁;
 *   冷启动首开还要动态加载 shiki 引擎,纯文本期数百 ms。
 * - 消息流消息行常驻 DOM 不重挂,所以无感。
 *
 * 修复(patch-lobehub-ui.mjs 对 useHighlight.mjs 的旁路):resolved html 同步写入
 * highlightHtmlCache,挂载首帧 useState 初值直接命中 → 缓存命中挂载零闪烁。
 *
 * 断言形态说明:在宿主组件渲染函数内记录渲染序列(renderLog)——首笔即首帧
 * 渲染值,发生在 commit 之前,不受 jsdom 宏任务时序影响(renderHook 的 act 会
 * drain 微任务,DOM 层首帧也不可观测)。消费形态贴近 StaticRenderer 的
 * dangerouslySetInnerHTML。
 */

import { createElement, type ReactNode } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, describe, expect, it } from 'vitest'
import { useHighlight } from '@lobehub/ui/es/hooks/useHighlight'

;(globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = false

const PY_CODE = ['def greet():', '    return "hi"', ''].join('\n')

/** 每次挂载的渲染序列记录(宿主组件渲染函数内采集,首笔=首帧渲染值) */
function makeRenderLog() {
  const log: string[] = []
  function Host({ code, language }: { code: string; language: string }) {
    const html = useHighlight(code, { language })
    log.push(html)
    return createElement('div', {
      className: html ? 'with-highlight' : 'fallback-frame',
      dangerouslySetInnerHTML: { __html: html || `<pre><code>${code}</code></pre>` },
    })
  }
  return { log, Host }
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

/** 等待渲染序列出现高亮结果(真实 shiki 高亮为异步链) */
async function waitForHighlighted(log: string[], timeoutMs = 15000): Promise<void> {
  const start = Date.now()
  for (;;) {
    if (log.some((html) => html.includes('shiki'))) return
    if (Date.now() - start > timeoutMs) throw new Error(`waitForHighlighted timeout: ${JSON.stringify(log)}`)
    await sleep(25)
  }
}

describe('useHighlight 挂载首帧缓存命中', () => {
  afterEach(() => {
    document.body.innerHTML = ''
  })

  /** 挂载宿主并等待高亮完成(真实提交,渲染值记入 log) */
  async function mountUntilHighlighted(
    code: string,
    log: string[],
    Host: (props: { code: string; language: string }) => ReactNode,
  ): Promise<Root> {
    const container = document.createElement('div')
    document.body.appendChild(container)
    const root = createRoot(container)
    root.render(createElement(Host, { code, language: 'python' }))
    await waitForHighlighted(log)
    return root
  }

  it('同内容二次挂载:首帧(首笔渲染)即高亮 HTML,不再先渲纯文本 fallback(闪烁根修锚)', async () => {
    // ── 冷启动(填缓存):首帧必须仍是空(fallback 纯文本),异步后切高亮 ──
    const cold = makeRenderLog()
    const coldRoot = await mountUntilHighlighted(PY_CODE, cold.log, cold.Host)
    expect(cold.log[0], '冷启动首帧应为空(fallback 纯文本,高亮前唯一可显示的内容)').toBe('')
    expect(cold.log.some((html) => html.includes('shiki')), '异步高亮最终应到达').toBe(true)
    coldRoot.unmount()

    // ── 卸载重挂(缓存命中):必须一次渲染即高亮,无空帧 ──
    const second = makeRenderLog()
    const container = document.createElement('div')
    document.body.appendChild(container)
    const secondRoot = createRoot(container)
    secondRoot.render(createElement(second.Host, { code: PY_CODE, language: 'python' }))
    // 首帧断言不等待任何异步:commit 前的首笔渲染值
    await sleep(0)
    expect(
      second.log[0],
      `缓存命中挂载首帧应直接是高亮 HTML(实际渲染序列: ${JSON.stringify(second.log.map((h) => h.slice(0, 40)))})`,
    ).toContain('shiki')
    expect(second.log, '缓存命中挂载不应出现空字符串帧(即用户可见的闪烁帧)').not.toContain('')
    secondRoot.unmount()
  })

  it('冷启动保持 fallback 语义:首帧空(fallback),高亮异步到达', async () => {
    const code = 'const x = 1 // cold-start unique sample'
    const cold = makeRenderLog()
    const container = document.createElement('div')
    document.body.appendChild(container)
    const root = createRoot(container)
    root.render(createElement(cold.Host, { code, language: 'javascript' }))
    await sleep(0)
    expect(cold.log[0], '冷启动首帧应为空(fallback 纯文本)').toBe('')
    await waitForHighlighted(cold.log)
    root.unmount()
  })
})
