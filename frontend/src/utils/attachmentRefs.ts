/**
 * 附件引用并入消息正文（ADR 2026-08-21：索引随 content 携带，内核零改动）。
 *
 * 前端发送时把已上传附件以 markdown 引用追加到消息 content——
 * 图片 `![filename](/uploads/x.png)`（消息气泡 markdown 渲染成图），
 * 其它文件 `[filename](/uploads/x.pdf)`（渲染为链接）。
 * 后端链路：multimodal_preprocessor 识别 /uploads/ 引用 → llm_core 发送前
 * 读文件转 base64 挂到 LLM 请求（二进制瞬态，不落任何持久层）。
 */

/** 附件引用拼装所需的最小附件形状（结构兼容 chat/types 的 Attachment） */
export interface AttachmentRefInput {
  name?: string
  type?: string
  url?: string
}

/**
 * 把附件引用并入消息正文。
 *
 * - 无附件 / 附件均无 url：原样返回 content（零改动）；
 * - 引用块之间及与正文之间以空行分隔（markdown 渲染成块级元素）；
 * - 文件名缺省回退 "附件"；name 中的方括号剔除（防 markdown 链接文本语法破损）。
 */
export function appendAttachmentRefs(
  content: string,
  attachments?: AttachmentRefInput[] | null,
): string {
  const refs = (attachments ?? [])
    .filter((att) => Boolean(att.url))
    .map((att) => {
      const name = (att.name ?? '附件').replace(/[[\]]/g, '').trim() || '附件'
      const isImage = Boolean(att.type?.startsWith('image/'))
      return `${isImage ? '!' : ''}[${name}](${att.url})`
    })
  if (refs.length === 0) {
    return content
  }
  return [content.trim(), ...refs]
    .filter((part) => part.length > 0)
    .join('\n\n')
}
