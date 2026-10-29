#!/usr/bin/env python3
"""
OxWeb - a tabbed web browser built from Project Nexus Vault, Phases 1-3.

Architecture (Vault 01): UI shell (this file) -> Browser engine glue -> Chromium
rendering engine via QtWebEngine. Data persistence (Vault 05) is SQLite for
history/bookmarks plus a persistent Chromium profile for cookies, cache, and
local storage.

Run:  pip install PyQt6 PyQt6-WebEngine
      python oxweb.py [optional-url]
"""
import os
import re
import sqlite3
import sys
import time
from urllib.parse import quote_plus

from PyQt6.QtCore import Qt, QSize, QStringListModel, QUrl
from PyQt6.QtGui import QAction, QKeySequence, QShortcut
from PyQt6.QtWebEngineCore import (QWebEnginePage, QWebEngineProfile,
                                   QWebEngineSettings)
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import (QApplication, QCompleter, QDialog, QFileDialog,
                             QHBoxLayout, QLabel, QLineEdit, QListWidget,
                             QListWidgetItem, QMainWindow, QMessageBox,
                             QProgressBar, QPushButton, QTabWidget, QToolBar,
                             QVBoxLayout, QWidget)

APP_NAME = "OxWeb"
APP_VERSION = "1.0"
DATA_DIR = os.path.join(os.path.expanduser("~"), ".oxweb")
DB_PATH = os.path.join(DATA_DIR, "oxweb.db")
SEARCH_URL = "https://duckduckgo.com/?q={query}"

