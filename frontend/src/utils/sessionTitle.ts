/**
 * 会话标题展示净化（纯展示层单点）
 *
 * 会话标题由首条消息原文截断生成（30 字符 + …），markdown 记号会原样露出
 * （活案例：`[test-upload.txt](/uploads/09f…`）。所有标题渲染出口经本函数
 * 统一净化（侧栏列表/删除确认/任务管理面板/调试页/交互来源行）；存储、
 * 生成与搜索匹配不经过这里——脏标题无需迁移，展示时净化即愈。
 *
 * 净化规则（只剥记号，不改语义）：
 * - 链接 `[text](url)` → `text`（含图片 `![alt](url)` → `alt`）；
 *   被截断的尾部链接 `[text](url…` → `text…`（保留省略号截断信号）
 * - 成对记号：`**加粗**`、`*斜体*`、`` `行内代码` `` → 内容；
 *   下划线记号不剥（snake_case `a_b_c` 会误伤）
 * - 行首记号：ATX 标题前缀 `#`~`######`（须后随空白，防 `#1` 编号误伤）、
 *   残缺强调 `*` 前缀（首条消息以 `**` 开头被截断的形态）
 * - 纯文本原样返回：`[附录] 与 (备注)`、`a*b`、`f(x)` 等不成链接/不成对
 *   记号的形态不误伤
 */

/** 剥离标题中的 markdown 记号，返回展示用文本 */
export function sanitizeSessionTitle(title: string): string {
  if (!title) return title
  let out = title
  // 完整链接（先剥：链接文本可能再含其他记号）
  out = out.replace(/!?\[([^\]]*)\]\(([^)]+)\)/g, '$1')
  // 被截断的尾部链接：`[text](url…` 保留省略号；`[text](url` 直接收口
  out = out.replace(/!?\[([^\]]*)\]\(([^)]*)$/, (_m, text: string, url: string) =>
    url.endsWith('…') ? `${text}…` : text,
  )
  // 成对记号：加粗 / 斜体 / 行内代码
  out = out.replace(/\*\*([^*]+)\*\*/g, '$1')
  out = out.replace(/\*([^*]+)\*/g, '$1')
  out = out.replace(/`([^`]+)`/g, '$1')
  // 行首记号：标题前缀 / 残缺强调前缀
  out = out.replace(/^#{1,6}\s+/, '')
  out = out.replace(/^\*+\s*/, '')
  return out
}
