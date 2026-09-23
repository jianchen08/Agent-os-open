/**
 * 认证会话镜像（主进程侧持久化）：refresh token 的强杀耐久备份。
 *
 * 渲染进程的 localStorage 走 Chromium Storage Service 批量提交，进程被
 * 强杀（任务管理器结束任务/断电）时最近的轮换写入可能尚未落盘——而服务端
 * 的单次轮换已作废旧值，重启后 stored token 被拒 → 每次都要重新登录。
 * 主进程 fs.writeFileSync 立即落入 OS 页缓存，进程强杀不丢。
 *
 * 信任域与 localStorage 相同（本机同用户），文件 0600 仅当前用户可读。
 * 删除文件 = 全端登出（与清除 localStorage 等效）。
 */
import * as fs from "fs";
import * as path from "path";

const AUTH_SESSION_FILE = "auth-session.json";

/** 保存 refresh token（null = 清除）。同步写——调用点在 IPC handler 内。 */
export function saveAuthSession(userDataDir: string, refreshToken: string | null): void {
  const file = path.join(userDataDir, AUTH_SESSION_FILE);
  if (refreshToken === null) {
    try {
      fs.rmSync(file, { force: true });
    } catch {
      // 文件本就不存在，视为已清除
    }
    return;
  }
  fs.mkdirSync(userDataDir, { recursive: true });
  fs.writeFileSync(
    file,
    JSON.stringify({ refresh_token: refreshToken }),
    { encoding: "utf-8", mode: 0o600 },
  );
}

/** 读取 refresh token；文件缺失/损坏/内容非法一律 null（fail-closed 走重新登录）。 */
export function loadAuthSession(userDataDir: string): string | null {
  try {
    const raw = JSON.parse(
      fs.readFileSync(path.join(userDataDir, AUTH_SESSION_FILE), "utf-8"),
    ) as { refresh_token?: unknown };
    const token = raw?.refresh_token;
    return typeof token === "string" && token.length > 0 ? token : null;
  } catch {
    return null;
  }
}
