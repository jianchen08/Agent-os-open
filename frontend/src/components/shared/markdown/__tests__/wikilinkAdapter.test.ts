/**
 * @feature FP-0.2.四 前端Schema | @ci frontend-test
 *
 * 双链适配层行为测试（AC-M1 解析面 / AC-M2 桥接面）
 *
 * 契约：仓内三段式 [[目标|关系|说明]]（真值 = scripts/graph_model.py WIKILINK_RE，
 * 第二段是关系词不是显示别名）；解析引擎 = @quartz-community/remark-obsidian
 * strict preset（与 remark-parse/gfm 规范对拍一致），桥接 = bridgeWikilinks。
 *
 * 行为断言（非实现细节）：mdast 节点 data 形态 + remark-rehype 后最终元素形态。
 */
import remarkGfm from 'remark-gfm'
import remarkParse from 'remark-parse'
import remarkRehype from 'remark-rehype'
import { unified } from 'unified'
import { describe, expect, it } from 'vitest'
import {
  bridgeWikilinks,
  parseWikilinkTarget,
  remarkObsidianStrict,
  wikilinkDisplay,
  wikiHref,
} from '../wikilinkAdapter'

/** 测试树节点最小结构类型（mdast/hast 公共面，避免 any——§4.3 禁 any 无注释例外） */
interface TestNode {
  type: string
  tagName?: string
  value?: string
  children?: TestNode[]
  properties?: Record<string, unknown>
  data?: {
    hName?: string
    hProperties?: Record<string, unknown>
    hChildren?: { type: string; value: string }[]
  }
}

/** md → mdast（parse + run，含 bridge 变换） */
function parseMdast(md: string): TestNode {
  const processor = unified()
    .use(remarkParse)
    .use(remarkGfm)
    .use(remarkObsidianStrict)
    .use(bridgeWikilinks)
  return processor.runSync(processor.parse(md), md) as TestNode
}

/** md → hast（渲染链最终元素形态，未过 sanitize——sanitize 放行已由探针实证） */
function toHast(md: string): TestNode {
  const processor = unified()
    .use(remarkParse)
    .use(remarkGfm)
    .use(remarkObsidianStrict)
    .use(bridgeWikilinks)
    .use(remarkRehype)
  return processor.runSync(processor.parse(md), md) as TestNode
}

function flatten(node: TestNode): TestNode[] {
  const out: TestNode[] = [node]
  for (const child of node.children ?? []) out.push(...flatten(child))
  return out
}

function wikilinkNodes(md: string): TestNode[] {
  return flatten(parseMdast(md)).filter((n) => n.type === 'wikilink' || n.data?.hName)
}

describe('parseWikilinkTarget — 目标解析（AC-M1）', () => {
  it('AC-M1-1 路径形态目标原样保留（≥2 组有区分度输入）', () => {
    expect(parseWikilinkTarget('docs/decisions/x.md')).toEqual({
      wikiPath: 'docs/decisions/x.md',
      raw: 'docs/decisions/x.md',
      isDocId: false,
    })
    expect(parseWikilinkTarget('docs/working/装机版维护轮配方_20261013.md')?.wikiPath).toBe(
      'docs/working/装机版维护轮配方_20261013.md',
    )
  })

  it('AC-M1-2 doc_id 形态返回目标原文（isDocId=true，真实路径解析交反链投影 id_to_path）', () => {
    // 锚定仓库真实 doc_id↔path 对（docs/decisions/2025-12-05-agent-three-path-generation.md
    // frontmatter doc_id: 20251205-agent-three-path-generation）——渲染层不再发明路径
    expect(parseWikilinkTarget('20251205-agent-three-path-generation')).toEqual({
      wikiPath: '20251205-agent-three-path-generation',
      raw: '20251205-agent-three-path-generation',
      isDocId: true,
    })
    expect(parseWikilinkTarget('20261121-508b4564f25e-wikilink-p1')?.isDocId).toBe(true)
  })

  it('AC-M1-3 占位写法示例（纯中文/裸词）返回 null——不是引用', () => {
    expect(parseWikilinkTarget('目标')).toBeNull()
    expect(parseWikilinkTarget('doc_id')).toBeNull()
    expect(parseWikilinkTarget('')).toBeNull()
  })

  it('AC-M1-3b 无斜杠但带代码文件后缀 = 引用（口径对齐 graph_model.looks_like_reference）', () => {
    // 仓内真实形态：[[70fb0a75e51c_查看器接入审查报告.md]]（无 / 但带 .md 后缀，
    // 后端判为引用参与悬空校验——前端若判占位即索引面与渲染面分叉）
    expect(parseWikilinkTarget('70fb0a75e51c_查看器接入审查报告.md')?.wikiPath).toBe(
      '70fb0a75e51c_查看器接入审查报告.md',
    )
    expect(parseWikilinkTarget('scripts/helper.py')?.isDocId).toBe(false)
    expect(parseWikilinkTarget('notes')).toBeNull()
  })

  it('AC-M1-4 反斜杠路径归一为正斜杠（Windows 宿主形态）', () => {
    expect(parseWikilinkTarget('docs\\decisions\\x.md')?.wikiPath).toBe('docs/decisions/x.md')
  })

  it('AC-M1-5 伪 doc_id（日期段含连字符）不误判', () => {
    expect(parseWikilinkTarget('2026-10-24-file-viewer-declarative')).toBeNull()
  })
})

