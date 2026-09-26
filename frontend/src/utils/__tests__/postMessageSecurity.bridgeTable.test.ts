/** @feature FP-T12 webview 桥协议统一-封闭表 | @ci: frontend-test */
/**
 * postMessageSecurity 桥上行封闭表（2026-09-25）
 *
 * 行为契约：
 * - WEBVIEW_UPLINK_METHODS 内全部 method 判定为桥方法（表即封闭词表）
 * - '/' 前缀 = REST 数据面约定，不属于桥词表（两者互斥分类）
 * - 空串/任意字符串不误判
 */

import { describe, expect, it } from 'vitest'
import {
  WEBVIEW_UPLINK_METHODS,
  isBridgedUplinkMethod,
  isRestPathMethod,
} from '../postMessageSecurity'

describe('postMessageSecurity · 桥上行封闭表', () => {
  it('表内 method 全部判真（封闭词表即全量真相）', () => {
    expect(WEBVIEW_UPLINK_METHODS.length).toBeGreaterThan(0)
    for (const m of WEBVIEW_UPLINK_METHODS) {
      expect(isBridgedUplinkMethod(m)).toBe(true)
    }
  })

  it('REST 路径与任意命令名判假（与桥词表互斥）', () => {
    expect(isBridgedUplinkMethod('/ext/demo/x')).toBe(false)
    expect(isBridgedUplinkMethod('demo.ping')).toBe(false)
    expect(isBridgedUplinkMethod('')).toBe(false)
    expect(isBridgedUplinkMethod('__ready2')).toBe(false)
  })

  it("REST 前缀判定：'/' 开头为数据面约定（≥2 组区分输入）", () => {
    expect(isRestPathMethod('/ext/demo/x')).toBe(true)
    expect(isRestPathMethod('/')).toBe(true)
    expect(isRestPathMethod('demo.ping')).toBe(false)
    expect(isRestPathMethod('')).toBe(false)
  })
})
