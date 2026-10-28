# PDF2zh

PDF2zh 桌面翻译 APP 与 Zotero 插件，当前版本为 v1.0.0。支持本地上传 PDF，或从 Zotero 提交翻译任务。

## 功能

- 在 APP 中配置翻译供应商、API、模型与思考参数，插件继承后端接口设置。
- 支持纯译文、双语对照、裁剪及左右拼接等输出格式。
- 提供任务进度、暂停、继续、取消、历史任务和分级日志。
- 支持项目内 CUDA 版面识别，运行环境与缓存存放在程序自身目录。

## 使用

从项目 GitHub Releases 下载 Windows 完整包，解压到专用英文路径后运行 `zotero-pdf2zh-server.exe`。首次使用时在“设置 · 内核”下载翻译内核，并在“设置 · 翻译”配置 API。

Zotero 插件可单独安装 XPI，也可将 APP 开始页的“Zotero插件”按钮拖到 Zotero → 工具 → Plugins Manager。插件连接本地服务前，需要在 APP 中启动服务。

当前提供 Windows 成品；macOS 尚未发布。

## 源码目录

- `APP/`：桌面 APP、服务端、测试、资源和 Windows 构建脚本。
- `Plugin/`：Zotero 插件、TypeScript 类型、测试和图标生成工具。
- `Release/`：本地发布文件的输出目录，构建产物不纳入普通 Git 提交。

## 构建

需要 Windows x64、Node.js 22 或更新版本及 npm。在项目根目录执行：

```powershell
cd APP
powershell -NoProfile -ExecutionPolicy Bypass -File ./build_exe.ps1
```

构建入口会先编译插件，再打包 APP；Python 环境和下载缓存保存在 APP 内，最终 ZIP 输出至 Release。Python 打包依赖见 `APP/requirements-build-lock.txt`，插件依赖由 `Plugin/package-lock.json` 锁定。

单独构建插件：

```powershell
cd Plugin
npm ci
npm test
npm run build
```

输出为 `Plugin/build/pdf-2-zh-for-zotero.xpi`。首次构建需要网络下载依赖。

## 开发验证

在项目虚拟环境中安装依赖，并先构建插件，再从 APP 目录执行：

```powershell
python -m unittest discover -s tests -v
```

完整测试需要与翻译内核一致的 BabelDOC 0.6.2 依赖。桌面程序的运行、构建和性能说明分别见 [APP 说明](APP/README-EXE.md) 与 [性能说明](APP/PERFORMANCE.md)。

## 来源与许可

本项目基于 [guaguastandup/zotero-pdf2zh](https://github.com/guaguastandup/zotero-pdf2zh) 二次开发；本项目主页为 [erxinzi/PDF2zh](https://github.com/erxinzi/PDF2zh)。

本项目免费开源，遵循 AGPL-3.0，许可原文见 [LICENSE](LICENSE)。请勿通过其他渠道购买。
