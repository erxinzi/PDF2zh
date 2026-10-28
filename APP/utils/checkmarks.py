"""DPI-aware check indicators independent of a platform theme's X glyph."""
import tkinter as tk
from tkinter import ttk


def install_checkmarks(root, scale=1.0):
    size = max(16, round(18 * scale))
    images = []
    for selected, disabled in ((False, False), (True, False), (False, True), (True, True)):
        image = tk.PhotoImage(master=root, width=size + 5, height=size + 2)
        border = '#b7bcc4' if disabled else '#858d99'
        fill = ('#aeb8c7' if disabled else '#0071e3') if selected else '#ffffff'
        image.put(border, to=(0, 1, size, size + 1))
        image.put(fill, to=(1, 2, size - 1, size))
        if selected:
            points = [(round(size * x), round(size * y) + 1) for x, y in ((.20, .51), (.41, .73), (.80, .28))]
            radius = max(1, round(scale))
            for (x1, y1), (x2, y2) in zip(points, points[1:]):
                steps = max(abs(x2 - x1), abs(y2 - y1))
                for step in range(steps + 1):
                    x, y = round(x1 + (x2 - x1) * step / steps), round(y1 + (y2 - y1) * step / steps)
                    image.put('#ffffff', to=(max(1, x - radius), max(2, y - radius), min(size - 1, x + radius + 1), min(size, y + radius + 1)))
        images.append(image)
    style = ttk.Style(root)
    name = 'PDF2zh.Check.indicator'
    style.element_create(name, 'image', images[0], ('disabled', 'selected', images[3]),
                         ('disabled', images[2]), ('selected', images[1]), sticky='w')

    def replace(layout):
        result = []
        for element, options in layout:
            options = dict(options)
            if 'children' in options:
                options['children'] = replace(options['children'])
            result.append((name if element.endswith('.indicator') else element, options))
        return result
    style.layout('TCheckbutton', replace(style.layout('TCheckbutton')))
    root._pdf2zh_check_images = images  # Tcl image names alone do not retain Python images.
