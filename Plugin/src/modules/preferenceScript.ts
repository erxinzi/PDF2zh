// Translation interfaces are configured by the backend; this pane owns only document options.
export async function registerPrefsScripts(window: Window) {
    addon.data.prefs = { window, columns: [], rows: [] };
    for (const link of window.document.querySelectorAll(
        ".pdf2zh-notes a[href]",
    )) {
        const anchor = link as HTMLAnchorElement | null;
        if (!anchor) continue;
        anchor.addEventListener("click", (event) => {
            event.preventDefault();
            const url = anchor.getAttribute("href");
            if (url) Zotero.launchURL(url);
        });
    }
}
