/** @feature FP-T12 PILOT-03 回退确认条对比度验收 | @ci: frontend-test */
/**
 * PILOT-03 签发验收测试：回退确认条对比度达 WCAG AA 小字线（浅色主题）。
 *
 * @feature PILOT-03 签发分离试点·回退确认条对比度 | @vision V6 可即用
 *
 * 需求来源：docs/working/E轨迹汇聚落地_台账.md R32 观察记录①（回退确认条
 * 文字 12px 小字对比度 4.08:1，低于 WCAG AA 小字线 4.5:1；"未来微调候选：
 * warning 文字深一档或底 tint 降档（一处 className）"）。
 *
 * 权威锚点（前置契约）：
 * - 组件：frontend/src/components/chat/MessageActions.tsx renderRollbackConfirm
 *   （data-testid="rollback-confirm"；文字色 text-[var(--status-warning)]，
 *   底色 bg-[rgb(var(--status-warning-rgb,217_119_6)/0.15)]）
 * - 主题真源：frontend/src/styles/design-tokens.css
 *   （--status-warning: var(--status-waiting-color)；浅色段 #D97706、深色段 #fbbf24；
 *   解析契约 = 按文件出现顺序取第 1 处为浅色、第 2 处为深色，主题组织方式
 *   变更属契约变更，回签发复核）
 * - 修前基线：4.08:1（R32 台账 computed 口径，白底合成）
 *
 * 已知歧义（冻结前澄清项 C1）：R32 台账观察 computed 值 rgb(180,83,9) 与
 * design-tokens.css 的 #D97706 不一致——运行时真源（themeService 镜像或
 * 其他覆盖）待冻结裁定；本草案解析 design-tokens.css 并在失败报告中带来源。
 *
 * 起草方：签发分离试点起草轮（2026-10-28）。
 * 状态：**已冻结 v1.0**（2026-10-28 签发轮：复检通过后由签发轮落位于本路径，断言与逻辑零改动）。
 * 签发记录（复检方/复检结论/签发方/C1-C3 裁决/F3·F4' 处置留档）：
 * docs/working/签发分离试点_20261028/试点方案.md §九。
 *
 * 可渲染约定：测试名含需求 ID + 场景； arrange/act/assert 分段；失败报告
 * 人类化（带实际计算对比度与解析来源）；两主题参数化 + token 族性质断言；
 * 对比度从源码实时解析计算——改色/改 alpha 必须真实提升对比度才能通过，
 * 硬编码期望值无法骗过。
 */
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

// --- 输入数据（前置契约锚点，结构化声明） ---

// REPO_ROOT 按落位形态锚定：本件正式路径 = frontend/src/components/chat/__tests__/，
// 从该位向上 5 层（__tests__→chat→components→src→frontend）即仓根。
// 处于 docs/ 草稿位时此解析不生效，属预期（草案未激活）；激活即正确，无绝对路径硬编码。
const REPO_ROOT = resolve(
  dirname(fileURLToPath(import.meta.url)),
  '../../../../..',
);
const COMPONENT = `${REPO_ROOT}/frontend/src/components/chat/MessageActions.tsx`;
const TOKENS = `${REPO_ROOT}/frontend/src/styles/design-tokens.css`;

/** 修前基线（R32 台账口径）：浅色主题 computed 对比度 4.08:1 */
const BASELINE_CONTRAST = 4.08;
/** WCAG AA 小字线（12px 文字适用 4.5:1，不适用 3:1 大字线） */
const AA_SMALL_TEXT = 4.5;

/** 主题参数化输入（≥2 组有区分度输入）：色值真源 = design-tokens.css 按出现顺序 */
const THEMES = [
  { name: '浅色', occurrence: 1, fallbackBase: [255, 255, 255], note: '白底合成口径与 R32 台账同源' },
  { name: '深色', occurrence: 2, fallbackBase: null, note: '深色气泡底真源未确认（澄清项 C2），见 skip 用例' },
] as const;

// --- 解析与计算辅助（纯函数，无副作用） ---

function srgbToLinear(c: number): number {
  return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
}