START_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>New Tab</title>
<style>
 body{margin:0;height:100vh;display:flex;flex-direction:column;align-items:center;
      justify-content:center;background:#14161c;color:#e8eaf0;
      font-family:-apple-system,Segoe UI,Roboto,sans-serif}
 h1{font-size:64px;margin:0 0 8px;letter-spacing:2px}
 h1 span{color:#6c8cff}
 p{color:#8a90a2;margin:0 0 28px}
 form{width:min(560px,86vw)}
 input{width:100%;box-sizing:border-box;padding:14px 20px;font-size:17px;color:#e8eaf0;
       background:#1f222b;border:1px solid #333847;border-radius:28px;outline:none}
 input:focus{border-color:#6c8cff}
</style></head><body>
<h1>Ox<span>Web</span></h1><p>Search the web or type a URL in the address bar</p>
<form action="https://duckduckgo.com/" method="get">
<input name="q" placeholder="Search DuckDuckGo" autofocus autocomplete="off"></form>
</body></html>"""

DARK_QSS = """
QMainWindow, QDialog { background:#1b1e26; }
QToolBar { background:#1b1e26; border:none; spacing:4px; padding:4px 6px; }
QToolButton { color:#e8eaf0; background:transparent; border-radius:6px; padding:5px 8px; font-size:15px; }
QToolButton:hover { background:#2a2f3b; }
QToolButton:disabled { color:#555b6b; }
QLineEdit { background:#262a35; color:#e8eaf0; border:1px solid #333847;
            border-radius:15px; padding:6px 14px; selection-background-color:#6c8cff; }
QLineEdit:focus { border:1px solid #6c8cff; }
QTabWidget::pane { border:none; }
QTabBar { background:#14161c; }
QTabBar::tab { background:#1b1e26; color:#9aa0b2; padding:8px 14px; min-width:110px; max-width:200px;
               border-top-left-radius:8px; border-top-right-radius:8px; margin-right:2px; }
QTabBar::tab:selected { background:#262a35; color:#ffffff; }
QTabBar::tab:hover:!selected { background:#222631; }
QMenuBar { background:#14161c; color:#cfd3e0; }
QMenuBar::item:selected, QMenu::item:selected { background:#2a2f3b; }
QMenu { background:#1f222b; color:#e8eaf0; border:1px solid #333847; }
QProgressBar { background:transparent; border:none; max-height:3px; }
QProgressBar::chunk { background:#6c8cff; }
QListWidget { background:#1f222b; color:#e8eaf0; border:1px solid #333847; }
QListWidget::item { padding:6px; }
QListWidget::item:selected { background:#2f3650; }
QPushButton { background:#2a2f3b; color:#e8eaf0; border:none; border-radius:6px; padding:6px 14px; }
QPushButton:hover { background:#353b4a; }
QLabel { color:#cfd3e0; }
QCompleter QAbstractItemView, QAbstractItemView { background:#1f222b; color:#e8eaf0;
            selection-background-color:#2f3650; border:1px solid #333847; }
"""


# --------------------------------------------------------------------------
# Data persistence (Vault 05)
# --------------------------------------------------------------------------
class Database:
    def __init__(self, path=DB_PATH):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS history(
                url TEXT PRIMARY KEY, title TEXT, visit_count INTEGER DEFAULT 1,
                last_visit REAL);
            CREATE TABLE IF NOT EXISTS bookmarks(
                url TEXT PRIMARY KEY, title TEXT, added REAL);
        """)
        self.conn.commit()

    def log_visit(self, url, title):
        if not url.startswith(("http://", "https://")):
            return
        self.conn.execute("""
            INSERT INTO history(url,title,visit_count,last_visit) VALUES(?,?,1,?)
            ON CONFLICT(url) DO UPDATE SET title=excluded.title,
                visit_count=visit_count+1, last_visit=excluded.last_visit
        """, (url, title or url, time.time()))
        self.conn.commit()

    def suggest(self, text, limit=8):
        like = f"%{text}%"
        rows = self.conn.execute("""
            SELECT url FROM history WHERE url LIKE ? OR title LIKE ?
            ORDER BY visit_count DESC, last_visit DESC LIMIT ?""",
            (like, like, limit)).fetchall()
        return [r[0] for r in rows]

    def recent_history(self, limit=500):
        return self.conn.execute(
            "SELECT url,title,last_visit FROM history ORDER BY last_visit DESC LIMIT ?",
            (limit,)).fetchall()

    def clear_history(self):
        self.conn.execute("DELETE FROM history")
        self.conn.commit()

    def is_bookmarked(self, url):
        return self.conn.execute("SELECT 1 FROM bookmarks WHERE url=?", (url,)).fetchone() is not None

    def toggle_bookmark(self, url, title):
        if self.is_bookmarked(url):
            self.conn.execute("DELETE FROM bookmarks WHERE url=?", (url,))
            added = False
        else:
            self.conn.execute("INSERT INTO bookmarks VALUES(?,?,?)", (url, title or url, time.time()))
            added = True
        self.conn.commit()
        return added

    def bookmarks(self):
        return self.conn.execute("SELECT url,title FROM bookmarks ORDER BY added DESC").fetchall()


# --------------------------------------------------------------------------
# Omnibox logic (Vault 06 / Phase 3)
# --------------------------------------------------------------------------
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")
_LOCAL = re.compile(r"^(localhost|(\d{1,3}\.){3}\d{1,3})(:\d+)?([/?#].*)?$", re.I)
_DOMAIN = re.compile(r"^([a-z0-9\-]+\.)+[a-z]{2,}(:\d+)?([/?#].*)?$", re.I)


def resolve_input(text):
    """Decide whether the user typed a URL or a search query."""
    text = text.strip()
    if not text:
        return QUrl("about:blank")
    if _SCHEME.match(text) or text.startswith(("about:", "data:", "file:")):
        return QUrl(text)
    if " " not in text:
        if _LOCAL.match(text):
            return QUrl("http://" + text)
        if _DOMAIN.match(text):
            return QUrl("https://" + text)
    return QUrl(SEARCH_URL.format(query=quote_plus(text)))


# --------------------------------------------------------------------------
# Web view / tab
# --------------------------------------------------------------------------
class BrowserView(QWebEngineView):
    def __init__(self, window, profile):
        super().__init__()
        self.window_ref = window
        self.setPage(QWebEnginePage(profile, self))
        s = self.settings()
        s.setAttribute(QWebEngineSettings.WebAttribute.FullScreenSupportEnabled, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.PluginsEnabled, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.ScrollAnimatorEnabled, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalStorageEnabled, True)
        self.page().fullScreenRequested.connect(self._fullscreen)

    def _fullscreen(self, request):
        request.accept()
        w = self.window_ref
        if request.toggleOn():
            w.showFullScreen()
        else:
            w.showNormal()

    def createWindow(self, _type):
        # Handles target=_blank and window.open()
        return self.window_ref.add_tab(None, switch=True)


# --------------------------------------------------------------------------
# History dialog
# --------------------------------------------------------------------------
class HistoryDialog(QDialog):
    def __init__(self, db, open_url, parent=None):
        super().__init__(parent)
        self.db, self.open_url = db, open_url
        self.setWindowTitle("History")
        self.resize(720, 520)
        lay = QVBoxLayout(self)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search history...")
        self.search.textChanged.connect(self.populate)
        self.list = QListWidget()
        self.list.itemActivated.connect(self._open)
        self.list.itemDoubleClicked.connect(self._open)
        clear = QPushButton("Clear all history")
        clear.clicked.connect(self._clear)
        lay.addWidget(self.search)
        lay.addWidget(self.list)
        lay.addWidget(clear)
        self.populate()

    def populate(self):
        q = self.search.text().lower()
        self.list.clear()
        for url, title, ts in self.db.recent_history():
            if q and q not in url.lower() and q not in (title or "").lower():
                continue
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
            item = QListWidgetItem(f"{title}\n{url}   ·   {when}")
            item.setData(Qt.ItemDataRole.UserRole, url)
            self.list.addItem(item)

    def _open(self, item):
        self.open_url(item.data(Qt.ItemDataRole.UserRole))
        self.accept()

    def _clear(self):
        if QMessageBox.question(self, APP_NAME, "Delete all browsing history?") == \
                QMessageBox.StandardButton.Yes:
            self.db.clear_history()
            self.populate()


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------
class OxWeb(QMainWindow):
    def __init__(self, start_url=None):
        super().__init__()
        self.db = Database()
        self.setWindowTitle(APP_NAME)
        self.resize(1280, 820)

        # Persistent Chromium profile: cookies, cache, localStorage survive restarts.
        self.profile = QWebEngineProfile(APP_NAME, self)
        self.profile.setPersistentStoragePath(os.path.join(DATA_DIR, "profile"))
        self.profile.setCachePath(os.path.join(DATA_DIR, "cache"))
        self.profile.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.AllowPersistentCookies)
        self.profile.downloadRequested.connect(self._download_requested)
        self._downloads = []

        self._build_ui()
        self._build_menu()
        self._build_shortcuts()
        self.add_tab(QUrl(start_url) if start_url else None)

    # ----- UI construction -------------------------------------------------
    def _build_ui(self):
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.tabs.currentChanged.connect(self._on_tab_changed)

        new_btn = QPushButton("+")
        new_btn.setFixedSize(28, 26)
        new_btn.setToolTip("New tab (Ctrl+T)")
        new_btn.clicked.connect(lambda: self.add_tab(None))
        self.tabs.setCornerWidget(new_btn, Qt.Corner.TopRightCorner)

        nav = QToolBar("Navigation")
        nav.setMovable(False)
        nav.setIconSize(QSize(18, 18))
        self.addToolBar(nav)

        self.act_back = QAction("◀", self, toolTip="Back (Alt+Left)",
                                triggered=lambda: self.cur().back())
        self.act_fwd = QAction("▶", self, toolTip="Forward (Alt+Right)",
                               triggered=lambda: self.cur().forward())
        self.act_reload = QAction("⟳", self, toolTip="Reload (Ctrl+R)",
                                  triggered=self._reload_or_stop)
        self.act_home = QAction("⌂", self, toolTip="New start page",
                                triggered=lambda: self.cur().setHtml(START_PAGE, QUrl("oxweb://start")))
        self.act_star = QAction("☆", self, toolTip="Bookmark this page (Ctrl+D)",
                                triggered=self.toggle_bookmark)
        for a in (self.act_back, self.act_fwd, self.act_reload, self.act_home):
            nav.addAction(a)

        self.omnibox = QLineEdit()
        self.omnibox.setPlaceholderText("Search or enter address")
        self.omnibox.setClearButtonEnabled(True)
        self.omnibox.returnPressed.connect(lambda: self.navigate(self.omnibox.text()))
        self.omnibox.textEdited.connect(self._update_suggestions)
        self.completer_model = QStringListModel(self)
        self.completer = QCompleter(self.completer_model, self)
        self.completer.setCompletionMode(QCompleter.CompletionMode.UnfilteredPopupCompletion)
        self.completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.completer.activated.connect(self.navigate)
        self.omnibox.setCompleter(self.completer)
        nav.addWidget(self.omnibox)
        nav.addAction(self.act_star)

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 100)
        self.progress.hide()

        # Find-in-page bar
        self.find_bar = QWidget()
        fl = QHBoxLayout(self.find_bar)
        fl.setContentsMargins(8, 4, 8, 4)
        self.find_input = QLineEdit()
        self.find_input.setPlaceholderText("Find in page")
        self.find_input.textChanged.connect(lambda t: self.cur().findText(t))
        self.find_input.returnPressed.connect(lambda: self.cur().findText(self.find_input.text()))
        self.find_status = QLabel("")
        close_find = QPushButton("✕")
        close_find.setFixedWidth(32)
        close_find.clicked.connect(self.hide_find)
        fl.addWidget(QLabel("Find:"))
        fl.addWidget(self.find_input, 1)
        fl.addWidget(self.find_status)
        fl.addWidget(close_find)
        self.find_bar.hide()

        central = QWidget()
        v = QVBoxLayout(central)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        v.addWidget(self.progress)
        v.addWidget(self.tabs, 1)
        v.addWidget(self.find_bar)
        self.setCentralWidget(central)

        self.statusBar().setStyleSheet("color:#8a90a2; background:#14161c;")

    def _build_menu(self):
        mb = self.menuBar()
        m = mb.addMenu("&File")
        m.addAction("New Tab", self._new_tab_default, QKeySequence("Ctrl+T"))
        m.addAction("Close Tab", lambda: self.close_tab(self.tabs.currentIndex()), QKeySequence("Ctrl+W"))
        m.addAction("Open File...", self.open_file, QKeySequence("Ctrl+O"))
        m.addAction("Save Page as PDF...", self.save_pdf, QKeySequence("Ctrl+P"))
        m.addSeparator()
        m.addAction("Quit", self.close, QKeySequence("Ctrl+Q"))

        v = mb.addMenu("&View")
        v.addAction("Zoom In", lambda: self.zoom(+0.1), QKeySequence("Ctrl+="))
        v.addAction("Zoom Out", lambda: self.zoom(-0.1), QKeySequence("Ctrl+-"))
        v.addAction("Reset Zoom", lambda: self.cur().setZoomFactor(1.0), QKeySequence("Ctrl+0"))
        v.addAction("Find in Page", self.show_find, QKeySequence("Ctrl+F"))
        v.addAction("Full Screen", self.toggle_fullscreen, QKeySequence("F11"))

        h = mb.addMenu("&History")
        h.addAction("Show History", self.show_history, QKeySequence("Ctrl+H"))

        self.bm_menu = mb.addMenu("&Bookmarks")
        self.bm_menu.aboutToShow.connect(self._populate_bookmarks)

        hp = mb.addMenu("&Help")
        hp.addAction(f"About {APP_NAME}", self._about)

    def _build_shortcuts(self):
        def sc(keys, fn):
            QShortcut(QKeySequence(keys), self, activated=fn)
        sc("Ctrl+L", self._focus_omnibox)
        sc("F6", self._focus_omnibox)
        sc("Ctrl+R", lambda: self.cur().reload())
        sc("F5", lambda: self.cur().reload())
        sc("Alt+Left", lambda: self.cur().back())
        sc("Alt+Right", lambda: self.cur().forward())
        sc("Ctrl+D", self.toggle_bookmark)
        sc("Ctrl+Tab", lambda: self._cycle_tab(+1))
        sc("Ctrl+Shift+Tab", lambda: self._cycle_tab(-1))
        sc("Escape", self.hide_find)
        for i in range(1, 9):
            sc(f"Ctrl+{i}", lambda i=i: self.tabs.setCurrentIndex(min(i - 1, self.tabs.count() - 1)))

    # ----- Tabs ------------------------------------------------------------
    def cur(self):
        return self.tabs.currentWidget()

    def add_tab(self, url=None, switch=True):
        view = BrowserView(self, self.profile)
        idx = self.tabs.addTab(view, "New Tab")
        view.titleChanged.connect(lambda t, v=view: self._set_tab_title(v, t))
        view.iconChanged.connect(lambda ic, v=view: self._set_tab_icon(v, ic))
        view.urlChanged.connect(lambda u, v=view: self._on_url_changed(v, u))
        view.loadStarted.connect(lambda v=view: self._on_load_started(v))
        view.loadProgress.connect(lambda p, v=view: self._on_progress(v, p))
        view.loadFinished.connect(lambda ok, v=view: self._on_load_finished(v, ok))
        view.page().linkHovered.connect(lambda l: self.statusBar().showMessage(l))
        view.page().findTextFinished.connect(self._on_find_finished)
        if url is not None and not url.isEmpty():
            view.load(url)
        elif url is None:
            view.setHtml(START_PAGE, QUrl("oxweb://start"))
        if switch:
            self.tabs.setCurrentIndex(idx)
            if url is None:
                self._focus_omnibox()
        return view

    def _new_tab_default(self):
        self.add_tab(None)

    def close_tab(self, index):
        w = self.tabs.widget(index)
        if self.tabs.count() <= 1:
            self.close()
            return
        self.tabs.removeTab(index)
        w.page().deleteLater()
        w.deleteLater()

    def _cycle_tab(self, step):
        n = self.tabs.count()
        self.tabs.setCurrentIndex((self.tabs.currentIndex() + step) % n)

    def _set_tab_title(self, view, title):
        i = self.tabs.indexOf(view)
        if i < 0:
            return
        title = title or "New Tab"
        self.tabs.setTabText(i, title[:28] + ("…" if len(title) > 28 else ""))
        self.tabs.setTabToolTip(i, title)
        if view is self.cur():
            self.setWindowTitle(f"{title} — {APP_NAME}")

    def _set_tab_icon(self, view, icon):
        i = self.tabs.indexOf(view)
        if i >= 0:
            self.tabs.setTabIcon(i, icon)

    # ----- Navigation / signals ---------------------------------------------
    def navigate(self, text):
        self.cur().load(resolve_input(text))
        self.cur().setFocus()

    def open_url_in_current(self, url):
        self.cur().load(QUrl(url))

    def _on_url_changed(self, view, url):
        if view is not self.cur():
            return
        self._show_url(url)

    def _show_url(self, url):
        s = url.toString()
        if url.scheme() == "oxweb" or s in ("", "about:blank"):
            s = ""
        if not self.omnibox.hasFocus():
            self.omnibox.setText(s)
            self.omnibox.setCursorPosition(0)
        self._refresh_star()

    def _on_tab_changed(self, _):
        v = self.cur()
        if not v:
            return
        self._show_url(v.url())
        self.setWindowTitle(f"{v.title() or 'New Tab'} — {APP_NAME}")
        self.progress.hide()
        self.hide_find()

    def _on_load_started(self, view):
        if view is self.cur():
            self.progress.setValue(0)
            self.progress.show()
            self.act_reload.setText("✕")

    def _on_progress(self, view, p):
        if view is self.cur():
            self.progress.setValue(p)

    def _on_load_finished(self, view, ok):
        if view is self.cur():
            self.progress.hide()
            self.act_reload.setText("⟳")
        if ok:
            self.db.log_visit(view.url().toString(), view.title())

    def _reload_or_stop(self):
        if self.progress.isVisible():
            self.cur().stop()
        else:
            self.cur().reload()

    def _focus_omnibox(self):
        self.omnibox.setFocus()
        self.omnibox.selectAll()

    def _update_suggestions(self, text):
        text = text.strip()
        self.completer_model.setStringList(self.db.suggest(text) if len(text) >= 2 else [])

    # ----- Bookmarks / history ---------------------------------------------
    def _refresh_star(self):
        url = self.cur().url().toString() if self.cur() else ""
        starred = bool(url) and self.db.is_bookmarked(url)
        self.act_star.setText("★" if starred else "☆")

    def toggle_bookmark(self):
        v = self.cur()
        url = v.url().toString()
        if not url.startswith(("http://", "https://")):
            self.statusBar().showMessage("Only web pages can be bookmarked", 2500)
            return
        added = self.db.toggle_bookmark(url, v.title())
        self.statusBar().showMessage("Bookmark added" if added else "Bookmark removed", 2500)
        self._refresh_star()

    def _populate_bookmarks(self):
        self.bm_menu.clear()
        self.bm_menu.addAction("Bookmark This Page", self.toggle_bookmark, QKeySequence("Ctrl+D"))
        self.bm_menu.addSeparator()
        rows = self.db.bookmarks()
        if not rows:
            self.bm_menu.addAction("(no bookmarks yet)").setEnabled(False)
        for url, title in rows:
            label = (title or url)[:50]
            self.bm_menu.addAction(label, lambda u=url: self.open_url_in_current(u))

    def show_history(self):
        HistoryDialog(self.db, self.open_url_in_current, self).exec()

    # ----- Find / zoom / misc ------------------------------------------------
    def show_find(self):
        self.find_bar.show()
        self.find_input.setFocus()
        self.find_input.selectAll()

    def hide_find(self):
        if self.find_bar.isVisible():
            self.find_bar.hide()
            self.find_status.setText("")
            if self.cur():
                self.cur().findText("")

    def _on_find_finished(self, result):
        n = result.numberOfMatches()
        self.find_status.setText(f"{result.activeMatchIndex() + 1}/{n}" if n else "No matches")

    def zoom(self, delta):
        v = self.cur()
        v.setZoomFactor(max(0.25, min(5.0, v.zoomFactor() + delta)))

    def toggle_fullscreen(self):
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    def open_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open File", "", "Web files (*.html *.htm *.svg *.pdf *.txt);;All (*)")
        if path:
            self.cur().load(QUrl.fromLocalFile(path))

    def save_pdf(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save as PDF", "page.pdf", "PDF (*.pdf)")
        if path:
            self.cur().page().printToPdf(path)
            self.statusBar().showMessage(f"Saving PDF to {path}", 3000)

    def _download_requested(self, download):
        dl_dir = os.path.join(os.path.expanduser("~"), "Downloads")
        os.makedirs(dl_dir, exist_ok=True)
        suggested = download.downloadFileName() or "download"
        path, _ = QFileDialog.getSaveFileName(self, "Save download", os.path.join(dl_dir, suggested))
        if not path:
            download.cancel()
            return
        download.setDownloadDirectory(os.path.dirname(path))
        download.setDownloadFileName(os.path.basename(path))
        download.isFinishedChanged.connect(
            lambda d=download: self.statusBar().showMessage(f"Downloaded {d.downloadFileName()}", 5000))
        download.accept()
        self._downloads.append(download)
        self.statusBar().showMessage(f"Downloading {os.path.basename(path)}...", 3000)

    def _about(self):
        QMessageBox.about(self, f"About {APP_NAME}",
                          f"<h3>{APP_NAME} {APP_VERSION}</h3>"
                          "<p>A tabbed browser built from Project Nexus.<br>"
                          "Engine: Chromium (QtWebEngine). Shell: PyQt6.</p>"
                          f"<p>Data folder: {DATA_DIR}</p>")


def main():
    # Needed for some Linux setups running as root / in containers; harmless otherwise.
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox")
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyleSheet(DARK_QSS)
    start = None
    if len(sys.argv) > 1:
        start = resolve_input(sys.argv[1]).toString()
    win = OxWeb(start)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
