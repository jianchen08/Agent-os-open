import { readFileSync, writeFileSync } from 'fs';
import { join, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const root = join(__dirname, '..');

const patches = [
  {
    file: 'node_modules/@lobehub/ui/es/Image/components/usePreview.mjs',
    from: 'rootClassName: cx(styles.preview, rootClassName),',
    to: 'classNames: { root: cx(styles.preview, rootClassName) },',
  },
  {
    file: 'node_modules/@lobehub/ui/es/Image/components/usePreviewGroup.mjs',
    from: 'rootClassName: cx(styles.preview, rootClassName),',
    to: 'classNames: { root: cx(styles.preview, rootClassName) },',
  },
  // ── useHighlight 挂载首帧缓存命中旁路(代码块闪烁根修,2026-11-06)──
  // 缺陷:高亮状态 data 初值恒空——即使模块级 highlightCache 命中(promise 已
  // resolved),setData 也要等 useEffect(paint 之后)→ 下一帧才切换高亮 HTML。
  // 代码块组件每次挂载都先渲一帧无高亮纯文本 fallback:工作区 MD 预览每次
  // 打开/切换文件全部代码块重挂 → 每次可见闪烁(消息流消息行常驻 DOM 不重挂,
  // 故无感)。修复:resolved html 同步写入 highlightHtmlCache,挂载首帧
  // useState 初值直接命中。行为锚测试:
  // src/components/shared/markdown/__tests__/useHighlightCacheHit.test.ts
  {
    file: 'node_modules/@lobehub/ui/es/hooks/useHighlight.mjs',
    from: 'const highlightCache = /* @__PURE__ */ new Map();\nconst MAX_CACHE_SIZE = 1e3;',
    to: 'const highlightCache = /* @__PURE__ */ new Map();\nconst highlightHtmlCache = /* @__PURE__ */ new Map();\nconst MAX_CACHE_SIZE = 1e3;',
  },
  {
    file: 'node_modules/@lobehub/ui/es/hooks/useHighlight.mjs',
    from: '\t\tfor (const key of keysToRemove) highlightCache.delete(key);\n\t}',
    to: '\t\tfor (const key of keysToRemove) highlightCache.delete(key);\n\t\thighlightHtmlCache.delete(key);\n\t}',
  },
  {
    file: 'node_modules/@lobehub/ui/es/hooks/useHighlight.mjs',
    from: '\tconst [data, setData] = useState();',
    to: '\tconst [data, setData] = useState(() => cacheKey ? highlightHtmlCache.get(cacheKey) : void 0);',
  },
  {
    file: 'node_modules/@lobehub/ui/es/hooks/useHighlight.mjs',
    from: '\t\t\tcachedPromise.then((html) => {\n\t\t\t\tsetData(html);\n\t\t\t}).catch(() => {});',
    to: '\t\t\tcachedPromise.then((html) => {\n\t\t\t\thighlightHtmlCache.set(cacheKey, html);\n\t\t\t\tsetData(html);\n\t\t\t}).catch(() => {});',
  },
  {
    file: 'node_modules/@lobehub/ui/es/hooks/useHighlight.mjs',
    from: '\t\thighlightPromise.then((html) => {\n\t\t\tif (highlightCache.get(cacheKey) === highlightPromise) setData(html);\n\t\t}).catch(() => {',
    to: '\t\thighlightPromise.then((html) => {\n\t\t\tif (highlightCache.get(cacheKey) === highlightPromise) { highlightHtmlCache.set(cacheKey, html); setData(html); }\n\t\t}).catch(() => {',
  },
];

let patched = 0;
for (const patch of patches) {
  const filePath = join(root, patch.file);
  try {
    let content = readFileSync(filePath, 'utf-8');
    if (content.includes(patch.from)) {
      content = content.replace(patch.from, patch.to);
      writeFileSync(filePath, content, 'utf-8');
      console.log(`[patch] Fixed: ${patch.file}`);
      patched++;
    } else if (content.includes(patch.to)) {
      console.log(`[patch] Already applied: ${patch.file}`);
    } else {
      console.warn(`[patch] Pattern not found: ${patch.file}`);
    }
  } catch {
    console.warn(`[patch] File not found: ${patch.file}`);
  }
}
console.log(`[patch] ${patched} file(s) patched`);
