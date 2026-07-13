/**
 * package.json 配置测试。
 *
 * 验证 Electron 相关 scripts 和依赖项已正确配置。
 */

// eslint-disable-next-line @typescript-eslint/no-require-imports
const pkg = require("../../package.json");

describe("package.json Electron 配置", () => {
  describe("基本字段", () => {
    it("name 应为 lingxi-electron", () => {
      expect(pkg.name).toBe("lingxi-electron");
    });

    it("main 应指向 dist-electron/main.js", () => {
      expect(pkg.main).toBe("dist-electron/main.js");
    });

    it("private 应为 true", () => {
      expect(pkg.private).toBe(true);
    });

    it("version 应存在且为字符串", () => {
      expect(typeof pkg.version).toBe("string");
      expect(pkg.version.length).toBeGreaterThan(0);
    });
  });

  describe("Electron 相关 scripts", () => {
    it("应包含 electron 启动脚本", () => {
      expect(pkg.scripts).toHaveProperty("electron");
      expect(pkg.scripts.electron).toContain("electron");
    });

    it("应包含 electron:dev 开发脚本", () => {
      expect(pkg.scripts).toHaveProperty("electron:dev");
      expect(pkg.scripts["electron:dev"]).toContain("electron");
    });

    it("应包含 electron:build 构建脚本", () => {
      expect(pkg.scripts).toHaveProperty("electron:build");
      const buildScript = pkg.scripts["electron:build"];
      expect(buildScript).toContain("tsc");
      expect(buildScript).toContain("electron-builder");
    });

    it("应包含 electron:compile 编译脚本", () => {
      expect(pkg.scripts).toHaveProperty("electron:compile");
      expect(pkg.scripts["electron:compile"]).toContain("tsc");
    });

    it("应包含 test 脚本", () => {
      expect(pkg.scripts).toHaveProperty("test");
      expect(pkg.scripts.test).toContain("jest");
    });

    it("应包含 test:coverage 脚本", () => {
      expect(pkg.scripts).toHaveProperty("test:coverage");
      expect(pkg.scripts["test:coverage"]).toContain("coverage");
    });
  });

  describe("devDependencies", () => {
    it("应包含 electron 依赖", () => {
      expect(pkg.devDependencies).toHaveProperty("electron");
    });

    it("应包含 electron-builder 依赖", () => {
      expect(pkg.devDependencies).toHaveProperty("electron-builder");
    });

    it("应包含 typescript 依赖", () => {
      expect(pkg.devDependencies).toHaveProperty("typescript");
    });

    it("应包含 jest 和 ts-jest 依赖", () => {
      expect(pkg.devDependencies).toHaveProperty("jest");
      expect(pkg.devDependencies).toHaveProperty("ts-jest");
    });

    it("应包含 @types/jest 依赖", () => {
      expect(pkg.devDependencies).toHaveProperty("@types/jest");
    });

    it("应包含 @types/node 依赖", () => {
      expect(pkg.devDependencies).toHaveProperty("@types/node");
    });
  });

  describe("build 配置", () => {
    it("应设置 appId", () => {
      expect(pkg.build).toHaveProperty("appId");
      expect(pkg.build.appId).toBe("com.lingxi.assistant");
    });

    it("应设置 productName", () => {
      expect(pkg.build).toHaveProperty("productName");
      expect(pkg.build.productName).toBe("灵汐助手");
    });

    it("应配置 win 构建目标", () => {
      expect(pkg.build.win).toBeDefined();
      expect(pkg.build.win.target).toContain("nsis");
    });

    it("应配置 mac 构建目标", () => {
      expect(pkg.build.mac).toBeDefined();
      expect(pkg.build.mac.target).toContain("dmg");
    });

    it("应配置 linux 构建目标", () => {
      expect(pkg.build.linux).toBeDefined();
      expect(pkg.build.linux.target).toContain("AppImage");
    });

    it("应包含 dist-electron 到 files", () => {
      expect(pkg.build.files).toContainEqual(
        expect.stringContaining("dist-electron")
      );
    });

    it("应包含 frontend/dist 到 files", () => {
      expect(pkg.build.files).toContainEqual(
        expect.stringContaining("frontend/dist")
      );
    });
  });
});
