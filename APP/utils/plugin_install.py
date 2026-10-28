"""Bundled Zotero plugin and the drag gesture on its home-page button."""
from pathlib import Path
import app_paths
from utils.links import Tooltip

PLUGIN_NAME = 'pdf-2-zh-for-zotero.xpi'
PLUGIN_TOOLTIP = '拖拽该按钮至 Zotero 插件管理器（Zotero → 工具 → Plugins Manager）即可完成安装。'


def plugin_path():
    bundled = app_paths.resource_path('plugins', PLUGIN_NAME)
    if bundled.is_file():
        return bundled
    if not app_paths.is_frozen():
        built = Path(__file__).resolve().parents[2] / 'Plugin/build' / PLUGIN_NAME
        if built.is_file():
            return built
    raise FileNotFoundError('随包的 Zotero 插件缺失，请重新解压完整版本或更新包。')


class PluginDragButton:
    def __init__(self, button, on_error):
        self.button, self.on_error = button, on_error
        self.start = None
        self.dragged = False
        self.tooltip = Tooltip(button, PLUGIN_TOOLTIP)
        button.bind('<ButtonPress-1>', self.press, add='+')
        button.bind('<B1-Motion>', self.motion, add='+')
        button.bind('<ButtonRelease-1>', self.release, add='+')

    def press(self, event):
        self.start = event.x_root, event.y_root
        self.dragged = False

    def motion(self, event):
        if self.start is None or self.dragged:
            return
        if max(abs(event.x_root - self.start[0]), abs(event.y_root - self.start[1])) < 6:
            return
        self.dragged = True
        self.tooltip.hide()
        try:
            from utils.windows_drag import drag_file
            drag_file(plugin_path())
        except (OSError, ValueError) as exc:
            self.on_error(exc)
        finally:
            self.button.state(['!pressed', '!active'])
        return 'break'

    def release(self, _):
        self.start = None
        if self.dragged:
            self.dragged = False
            return 'break'
