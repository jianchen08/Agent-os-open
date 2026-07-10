/**
 * UUID 生成工具
 *
 * crypto.randomUUID() 仅在安全上下文（HTTPS / localhost）中可用。
 * 非 HTTPS 环境（如局域网 http://IP:port）会抛出 TypeError。
 * 此模块提供兼容的 UUID v4 生成函数，优先使用原生 API，不可用时回退到手动实现。
 */

/**
 * 生成 UUID v4（兼容非安全上下文）
 */
export function generateUUID(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0
    const v = c === 'x' ? r : (r & 0x3) | 0x8
    return v.toString(16)
  })
}
