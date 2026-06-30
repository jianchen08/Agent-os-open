/**
 * 独立 SVG 预处理测试运行器
 * 直接复用 shared.tsx 中的函数逻辑，在 Node.js 中运行
 */
const fs = require('fs');
const path = require('path');
const { execSync } = require('child_process');

// ============================================================
// 源码函数 (从 shared.tsx 提取，保持逻辑完全一致)
// ============================================================

/**
 * SVG XSS 安全过滤：移除 script 标签和 on* 事件属性
 */
function sanitizeSvg(svg) {
  return svg
    .replace(/<script[\s\S]*?<\/script>/gi, '')
    .replace(/\son\w+\s*=\s*"[^"]*"/gi, '')
    .replace(/\son\w+\s*=\s*'[^']*'/gi, '')
    .replace(/\son\w+\s*=\s*[^\s>]+/gi, '')
}

/**
 * 将 ```svg 代码块预处理为 img data URI，使用 Base64 编码
 */
function preprocessSvgCodeBlocks(content) {
  if (!content) return content

  return content.replace(/```svg\s*\n([\s\S]*?)```/gi, (_, svgCode) => {
    const sanitized = sanitizeSvg(svgCode.trim())
    const encoded = typeof window !== 'undefined'
      ? window.btoa(unescape(encodeURIComponent(sanitized)))
      : Buffer.from(sanitized, 'utf-8').toString('base64')
    return `![svg](data:image/svg+xml;base64,${encoded})`
  })
}

// ============================================================
// 测试运行器
// ============================================================

let passed = 0;
let failed = 0;
const failures = [];

function assert(condition, message) {
  if (condition) {
    passed++;
  } else {
    failed++;
    failures.push(message);
    console.error(`  FAIL: ${message}`);
  }
}

function test(name, fn) {
  console.log(`\n▶ ${name}`);
  try {
    fn();
  } catch (e) {
    failed++;
    failures.push(`${name}: ${e.message}`);
    console.error(`  ERROR: ${e.message}`);
  }
}

// ============================================================
// sanitizeSvg 测试 (6个)
// ============================================================

console.log('\n========================================');
console.log('describe: sanitizeSvg');
console.log('========================================');

test('保留合法 SVG 元素和属性', () => {
  const input = '<svg><circle cx="50" cy="50" r="40" fill="red"/></svg>';
  const result = sanitizeSvg(input);
  assert(result.includes('<circle'), '应包含 <circle');
  assert(result.includes('cx="50"'), '应包含 cx="50"');
  assert(result.includes('fill="red"'), '应包含 fill="red"');
});

test('移除 <script> 标签', () => {
  const input = '<svg><script>alert("xss")</script><rect width="100" height="100"/></svg>';
  const result = sanitizeSvg(input);
  assert(!result.toLowerCase().includes('<script'), '不应包含 <script');
  assert(!result.includes('alert'), '不应包含 alert');
  assert(result.includes('<rect'), '应包含 <rect');
});

test('移除 on* 事件处理属性（双引号）', () => {
  const input = '<svg><rect onload="alert(1)" width="100" height="100"/></svg>';
  const result = sanitizeSvg(input);
  assert(!result.toLowerCase().includes('onload'), '不应包含 onload');
  assert(result.includes('<rect'), '应包含 <rect');
});

test('移除 on* 事件处理属性（单引号）', () => {
  const input = "<svg><rect onclick='evil()' width='10' height='10'/></svg>";
  const result = sanitizeSvg(input);
  assert(!result.toLowerCase().includes('onclick'), '不应包含 onclick');
  assert(result.includes('width'), '应包含 width');
});

test('移除 on* 事件处理属性（无引号）', () => {
  const input = '<svg><rect onmouseover=alert(1) width="10"/></svg>';
  const result = sanitizeSvg(input);
  assert(!result.toLowerCase().includes('onmouseover'), '不应包含 onmouseover');
  assert(result.includes('width'), '应包含 width');
});

test('保留文本内容', () => {
  const input = '<svg><text x="10" y="20">Hello SVG</text></svg>';
  const result = sanitizeSvg(input);
  assert(result.includes('Hello SVG'), '应保留文本 Hello SVG');
});

// ============================================================
// preprocessSvgCodeBlocks 测试 (11个)
// ============================================================

console.log('\n========================================');
console.log('describe: preprocessSvgCodeBlocks');
console.log('========================================');

