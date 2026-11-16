/**
 * *** DO NOT EDIT — GENERATED FILE ***
 *
 * render.card 卡型词汇表（审查总册 P1-20，AR-M5 单源化）。
 * 由 scripts/generate_render_card_vocabulary.py 扫描插件侧声明面生成：
 *   1. plugins/shared 下各插件 plugin.json 的 render.card 声明（第一方工具面）；
 *   2. plugins/shared/dsh_adapter/translator.py 的 DSH_TOOLVIEW_COMPONENT_MAP
 *      卡型映射（经 contributes.renderers 面供 dshAdapter 装载）。
 * 消费面：RENDER_CARD_VOCABULARY 供 renderIntent.ts（类型面 + 声明校验）；
 * DSH_RENDER_CARDS 供 dshAdapter/index.ts（贡献装载校验，translator 映射
 * 面子集）。
 * 手改必被 scripts/check_render_card_vocabulary_sync.py（重渲染逐字节一致
 * 闸）打回。增删卡型 = 改插件侧声明 → python
 * scripts/generate_render_card_vocabulary.py → 本生成物随动，连同 commit。
 */

/** 卡型词汇表（插件侧两声明面并集，字典序；声明校验与类型面的单源） */
export const RENDER_CARD_VOCABULARY = [
  'diff',
  'file',
  'file_card',
  'form',
  'generic',
  'image',
  'read',
  'search',
  'table',
  'task_card',
  'terminal',
  'web',
] as const

/** 卡型联合类型（renderIntent 的 RenderIntentCard 单源） */
export type RenderCardValue = (typeof RENDER_CARD_VOCABULARY)[number]

/** DSH 适配器面卡型子集（translator 卡型映射面，字典序；dshAdapter 贡献装载校验用） */
export const DSH_RENDER_CARDS = [
  'diff',
  'generic',
  'read',
  'search',
  'terminal',
  'web',
] as const
