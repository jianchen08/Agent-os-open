/**
 * 思考模式类型定义
 */

/**
 * 思考选择值：thinking_strength_params 配置的参数组 JSON 串（选项即参数组，
 * llm_service thinking-levels 端点下发），选中即随消息透传、llm_core 解析后
 * 白名单过滤直覆盖。'' = 未选择/不覆盖（显示占位、发送不带思考参数）。
 */
export type ThinkingStrength = string
