/**
 * js-yaml 最小类型声明
 *
 * js-yaml 为 eslint 的传递依赖（node_modules 已存在，v4.3.0），未安装 @types/js-yaml。
 * 本声明仅覆盖 SchemaDriver 表单驱动实际用到的 load/dump 两个 API。
 */

declare module 'js-yaml' {
  /** 解析 yaml 字符串 */
  export function load(input: string, options?: Record<string, unknown>): unknown
  /** 序列化对象为 yaml 字符串 */
  export function dump(input: unknown, options?: Record<string, unknown>): string
}
