"""Session Sidebar pane: list, select, create Runtime sessions."""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets


class SessionSidebar(QtWidgets.QWidget):
    """Left pane: sessions newest-first, active marked, new-session button."""

    sessionChosen = QtCore.Signal(str)
    newSessionRequested = QtCore.Signal()

    def __init__(self, client, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("SessionSidebar")
        self._client = client  # RuntimeObserverClient, for SessionTitleDialog
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        header = QtWidgets.QLabel("Sessions")
        header.setObjectName("ProminentLabel")
        layout.addWidget(header)
        self.list = QtWidgets.QListWidget()
        self.list.setAlternatingRowColors(True)
        self.list.currentRowChanged.connect(self._row_changed)
        layout.addWidget(self.list, 1)
        self.new_button = QtWidgets.QPushButton("New session")
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
