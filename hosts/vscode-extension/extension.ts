/**
 * AgentOS Host - VSCode 扩展入口
 *
 * 在 VSCode 内启动一个最小 HTTP 服务（Node http 模块，无第三方依赖），
 * 作为灵汐 vscode 连接器（plugins/shared/system/connectors/vscode/）的"对端"。
 *
 * 端点设计严格对齐 channel.py / connector.py：
 * - GET  /health  -> 200 { status: "ok" }（供 VSCodeChannel.is_available 探活）
 * - POST /context -> {
 *     active_file: string | null,
 *     selected_text: string | null,
 *     cursor_position: { line: number, column: number } | null,
 *     open_files: string[],
 *     metadata: object,
 *   }（对齐 channel.py _parse_context）
 * - POST /action  -> 接收 { action_type, parameters, action_id }
 *   支持：open_file / insert_content / show_diff（对齐 connector.py capabilities）
 *   返回 { success: boolean, data?: any, error?: string }
 * - GET  /capabilities -> { capabilities: string[] }（可选，便于排查）
 *
 * 注意：连接器侧统一使用 POST（channel.send_request 固定 POST），
 * 因此 /context 虽语义为获取，也按 POST 实现。
 */

import * as http from "http";
import * as vscode from "vscode";
import { URL } from "url";

/** 扩展名，用于日志前缀 */
const TAG = "[AgentOS Host]";

/** HTTP 服务实例 */
let server: http.Server | null = null;

/** 默认监听端口，需与灵汐 vscode 连接器（channel.py）保持一致 */
const DEFAULT_PORT = 9741;

/** 默认监听地址，仅本机访问 */
const DEFAULT_HOST = "127.0.0.1";

/**
 * 读取用户配置中的端口与地址。
 */
function readListenConfig(): { host: string; port: number } {
  const config = vscode.workspace.getConfiguration("agentosHost");
  const port = config.get<number>("port", DEFAULT_PORT);
  const host = config.get<string>("host", DEFAULT_HOST);
  return { host, port };
}

/**
 * 采集当前 VSCode 上下文，字段对齐 channel.py._parse_context。
 */
function collectContext(): {
  active_file: string | null;
  selected_text: string | null;
  cursor_position: { line: number; column: number } | null;
  open_files: string[];
  metadata: Record<string, unknown>;
} {
  const editor = vscode.window.activeTextEditor;

  let active_file: string | null = null;
  let selected_text: string | null = null;
  let cursor_position: { line: number; column: number } | null = null;

  if (editor) {
    active_file = editor.document.uri.fsPath || editor.document.uri.toString();
    selected_text = editor.selection.isEmpty ? null : editor.document.getText(editor.selection);
    cursor_position = {
      line: editor.selection.active.line,
      column: editor.selection.active.character,
    };
  }

  // 已打开的文件：取所有可见编辑器对应的文档路径（去重）
  const open_files: string[] = Array.from(
    new Set(
      vscode.window.visibleTextEditors
        .map((e) => e.document.uri.fsPath || e.document.uri.toString())
        .filter((p): p is string => Boolean(p))
    )
  );

  const metadata: Record<string, unknown> = {
    language_id: editor ? editor.document.languageId : null,
    workspace:
      vscode.workspace.workspaceFolders && vscode.workspace.workspaceFolders.length > 0
        ? vscode.workspace.workspaceFolders[0].uri.fsPath
        : null,
    extension_version: "0.2.0",
  };

  return { active_file, selected_text, cursor_position, open_files, metadata };
}

/**
 * 读取请求 body 并解析为 JSON。空 body 返回 {}。
 */
function readJsonBody(req: http.IncomingMessage): Promise<Record<string, unknown>> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    req.on("data", (chunk: Buffer) => chunks.push(chunk));
    req.on("end", () => {
      const raw = Buffer.concat(chunks).toString("utf-8").trim();
      if (!raw) {
        resolve({});
        return;
      }
      try {
        resolve(JSON.parse(raw) as Record<string, unknown>);
      } catch (err) {
        reject(err);
      }
    });
    req.on("error", reject);
  });
}

/** 统一写回 JSON 响应 */
function writeJson(res: http.ServerResponse, status: number, payload: unknown): void {
  const body = JSON.stringify(payload);
  res.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Content-Length": Buffer.byteLength(body),
  });
  res.end(body);
}

/**
 * 执行一个动作。对齐 connector.py 的 capabilities。
 * 返回 { success, data?, error? }。
 */
