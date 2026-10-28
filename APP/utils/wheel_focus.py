"""Route wheel input before Tk class bindings can change unfocused settings."""
import tkinter as tk


class WheelFocus:
    INPUTS = {'Entry', 'TEntry', 'TCombobox', 'Spinbox', 'TSpinbox'}
    SCROLLABLE = {'Text', 'Treeview', 'Listbox'}

    def __init__(self, root, page_scroll):
        self.root, self.page_scroll = root, page_scroll
        self.armed = None
        self.tag = 'PDF2zhWheelFocus' + str(id(self))
        root.bind_class(self.tag, '<Button-1>', self.click)
        root.bind_class(self.tag, '<Button-3>', self.click)
        for event in ('<MouseWheel>', '<Button-4>', '<Button-5>'):
            root.bind_class(self.tag, event, self.wheel)
        root.bind('<Map>', self.mapped, add='+')
        self.install(root)

    def install(self, widget):
        tags = widget.bindtags()
        if self.tag not in tags:
            widget.bindtags((self.tag, *tags))
        for child in widget.winfo_children():
            self.install(child)

    def mapped(self, event):
        if isinstance(event.widget, tk.Misc):
            self.install(event.widget)

    def click(self, event):
        widget = event.widget
        kind = widget.winfo_class()
        self.armed = widget if kind in self.INPUTS | self.SCROLLABLE else None
        # Return normally so ordinary click/selection bindings still run.

    @staticmethod
    def units(event):
        number = getattr(event, 'num', None)
        if number in (4, 5):
            return -1 if number == 4 else 1
        delta = getattr(event, 'delta', 0)
        return -max(1, abs(int(delta / 120))) * (1 if delta > 0 else -1) if delta else 0

    def wheel(self, event):
        widget = event.widget
        kind = widget.winfo_class()
        if widget is self.armed:
            if kind in self.SCROLLABLE:
                units = self.units(event)
                if units:
                    widget.yview_scroll(units, 'units')
                return 'break'
            if kind in {'TCombobox', 'Spinbox', 'TSpinbox'}:
                return None  # Explicit click enabled this control's class binding.
        self.page_scroll(event)
        return 'break'  # Prevent a hover from changing a model/number/selection.

    def reset(self):
        self.armed = None
