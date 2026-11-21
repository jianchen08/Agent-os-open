/**
 * 双链适配层：@quartz-community/remark-obsidian（strict preset）与仓内三段式口径的桥。
 *
 * 语法真值 = scripts/graph_model.py WIKILINK_RE（`[[目标|关系|说明]]`，第二段是关系词
 * 不是显示别名）；解析引擎选型与节点形态探针证据 =
 * docs/working/971600b7ff1e_查看器选型与双链方案.md §五/§六（用户裁定 2026-11-21：
 * 接现成包，新增依赖仅此 1 个）。
 *
 * 跳转契约：wikilink 桥接为 `a[href="/wiki/<encodeURIComponent(目标原文)>"]`，
 * 渲染层 components.a 拦截该前缀 → 解码 →（doc_id 形态先经反链投影 id_to_path
 * 反查真实路径）→ fileOpener.openFile（任务工作区优先、`_local` 项目根回退）
 * 打开工作区页签；其余 a 元素行为不变。
 */

import remarkObsidian from '@quartz-community/remark-obsidian'
import { visit } from 'unist-util-visit'
import type { PluggableList } from 'unified'

/** doc_id 形态：YYYYMMDD-小写连字符串（同 scripts/graph_model.py DOC_ID_RE） */
const DOC_ID_RE = /^\d{8}-[a-z0-9]+(?:-[a-z0-9]+)*$/

/** /wiki/ URL 前缀（双链 href 契约面，渲染层按此前缀识别拦截） */
export const WIKI_URL_PREFIX = '/wiki/'

/** 预配置解析插件：strict preset 与 remark-parse/gfm 规范对拍一致，不替换解析器。
 *  嵌套元组形态：unified `use(数组)` 走 addList 逐项展开，[plugin, options] 必须包一层
 *  才被解构为单插件的参数元组（平铺会把 {preset} 误判为空 preset 抛错）；
 *  该形态同时匹配 streamdown remarkPlugins 的展开合并语义。 */
// 最小语法增量：只开 wikilinks——math/tags/comments/callouts/highlights/frontmatter
// 等全部关闭（共享渲染器面对消息流与文档预览，除双链外不允许任何既有渲染行为变化）
export const remarkObsidianStrict: PluggableList = [
  [
    remarkObsidian,
    {
      preset: 'strict',
      wikilinks: true,
      math: false,
      tags: false,
      comments: false,
      keepComments: false,
      callouts: false,
      highlights: false,
      inlineFootnotes: false,
      customTaskChars: false,
      frontmatter: false,
      bareFootnoteRefs: false,
    },
  ],
]

/** 双链跳转目标（wikiPath 为 /wiki/ 语义下的目标原文：路径形态=仓库相对路径；
 *  doc_id 形态=doc_id 本身，真实路径由反链投影 id_to_path 反查——渲染层不发明
 *  目录约定，审查 MF-1 修复面） */
export interface WikilinkTarget {
  wikiPath: string
  raw: string
  /** doc_id 形态标记（true = 点击跳转需先经反链索引反查真实路径） */
  isDocId: boolean
}

/** 引用形态文件后缀（无斜杠但带后缀 = 真实引用；口径对齐 graph_model.looks_like_reference） */
const REF_SUFFIX_RE = /\.(?:py|rs|ts|tsx|yaml|yml|json|toml|md|html|js|mjs|txt)$/

/**
 * 双链目标解析：路径形态（含分隔符）、doc_id 形态、无斜杠带文件后缀形态 → 跳转目标；
 * 占位写法示例（纯中文/裸词，如 `[[目标]]`/`[[doc_id]]`）→ null（不是引用）。
 * 判定口径对齐 graph_model.looks_like_reference（悬空判定的同一真值）：
 * doc_id 或带路径分隔符或带代码文件后缀才算引用。
 */
export function parseWikilinkTarget(rawTarget: string): WikilinkTarget | null {
  const raw = rawTarget.trim().replace(/\\/g, '/')
  if (!raw) return null
  if (DOC_ID_RE.test(raw)) {
    return { wikiPath: raw, raw, isDocId: true }
  }
  if (raw.includes('/') || REF_SUFFIX_RE.test(raw)) {
    return { wikiPath: raw, raw, isDocId: false }
  }
  return null
}

/**
 * 显示文本裁定：两段式 `[[目标|别名]]` 显示别名；三段式 `[[目标|关系|说明]]` 的
 * 第二段是关系词（真值 graph_model.WIKILINK_RE），显示目标本身；无别名显示目标。
 */
export function wikilinkDisplay(path: string, alias?: string): string {
  const trimmed = (alias ?? '').trim()
  if (trimmed && !trimmed.includes('|')) return trimmed
  return path
}

/**
 * href 语义：可跳转目标 → `/wiki/<encoded wikiPath>`；嵌入 ![[x]]（transclusion
 * 明确不做）、标题链接 `[[x#h]]`（P1 不做）、占位目标 → null（降级非链接）。
 */
export function wikiHref(
  path: string,
  heading: string | undefined,
  embedded: boolean,
): string | null {
  if (embedded || heading) return null
  const target = parseWikilinkTarget(path)
  if (!target) return null
  return WIKI_URL_PREFIX + encodeURIComponent(target.wikiPath)
}

/** wikilink 节点原始字面重造（降级元素保留作者写的原文形态） */
function wikilinkLiteral(node: {
  path: string
  heading?: string
  alias?: string
  embedded?: boolean
}): string {
  const alias = (node.alias ?? '').trim()
  const inner = `${node.path}${node.heading ? `#${node.heading}` : ''}${alias ? `|${alias}` : ''}`
  return `${node.embedded ? '!' : ''}[[${inner}]]`
}

/**
 * remark transformer：把 remark-obsidian 的 wikilink 节点桥接成渲染元素——
 * 可跳转目标 → a[data]（hChildren = 显示裁定文本）；嵌入/占位/标题链接 →
 * span 保留原始字面（内容零丢失，且不会被当成可点击链接）。
 */
export function bridgeWikilinks() {
  return (tree: unknown): void => {
    visit(tree as never, 'wikilink', (node: never) => {
      const n = node as {
        path: string
        heading?: string
        alias?: string
        embedded?: boolean
        data?: Record<string, unknown>
      }
      const href = wikiHref(n.path, n.heading, n.embedded === true)
      if (href) {
        n.data = {
          hName: 'a',
          hProperties: { href },
          hChildren: [{ type: 'text', value: wikilinkDisplay(n.path, n.alias) }],
        }
      } else {
        n.data = {
          hName: 'span',
          hProperties: {},
          hChildren: [{ type: 'text', value: wikilinkLiteral(n) }],
        }
      }
    })
  }
}

/** 渲染链单点挂载件：remark-obsidian strict（wikilink 解析）+ bridge（节点→a/span
 *  桥接），整体作为 Streamdown remarkPlugins prop 传入——顺序保证 bridge 在解析之后。
 *  bridge 传工厂本身不传调用结果：unified 在 freeze 时调用 plugin 工厂取得 transformer，
 *  传入调用后的 transformer 会被当成 plugin 无参再调（内部 tree=undefined 抛
 *  "Unexpected value undefined for tree"）。 */
export const wikilinkRemarkPlugins: PluggableList = [...remarkObsidianStrict, bridgeWikilinks]