test('将 ```svg 代码块转换为 img data URI（Base64 编码）', () => {
  const input = '```svg\n<svg width="50"><circle r="20"/></svg>\n```';
  const result = preprocessSvgCodeBlocks(input);
  assert(!result.includes('```svg'), '不应包含原始 ```svg');
  assert(result.includes('![svg]'), '应包含 ![svg]');
  assert(result.includes('data:image/svg+xml;base64,'), '应包含 base64 data URI');
});

test('对 SVG 内容做 XSS 过滤后再编码', () => {
  const input = '```svg\n<svg><script>alert(1)</script><rect/></svg>\n```';
  const result = preprocessSvgCodeBlocks(input);
  assert(!result.includes('```svg'), '不应包含原始 ```svg');
  assert(result.includes('![svg]'), '应包含 ![svg]');
  assert(!result.includes('alert'), '不应包含 alert（XSS已过滤）');
});

test('不影响其他代码块', () => {
  const input = '```javascript\nconst x = 1;\n```';
  const result = preprocessSvgCodeBlocks(input);
  assert(result === input, 'javascript 代码块应原样返回');
});

test('不影响 mermaid 代码块', () => {
  const input = '```mermaid\ngraph TD; A-->B;\n```';
  const result = preprocessSvgCodeBlocks(input);
  assert(result === input, 'mermaid 代码块应原样返回');
});

test('不影响没有代码块的纯文本', () => {
  const input = 'Hello world, this is plain text.';
  const result = preprocessSvgCodeBlocks(input);
  assert(result === input, '纯文本应原样返回');
});

test('处理多个 svg 代码块', () => {
  const input = '```svg\n<svg><rect/>\n```\nText\n```svg\n<svg><circle/>\n```';
  const result = preprocessSvgCodeBlocks(input);
  assert(!result.includes('```svg'), '不应包含原始 ```svg');
  const imgCount = (result.match(/!\[svg\]/g) || []).length;
  assert(imgCount === 2, `应有2个图片，实际 ${imgCount}`);
});

test('svg 代码块与普通代码块混合时只转换 svg', () => {
  const input = [
    'Some text',
    '```svg',
    '<svg><rect/>',
    '```',
    'More text',
    '```python',
    'print("hello")',
    '```',
  ].join('\n');
  const result = preprocessSvgCodeBlocks(input);
  assert(result.includes('![svg]'), '应包含 ![svg]');
  assert(result.includes('```python'), 'python 代码块应保留');
  assert(result.includes('print'), 'python 内容应保留');
});

test('SVG 内容含括号时不破坏 markdown 图片链接（Base64 编码验证）', () => {
  const input = '```svg\n<svg><g transform="translate(10,20)">text</g></svg>\n```';
  const result = preprocessSvgCodeBlocks(input);
  assert(!result.includes('```svg'), '不应包含原始 ```svg');
  assert(result.includes('![svg]'), '应包含 ![svg]');
  assert(!result.includes('translate(10,20)'), '不应出现明文括号');
  const match = result.match(/^!\[svg\]\(data:image\/svg\+xml;base64,[A-Za-z0-9+/=]+\)$/);
  assert(match !== null, '应是完整的单行 markdown 图片语法（仅含 Base64 字符集）');
});

test('SVG 含 <style> 标签时仍能正常编码', () => {
  const input = '```svg\n<svg><style>.cls{fill:red}</style><rect class="cls"/></svg>\n```';
  const result = preprocessSvgCodeBlocks(input);
  assert(result.includes('![svg]'), '应包含 ![svg]');
  assert(result.includes('data:image/svg+xml;base64,'), '应包含 base64 data URI');
});

test('空 SVG 代码块不崩溃', () => {
  const input = '```svg\n```';
  const result = preprocessSvgCodeBlocks(input);
  assert(result.includes('![svg]'), '应包含 ![svg]');
  assert(result.includes('data:image/svg+xml;base64,'), '应包含 base64 data URI');
});

test('大写 ```SVG 代码块标识也能匹配', () => {
  const input = '```SVG\n<svg><rect/>\n```';
  const result = preprocessSvgCodeBlocks(input);
  assert(!result.includes('```SVG'), '不应包含原始 ```SVG');
  assert(result.includes('![svg]'), '应包含 ![svg]');
});

// ============================================================
// 用户旅程测试 (11个场景)
// ============================================================

console.log('\n========================================');
console.log('describe: 用户旅程 — SVG 渲染完整链路');
console.log('========================================');