function relativeLuminance(rgb: readonly [number, number, number]): number {
  const [r, g, b] = rgb.map((v) => srgbToLinear(v / 255));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrastRatio(
  a: readonly [number, number, number],
  b: readonly [number, number, number],
): number {
  const la = relativeLuminance(a);
  const lb = relativeLuminance(b);
  const [hi, lo] = la >= lb ? [la, lb] : [lb, la];
  return (hi + 0.05) / (lo + 0.05);
}

function parseHex(hex: string): [number, number, number] {
  const m = hex.trim().match(/^#([0-9a-fA-F]{6})$/);
  expect(m, `hex 解析失败: ${hex}`).toBeTruthy();
  const n = parseInt(m![1], 16);
  return [(n >> 16) & 0xff, (n >> 8) & 0xff, n & 0xff];
}

/** 从 design-tokens.css 按出现顺序取第 occurrence 处 --status-waiting-color */
function waitingColor(occurrence: number): [number, number, number] {
  const css = readFileSync(TOKENS, 'utf-8');
  const re = /--status-waiting-color:\s*(#[0-9a-fA-F]{6})/g;
  const hits = [...css.matchAll(re)];
  expect(
    hits.length,
    `design-tokens.css 中 --status-waiting-color 出现 ${hits.length} 处，主题组织方式可能已变更（契约：第 1 处浅色/第 2 处深色）——回签发复核`,
  ).toBeGreaterThanOrEqual(occurrence);
  return parseHex(hits[occurrence - 1][1]);
}

/** 回退确认条当前形态契约：文字色 token 名与 tint alpha */
function rollbackConfirmClass(): string {
  const src = readFileSync(COMPONENT, 'utf-8');
  // className 声明紧邻 data-testid="rollback-confirm" 之前（组件 JSX 现状结构）
  const m = src.match(/className="([^"]*)"\s*\n\s*data-testid="rollback-confirm"/);
  expect(
    m,
    'MessageActions.tsx 中未找到 rollback-confirm 的 className（组件结构变更——回签发复核）',
  ).toBeTruthy();
  return m![1];
}

describe('TestPILOT03回退确认条对比度', () => {
  it.each([THEMES[0]])(
    'test_PILOT_03_浅色主题回退确认条对比度达AA小字线（$name）',
    (theme) => {
      // --- 输入数据 ---
      // 文字色真源 + 组件 tint alpha（结构化声明，见模块头锚点）

      // --- 执行 ---
      const cls = rollbackConfirmClass();
      const textColor = waitingColor(theme.occurrence);
      // 底色 = 文字色 15% tint 叠加白底（从 className 提取 alpha，防实现改 alpha 后口径失真）
      // 形状契约：rgb(var(--status-warning-rgb,<fallback>)/<alpha>)——回退值在 alpha
      // 斜杠之前，故取色名后第一个 '/' 前的整段（原正则排除 ')' 导致永远取不到）
      const tintMatch = cls.match(/status-warning-rgb[^/]*\/\s*([\d.]+)/);
      const alpha = tintMatch ? parseFloat(tintMatch[1]) : NaN;
      const base = theme.fallbackBase!;
      // 真实合成：bg_channel = alpha * text_channel + (1 - alpha) * base_channel
      const realBg = [0, 1, 2].map(
        (i) => alpha * textColor[i] + (1 - alpha) * base[i],
      ) as [number, number, number];
      const ratio = contrastRatio(textColor, realBg);

      // --- 输出断言 ---
      expect(
        alpha,
        `[PILOT-03] 场景: ${theme.name}主题tint alpha 提取\n  输入: ${cls}\n  期望: 从 bg-[rgb(var(--status-warning-rgb,...)/alpha)] 提取到 alpha\n  实际: ${alpha}\n  定位: rollback-confirm className`,
      ).toBeGreaterThan(0);
      expect(ratio, `[PILOT-03] 场景: ${theme.name}主题对比度\n  输入: 文字色=${textColor} alpha=${alpha} 底=${realBg}\n  期望: ≥${AA_SMALL_TEXT}（AA 小字线；修前基线 ${BASELINE_CONTRAST}:1）\n  实际: ${ratio.toFixed(2)}:1\n  定位: ${COMPONENT} rollback-confirm + ${TOKENS}（${theme.note}）`).toBeGreaterThanOrEqual(
        AA_SMALL_TEXT,
      );
      // 性质断言：修复必须严格优于修前基线（≥4.5 已蕴含，此处保留显式记录）
      expect(ratio, `[PILOT-03] 修复须优于修前基线 ${BASELINE_CONTRAST}:1，实际 ${ratio.toFixed(2)}:1`).toBeGreaterThan(BASELINE_CONTRAST);
    },
  );

  it('test_PILOT_03_回退确认条配色未引入新token族_仍为warning族引用', () => {
    // --- 输入数据 ---
    // 族契约：文字 = var(--status-warning)；底 = var(--status-warning-rgb) 的 alpha tint

    // --- 执行 ---
    const cls = rollbackConfirmClass();

    // --- 输出断言 ---
    expect(cls, `[PILOT-03] 文字色族\n  实际 className: ${cls}\n  期望: text-[var(--status-warning)] 族引用在场（不引入新 token 族，设计系统一致性）`).toContain(
      'var(--status-warning)',
    );
    expect(cls, `[PILOT-03] 底色族\n  实际 className: ${cls}\n  期望: bg 走 --status-warning-rgb tint（同族）`).toContain(
      '--status-warning-rgb',
    );

    // --- 副作用断言 ---
    // 无副作用：只读组件与 token 源文件，不写任何状态。
  });

  // 澄清项 C2：深色主题气泡底真源未确认（design-tokens.css 深色段未见气泡/面板底 token）。
  // 断言前提（合成基准底）缺失，显式延后并列入选点方案文档「冻结前澄清项」；
  // 冻结裁定补齐真源后，本用例由签发复核解除 skip——不虚构基准底造断言。
  it.skip('test_PILOT_03_深色主题回退确认条对比度达AA小字线（待澄清项C2：深色底真源）', () => {
    expect.unreachable('冻结裁定补齐深色气泡底真源后实现本用例');
  });
});
