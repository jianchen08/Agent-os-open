/**
 * YAML 解析/序列化工具
 *
 * 封装 js-yaml（node_modules 已随 eslint 传递依赖存在，本模块是唯一消费点，
 * 便于将来升级为独立依赖或替换实现）。
 *
 * 用途：Agent 配置页 yaml 原文 ↔ 表单值对象 的双向转换。
 */

import { dump, load } from 'js-yaml'

/**
 * 解析 yaml 字符串为对象
 *
 * @param yamlText - yaml 原文
 * @returns 解析后的对象；解析失败返回空对象（不抛错，表单按空值渲染）
 */
export function parseYamlObject(yamlText: string): Record<string, unknown> {
  try {
    const parsed = load(yamlText)
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>
    }
    return {}
  } catch {
    return {}
  }
}

/**
 * 序列化对象为 yaml 字符串
 *
 * @param values - 表单值对象
 * @returns yaml 字符串（lineWidth -1：不强制折行，保留长文本原样）
 */
export function serializeYaml(values: Record<string, unknown>): string {
  return dump(values, { lineWidth: -1 })
}
