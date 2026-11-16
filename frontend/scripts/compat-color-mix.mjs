/**
 * 构建后处理：为不支持 color-mix() 的老 WebView（Chromium < 111，如 MuMu
 * AOSP WebView 110）补透明度修饰工具类的 legacy 规则。
 *
 * 根因（双端优化轮 R2 实测，CDP @ MuMu WebView 110.0.5481.154）：
 * Tailwind v4 对 `bg-primary/10` 等透明度修饰一律编译为
 *   .x{background-color:<基色>}                      ← 门外 fallback（全饱和）
 *   @supports (color:color-mix(in lab, red, red)){   ← Chromium 111+ 才过门
 *     .x{background-color:color-mix(in oklab,<基色> N%,transparent)}
 *   }
 * 不支持 color-mix 的 WebView 只取到全饱和 fallback——淡色容器（bg-primary/10）
 * 渲染成实色块，同色图标/文字（text-primary）画在其上"消失"。
 *
 * 本脚本对产物 CSS 中可机械等价变换的形态，在对应 @supports 块之前补一条
 * 同选择器的 legacy 规则（同 specificity 源序在前 = 低优先）：
 *   color-mix(in oklab, hsl(var(--x) / 1) N%, transparent)
 *     → hsl(var(--x-t, var(--x)) / calc(0.NN * var(--x-a, 1)))
 *       （hsl 桥基色因子化：带 alpha 主题由 -t/-a 镜像精确得 alpha 乘积，
 *        三元组主题缺省回退逐字节等价 hsl(var(--x) / 0.NN)——R25）
 *   color-mix(in oklab, rgb(var(--x) / 1) N%, transparent) → rgb(var(--x) / 0.NN)
 *   color-mix(in oklab, #rrggbb N%, transparent)           → #rrggbbaa
 * color-mix 混 transparent 的结果 = 基色通道不变、alpha 乘 N/100，与上述
 * 写法渲染色值等价；支持 color-mix 的浏览器仍取门内值，渲染零变化。
 * 无法机械等价的基色形态（var(--full-color)、currentcolor）不动，维持既有降级。
 *
 * 幂等性：legacy 规则不在 @supports 门内，重跑不会产生新匹配。
 * 挂载：package.json postbuild（vite build 后自动执行，syncweb/部署同链路受益）。
 */
import { readdirSync, readFileSync, writeFileSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const distAssets = join(dirname(fileURLToPath(import.meta.url)), '..', 'dist', 'assets')

const SUPPORTS_GUARD = '@supports (color:color-mix(in lab, red, red)){'
/** 注入规则标记：幂等锚（已注入的块重跑跳过） */
const MARKER = '/*cm-compat*/'

/** color-mix(in <space>, <base> N%, transparent) → legacy 等价值；不可变换返回 null */
function legacyValue(space, base, pct) {
  const alpha = Number(pct) / 100
  let m
  if ((m = base.match(/^(hsl|rgb)\(var\((--[^)]+)\)(?: \/ 1)?\)$/))) {
    const [, fn, varName] = m
    // hsl 桥基色可能自带声明级 alpha（主题源值 rgba 经 colorToHsl 保留，如
    // `222 47% 11% / 0.1`）——直形态 `hsl(var(--x) / N)` 展开双 alpha 非法，
    // IACVT 退化（border→currentColor、bg→transparent）。因子化形态：主题
    // 引擎对带 alpha 值发射 -t（纯三元组）/ -a（alpha 标量）镜像，三元组
    // 主题下缺省回退逐字节等价直形态，带 alpha 主题下精确得 alpha 乘积。
    if (fn === 'hsl') {
      return `hsl(var(${varName}-t, var(${varName})) / calc(${alpha} * var(${varName}-a, 1)))`
    }
    return `rgb(var(${varName}) / ${alpha})`
  }
  if ((m = base.match(/^#([0-9a-fA-F]{6})$/))) {
    return `#${m[1]}${Math.round(alpha * 255).toString(16).padStart(2, '0')}`
  }
  return null
}

/** 把一条声明里的 color-mix(...) 换成 legacy 值；存在不可变换成分返回 null */
function legacyDecl(decl) {
  const re = /color-mix\(in (\w+), ([^,]+) ([\d.]+)%, transparent\)/g
  let out = ''
  let last = 0
  let changed = false
  for (let m; (m = re.exec(decl)); ) {
    const legacy = legacyValue(m[1], m[2], m[3])
    if (legacy === null) return null
    out += decl.slice(last, m.index) + legacy
    last = m.index + m[0].length
    changed = true
  }
  return changed ? out + decl.slice(last) : null
}

/** 提取下一个 @supports color-mix 块；返回 {start, end, body}（body 不含外层花括号） */
function findSupportsBlock(css, from) {
  const start = css.indexOf(SUPPORTS_GUARD, from)
  if (start === -1) return null
  let depth = 1
  let i = start + SUPPORTS_GUARD.length
  while (i < css.length && depth > 0) {
    if (css[i] === '{') depth++
    else if (css[i] === '}') depth--
    i++
  }
  if (depth !== 0) throw new Error('compat-color-mix: @supports 块花括号不平衡')
  return { start, end: i, body: css.slice(start + SUPPORTS_GUARD.length, i - 1) }
}

let totalRules = 0
for (const file of readdirSync(distAssets)) {
  if (!file.endsWith('.css')) continue
  const path = join(distAssets, file)
  const css = readFileSync(path, 'utf8')
  let out = ''
  let pos = 0
  for (;;) {
    const block = findSupportsBlock(css, pos)
    if (!block) {
      out += css.slice(pos)
      break
    }
    out += css.slice(pos, block.start)
    if (css.slice(pos, block.start).includes(MARKER)) {
      // 该块已有 legacy 规则（重跑），原样保留
      out += css.slice(block.start, block.end)
      pos = block.end
      continue
    }
    // 块体 = 一或多条 selector{decl;decl}；逐声明尝试变换
    const legacyRules = []
    const ruleRe = /([^{}]+)\{([^{}]*)\}/g
    for (const rule of block.body.match(ruleRe) ?? []) {
      const [, selector, decls] = rule.match(/^([^{}]+)\{([^{}]*)\}$/)
      const legacyDecls = decls
        .split(';')
        .map((d) => legacyDecl(d))
        .filter((d) => d !== null)
      if (legacyDecls.length > 0) legacyRules.push(`${MARKER}${selector}{${legacyDecls.join(';')}}`)
    }
    if (legacyRules.length > 0) {
      out += legacyRules.join('')
      totalRules += legacyRules.length
    }
    out += css.slice(block.start, block.end)
    pos = block.end
  }
  if (out !== css) {
    writeFileSync(path, out)
    console.log(`[compat-color-mix] ${file}: +${totalRules} legacy rules (cumulative)`)
  }
}
console.log(`[compat-color-mix] done: ${totalRules} legacy rules injected`)
