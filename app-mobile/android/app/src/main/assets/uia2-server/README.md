# vendored: appium-uiautomator2-server

**上游**：https://github.com/appium/appium-uiautomator2-server
**版本**：v8.6.0（release APK）
**许可证**：Apache-2.0（见同目录 LICENSE，上游原文保留）

**拍板依据**：调研报告
`docs/working/dsh-go移动端本地运行调研与接入工作量_20261112.md` §四B/§七——
mobilerun-portal 实测定谳 AGPL-3.0 不可 vendor，二选一选 uia2-server
（Apache-2.0 兼容本仓 Apache-2.0；能力覆盖文本操控档：source 元素树 /
click / input / swipe / screenshot，设备 6790 端口 REST）。

**集成方式**：APK 随壳打包于 assets；首次启用手机操控时由 Android 壳侧
释放并以 instrumentation 形态拉起（`adb shell am instrument` 等价物经
壳内执行），phone_control 插件（plugins/shared/phone_control）经
localhost HTTP（127.0.0.1:6790）调用本服务。**勿与 portal 双接**
（两套无障碍服务冲突）。
