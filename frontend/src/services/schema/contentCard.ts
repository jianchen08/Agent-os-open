/**
 * content_cards 内容卡声明契约与匹配纯逻辑（ADR 2026-10-14-content-card-registry）。
 *
 * 分发层类型零知识的单源：markdown pre 门按 fence language 查表
 * （matchContentCard），命中交 ContentCardRenderer 按 form 渲染，未命中 = 现状
 * 代码块原样。宿主零卡型知识——html/csv 内置首选卡与插件卡走完全同一注册/查询/
 * 裁决路径，类型特殊性只存在于声明内容。
 */

/** 卡渲染形态（三形态，ADR §4） */
export type ContentCardForm = 'webview' | 'css' | 'template'

/** content_cards 声明条目（contributes.content_cards[]，键名冻结于 ADR） */
export interface ContentCardDeclaration {
  /** 卡标识（插件内唯一；全局键 {pluginId}:{id}） */
  id: string
  /** 卡头显示标题（缺省用 id） */
  title?: string
  /**
   * 命中的 fence language 列表（归一小写比对；无语言 fence 不参与匹配）；
   * fence_content_pattern 在场时还须内容特征通过（见该键）。
   */
  match: { fences: string[]; blocks?: string[]; fence_content_pattern?: string }
  /** 渲染形态 */
  form: ContentCardForm
  /** 缺省态：true = 源码视图，false/缺省 = 渲染视图 */
  default_collapsed?: boolean
  /**
   * 无头部件声明（2026-10-25 用户裁定："输出任务清单就只渲染清单"）：true =
   * 不渲染卡头（标题/来源/源码视图切换条），卡体即全部——适合卡体本身自带
   * 折叠/头部的形态（如 todo 的 <details> 卡）。缺省 false = 既有带头卡零变化。
   */
  chromeless?: boolean
  /** 同 fence 多命中裁决权重（小者胜，缺省 100） */
  order?: number
  /** template 形态的声明模板（{{content}} / {{csv_table}}） */
  template?: string
  /** 卡作用域样式文本（作者须以 .agentos-content-card 根类自作用域） */
  css?: string
  /** 来源插件 id（first-party 卡为 'host'） */
  pluginId: string
}

/** content_cards 声明原始形态（manifest 透传，校验前的未知形状） */
export type RawContentCardDeclaration = Record<string, unknown>

/** 同 fence 多命中的缺省裁决权重（ADR §1） */
export const CONTENT_CARD_DEFAULT_ORDER = 100

/** form 合法值域（fail-closed 校验用） */
const VALID_FORMS: ReadonlySet<string> = new Set(['webview', 'css', 'template'])

/** 卡容器根类（css 声明的作用域约定锚，ADR §1） */
export const CONTENT_CARD_ROOT_CLASS = 'agentos-content-card'

/**
 * fence language 归一：从 code 元素 className 提取 language-* 首个匹配并小写。
 * 无 language-* 类（裸 ``` / 行内 code）返回 null——不参与卡匹配。
 */
