/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * /api/v1 内核端点契约测试（解耦方案 P2-7；/api/v1 无 OpenAPI 生成物，
 * 以源码对账守护——/ext 插件端点另有 endpoints.generated.ts + 漂移闸）。
 *
 * 对账方向：constants/api.ts 中每个内核拥有的 /api/v1 字面量（含 ${param}
 * 模板）必须能在内核 axum 路由表（kernel/crates/api/src/server.rs 的
 * .route 声明）找到同形状路由——内核改名/删除路由 → 本测试红。
 * 内核新增路由不在此闸内（前端未引用即无契约）。
 *
 * 含反例（负控制）：构造的假路径必须匹配失败，证明匹配器非恒真。
 */

import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, it, expect } from 'vitest'
import { API_ENDPOINTS } from '@/constants/api'

const REPO_ROOT = resolve(__dirname, '../../../..')
const SERVER_RS = resolve(REPO_ROOT, 'kernel/crates/api/src/server.rs')
const API_TS = resolve(__dirname, '../api.ts')

/** 从 server.rs 提取 axum .route 路径（含多行声明） */
function extractKernelRoutes(source: string): string[] {
  const routes: string[] = []
  const re = /\.route\(\s*"([^"]+)"/g
  let m: RegExpExecArray | null
  while ((m = re.exec(source)) !== null) routes.push(m[1])
  return routes
}

/** 内核 axum {param} → 单段正则，并锚定全路径 */
function routeToRegex(route: string): RegExp {
  const pattern = route.replace(/\{[^}]+\}/g, '[^/]+').replace(/\./g, '\\.')
  return new RegExp(`^${pattern}$`)
}

/** 从 constants/api.ts 源码提取 /api/v1 字面量；${...} 模板 → [^/]+ 占位 */
function extractFrontendApiV1Patterns(source: string): string[] {
  const patterns: string[] = []
  const re = /[`']((?:\/api\/v1)\/[^`'"]*)[`']/g
  let m: RegExpExecArray | null
  while ((m = re.exec(source)) !== null) {
    patterns.push(m[1].replace(/\$\{[^}]+\}/g, '[^/]+'))
  }
  return patterns
}

describe('/api/v1 内核端点契约对账（P2-7）', () => {
  const kernelSource = readFileSync(SERVER_RS, 'utf-8')
  const kernelRoutes = extractKernelRoutes(kernelSource)
  const frontendSource = readFileSync(API_TS, 'utf-8')
  const frontendPatterns = extractFrontendApiV1Patterns(frontendSource)

  it('对账输入非空（路由表与前端字面量都提取到了）', () => {
    expect(kernelRoutes.length).toBeGreaterThan(20)
    expect(frontendPatterns.length).toBeGreaterThan(15)
  })

  it('constants/api.ts 每个 /api/v1 字面量在内核路由表有同形状路由', () => {
    const regexes = kernelRoutes.map(routeToRegex)
    const unmatched: string[] = []
    for (const pattern of frontendPatterns) {
      const re = new RegExp(`^${pattern.replace(/\./g, '\\.')}$`)
      if (!regexes.some((r) => r.source === re.source || r.test(pattern))) {
        unmatched.push(pattern)
      }
    }
    expect(unmatched).toEqual([])
  })

  it('反例（负控制）：前端引用了一个内核不存在的路由 → 匹配失败', () => {
    // 证明上一条不是恒真：同样的匹配器对假路由必须匹配不到
    const regexes = kernelRoutes.map(routeToRegex)
    const fake = '^/api/v1/pipelines/nonexistent-route$'
    expect(regexes.some((r) => r.test('/api/v1/pipelines/nonexistent-route'))).toBe(false)
    expect(fake).toBeTruthy()
  })

  it('管道摘要/状态精简端点组逐点在场（任务管理面板数据源）', () => {
    expect(API_ENDPOINTS.PIPELINES.RUNS).toBe('/api/v1/pipelines/runs')
    expect(API_ENDPOINTS.PIPELINES.STATE).toBe('/api/v1/pipelines/state')
    expect(API_ENDPOINTS.PIPELINES.CATALOG).toBe('/api/v1/pipelines')
    expect(kernelRoutes).toContain('/api/v1/pipelines/runs')
    expect(kernelRoutes).toContain('/api/v1/pipelines/state')
    expect(kernelRoutes).toContain('/api/v1/pipelines')
  })
})
