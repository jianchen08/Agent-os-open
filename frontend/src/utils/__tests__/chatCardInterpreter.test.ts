/**
 * 功能测试：chat_card 声明解释器（TC S3 翻译层 / chat inline 声明驱动）
 *
 * 推演链：愿景（插件声明能力）→ 前端愿景（工具卡片声明驱动）→ 架构（插件 ui.chat_card
 * 声明 → 翻译成现有 ActivityDetailBlock[] → ActivityCard 原样渲染，不重造块）→ 功能点
 * （"声明 {title 模板, blocks[]} + tool call 上下文 → 解释器产出 ActivityDetailBlock[]"）。
 *
 * 这是 S3 的前端翻译层（模板引擎 + source 求值 + when）。YAML 加载/后端透传（G1）是后端工作。
 * 本测试直接喂声明 + 上下文，断言产出的 ActivityDetailBlock[]——端到端验证翻译逻辑。
 */

import { describe, expect, it } from 'vitest'
import { evalPath, interpretChatCard } from '@/utils/chatCardInterpreter'
import type { ChatCardDeclaration } from '@/utils/chatCardInterpreter'

describe('功能点：interpretChatCard 把声明翻译成 ActivityDetailBlock[]', () => {
  it('title 模板 + 过滤器（first_line / truncate）求值', () => {
    const decl: ChatCardDeclaration = { title: '{{args.command | first_line | truncate:10}}' }
    const ctx = { args: { command: 'ls -la /very/long/path/here\nsecond line' } }
    const out = interpretChatCard(decl, ctx)
    expect(out.title).toBe('ls -la /ve…')
  })

  it('basename 过滤器取文件名', () => {
    const decl: ChatCardDeclaration = { title: '{{args.path | basename}}' }
    const out = interpretChatCard(decl, { args: { path: '/a/b/c.txt' } })
    expect(out.title).toBe('c.txt')
  })

  it('kv 块：fields 按 source 取值，缺失项跳过', () => {
    const decl: ChatCardDeclaration = {
      blocks: [
        {
          type: 'kv',
          label: '概览',
          fields: [
            { key: '源文', source: 'args.source' },
            { key: '译文', source: 'args.target' },
            { key: '缺失', source: 'args.missing' },
          ],
        },
      ],
    }
    const out = interpretChatCard(decl, { args: { source: 'hello', target: '你好' } })
    expect(out.details).toHaveLength(1)
    expect(out.details[0].contentType).toBe('kv')
    expect(out.details[0].kvItems).toEqual([
      { key: '源文', value: 'hello' },
      { key: '译文', value: '你好' },
    ])
  })

  it('file 块：source 求值为 path', () => {
    const decl: ChatCardDeclaration = {
      blocks: [{ type: 'file', label: '输出文件', source: 'args.out_path' }],
    }
    const out = interpretChatCard(decl, { args: { out_path: '/tmp/result.txt' } })
    expect(out.details[0].contentType).toBe('file')
    expect(out.details[0].path).toBe('/tmp/result.txt')
  })

  it('link 块：source 求值为 url', () => {
    const decl: ChatCardDeclaration = {
      blocks: [{ type: 'link', label: '文档', source: 'args.doc_url' }],
    }
    const out = interpretChatCard(decl, { args: { doc_url: 'https://x.com/y' } })
    expect(out.details[0].contentType).toBe('link')
    expect(out.details[0].url).toBe('https://x.com/y')
  })

  it('when 条件 falsy → 整块不渲染', () => {
    const decl: ChatCardDeclaration = {
      blocks: [
        { type: 'image', source: 'result.screenshot', when: 'result.screenshot' },
        { type: 'log', source: 'result.stdout' },
      ],
    }
    const out = interpretChatCard(decl, { result: { stdout: 'ok', screenshot: null } })
    // image 块 when=result.screenshot 为 null（falsy）→ 跳过；只剩 log
    expect(out.details).toHaveLength(1)
    expect(out.details[0].contentType).toBe('log')
  })

  it('result 的 Python dict 字符串经 safeParse 可被 source 取值', () => {
    const decl: ChatCardDeclaration = {
      blocks: [{ type: 'log', source: 'result.output' }],
    }
    // 后端常把 result 序列化成 "{'output': 'done'}" 形态
    const out = interpretChatCard(decl, { result: "{'output': 'done'}" })
    expect(out.details[0].contentType).toBe('log')
    expect(out.details[0].content).toBe('done')
  })

  it('code 块透传 language + collapsible', () => {
    const decl: ChatCardDeclaration = {
      blocks: [{ type: 'code', label: '命令', source: 'args.cmd', language: 'bash', collapsible: true }],
    }
    const out = interpretChatCard(decl, { args: { cmd: 'echo hi' } })
    expect(out.details[0]).toMatchObject({ contentType: 'code', language: 'bash', collapsible: true, content: 'echo hi' })
  })

  // ── 迁移内置工具引入的 interpreter 能力（TC T1） ──

  it('result 为非 JSON 纯文本（如 file_read/bash 输出）→ 回退原始字符串取值', () => {
    // safeParse 失败时不丢失内容：source 'result' 应得到原始字符串
    const decl: ChatCardDeclaration = { blocks: [{ type: 'code', source: 'result' }] }
    const out = interpretChatCard(decl, { result: 'plain text output\nline2' })
    expect(out.details[0].content).toBe('plain text output\nline2')
  })

  it('default 过滤器可兜底缺失（undefined）的 source', () => {
    // command 缺失 → first_line/truncate 得空串 → default 兜底
    const decl: ChatCardDeclaration = { title: '{{args.command | first_line | truncate:60 | default:执行命令}}' }
    const out = interpretChatCard(decl, {})
    expect(out.title).toBe('执行命令')
  })

  it('内容块 source 支持过滤器管道（如 truncate）', () => {
    const decl: ChatCardDeclaration = { blocks: [{ type: 'text', source: 'result | truncate:5' }] }
    const out = interpretChatCard(decl, { result: 'abcdefghij' })
    expect(out.details[0].content).toBe('abcde…')
  })

  it('filePathSource 求值非空 → 返回 filePath（供 enhance 注入点击打开）', () => {
    const decl: ChatCardDeclaration = { filePathSource: 'args.file_path' }
    expect(interpretChatCard(decl, { args: { file_path: '/a/b.ts' } }).filePath).toBe('/a/b.ts')
    expect(interpretChatCard(decl, { args: {} }).filePath).toBeUndefined()
  })

  // ── file_write 迁移引入的 interpreter 能力（TC T1 6/6） ──

  it('evalPath 支持 `||` 路径回退：返回第一个非 undefined 的备选', () => {
    // `||` 用于路径型字段（diffStat / diff 源 / when / unless，均经 evalPath），
    // 兼容「output 子层包装」与「扁平」两种数据形态
    // 形态一：output 包装 → output.added 命中
    expect(evalPath({ result: { output: { added: 2 } } }, 'output.added || result.added')).toBe(2)
    // 形态二：扁平（output 缺失）→ 回退 result.added
    expect(evalPath({ result: { added: 5 } }, 'output.added || result.added')).toBe(5)
    // 两者都缺 → undefined
    expect(evalPath({ result: {} }, 'output.added || result.added')).toBeUndefined()
    // 单路径（无 ||）保持原行为
    expect(evalPath({ args: { x: 9 } }, 'args.x')).toBe(9)
  })

  it('`||` 把空串视为有效非 undefined 值（不继续回退）', () => {
    // old_content='' 是有效正文（非 undefined），应返回 '' 而非继续找下一条
    const decl: ChatCardDeclaration = {
      blocks: [
        {
          type: 'diff',
          diffOldSource: 'output.old_content || result.old_content',
          diffNewSource: 'output.new_content || result.new_content',
        },
      ],
    }
    const out = interpretChatCard(decl, { result: { old_content: '', new_content: 'x' } })
    expect(out.details[0].diffOld).toBe('')
    expect(out.details[0].diffNew).toBe('x')
  })

  it('unless 条件（when 的补集）：truthy → 整块不渲染', () => {
    const decl: ChatCardDeclaration = {
      blocks: [
        { type: 'text', label: '差异', source: 'result.diff', when: 'result.diff' },
        { type: 'text', label: '写入内容', source: 'args.content', unless: 'result.diff' },
      ],
    }
    // 有 diff → 差异块渲染、写入内容块跳过（if/else 互斥）
    const withDiff = interpretChatCard(decl, { args: { content: 'c' }, result: { diff: 'd' } })
    expect(withDiff.details.map((d) => d.label)).toEqual(['差异'])
    // 无 diff → 写入内容块渲染
    const noDiff = interpretChatCard(decl, { args: { content: 'c' }, result: {} })
    expect(noDiff.details.map((d) => d.label)).toEqual(['写入内容'])
  })

  it('diffStat：addedSource/removedSource 均为 number → 产出 {added, removed}', () => {
    const decl: ChatCardDeclaration = {
      diffStat: { addedSource: 'output.added || result.added', removedSource: 'output.removed || result.removed' },
    }
    // output 包装形态
    expect(
      interpretChatCard(decl, { result: { output: { added: 3, removed: 1 } } }).diffStat,
    ).toEqual({ added: 3, removed: 1 })
    // 扁平形态
    expect(interpretChatCard(decl, { result: { added: 1, removed: 0 } }).diffStat).toEqual({
      added: 1,
      removed: 0,
    })
  })

  it('diffStat：added/removed 非同时为 number → 不产出（对齐 extractWriteDiff）', () => {
    const decl: ChatCardDeclaration = {
      diffStat: { addedSource: 'output.added || result.added', removedSource: 'output.removed || result.removed' },
    }
    // 缺 removed
    expect(interpretChatCard(decl, { result: { added: 1 } }).diffStat).toBeUndefined()
    // 字符串类型（非 number）
    expect(
      interpretChatCard(decl, { result: { added: '1', removed: '0' } }).diffStat,
    ).toBeUndefined()
    // 完全无数据
    expect(interpretChatCard(decl, { result: undefined }).diffStat).toBeUndefined()
  })

  it('块声明 id 透传到 ActivityDetailBlock.id（供按 id 定位）', () => {
    const decl: ChatCardDeclaration = {
      blocks: [{ type: 'code', id: 'content', label: '写入内容', source: 'args.content' }],
    }
    const out = interpretChatCard(decl, { args: { content: 'hi' } })
    expect(out.details[0].id).toBe('content')
  })
})