async function handleAction(
  actionType: string,
  parameters: Record<string, unknown>
): Promise<{ success: boolean; data?: unknown; error?: string }> {
  switch (actionType) {
    case "open_file": {
      const filePath = String(parameters.file ?? parameters.path ?? "");
      if (!filePath) {
        return { success: false, error: "缺少参数 file/path" };
      }
      try {
        const uri = vscode.Uri.file(filePath);
        const doc = await vscode.workspace.openTextDocument(uri);
        await vscode.window.showTextDocument(doc);
        return { success: true, data: { file: filePath } };
      } catch (err) {
        return { success: false, error: `打开文件失败: ${String(err)}` };
      }
    }

    case "insert_content": {
      const content = String(parameters.content ?? "");
      const filePath = parameters.file != null ? String(parameters.file) : null;
      try {
        let editor = vscode.window.activeTextEditor;
        // 若指定了文件且与当前活动编辑器不同，先打开它
        if (filePath) {
          const uri = vscode.Uri.file(filePath);
          const doc = await vscode.workspace.openTextDocument(uri);
          editor = await vscode.window.showTextDocument(doc);
        }
        if (!editor) {
          return { success: false, error: "没有可用的活动编辑器" };
        }
        // 可选 position: { line, column }；缺省使用当前光标
        let position: vscode.Position | undefined;
        const pos = parameters.position as { line?: number; column?: number } | undefined;
        if (pos && typeof pos.line === "number" && typeof pos.column === "number") {
          position = new vscode.Position(pos.line, pos.column);
        }
        const target = position ?? editor.selection.active;
        const ok = await editor.edit((builder) => builder.insert(target, content));
        return { success: ok, data: { inserted: content.length } };
      } catch (err) {
        return { success: false, error: `插入内容失败: ${String(err)}` };
      }
    }

    case "show_diff": {
      const left = String(parameters.left ?? parameters.original ?? "");
      const right = String(parameters.right ?? parameters.modified ?? "");
      const label = String(parameters.label ?? "AgentOS Diff");
      if (!left || !right) {
        return { success: false, error: "缺少参数 left/right" };
      }
      try {
        const leftUri = vscode.Uri.file(left).with({ scheme: "file" });
        const rightUri = vscode.Uri.file(right).with({ scheme: "file" });
        await vscode.commands.executeCommand("vscode.diff", leftUri, rightUri, label);
        return { success: true, data: { left, right } };
      } catch (err) {
        return { success: false, error: `显示差异失败: ${String(err)}` };
      }
    }

    default:
      return {
        success: false,
        error: `不支持的动作类型: ${actionType}（支持 open_file/insert_content/show_diff）`,
      };
  }
}

/** 创建 HTTP 服务 */
function createServer(): http.Server {
  const s = http.createServer(async (req, res) => {
    // 处理 CORS 预检
    if (req.method === "OPTIONS") {
      writeJson(res, 204, {});
      return;
    }

    let pathname = "/";
    try {
      pathname = new URL(req.url ?? "/", "http://localhost").pathname;
    } catch {
      // 保持默认
    }

    try {
      // GET /health
      if (req.method === "GET" && pathname === "/health") {
        writeJson(res, 200, { status: "ok", version: "0.2.0" });
        return;
      }

      // GET /capabilities
      if (req.method === "GET" && pathname === "/capabilities") {
        writeJson(res, 200, {
          capabilities: ["open_file", "insert_content", "show_diff"],
        });
        return;
      }

      // POST /context
      if (req.method === "POST" && pathname === "/context") {
        writeJson(res, 200, collectContext());
        return;
      }

      // POST /action
      if (req.method === "POST" && pathname === "/action") {
        const body = await readJsonBody(req);
        const actionType = String(body.action_type ?? "");
        const parameters =
          (body.parameters as Record<string, unknown> | undefined) ?? {};
        const result = await handleAction(actionType, parameters);
        writeJson(res, 200, result);
        return;
      }

      writeJson(res, 404, { error: `未知端点: ${req.method} ${pathname}` });
    } catch (err) {
      writeJson(res, 500, { success: false, error: String(err) });
    }
  });
  return s;
}

/** 启动 HTTP 服务 */
export async function startServer(): Promise<void> {
  if (server) {
    vscode.window.showInformationMessage("AgentOS Host 服务已在运行。");
    return;
  }
  const { host, port } = readListenConfig();
  const s = createServer();
  await new Promise<void>((resolve, reject) => {
    s.once("error", reject);
    s.listen(port, host, () => {
      s.removeListener("error", reject);
      console.info(`${TAG} HTTP 服务已启动: http://${host}:${port}`);
      resolve();
    });
  });
  server = s;
  vscode.window.showInformationMessage(`AgentOS Host 已启动 (${host}:${port})`);
}

/** 停止 HTTP 服务 */
export async function stopServer(): Promise<void> {
  if (!server) {
    return;
  }
  const s = server;
  server = null;
  await new Promise<void>((resolve) => {
    s.close(() => {
      console.info(`${TAG} HTTP 服务已停止`);
      resolve();
    });
  });
}

/**
 * 扩展激活入口。启动 HTTP 服务。
 */
export async function activate(context: vscode.ExtensionContext): Promise<void> {
  console.info(`${TAG} 扩展激活`);

  const startCmd = vscode.commands.registerCommand("agentosHost.start", () => {
    startServer().catch((err) => {
      vscode.window.showErrorMessage(`AgentOS Host 启动失败: ${String(err)}`);
    });
  });
  const stopCmd = vscode.commands.registerCommand("agentosHost.stop", () => {
    stopServer();
    vscode.window.showInformationMessage("AgentOS Host 已停止");
  });

  context.subscriptions.push(startCmd, stopCmd);

  // onStartupFinished 后自动启动
  await startServer().catch((err) => {
    vscode.window.showErrorMessage(`AgentOS Host 启动失败: ${String(err)}`);
  });
}

/**
 * 扩展停用入口。关闭 HTTP 服务。
 */
export async function deactivate(): Promise<void> {
  await stopServer();
}
