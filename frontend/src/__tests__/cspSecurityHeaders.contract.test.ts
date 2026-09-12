// @feature FP-T12 前端组件补测
/**
 * CSP 与安全响应头静态契约测试
 *
 * 思路同 tests/test_startup_scripts_fix.py：对部署面配置做静态断言，防止
 * 无意中移除安全头、放开任意外源脚本、或让应用壳重新引入内联脚本。
 *
 * 契约（资源形态实证见 nginx.conf 注释块）：
 * - nginx.conf：服务端携带 CSP + nosniff + SAMEORIGIN + Referrer-Policy；
 *   script-src 'self' 'unsafe-inline' blob:（'unsafe-inline' 为 WebviewWidget
 *   srcDoc 内联脚本所必需——srcDoc iframe 继承父文档 CSP，无法局部豁免；
 *   blob: 为 skinRuntime 皮肤 hooks 装载所必需）。
 * - vite.config.ts：dev server 同款头（dev 另有 React Refresh/HMR 内联脚本）。
 * - index.html：无内联 <script>（主题预置脚本外置 public/theme-init.js）——
 *   应用壳自身是唯一能收紧的部分，有本测试看守防回潮。
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

// vitest 以 frontend/ 为运行根（pnpm test 工作目录），部署面文件都在仓库 frontend 根下
const frontendRoot = process.cwd()
const nginxConf = readFileSync(resolve(frontendRoot, 'nginx.conf'), 'utf-8')
const viteConfig = readFileSync(resolve(frontendRoot, 'vite.config.ts'), 'utf-8')
const indexHtml = readFileSync(resolve(frontendRoot, 'index.html'), 'utf-8')

/** 从文本中提取第一条 CSP 头里的 script-src 指令值（头值以双引号界定，内含 'self' 等单引号源） */
function scriptSrcDirectiveOf(text: string): string | null {
  const cspMatch = text.match(/Content-Security-Policy[^"]*"([^"]+)"/)
  if (!cspMatch) return null
  const directive = cspMatch[1]
    .split(';')
    .map((d) => d.trim())
    .find((d) => d.startsWith('script-src'))
  return directive ?? null
}

describe('CSP 安全头静态契约', () => {
  it('nginx.conf 携带 CSP 及全部辅助安全头', () => {
    expect(nginxConf).toContain('Content-Security-Policy')
    expect(nginxConf).toContain("default-src 'self'")
    expect(nginxConf).toContain('object-src ' + "'none'")
    expect(nginxConf).toContain('frame-ancestors ' + "'self'")
    expect(nginxConf).toContain('connect-src')
    expect(nginxConf).toMatch(/connect-src[^;]*\bws:/)
    expect(nginxConf).toContain('X-Content-Type-Options')
    expect(nginxConf).toContain('nosniff')
    expect(nginxConf).toContain('X-Frame-Options')
    expect(nginxConf).toContain('SAMEORIGIN')
    expect(nginxConf).toContain('Referrer-Policy')
    expect(nginxConf).toContain('strict-origin-when-cross-origin')
  })

  it('nginx.conf 的 CSP 声明至少出现两处（server 级 + /assets/ 重复声明）', () => {
    // nginx add_header 继承规则：location 内一旦自带 add_header（/assets/ 的
    // Cache-Control），server 级安全头不再生效，必须在 location 内重复声明，
    // 否则 JS/CSS 资源响应静默丢失安全头。
    const occurrences = nginxConf.split('Content-Security-Policy').length - 1
    expect(occurrences).toBeGreaterThanOrEqual(2)
  })

  it('生产与 dev 的 script-src 同基线：仅 self/内联/blob:，拒绝任意外源与通配', () => {
    const prodScriptSrc = scriptSrcDirectiveOf(nginxConf)
    expect(prodScriptSrc).toBeTruthy()
    // 'self'：任意外部源（CDN/注入远程脚本）依旧被拒——script-src 保留的硬化面
    expect(prodScriptSrc!).toContain("'self'")
    // blob:：skinRuntime 以 dynamic import(blob:) 装载皮肤 hooks.mjs
    expect(prodScriptSrc!).toContain('blob:')
    // 'unsafe-inline'：WebviewWidget/HtmlPreviewWidget 的 srcDoc 内联脚本所必需
    // （srcDoc iframe 继承父文档 CSP，父级不放行则全部插件 webview 被拦截）
    expect(prodScriptSrc!).toContain("'unsafe-inline'")
    // 防滑坡：不允许通配或写死任意外源
    expect(prodScriptSrc!).not.toMatch(/\*/)
    expect(prodScriptSrc!).not.toMatch(/https?:\/\//)

    const devScriptSrc = scriptSrcDirectiveOf(viteConfig)
    expect(devScriptSrc).toBe(prodScriptSrc)
  })

  it('vite.config.ts dev server 携带与生产同基线的辅助安全头', () => {
    expect(viteConfig).toContain("'X-Content-Type-Options'")
    expect(viteConfig).toContain("'X-Frame-Options'")
    expect(viteConfig).toContain("'Referrer-Policy'")
  })

  it('index.html 无内联 <script>，主题预置脚本外置为同源静态文件', () => {
    // 去 HTML 注释后断言：不允许出现无 src 的 script 标签（生产 CSP 会拦截）
    const withoutComments = indexHtml.replace(/<!--[\s\S]*?-->/g, '')
    expect(withoutComments).not.toMatch(/<script(?![^>]*\bsrc=)[^>]*>/)
    expect(indexHtml).toContain('<script src="/theme-init.js">')
  })
})
