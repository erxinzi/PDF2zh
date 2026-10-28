"""Accessible sidebar links with native hover tooltips."""
import tkinter as tk
import webbrowser

UPSTREAM_URL = 'https://github.com/guaguastandup/zotero-pdf2zh'
PROJECT_URL = 'https://github.com/erxinzi/PDF2zh'


class Tooltip:
    def __init__(self, widget, text):
        self.widget, self.text = widget, text
        self.timer = None
        self.window = None
        widget.bind('<Enter>', self.schedule, add='+')
        widget.bind('<Leave>', self.hide, add='+')
        widget.bind('<Button-1>', self.hide, add='+')
        widget.bind('<Destroy>', self.hide, add='+')

    def schedule(self, _=None):
        self.hide()
        self.timer = self.widget.after(350, self.show)

    def show(self):
        self.timer = None
        if not self.widget.winfo_exists():
            return
        tip = self.window = tk.Toplevel(self.widget)
        tip.withdraw(); tip.overrideredirect(True)
        tk.Label(tip, text=self.text, background='#fffbe6', foreground='#202020',
                 relief='solid', borderwidth=1, padx=8, pady=5, wraplength=380,
                 justify='left', font=('Segoe UI', 10)).pack()
        tip.update_idletasks()
        x = min(self.widget.winfo_rootx(), self.widget.winfo_screenwidth() - tip.winfo_reqwidth() - 12)
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        if y + tip.winfo_reqheight() > self.widget.winfo_screenheight() - 12:
            y = self.widget.winfo_rooty() - tip.winfo_reqheight() - 6
        tip.geometry(f'+{max(0,x)}+{max(0,y)}'); tip.deiconify()

    def hide(self, _=None):
        if self.timer:
            try:
                self.widget.after_cancel(self.timer)
            except tk.TclError:
                pass
            self.timer = None
        if self.window:
            self.window.destroy(); self.window = None


class LinkLabel(tk.Label):
    def __init__(self, parent, text, url, tooltip, **kwargs):
        super().__init__(parent, text=text, cursor='hand2', takefocus=True, **kwargs)
        self.url = url
        self.bind('<Button-1>', self.open)
        self.bind('<Return>', self.open)
        self.bind('<space>', self.open)
        self.tooltip = Tooltip(self, tooltip)

    def open(self, _=None):
        self.tooltip.hide()
        webbrowser.open_new_tab(self.url)
        return 'break'
