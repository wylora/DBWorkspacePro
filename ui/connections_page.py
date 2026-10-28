"""Страница «Подключения к SQL Server»: профили, проверка, подключение, отключение."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QFrame, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)

from database.connection import STATE_LABELS, ConnectionState
from database.driver import installed_drivers
from database.errors import DbWorkspaceError, friendly_message
from database.profile import AUTH_SQL, AUTH_WINDOWS, ConnectionProfile
from storage.profiles import ProfileError

from .context import AppContext
from .theme import colors
from .widgets import PageHeader

ENCRYPT_ITEMS = [("Да (рекомендуется)", "yes"), ("Нет", "no"), ("Строгое (TDS 8.0, драйвер 18+)", "strict")]


class ConnectionsPage(QWidget):
    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.svc = ctx.services
        self._current: ConnectionProfile | None = None
        self._loading = False
        self._build()
        self.reload_profiles()
        ctx.connections_changed.connect(self._refresh_states)
        ctx.theme_changed.connect(lambda _n: self._refresh_states())

    # ---- построение интерфейса ---------------------------------------
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 12)
        root.addWidget(PageHeader(
            "Подключения",
            "Профили подключения к Microsoft SQL Server. Пароли хранятся только в системном хранилище "
            "учётных данных и никогда не пишутся в файлы настроек."))
        body = QHBoxLayout()
        body.setSpacing(16)
        root.addLayout(body, 1)

        # левая колонка: список профилей
        left = QFrame()
        left.setObjectName("Card")
        left.setFixedWidth(280)
        ll = QVBoxLayout(left)
        ll.setContentsMargins(12, 12, 12, 12)
        self.list = QListWidget()
        self.list.currentItemChanged.connect(self._on_select)
        ll.addWidget(self.list, 1)
        row = QHBoxLayout()
        self.btn_new = QPushButton("＋ Новое")
        self.btn_new.clicked.connect(self._new)
        self.btn_delete = QPushButton("Удалить")
        self.btn_delete.setObjectName("Danger")
        self.btn_delete.clicked.connect(self._delete)
        row.addWidget(self.btn_new)
        row.addWidget(self.btn_delete)
        ll.addLayout(row)
        body.addWidget(left)

        # правая колонка: форма
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        card = QFrame()
        card.setObjectName("Card")
        form = QFormLayout(card)
        form.setContentsMargins(20, 16, 20, 16)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(10)
        scroll.setWidget(card)
        body.addWidget(scroll, 1)

        self.f_name = QLineEdit()
        self.f_server = QLineEdit()
        self.f_server.setPlaceholderText("localhost, имя хоста, host\\SQLEXPRESS или host,1433")
        self.f_server.setToolTip("Имя или адрес сервера. Для именованного экземпляра можно ввести host\\instance.")
        self.f_instance = QLineEdit()
        self.f_instance.setPlaceholderText("необязательно (именованный экземпляр)")
        self.f_port = QSpinBox()
        self.f_port.setRange(0, 65535)
        self.f_port.setSpecialValueText("по умолчанию (1433)")
        self.f_port.setToolTip("Порт TCP. Нельзя указывать вместе с именем экземпляра.")
        self.f_database = QLineEdit()
        self.f_database.setPlaceholderText("необязательно — база по умолчанию для логина")
        self.f_auth = QComboBox()
        self.f_auth.addItem("Аутентификация Windows", AUTH_WINDOWS)
        self.f_auth.addItem("Аутентификация SQL Server", AUTH_SQL)
        self.f_auth.currentIndexChanged.connect(self._update_auth_fields)
        self.f_user = QLineEdit()
        self.f_password = QLineEdit()
        self.f_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.f_remember = QCheckBox("Сохранить пароль в Windows Credential Manager")
        self.f_driver = QComboBox()
        self.f_driver.addItem("Выбрать автоматически (самый новый)", "")
        self.f_encrypt = QComboBox()
        for text, val in ENCRYPT_ITEMS:
            self.f_encrypt.addItem(text, val)
        self.f_trust = QCheckBox("Доверять сертификату сервера (отключает проверку — небезопасно)")
        self.f_trust.toggled.connect(self._on_trust_toggled)
        self.f_connect_timeout = QSpinBox()
        self.f_connect_timeout.setRange(1, 300)
        self.f_connect_timeout.setSuffix(" с")
        self.f_query_timeout = QSpinBox()
        self.f_query_timeout.setRange(0, 86400)
        self.f_query_timeout.setSuffix(" с")
        self.f_query_timeout.setSpecialValueText("без ограничения")

        form.addRow("Название", self.f_name)
        form.addRow("Сервер", self.f_server)
        form.addRow("Экземпляр", self.f_instance)
        form.addRow("Порт", self.f_port)
        form.addRow("База данных", self.f_database)
        form.addRow("Аутентификация", self.f_auth)
        form.addRow("Логин", self.f_user)
        form.addRow("Пароль", self.f_password)
        form.addRow("", self.f_remember)
        form.addRow("ODBC-драйвер", self.f_driver)
        form.addRow("Шифрование", self.f_encrypt)
        form.addRow("", self.f_trust)
        form.addRow("Тайм-аут подключения", self.f_connect_timeout)
        form.addRow("Тайм-аут запроса", self.f_query_timeout)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        form.addRow("", self.status)

        buttons = QHBoxLayout()
        self.btn_save = QPushButton("Сохранить")
        self.btn_save.clicked.connect(self._save_clicked)
        self.btn_test = QPushButton("Проверить соединение")
        self.btn_test.clicked.connect(self._test)
        self.btn_connect = QPushButton("Подключиться")
        self.btn_connect.setObjectName("Primary")
        self.btn_connect.clicked.connect(self._connect)
        self.btn_disconnect = QPushButton("Отключиться")
        self.btn_disconnect.clicked.connect(self._disconnect)
        for b in (self.btn_save, self.btn_test, self.btn_connect, self.btn_disconnect):
            buttons.addWidget(b)
        buttons.addStretch(1)
        form.addRow("", buttons)

        self._load_drivers()
        self._update_auth_fields()

    def _load_drivers(self) -> None:
        try:
            for d in installed_drivers():
                if "SQL Server" in d:
                    self.f_driver.addItem(d, d)
        except DbWorkspaceError as exc:
            self.f_driver.setToolTip(str(exc))

    # ---- список профилей ---------------------------------------------
    def reload_profiles(self, select_id: str | None = None) -> None:
        self._loading = True
        self.list.clear()
        profiles = self.svc.profiles.repo.list()
        for p in profiles:
            item = QListWidgetItem(p.name)
            item.setData(Qt.ItemDataRole.UserRole, p.id)
            self.list.addItem(item)
        self._loading = False
        self._refresh_states()
        target = select_id or self.svc.settings.get("last_profile_id")
        for i in range(self.list.count()):
            if self.list.item(i).data(Qt.ItemDataRole.UserRole) == target:
                self.list.setCurrentRow(i)
                return
        if self.list.count():
            self.list.setCurrentRow(0)
        else:
            self._current = None
            self._fill(None)

    def _state_of(self, profile_id: str) -> ConnectionState:
        conn = self.svc.manager.primary(profile_id)
        return conn.state if conn else ConnectionState.DISCONNECTED

    def _refresh_states(self) -> None:
        c = colors(self.ctx.theme)
        for i in range(self.list.count()):
            item = self.list.item(i)
            pid = item.data(Qt.ItemDataRole.UserRole)
            st = self._state_of(pid)
            name = item.text().split("  •")[0]
            suffix = "" if st is ConnectionState.DISCONNECTED else f"  • {STATE_LABELS[st]}"
            item.setText(name + suffix)
            color = {ConnectionState.CONNECTED: c["success"], ConnectionState.CONNECTING: c["warning"],
                     ConnectionState.LOST: c["warning"], ConnectionState.ERROR: c["danger"]}.get(st)
            item.setForeground(QBrush(QColor(color)) if color else QBrush(QColor(c["text"])))
        self._update_buttons()

    def _on_select(self, current, _previous) -> None:
        if self._loading or current is None:
            return
        pid = current.data(Qt.ItemDataRole.UserRole)
        self._current = self.svc.profiles.repo.get(pid)
        if self._current:
            self.svc.settings.set("last_profile_id", pid)
        self._fill(self._current)

    # ---- форма <-> профиль -------------------------------------------
    def _fill(self, p: ConnectionProfile | None) -> None:
        if p is None:
            p = ConnectionProfile.new("", "")
            enabled = False
        else:
            enabled = True
        s = self.svc.settings
        self.f_name.setText(p.name)
        self.f_server.setText(p.server)
        self.f_instance.setText(p.instance)
        self.f_port.setValue(p.port or 0)
        self.f_database.setText(p.database)
        self.f_auth.setCurrentIndex(max(0, self.f_auth.findData(p.auth)))
        self.f_user.setText(p.username)
        self.f_password.clear()
        stored = bool(enabled and self.svc.profiles.stored_password(p))
        self.f_password.setPlaceholderText("сохранённый пароль будет использован" if stored else "")
        self.f_remember.setChecked(p.save_password)
        self.f_driver.setCurrentIndex(max(0, self.f_driver.findData(p.driver)))
        self.f_encrypt.setCurrentIndex(max(0, self.f_encrypt.findData(p.encrypt)))
        self.f_trust.blockSignals(True)
        self.f_trust.setChecked(p.trust_server_certificate)
        self.f_trust.blockSignals(False)
        self.f_connect_timeout.setValue(p.connect_timeout or s.get("connect_timeout"))
        self.f_query_timeout.setValue(p.query_timeout)
        self.status.setText("")
        for w in (self.f_name, self.f_server, self.f_instance, self.f_port, self.f_database, self.f_auth,
                  self.f_driver, self.f_encrypt, self.f_trust, self.f_connect_timeout, self.f_query_timeout):
            w.setEnabled(enabled)
        self._update_auth_fields()
        self._update_buttons()

    def _collect(self) -> ConnectionProfile:
        base = self._current or ConnectionProfile.new()
        return ConnectionProfile(
            id=base.id,
            name=self.f_name.text(),
            server=self.f_server.text(),
            instance=self.f_instance.text(),
            port=self.f_port.value() or None,
            database=self.f_database.text(),
            auth=self.f_auth.currentData(),
            username=self.f_user.text(),
            driver=self.f_driver.currentData() or "",
            encrypt=self.f_encrypt.currentData(),
            trust_server_certificate=self.f_trust.isChecked(),
            connect_timeout=self.f_connect_timeout.value(),
            query_timeout=self.f_query_timeout.value(),
            save_password=self.f_remember.isChecked(),
        )

    def _update_auth_fields(self) -> None:
        sql = self.f_auth.currentData() == AUTH_SQL
        self.f_user.setEnabled(sql and self.f_auth.isEnabled())
        self.f_password.setEnabled(sql and self.f_auth.isEnabled())
        persistent = self.svc.credentials.persistent
        self.f_remember.setEnabled(sql and persistent and self.f_auth.isEnabled())
        if not persistent:
            self.f_remember.setToolTip(
                "Системное хранилище паролей недоступно — пароль можно ввести только на время работы программы.")
            self.f_remember.setChecked(False)
        else:
            self.f_remember.setToolTip(f"Хранилище: {self.svc.credentials.backend_name()}")

    def _update_buttons(self) -> None:
        has = self._current is not None
        st = self._state_of(self._current.id) if has else ConnectionState.DISCONNECTED
        active = st in (ConnectionState.CONNECTED, ConnectionState.LOST)
        busy = self._busy()
        self.btn_new.setEnabled(not busy)
        self.btn_delete.setEnabled(has and not busy)
        self.btn_save.setEnabled(has and not busy)
        self.btn_test.setEnabled(has and not busy)
        self.btn_connect.setEnabled(has and not busy and st is not ConnectionState.CONNECTED)
        self.btn_connect.setText("Переподключиться" if st is ConnectionState.LOST else "Подключиться")
        self.btn_disconnect.setEnabled(has and active and not busy)

    def _busy(self) -> bool:
        return any(t.key and t.key.startswith("conn:") for t in self.svc.tasks.tasks(active_only=True))

    def _set_status(self, text: str, kind: str = "info") -> None:
        c = colors(self.ctx.theme)
        color = {"ok": c["success"], "error": c["danger"], "warn": c["warning"]}.get(kind, c["muted"])
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {color};")

    def _on_trust_toggled(self, checked: bool) -> None:
        if not checked:
            return
        answer = QMessageBox.warning(
            self, "Отключение проверки сертификата",
            "Если доверять любому сертификату сервера, соединение защищено от прослушивания, но не от "
            "подмены сервера. Включайте это только для серверов, которым вы доверяете (например, "
            "тестовый сервер с самоподписанным сертификатом).\n\nВключить?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            self.f_trust.blockSignals(True)
            self.f_trust.setChecked(False)
            self.f_trust.blockSignals(False)

    # ---- действия ----------------------------------------------------
    def _new(self) -> None:
        s = self.svc.settings
        p = ConnectionProfile.new("Новое подключение", "")
        p.connect_timeout = s.get("connect_timeout")
        p.query_timeout = s.get("query_timeout")
        base = "Новое подключение"
        existing = {x.name.lower() for x in self.svc.profiles.repo.list()}
        name, n = base, 1
        while name.lower() in existing:
            n += 1
            name = f"{base} {n}"
        p.name = name
        self._current = p
        self.list.blockSignals(True)
        self.list.clearSelection()
        self.list.setCurrentRow(-1)
        self.list.blockSignals(False)
        self._fill(p)
        self.btn_save.setEnabled(True)
        self.f_server.setFocus()
        self._set_status("Новое подключение: заполните поля и нажмите «Сохранить».")

    def _save(self) -> ConnectionProfile | None:
        profile = self._collect()
        errors = profile.validate()
        if errors:
            self._set_status("\n".join(errors), "error")
            return None
        password = self.f_password.text() or None
        remember = self.f_remember.isChecked()
        try:
            saved = self.svc.profiles.save(profile, password, remember)
        except ProfileError as exc:
            self._set_status(str(exc), "error")
            return None
        except Exception as exc:  # noqa: BLE001
            self._set_status(friendly_message(exc), "error")
            return None
        self._current = saved
        self.ctx.profiles_changed.emit()
        self.reload_profiles(saved.id)
        if remember and saved.auth == AUTH_SQL and not saved.save_password:
            self._set_status("Профиль сохранён, но пароль не сохранён: системное хранилище недоступно.", "warn")
        else:
            self._set_status("Профиль сохранён.", "ok")
        return saved

    def _save_clicked(self) -> None:
        self._save()

    def _delete(self) -> None:
        if self._current is None:
            return
        name = self._current.name
        answer = QMessageBox.question(
            self, "Удаление подключения",
            f"Удалить подключение «{name}» вместе с сохранённым паролем?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        pid = self._current.id
        try:
            self.svc.profiles.delete(pid)
        except Exception as exc:  # noqa: BLE001
            self._set_status(friendly_message(exc), "error")
            return
        self._current = None
        self.ctx.profiles_changed.emit()
        self.ctx.connections_changed.emit()
        self.reload_profiles()
        self._set_status(f"Подключение «{name}» удалено.", "ok")

    def _password_for(self, profile: ConnectionProfile) -> tuple[bool, str | None]:
        """(можно_продолжать, пароль). Спрашивает пароль, если он нигде не сохранён."""
        if profile.auth != AUTH_SQL:
            return True, None
        typed = self.f_password.text()
        if typed:
            return True, typed
        if self.svc.profiles.stored_password(profile):
            return True, None
        text, ok = QInputDialog.getText(
            self, "Пароль", f"Пароль для «{profile.username}» на {profile.server_spec()}:",
            QLineEdit.EchoMode.Password)
        return (bool(ok and text), text or None)

    def _test(self) -> None:
        profile = self._collect()
        errors = profile.validate()
        if errors:
            self._set_status("\n".join(errors), "error")
            return
        ok, pw = self._password_for(profile)
        if not ok:
            return
        self._set_status("Проверка соединения…")
        self.btn_test.setEnabled(False)

        def done(info):
            self._set_status(f"Соединение установлено. SQL Server {info.version} ({info.edition}).", "ok")
            self.btn_test.setEnabled(True)
            self._update_buttons()

        def failed(msg):
            self._set_status(msg, "error")
            self.btn_test.setEnabled(True)
            self._update_buttons()

        try:
            self.ctx.runner.submit("Проверка подключения", lambda c: self.svc.profiles.test(profile, pw),
                                   on_success=done, on_error=failed, key=f"conn:test:{profile.id}")
        except Exception as exc:  # noqa: BLE001
            failed(friendly_message(exc))

    def _connect(self) -> None:
        profile = self._save()
        if profile is None:
            return
        ok, pw = self._password_for(profile)
        if not ok:
            return
        self._set_status("Подключение…")
        self._update_buttons()

        def done(conn):
            self.f_password.clear()
            self._set_status(f"Подключено: SQL Server {conn.server_info.version}.", "ok")
            self.ctx.connections_changed.emit()
            self.ctx.message.emit(f"Подключено к «{profile.name}»")
            self._update_buttons()

        def failed(msg):
            self._set_status(msg, "error")
            self.ctx.connections_changed.emit()
            self._update_buttons()

        try:
            self.ctx.runner.submit(f"Подключение к «{profile.name}»",
                                   lambda c: self.svc.profiles.connect(profile, pw),
                                   on_success=done, on_error=failed, key=f"conn:{profile.id}")
        except Exception as exc:  # noqa: BLE001
            failed(friendly_message(exc))

    def _disconnect(self) -> None:
        if self._current is None:
            return
        profile = self._current

        def done(_r):
            self._set_status("Отключено.", "ok")
            self.ctx.connections_changed.emit()
            self._update_buttons()

        try:
            self.ctx.runner.submit(f"Отключение от «{profile.name}»",
                                   lambda c: self.svc.profiles.disconnect(profile),
                                   on_success=done, on_error=lambda m: self._set_status(m, "error"),
                                   key=f"conn:{profile.id}")
        except Exception as exc:  # noqa: BLE001
            self._set_status(friendly_message(exc), "error")
