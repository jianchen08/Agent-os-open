/**
 * 声明式模板解释器（content_cards form=template，ADR 2026-10-14-content-card-registry §4）。
 *
 * 宿主通用解释器：DOMParser 解析声明模板（声明面，插件作者可信内容）→ 挂载前
 * 剥离 script/iframe/object/embed/link/meta 与 on* 事件属性（纵深防御：DOMParser
 * 文档内 script 不执行，但经 importNode 克隆进主文档后会执行）→ 文本节点中
 * {{content}} 以 textContent 注入 fence 内容（不可信内容永不走 innerHTML）→
 * {{csv_table}} 由宿主内置 CSV 解析构建 table DOM（单元格 textContent，天然
 * 转义；首行解析为表头——CSV 数据表惯例，ADR §影响③）。
 */

/** 模板内剥离的整元素标签（ADR §4/§G：声明面半可信，纵深防御成本一行剥离器） */
const STRIP_TAGS: ReadonlySet<string> = new Set(['script', 'iframe', 'object', 'embed', 'link', 'meta'])

/** 模板占位符指令（ADR §1 键名冻结） */
const CONTENT_DIRECTIVE = '{{content}}'
const CONTENT_TEXT_DIRECTIVE = '{{content_text}}'
const CSV_TABLE_DIRECTIVE = '{{csv_table}}'

/**
 * HTML 内容 → 纯文本（{{content_text}} 消费，2026-10-25 任务卡双形态统一）：
 * 块级闭合补换行防 li 文本粘连 → DOMParser 解析（实体天然解码）→ textContent
 * 取文 → 空白收敛。机制通用零卡型知识；何时用 {{content_text}} 由声明模板定。
 */
export function contentToPlainText(content: string): string {
  const withBreaks = String(content ?? '')
    .replace(/<\/(li|p|div|h[1-6]|tr|summary|details)>/gi, '\n')
    // 内联元素闭合后紧跟非空白文本 → 补一个空格（textContent 拼接天然无界，
    // 标记 span 与正文之间否则粘连，如 "☑三线…" → "☑ 三线…"）
    .replace(/<\/(span|b|strong|em|i|code|mark)>(?=[^\s<])/gi, ' ')
  const doc = new DOMParser().parseFromString(withBreaks, 'text/html')
  const text = (doc.body.textContent ?? '').replace(/[ \t]+\n/g, '\n')
  return text.replace(/\n{3,}/g, '\n\n').trim()
}

/** CSV 行数组 → table DOM（首行 thead 表头，其余 tbody；单元格 textContent） */
export function buildCsvTable(rows: readonly string[][]): HTMLTableElement {
  const table = document.createElement('table')
  if (rows.length === 0) return table
  const [header, ...body] = rows
  const thead = document.createElement('thead')
  const headRow = document.createElement('tr')
  for (const cell of header) {
    const th = document.createElement('th')
    th.textContent = cell
    headRow.appendChild(th)
  }
  thead.appendChild(headRow)
  table.appendChild(thead)
  const tbody = document.createElement('tbody')
  for (const row of body) {
    const tr = document.createElement('tr')
    for (const cell of row) {
      const td = document.createElement('td')
      td.textContent = cell
      tr.appendChild(td)
    }
    tbody.appendChild(tr)
  }
  table.appendChild(tbody)
  return table
}

/** 文本节点展开：{{content}} → fence 文本段；{{content_text}} → HTML 去标签纯文本；
 *  {{csv_table}} → table 元素（csv=null 时指令原样缺席展开为空） */
function expandTextNode(node: Text, content: string, csvRows: readonly string[][] | null): void {
  const raw = node.nodeValue ?? ''
  if (!raw.includes(CONTENT_DIRECTIVE) && !raw.includes(CONTENT_TEXT_DIRECTIVE) && !raw.includes(CSV_TABLE_DIRECTIVE))
    return
  const fragment = document.createDocumentFragment()
  const parts = raw.split(/(\{\{content\}\}|\{\{content_text\}\}|\{\{csv_table\}\})/g)
  for (const part of parts) {
    if (part === CONTENT_DIRECTIVE) {
      fragment.appendChild(document.createTextNode(content))
    } else if (part === CONTENT_TEXT_DIRECTIVE) {
      fragment.appendChild(document.createTextNode(contentToPlainText(content)))
    } else if (part === CSV_TABLE_DIRECTIVE) {
      if (csvRows) fragment.appendChild(buildCsvTable(csvRows))
    } else if (part !== '') {
      fragment.appendChild(document.createTextNode(part))
    }
  }
  node.replaceWith(fragment)
}

/**
 * 模板渲染：返回挂载就绪的 DocumentFragment（调用方 replaceChildren 装载）。
 * 剥离先于展开（模板作者在 script 内写指令的形态整元素已移除）。
 */
export function renderTemplate(
  template: string,
  content: string,
  csvRows: readonly string[][] | null,
): DocumentFragment {
  const doc = new DOMParser().parseFromString(String(template ?? ''), 'text/html')
  for (const el of Array.from(doc.body.querySelectorAll('*'))) {
    if (STRIP_TAGS.has(el.tagName.toLowerCase())) {
      el.remove()
      continue
    }
    for (const attr of Array.from(el.attributes)) {
      if (attr.name.toLowerCase().startsWith('on')) el.removeAttribute(attr.name)
    }
  }
  const walker = doc.body.ownerDocument!.createTreeWalker(doc.body, NodeFilter.SHOW_TEXT)
  const textNodes: Text[] = []
  while (walker.nextNode()) textNodes.push(walker.currentNode as Text)
  for (const node of textNodes) expandTextNode(node, content, csvRows)
  const fragment = document.createDocumentFragment()
  for (const child of Array.from(doc.body.childNodes)) fragment.appendChild(child)
  return fragment
}
