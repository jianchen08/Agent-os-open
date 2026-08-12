/**
 * 页面生成脚手架 —— 生成即合规的 PageShell 页面骨架
 *
 * 用法：pnpm gen:page <name>   或   node scripts/gen-page.mjs <name>
 *
 * 生成 src/pages/<kebab-name>/<Name>Page.tsx，内置：
 * - shared/PageShell 外壳（无手写 flex h-screen / <a href>）
 * - loading/error/empty 三态（shared 组件）
 * - useState 驱动的列表/数据加载样板
 *
 * 意图：新页面从模板出生即满足 no-drift 契约（统一审查 §4.3 接线机制）。
 */

import { writeFileSync, mkdirSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const FRONTEND_ROOT = path.resolve(__dirname, '..')

const rawName = process.argv[2]
if (!rawName) {
  console.error('用法: node scripts/gen-page.mjs <name>  (例如: knowledge-base)')
  process.exit(1)
}

/** kebab-case 目录名 */
const kebab = rawName
  .replace(/([a-z0-9])([A-Z])/g, '$1-$2')
  .replace(/[_\s]+/g, '-')
  .toLowerCase()
/** PascalCase 组件名 */
const pascal = kebab
  .split('-')
  .map((s) => s.charAt(0).toUpperCase() + s.slice(1))
  .join('') + 'Page'
/** 中文标题（简单映射，可手动改） */
const title = kebab
  .split('-')
  .map((s) => s.charAt(0).toUpperCase() + s.slice(1))
  .join(' ')

const dir = path.join(FRONTEND_ROOT, 'src', 'pages', kebab)
const file = path.join(dir, `${pascal}.tsx`)

const content = `/**
 * ${title} 页面
 *
 * 由 gen:page 生成：统一外壳走 shared/PageShell，三态走 shared 组件。
 */

import { useState } from 'react'
import { EmptyState } from '@/components/shared/EmptyState'
import { ErrorState } from '@/components/shared/ErrorState'
import { LoadingState } from '@/components/shared/LoadingState'
import { PageShell } from '@/components/shared/PageShell'

export function ${pascal}() {
  const [isLoading, setIsLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  return (
    <PageShell title="${title}" backHref="/">
      {isLoading && <LoadingState variant="skeleton" />}
      {error && <ErrorState message={error} />}
      {!isLoading && !error && (
        <EmptyState title="暂无内容" description="此处展示 ${title} 内容" />
      )}
    </PageShell>
  )
}

export default ${pascal}
`

mkdirSync(dir, { recursive: true })
writeFileSync(file, content, 'utf8')
console.log(`已生成：src/pages/${kebab}/${pascal}.tsx`)
console.log('下一步：在 router.tsx 注册路由，并实现具体业务逻辑。')
