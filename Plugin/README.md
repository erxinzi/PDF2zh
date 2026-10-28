# PDF2zh for Zotero

PDF2zh 的 Zotero 插件，当前版本为 v1.0.0。插件 ID 为 `pdf2zh@guaguastandup.com`，偏好前缀为 `extensions.zotero.pdf2zh`。

## 安装与使用

在 Zotero 插件管理器中安装发布的 XPI，或将 APP 开始页的“Zotero插件”按钮拖入管理器。

API 添加、模型探测和模型选择由 APP 管理；插件继承后端接口设置，仅传入文档处理参数。提交任务前请启动 APP 中的服务，并配置正确的服务地址。

## 构建

需要 Node.js 22 或更新版本、npm。在本目录执行：

```powershell
npm ci
npm test
npm run build
```

构建结果为 `build/pdf-2-zh-for-zotero.xpi`。`package-lock.json` 锁定依赖，`prebuild` 使用本目录内的工具和原始 Logo 生成图标。

基于 [guaguastandup/zotero-pdf2zh](https://github.com/guaguastandup/zotero-pdf2zh) 二次开发；本项目为 [erxinzi/PDF2zh](https://github.com/erxinzi/PDF2zh)。许可见 LICENSE。
