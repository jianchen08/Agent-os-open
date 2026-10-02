/**
 * 装机版自动登录凭据解析（ADR 2026-10-02-packaged-auto-login-env-only）：
 * 只认 ambient AGENTOS_ADMIN_PASSWORD——launcher 播种是部署契约，未播种
 * 即部署 bug（回落手动登录），应用绝不自造口令。
 */
import { describe, expect, it } from "vitest";

import { resolveAutoLoginCredential } from "../kernel-manager";

describe("resolveAutoLoginCredential（env 事实源解析）", () => {
  it("env 提供口令 → admin 凭据返回", () => {
    expect(resolveAutoLoginCredential("operator-pw")).toEqual({
      username: "admin",
      password: "operator-pw",
    });
  });

  it("未设置/空串/空白串 → null（契约未满足，不自动登录）", () => {
    expect(resolveAutoLoginCredential(undefined)).toBeNull();
    expect(resolveAutoLoginCredential(null)).toBeNull();
    expect(resolveAutoLoginCredential("")).toBeNull();
    expect(resolveAutoLoginCredential("   ")).toBeNull();
  });

  it("口令首尾空白 trim 后返回（对齐内核「空白即未设」语义，值本身不变形）", () => {
    const cred = resolveAutoLoginCredential("  pw-keep-inner  ");
    expect(cred).toEqual({ username: "admin", password: "pw-keep-inner" });
  });
});
