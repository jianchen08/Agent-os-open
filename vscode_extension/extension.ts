/**
 * Agent OS VSCode Extension — 接口定义文件
 *
 * 本文件仅作为接口定义和协议说明，不需要实际可运行的 TS 代码。
 * 定义与 Agent OS 后端的通信协议接口。
 */

// ============================================================
// 类型定义
// ============================================================

/** 光标位置 */
interface CursorPosition {
  /** 行号（从 0 开始） */
  line: number;
  /** 列号（从 0 开始） */
  column: number;
}

/** 连接器上下文数据 — 从 IDE 推送到 Agent OS */
interface ContextMessage {
  /** 消息类型标识，固定为 "context" */
  type: "context";
  /** 活动文件路径 */
  active_file: string | null;
  /** 选中的文本 */
  selected_text: string | null;
  /** 光标位置 */
  cursor_position: CursorPosition | null;
  /** 所有打开的文件列表 */
  open_files: string[];
  /** 额外元数据 */
  metadata: Record<string, unknown>;
}

/** 操作指令 — 从 Agent OS 发送到 IDE */
interface ActionMessage {
  /** 消息类型标识，固定为 "action" */
  type: "action";
  /** 操作类型（open_file / insert_content / jump_to / show_diff） */
  action_type: string;
  /** 操作参数 */
  parameters: Record<string, unknown>;
  /** 操作唯一标识 */
  action_id: string;
}

/** 状态更新通知 */
interface StateMessage {
  /** 消息类型标识，固定为 "state" */
  type: "state";
  /** 连接器状态（disconnected / connecting / connected / active / disconnecting / error） */
  state: string;
  /** 可选的状态详情 */
  detail?: string;
}

/** 操作执行结果 */
interface ActionResultMessage {
  /** 消息类型标识，固定为 "action_result" */
  type: "action_result";
  /** 对应的 action_id */
  action_id: string;
  /** 是否成功 */
  success: boolean;
  /** 返回数据 */
  data?: unknown;
  /** 错误信息 */
  error?: string;
}

/** 统一消息类型 */
type AgentOSMessage = ContextMessage | ActionMessage | StateMessage | ActionResultMessage;

// ============================================================
// Agent OS 通信协议接口
// ============================================================

/**
 * AgentOSClient — 与 Agent OS 后端通信的客户端接口。
 *
 * 职责：
 * - pushContext(context) → 向 Agent OS 推送上下文
 * - onAction(callback)   → 监听 Agent OS 的操作指令
 * - updateState(state)   → 通知连接器状态变更
 */
interface AgentOSClient {
  /**
   * 向 Agent OS 推送 IDE 上下文。
   *
   * @param context - IDE 当前上下文数据
   * @returns 推送是否成功
   */
  pushContext(context: ContextMessage): Promise<boolean>;

  /**
   * 注册操作指令监听器。
   *
   * @param callback - 收到操作指令时的回调函数
   */
  onAction(callback: (action: ActionMessage) => Promise<ActionResultMessage>): void;

  /**
   * 通知 Agent OS 连接器状态变更。
   *
   * @param state - 新的连接器状态
   */
  updateState(state: StateMessage): Promise<void>;
}

// ============================================================
// VSCode 扩展入口（桩实现）
// ============================================================

/**
 * 扩展激活入口。
 *
 * @param context - VSCode 扩展上下文
 */
function activate(context: unknown): void {
  // 桩实现：注册命令、初始化 AgentOSClient
}

/**
 * 扩展停用入口。
 */
function deactivate(): void {
  // 桩实现：清理资源
}

export { activate, deactivate };
export type {
  AgentOSClient,
  ActionMessage,
  ActionResultMessage,
  AgentOSMessage,
  ContextMessage,
  CursorPosition,
  StateMessage,
};