export function extractFenceLanguage(className: unknown): string | null {
  if (typeof className !== 'string') return null
  const matched = /language-([\w+#.-]+)/.exec(className)
  return matched ? matched[1].toLowerCase() : null
}

/**
 * 围栏内容特征判定：pattern 卡须围栏内容通过声明正则；无 pattern 卡不参与
 * （无语言围栏的认领只靠内容特征——特征即身份，防劫持任意裸围栏）。
 */
function fenceContentMatches(card: ContentCardDeclaration, fenceContent: string | undefined): boolean {
  const pattern = card.match.fence_content_pattern
  if (pattern === undefined || fenceContent === undefined) return false
  try {
    return new RegExp(pattern).test(fenceContent)
  } catch {
    return false
  }
}

/**
 * content_cards 声明 → 按 fence language 查命中卡。
 *
 * 裁决（ADR §2）：order 升序稳定排序后取首个命中——同 order 时数组序靠前（插件段
 * 排在 first-party 段之前）者胜，与注册时序无关。未命中（含无语言 fence）返回 null，
 * 调用方回退现状渲染。fenceContent：围栏内容原文，声明带 fence_content_pattern
 * 时参与特征判定（2026-10-25 裸 HTML 任务卡根修——模型仿历史卡形态把 HTML 包进
 * ```html 围栏，仅凭 language 匹配会把普通 html 代码块一并劫持）。无语言围栏：
 * 仅带 fence_content_pattern 的卡可认领（2026-10-25 实测模型常丢语言标注）。
 */
export function matchContentCard(
  cards: readonly ContentCardDeclaration[],
  fenceLanguage: string | null,
  fenceContent?: string,
): ContentCardDeclaration | null {
  const ordered = cards
    .map((card, index) => ({ card, index }))
    .sort((a, b) => {
      const delta = (a.card.order ?? CONTENT_CARD_DEFAULT_ORDER) - (b.card.order ?? CONTENT_CARD_DEFAULT_ORDER)
      // 稳定裁决：order 相同按注册序（Array.prototype.sort 已稳定，显式 index 兜底语义）
      return delta !== 0 ? delta : a.index - b.index
    })
  if (!fenceLanguage) {
    for (const { card } of ordered) {
      if (fenceContentMatches(card, fenceContent)) return card
    }
    return null
  }
  const fence = fenceLanguage.toLowerCase()
  for (const { card } of ordered) {
    const langHit = card.match.fences.some(
      (f) => typeof f === 'string' && f.toLowerCase() === fence,
    )
    if (!langHit) continue
    const pattern = card.match.fence_content_pattern
    if (pattern === undefined) return card
    if (fenceContentMatches(card, fenceContent)) return card
  }
  return null
}

/**
 * 声明校验（fail-closed，ADR §2）：必填键缺失/形态非法的条目剔除，返回 null 表示
 * 拒绝注册。fences 归一小写去重；template 形态须携带非空 template 字段。
 */
export function normalizeContentCard(
  raw: RawContentCardDeclaration,
  pluginId: string,
): ContentCardDeclaration | null {
  const id = raw.id
  if (typeof id !== 'string' || id === '') return null
  // 匹配维两选一即可注册（2026-10-25 端到端实证根修）：fences（代码围栏）
  // 或 blocks（结构块位，match.blocks）——todo_track 1.2.8 的 blocks-only
  // 声明曾被「fences 必须非空」硬闸整条丢弃，matchContentCardForBlock 永远
  // 拿不到声明（气泡卡不渲染、角标无数据）；双维皆空仍 fail-closed。
  const match = raw.match as
    | { fences?: unknown; blocks?: unknown; fence_content_pattern?: unknown }
    | undefined
  const fences = match?.fences
  const blocks = match?.blocks
  const pattern = match?.fence_content_pattern
  const fencesOk =
    Array.isArray(fences) && fences.length > 0 && fences.every((f) => typeof f === 'string' && f !== '')
  const blocksOk =
    Array.isArray(blocks) && blocks.length > 0 && blocks.every((b) => typeof b === 'string' && b !== '')
  if (!fencesOk && !blocksOk) return null
  // fence 内容特征（可选维，fail-closed）：非字符串或非法正则整卡拒绝——
  // 坏声明不崩注册表（renderIntent/文件查看器同款装载哲学）
  let fencePatternOk = false
  if (pattern === undefined) {
    fencePatternOk = true
  } else if (typeof pattern === 'string' && pattern !== '') {
    try {
      new RegExp(pattern)
      fencePatternOk = true
    } catch {
      fencePatternOk = false
    }
  }
  if (!fencePatternOk) return null
  const form = raw.form
  if (typeof form !== 'string' || !VALID_FORMS.has(form)) return null
  const template = raw.template
  if (form === 'template' && (typeof template !== 'string' || template === '')) return null
  const normalized: ContentCardDeclaration = {
    id,
    match: {
      fences: fencesOk ? [...new Set(fences.map((f) => (f as string).toLowerCase()))] : [],
      ...(blocksOk ? { blocks: [...new Set(blocks as string[])] } : {}),
      ...(pattern !== undefined ? { fence_content_pattern: pattern as string } : {}),
    },
    form: form as ContentCardForm,
    pluginId,
  }
  if (typeof raw.title === 'string' && raw.title !== '') normalized.title = raw.title
  if (typeof raw.default_collapsed === 'boolean') normalized.default_collapsed = raw.default_collapsed
  if (typeof raw.chromeless === 'boolean') normalized.chromeless = raw.chromeless
  if (typeof raw.order === 'number' && Number.isFinite(raw.order)) normalized.order = raw.order
  if (form === 'template') normalized.template = template as string
  if (typeof raw.css === 'string' && raw.css !== '') normalized.css = raw.css
  return normalized
}


/** 结构块位（声明可匹配的 markdown 结构块；机制通用，块语义由声明插件自释义） */
export type ContentBlockKind = 'task-list'

/**
 * 结构块声明匹配（2026-10-24 用户裁定：待办折叠等"聊天框里的单"由业务
 * 插件声明渲染，前端只提供声明消费机制）：card.match.blocks 含块位即命中；
 * order 小者胜。
 */
export function matchContentCardForBlock(
  cards: readonly ContentCardDeclaration[],
  block: ContentBlockKind,
): ContentCardDeclaration | null {
  let best: ContentCardDeclaration | null = null
  for (const card of cards) {
    const blocks = (card.match as { blocks?: string[] }).blocks ?? []
    if (!blocks.includes(block)) continue
    if (!best || (card.order ?? 100) < (best.order ?? 100)) best = card
  }
  return best
}

/**
 * task-list 块内容提取：react-markdown 产出的 ul children（li 数组）→
 * GFM 清单行文本（"- [x] 文本"）。供声明卡（form=template 的 {{content}}）
 * 消费；li 内非文本子树递归取 textContent 等价文本。
 */
export function extractTaskListText(children: unknown): string {
  const lines: string[] = []
  const kids = Array.isArray(children) ? children : [children]
  for (const li of kids) {
    if (typeof li !== 'object' || li === null || !('props' in li)) continue
    const props = (li as { props?: { children?: unknown; checked?: unknown } }).props
    if (!props) continue
    let text = ''
    const collect = (node: unknown): void => {
      if (typeof node === 'string' || typeof node === 'number') {
        text += node
        return
      }
      if (typeof node !== 'object' || node === null) return
      if (Array.isArray(node)) {
        node.forEach(collect)
        return
      }
      if ('props' in node) {
        const p = (node as { props?: { children?: unknown } }).props
        if (p && typeof p === 'object' && 'children' in p) collect((p as { children?: unknown }).children)
      }
    }
    collect(props.children)
    const checked = findCheckboxChecked(props.children)
    const t = text.trim()
    if (t) lines.push(checked === true ? `- [x] ${t}` : `- [ ] ${t}`)
  }
  return lines.join('\n')
}

function findCheckboxChecked(node: unknown): boolean | undefined {
  if (typeof node !== 'object' || node === null) return undefined
  if (Array.isArray(node)) {
    for (const n of node) {
      const r = findCheckboxChecked(n)
      if (r !== undefined) return r
    }
    return undefined
  }
  if ('props' in node) {
    const p = (node as { props?: { type?: string; checked?: boolean; children?: unknown } }).props
    if (p?.type === 'checkbox') return Boolean(p.checked)
    if (p && typeof p === 'object' && 'children' in p) return findCheckboxChecked(p.children)
  }
  return undefined
}