test('场景1: SVG 代码块 → Base64 图片 → Base64 解码内容正确', () => {
  const input = '```svg\n<svg width="50" height="50"><circle cx="25" cy="25" r="20" fill="blue"/></svg>\n```';
  const output = preprocessSvgCodeBlocks(input);
  assert(output.includes('![svg]'), '应包含 ![svg]');
  assert(output.includes('data:image/svg+xml;base64,'), '应包含 base64 data URI');
  const b64 = output.match(/base64,([A-Za-z0-9+/=]+)\)/)?.[1];
  assert(b64 !== undefined, 'Base64 内容应存在');
  const decoded = Buffer.from(b64, 'base64').toString('utf-8');
  assert(decoded.includes('<circle'), '解码后应包含 <circle');
  assert(decoded.includes('fill="blue"'), '解码后应包含 fill="blue"');
  const match = output.match(/^!\[svg\]\(data:image\/svg\+xml;base64,[A-Za-z0-9+/=]+\)$/);
  assert(match !== null, '应是完整的单行 markdown 图片语法');
});

test('场景2: Mermaid 代码块原样保留', () => {
  const input = '```mermaid\ngraph TD; A-->B;\n```';
  const output = preprocessSvgCodeBlocks(input);
  assert(output === input, '应原样返回');
  assert(output.includes('```mermaid'), '应包含 ```mermaid');
  assert(output.includes('graph TD'), '应包含 graph TD');
});

test('场景3: XSS 安全过滤 — script 标签 + onload 事件均移除', () => {
  const malicious = '<svg><script>alert("xss")</script><rect onload="evil()" width="10"/></svg>';
  const cleaned = sanitizeSvg(malicious);
  assert(!cleaned.toLowerCase().includes('<script'), '不应包含 <script');
  assert(!cleaned.includes('alert'), '不应包含 alert');
  assert(!cleaned.toLowerCase().includes('onload'), '不应包含 onload');
  assert(!cleaned.includes('evil'), '不应包含 evil');
  assert(cleaned.includes('<rect'), '应保留 <rect');
});

test('场景4: 不影响 javascript / python 等普通代码块', () => {
  const input = '```javascript\nconst x = 1;\n```\n\n```python\nprint("hello")\n```';
  const output = preprocessSvgCodeBlocks(input);
  assert(output === input, '应原样返回');
  assert(output.includes('```javascript'), '应保留 javascript 代码块');
  assert(output.includes('```python'), '应保留 python 代码块');
});

test('场景5: SVG 含括号的 transform 属性 Base64 编码正确', () => {
  const input = '```svg\n<svg><g transform="translate(10,20)">text</g></svg>\n```';
  const output = preprocessSvgCodeBlocks(input);
  assert(output.includes('![svg]'), '应包含 ![svg]');
  assert(!output.includes('translate(10,20)'), '不应包含明文括号');
  const match = output.match(/^!\[svg\]\(data:image\/svg\+xml;base64,[A-Za-z0-9+/=]+\)$/);
  assert(match !== null, '应是完整的单行 markdown 图片语法');
  const b64 = output.match(/base64,([A-Za-z0-9+/=]+)\)/)?.[1];
  const decoded = Buffer.from(b64, 'base64').toString('utf-8');
  assert(decoded.includes('translate(10,20)'), '解码后应包含 translate(10,20)');
  assert(decoded.includes('text'), '解码后应包含 text');
});

test('组合场景: SVG+XSS+Mermaid+TypeScript 混合内容', () => {
  const combo = [
    'Here is a diagram:',
    '',
    '```mermaid',
    'graph LR; A-->B;',
    '```',
    '',
    'And an SVG:',
    '',
    '```svg',
    '<svg><script>bad()</script><rect onload="evil()" width="20"/></svg>',
    '```',
    '',
    '```typescript',
    'const x: number = 42;',
    '```',
  ].join('\n');
  const output = preprocessSvgCodeBlocks(combo);
  assert(output.includes('```mermaid'), '应保留 mermaid');
  assert(output.includes('graph LR'), '应保留 graph LR');
  assert(output.includes('![svg]'), 'SVG 应转为图片');
  assert(!output.includes('```svg'), '不应包含 ```svg');
  assert(!output.includes('<script'), '不应包含 <script');
  assert(!output.includes('evil'), '不应包含 evil');
  assert(output.includes('```typescript'), '应保留 typescript');
  assert(output.includes('const x'), '应保留 const x');
});

console.log('\n========================================');
console.log('describe: 补充场景 — 边界与异常');
console.log('========================================');

test('空字符串输入不崩溃', () => {
  assert(preprocessSvgCodeBlocks('') === '', '空字符串应返回空字符串');
  assert(sanitizeSvg('') === '', 'sanitizeSvg 空字符串应返回空字符串');
});