describe('wikilinkDisplay — 显示文本裁定（AC-M2 别名）', () => {
  it('AC-M2-1 两段式别名：显示别名', () => {
    expect(wikilinkDisplay('docs/a.md', '显示文本')).toBe('显示文本')
    expect(wikilinkDisplay('docs/a.md', '真值')).toBe('真值')
  })

  it('AC-M2-2 三段式（alias 含 |）：第二段是关系词，显示目标本身', () => {
    expect(wikilinkDisplay('docs/decisions/x.md', '取代|推翻本件结论')).toBe('docs/decisions/x.md')
    expect(wikilinkDisplay('plugins/shared/beta/plugin.json', '依赖|清单文件')).toBe(
      'plugins/shared/beta/plugin.json',
    )
  })

  it('AC-M2-3 无别名：显示目标', () => {
    expect(wikilinkDisplay('docs/a.md', '')).toBe('docs/a.md')
    expect(wikilinkDisplay('docs/a.md', undefined as unknown as string)).toBe('docs/a.md')
  })
})

describe('wikiHref — href 语义（AC-M1/AC-M2 降级面）', () => {
  it('AC-M2-4 正常目标产出 /wiki/ 前缀 href（CJK 经 encodeURIComponent）', () => {
    expect(wikiHref('docs/a.md', '', false)).toBe('/wiki/docs%2Fa.md')
    expect(wikiHref('docs/working/配方.md', '', false)).toBe(
      `/wiki/${encodeURIComponent('docs/working/配方.md')}`,
    )
  })

  it('AC-M2-5 嵌入 ![[x]] 不产出 href（明确不做 transclusion）', () => {
    expect(wikiHref('note', '', true)).toBeNull()
  })

  it('AC-M2-6 占位目标不产出 href', () => {
    expect(wikiHref('目标', '', false)).toBeNull()
  })

  it('AC-M2-7 标题链接不产出 href（P1 明确不做，降级字面）', () => {
    expect(wikiHref('docs/x.md', '某标题', false)).toBeNull()
  })
})

describe('bridgeWikilinks — mdast 桥接（AC-M2 渲染形态）', () => {
  it('AC-M2-8 可解析目标桥接为 a[href^=/wiki/]，文本走显示裁定', () => {
    const nodes = wikilinkNodes('见 [[docs/decisions/x.md|取代|推翻本件结论]] 与 [[a/b.md|显示文本]]')
    const anchors = nodes.filter((n) => n.data?.hName === 'a')
    expect(anchors).toHaveLength(2)
    // 三段式：显示目标；href 指向目标
    expect(anchors[0].data.hProperties.href).toBe('/wiki/docs%2Fdecisions%2Fx.md')
    expect(anchors[0].data.hChildren[0].value).toBe('docs/decisions/x.md')
    // 两段式：显示别名
    expect(anchors[1].data.hChildren[0].value).toBe('显示文本')
    expect(anchors[1].data.hProperties.href).toBe('/wiki/a%2Fb.md')
  })

  it('AC-M2-9 嵌入与占位降级为非链接元素且保留字面形态', () => {
    const hast = toHast('嵌入 ![[note]] 占位 [[目标|关系|说明]]')
    const elements = flatten(hast).filter((n) => n.type === 'element')
    const anchors = elements.filter((n) => n.tagName === 'a')
    expect(anchors).toHaveLength(0)
    // 字面保留（含 ! 与括号），不丢内容
    const text = JSON.stringify(hast)
    expect(text).toContain('![[note]]')
    expect(text).toContain('[[目标|关系|说明]]')
  })

  it('AC-M2-10 端到端：正文双链出 a，代码块与行内代码内双链不解析', () => {
    const md = '```md\n[[docs/a.md]]\n```\n正文 [[docs/b.md]] 与行内 `[[docs/c.md]]`'
    const hast = toHast(md)
    const anchors = flatten(hast).filter((n) => n.type === 'element' && n.tagName === 'a')
    expect(anchors).toHaveLength(1)
    expect(anchors[0].properties.href).toBe('/wiki/docs%2Fb.md')
  })

  it('AC-M2-11 GFM 与普通链接不回归：表格内双链与外链共存', () => {
    const md = '| 表头A | 表头B |\n| ----- | ----- |\n| 单元 | [[docs/t.md]] |\n\n[普通](https://a.com)'
    const hast = toHast(md)
    const elements = flatten(hast).filter((n) => n.type === 'element')
    expect(elements.filter((n) => n.tagName === 'table')).toHaveLength(1)
    expect(elements.filter((n) => n.tagName === 'a')).toHaveLength(2)
  })
})
