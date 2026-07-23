"""Session Sidebar pane: list, select, create Runtime sessions."""

from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets


class SessionSidebar(QtWidgets.QWidget):
    """Left pane: sessions newest-first, active marked, new-session button."""

    sessionChosen = QtCore.Signal(str)
    newSessionRequested = QtCore.Signal()
    sessionArchiveRequested = QtCore.Signal(str)
    sessionDeleteRequested = QtCore.Signal(str)

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("SessionSidebar")
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        header = QtWidgets.QLabel("SESSIONS")
        header.setObjectName("Kicker")
        layout.addWidget(header)
        self.list = QtWidgets.QListWidget()
        self.list.setAlternatingRowColors(True)
        # Right-click context menu for archive/delete on a hovered row.
        self.list.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._context_menu)
        self.list.currentRowChanged.connect(self._row_changed)
        layout.addWidget(self.list, 1)
        self.new_button = QtWidgets.QPushButton("New session")
        self.new_button.setObjectName("PrimaryButton")
        self.new_button.setAutoDefault(False)
        self.new_button.clicked.connect(self.newSessionRequested)
        layout.addWidget(self.new_button)
        self._updating = False

    def set_sessions(self, sessions, selected_id: str) -> None:
        """Render session dicts from the client's sessionsChanged signal."""
        self._updating = True
        try:
            self.list.clear()
            selected_row = -1
            for row, item in enumerate(sessions):
                title = item.get("title", "session")
                if title == "New session":
                    title = "未命名对话"
                marker = "● " if item.get("session_id") == selected_id else ""
                row_item = QtWidgets.QListWidgetItem(marker + title)
                row_item.setData(QtCore.Qt.ItemDataRole.UserRole,
                                 item.get("session_id"))
                self.list.addItem(row_item)
                if item.get("session_id") == selected_id:
                    selected_row = row
            if selected_row >= 0:
                self.list.setCurrentRow(selected_row)
        finally:
            self._updating = False

    def _row_changed(self, row: int) -> None:
        if self._updating or row < 0:
            return
        item = self.list.item(row)
        session_id = item.data(QtCore.Qt.ItemDataRole.UserRole)
        if type(session_id) is str and session_id:
            self.sessionChosen.emit(session_id)

    def _session_id_at(self, pos: QtCore.QPoint) -> str | None:
        item = self.list.itemAt(pos)
        if item is None:
            return None
        session_id = item.data(QtCore.Qt.ItemDataRole.UserRole)
        if type(session_id) is str and session_id:
            return session_id
        return None

    def _context_menu(self, pos: QtCore.QPoint) -> None:
        session_id = self._session_id_at(pos)
        if session_id is None:
            return
        # popup() + triggered is the non-blocking way to show the menu (a
        # blocking modal call is rejected by the package boundary guard). Each
        # action carries its session_id so one handler resolves it.
        menu = QtWidgets.QMenu(self)
        menu.addAction("归档对话").setData(("archive", session_id))
        menu.addAction("删除对话").setData(("delete", session_id))
        menu.triggered.connect(self._menu_action_triggered)
        menu.popup(self.list.mapToGlobal(pos))

    @QtCore.Slot(QtGui.QAction)
    def _menu_action_triggered(self, action: QtGui.QAction) -> None:
        data = action.data()
        if not (isinstance(data, tuple) and len(data) == 2):
            return
        kind, session_id = data
        if type(session_id) is not str or not session_id:
            return
        if kind == "archive":
            self.sessionArchiveRequested.emit(session_id)
        elif kind == "delete":
            self.sessionDeleteRequested.emit(session_id)