test('null/undefined 输入不崩溃', () => {
  assert(preprocessSvgCodeBlocks(null) === null, 'null 应返回 null');
  assert(preprocessSvgCodeBlocks(undefined) === undefined, 'undefined 应返回 undefined');
});

test('只有 ```svg 开头没有结尾 — 不匹配，原样返回', () => {
  const input = '```svg\n<svg><rect/></svg>';
  const output = preprocessSvgCodeBlocks(input);
  assert(output === input, '未闭合代码块应原样返回');
});

test('多个 on* 事件属性全部移除', () => {
  const input = '<svg onclick="a()" onmouseover="b()" onerror="c()" onload="d()"><rect/></svg>';
  const output = sanitizeSvg(input);
  assert(!output.toLowerCase().includes('onclick'), '不应包含 onclick');
  assert(!output.toLowerCase().includes('onmouseover'), '不应包含 onmouseover');
  assert(!output.toLowerCase().includes('onerror'), '不应包含 onerror');
  assert(!output.toLowerCase().includes('onload'), '不应包含 onload');
  assert(output.includes('<rect'), '应保留 <rect');
});

test('大写 SVG 标识匹配', () => {
  const input = '```SVG\n<svg><rect/></svg>\n```';
  const output = preprocessSvgCodeBlocks(input);
  assert(output.includes('![svg]'), '应包含 ![svg]');
  assert(!output.includes('```SVG'), '不应包含 ```SVG');
});

// ============================================================
// Base64 编码一致性验证
// ============================================================

console.log('\n========================================');
console.log('Base64 编码/解码一致性验证');
console.log('========================================');

test('含括号的 SVG Base64 编码后解码内容完整', () => {
  const original = '<svg><g transform="translate(10,20)"><circle r="5"/></g></svg>';
  const encoded = Buffer.from(original, 'utf-8').toString('base64');
  const decoded = Buffer.from(encoded, 'base64').toString('utf-8');
  assert(decoded === original, `解码内容应与原始一致\n  原始: ${original}\n  解码: ${decoded}`);
  // 验证 Base64 字符集不含括号
  assert(/^[A-Za-z0-9+/=]+$/.test(encoded), `Base64 应只含合法字符，实际: ${encoded}`);
  assert(!encoded.includes('('), 'Base64 不应含 (');
  assert(!encoded.includes(')'), 'Base64 不应含 )');
});

test('preprocessSvgCodeBlocks 输出的 data URI 解码后内容正确', () => {
  const svgContent = '<svg width="100" height="100"><rect width="50" height="50" fill="green"/><text x="10" y="20">Test</text></svg>';
  const input = '```svg\n' + svgContent + '\n```';
  const output = preprocessSvgCodeBlocks(input);
  const b64 = output.match(/base64,([A-Za-z0-9+/=]+)\)/)?.[1];
  assert(b64 !== undefined, '应提取到 Base64 内容');
  const decoded = Buffer.from(b64, 'base64').toString('utf-8');
  assert(decoded.includes('<rect'), '解码后应包含 <rect');
  assert(decoded.includes('fill="green"'), '解码后应包含 fill="green"');
  assert(decoded.includes('Test'), '解码后应包含文本 Test');
  assert(!decoded.includes('<script'), '解码后不应包含 <script');
});

test('preprocessSvgCodeBlocks 输出的 data URI 解码后无 XSS', () => {
  const svgContent = '<svg><script>alert(1)</script><rect onload="evil()"/></svg>';
  const input = '```svg\n' + svgContent + '\n```';
  const output = preprocessSvgCodeBlocks(input);
  const b64 = output.match(/base64,([A-Za-z0-9+/=]+)\)/)?.[1];
  const decoded = Buffer.from(b64, 'base64').toString('utf-8');
  assert(!decoded.includes('<script'), '解码后不应包含 <script');
  assert(!decoded.toLowerCase().includes('onload'), '解码后不应包含 onload');
  assert(decoded.includes('<rect'), '解码后应包含 <rect');
});

// ============================================================
// 汇总
// ============================================================

console.log('\n\n=============================================');
console.log('              测试结果汇总');
console.log('=============================================');
console.log(`  总计: ${passed + failed}`);
console.log(`  通过: ${passed}`);
console.log(`  失败: ${failed}`);
console.log(`  通过率: ${((passed / (passed + failed)) * 100).toFixed(1)}%`);
if (failures.length > 0) {
  console.log('\n  失败详情:');
  failures.forEach((f, i) => console.log(`    ${i + 1}. ${f}`));
}
console.log('=============================================\n');

process.exit(failed > 0 ? 1 : 0);
