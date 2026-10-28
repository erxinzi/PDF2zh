# PDF2zh Windows APP

PDF2zh 提供本地 PDF 翻译界面与 Zotero 插件调用接口，默认监听地址为 `http://127.0.0.1:8890`。

## 安装与启动

解压 Windows 完整包，进入 `zotero-pdf2zh-server` 文件夹，运行 `zotero-pdf2zh-server.exe`。使用专用英文路径，保留 EXE 与 `_internal` 的目录关系。

完整包内置基础运行环境和 Python 3.12。首次使用时在“设置 · 内核”下载翻译内核；API 与模型在“设置 · 翻译”配置。

## 翻译与插件

开始页支持多选 PDF，并选择输出格式和 LR/TB 双语布局。语言、模型、QPS、并发、OCR、字体及裁剪参数在翻译设置中管理。

侧栏显示内核与服务状态。启动服务后，Zotero 插件可提交任务；插件继承后端接口设置，同时保留自己的文档处理参数。

APP 随包包含同版本插件。将“Zotero插件”按钮拖至 Zotero → 工具 → Plugins Manager 即可安装，也可单独安装发布的 XPI。

## 数据目录

| 目录或文件 | 用途 |
| --- | --- |
| `core/` | 项目自带 Python、翻译内核及 CUDA 组件 |
| `config/` | API 与内核配置 |
| `settings.json` | 桌面设置 |
| `cache/` | 下载、模型和临时缓存 |
| `translated/` | 翻译输出 |
| `logs/` | 分级日志及任务历史 |

这些运行数据由程序生成，不随源码提交。CUDA 组件仅供本项目使用，不安装到系统环境。

## 任务与日志

翻译任务支持暂停、继续和取消；已完成的任务保存在历史页。日志区按 INFO、WARNING、ERROR 分级，任务详情保留翻译错误和质量警告。

参考文献断行、数学结构、算法行和图表字体在项目适配层处理。已有 PDF 的排版不会因更新程序而自动改变，需要重新翻译生成。

## 从源码构建

在项目根目录的 APP 文件夹运行 `build_exe.ps1`。构建入口使用同级 Plugin 源码和本目录的图标、配置模板及依赖清单，输出完整包和更新包至同级 Release。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ./build_exe.ps1
```

依赖、测试及许可说明见项目根目录 README。性能参数见 [PERFORMANCE.md](PERFORMANCE.md)。
