/**
 * 配置编辑器「加载失败 = 只读/禁存」共用守卫（FE1/FE2 兜底反模式修复）。
 *
 * 约束：配置加载失败时 config 必须保持 null（编辑区不渲染）、保存入口必须禁用——
 * 失败后的空态一旦可保存，用户点保存就会把空对象覆盖写回内核实际执行的
 * 配置文件（autonomous.yaml / 插件配置文件）。任一条件命中即禁存：
 * 保存中 / 加载失败 / 配置未加载（null）。
 */

/** 是否应禁用配置保存按钮（saving=保存中；loadError=加载失败；config=当前配置） */
export function shouldDisableConfigSave(
  saving: boolean,
  loadError: string | null,
  config: Record<string, unknown> | null,
): boolean {
  return saving || loadError !== null || config === null
}
