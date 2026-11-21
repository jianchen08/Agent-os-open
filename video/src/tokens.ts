/**
 * 设计 tokens —— 全部取自产品自身的设计系统，不另造宣传片皮肤。
 *
 * 来源：
 * - frontend/src/styles/deep-space-v2.css（暗色默认：品牌色 / 背景 / 文本 / 状态色）
 * - frontend/src/styles/design-tokens.css（radius / 字体族 / 字号语义阶梯）
 * - plugins/shared/mode_godot/webview/godot_panel.html（插件面板同源 token）
 *
 * 动效性格 tokens 由「品牌→动效参数」推导：产品是游戏开发工具（高能量）+ 工程感
 * （严肃调性），取「专业信赖」为主、落地动作给少量过冲（y1>1）。
 */

export const C = {
  // 背景（deep-space-v2 暗色默认）
  canvas: '#04060F',
  panel: '#0A1226',
  elevated: '#111C38',
  hover: '#1A2748',
  // 品牌
  accent: '#22D3EE',
  ai: '#A78BFA',
  blue: '#3B82F6',
  glow: '#00F0FF',
  // 文本
  text: '#F1F5F9',
  text2: '#CBD5E1',
  muted: '#94A3B8',
  disabled: '#64748B',
  // 边框
  border: 'rgba(148,163,184,0.12)',
  borderStrong: 'rgba(148,163,184,0.22)',
  borderActive: 'rgba(34,211,238,0.45)',
  // 状态
  running: '#22D3EE',
  waiting: '#FBBF24',
  success: '#34D399',
  error: '#F87171',
  info: '#60A5FA',
} as const;

export const FONT = {
  ui: "'Noto Sans SC', system-ui, sans-serif",
  mono: "'JetBrains Mono', ui-monospace, monospace",
} as const;

export const R = {
  sm: 6,
  md: 8,
  lg: 10,
  xl: 12,
  bubble: 16,
} as const;

/** Godot 编辑器（手搓还原的主色）——按 Godot 4 默认暗色主题取色 */
export const G = {
  win: '#26282C',
  panel: '#2C2F33',
  panelAlt: '#303338',
  border: '#1B1D20',
  text: '#E3E6E8',
  muted: '#8E959B',
  sel: '#3C6E9B',
  accent: '#4E9A96',
  viewport: '#101619',
  grid: '#232B31',
  axisX: '#D2635A',
  axisY: '#7FBF6A',
  axisZ: '#5A8FE0',
} as const;

export const FPS = 30;

/** 入场/动作缓动（品牌→动效参数：专业信赖，落地动作允许轻微过冲） */
export const E = {
  /** 标准入场：快出慢收 */
  out: 'cubic-bezier(0.16,1,0.3,1)',
  /** 平稳推移 */
  smooth: 'cubic-bezier(0.4,0,0.2,1)',
  /** 落地：允许过冲（y1 > 1） */
  land: 'cubic-bezier(0.34,1.56,0.64,1)',
  /** 等速/线性（仅用于扫描、进度类） */
  linear: 'linear',
} as const;

/** 确定性伪随机：禁 Math.random，seed 从 index 派生 */
export const mulberry32 = (seed: number) => {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
};
