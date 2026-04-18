from __future__ import annotations

import contextlib
import ctypes
import datetime
import io
import os
import plistlib
import queue
import secrets
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
import tkinter as tk
from dataclasses import asdict
from pathlib import Path
from tkinter import ttk

import customtkinter as ctk
import pystray
import qrcode
from PIL import Image, ImageDraw
try:
    import imageio.v2 as imageio
except ImportError:  # pragma: no cover
    try:
        import imageio
    except ImportError:
        imageio = None

try:
    import winreg
except ImportError:  # pragma: no cover
    winreg = None

from mtproxy_app_backend import (
    ALL_FILE_NAME,
    AppConfig,
    AppRuntime,
    LEGACY_OUT_DIR_NAME,
    LEGACY_WORKING_FILE_NAME,
    LIST_DIR_NAME,
    LIST_FILE_NAME,
    REPORT_FILE_NAME,
    is_public_release,
)
from mtproxy_collector import DEFAULT_SOURCES
from mtproxy_telegram import DEFAULT_TELEGRAM_SOURCE_URLS, normalize_telegram_phone
from mtproxy_updater import APP_PUBLIC_VERSION, fetch_latest_release, is_update_available, launch_prepared_update, prepare_update
from ui_tooltip import attach_ctk_tooltip

APP_NAME = "MTProxy AutoSwitch"


def _asset_path(*relative_parts: str) -> Path:
    roots = []
    if getattr(sys, "frozen", False):
        roots.append(Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent)))
        roots.append(Path(sys.executable).resolve().parent)
    roots.append(Path(__file__).resolve().parent)
    for root in roots:
        candidate = root.joinpath(*relative_parts)
        if candidate.exists():
            return candidate
    legacy_candidate = Path(__file__).resolve().with_name(relative_parts[-1])
    if legacy_candidate.exists():
        return legacy_candidate
    return roots[0].joinpath(*relative_parts)


APP_ICON_PATH = _asset_path("img", "icon.ico")
ABOUT_VIDEO_PATH = _asset_path("img", "dancecardiscordrtc.mp4")
AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_VALUE = "MTProxyAutoSwitch"
SINGLE_INSTANCE_MUTEX_NAME = "Global\\MTProxyAutoSwitch.Singleton"
CLOSE_LABELS = {
    "ask": "Всегда спрашивать",
    "tray": "Скрывать в трей",
    "exit": "Закрывать приложение",
}
AUTOSTART_SUPPORTED = sys.platform in {"win32", "darwin"}
AUTOSTART_LABEL = {
    "win32": "Запускать вместе с Windows",
    "darwin": "Запускать вместе с macOS",
}.get(sys.platform, "Запускать вместе с системой")

ctk.set_appearance_mode("system")
ctk.set_default_color_theme("blue")

APPEARANCE_LABELS = {
    "auto": "Авто",
    "light": "Светлая",
    "dark": "Темная",
}

COLOR_BG = ("#E6EBF2", "#0D1117")
COLOR_CARD = ("#F1F5FA", "#151B23")
COLOR_BORDER = ("#C8D2DF", "#263244")
COLOR_TEXT = ("#1A2433", "#F5F7FA")
COLOR_TEXT_SOFT = ("#5B6777", "#99A9BD")
COLOR_TEXT_FAINT = ("#728095", "#7D8DA6")
COLOR_FIELD = ("#E9EFF6", "#0F1620")
COLOR_FIELD_BORDER = ("#BCC8D7", "#2D3A4D")
COLOR_ACCENT = ("#2563EB", "#3B82F6")
COLOR_ACCENT_HOVER = ("#1E4FD7", "#2563EB")
COLOR_ACCENT_SOFT = ("#D7E5FF", "#162235")
COLOR_ACCENT_SOFT_HOVER = ("#C3D7FA", "#1B2C45")
COLOR_SUCCESS_BG = ("#CBE9DB", "#10261B")
COLOR_SUCCESS_TEXT = ("#0E6A46", "#6EE7B7")
COLOR_WARN_BG = ("#F2DFC3", "#2C210F")
COLOR_WARN_TEXT = ("#B45309", "#FBBF24")
COLOR_IDLE_BG = ("#DEE5EF", "#161E28")
COLOR_IDLE_TEXT = ("#475467", "#9AA6B2")
COLOR_DANGER_BG = ("#D85E74", "#7F1D35")
COLOR_DANGER_BORDER = ("#BF445A", "#9F2946")
COLOR_DANGER_TEXT = ("#FFFFFF", "#FFFFFF")
DISPLAY_SPEED_MIN_TRANSFER_BYTES = 128 * 1024

HOSTS_PATH = Path(r"C:\Windows\System32\drivers\etc\hosts")
HOSTS_BLOCK_BEGIN = "# MTProxy AutoSwitch Telegram Web Begin"
HOSTS_BLOCK_END = "# MTProxy AutoSwitch Telegram Web End"
TELEGRAM_WEB_HOSTS_LINES = [
    "149.154.167.220 telegram.me",
    "149.154.167.220 telegram.dog",
    "149.154.167.220 telegram.space",
    "149.154.167.220 telesco.pe",
    "149.154.167.220 tg.dev",
    "149.154.167.220 telegram.org",
    "149.154.167.220 t.me",
    "149.154.167.220 api.telegram.org",
    "149.154.167.220 td.telegram.org",
    "149.154.167.220 web.telegram.org",
    "149.154.167.220 pluto.web.telegram.org",
    "149.154.167.220 pluto-1.web.telegram.org",
    "149.154.167.220 flora.web.telegram.org",
    "149.154.167.220 flora-1.web.telegram.org",
    "149.154.167.220 venus.web.telegram.org",
    "149.154.167.220 venus-1.web.telegram.org",
    "149.154.167.220 vesta.web.telegram.org",
    "149.154.167.220 vesta-1.web.telegram.org",
    "149.154.167.220 aurora.web.telegram.org",
    "149.154.167.220 aurora-1.web.telegram.org",
    "149.154.167.220 kws1.web.telegram.org",
    "149.154.167.220 kws1-1.web.telegram.org",
    "149.154.167.220 kws2.web.telegram.org",
    "149.154.167.220 kws2-1.web.telegram.org",
    "149.154.167.220 kws4.web.telegram.org",
    "149.154.167.220 kws4-1.web.telegram.org",
    "149.154.167.220 kws5.web.telegram.org",
    "149.154.167.220 kws5-1.web.telegram.org",
    "149.154.167.220 zws1.web.telegram.org",
    "149.154.167.220 zws1-1.web.telegram.org",
    "149.154.167.220 zws2.web.telegram.org",
    "149.154.167.220 zws2-1.web.telegram.org",
    "149.154.167.220 zws4.web.telegram.org",
    "149.154.167.220 zws4-1.web.telegram.org",
    "149.154.167.220 zws5.web.telegram.org",
    "149.154.167.220 zws5-1.web.telegram.org",
    "149.154.167.220 my.telegram.org",
]

ADVANCED_PROBE_TIPS = {
    "Duration": "Сколько секунд идет базовая проверка одного прокси. Больше значение точнее, но обновление заметно дольше.",
    "Interval": "Пауза между попытками проверки одного и того же прокси. Меньше значение ускоряет проверку, но повышает нагрузку.",
    "Timeout": "Максимальное время ожидания одного сетевого действия. Если увеличить слишком сильно, зависшие прокси будут тормозить общий refresh.",
    "Workers": "Сколько прокси проверяется параллельно. Слишком большое значение может перегрузить сеть или систему.",
    "Fetch timeout": "Таймаут загрузки страницы-источника. Обычно трогать не нужно.",
    "Max latency": "Порог пинга, после которого прокси считается слишком медленным.",
    "Min success rate": "Минимальная доля успешных попыток, чтобы прокси считался рабочим.",
    "High latency ratio": "Допустимая доля медленных ответов выше порога Max latency.",
    "High latency streak": "Сколько подряд медленных ответов допускается до ранней остановки проверки.",
    "Max proxies": "Ограничение числа уникальных прокси на один refresh. 0 значит без ограничения.",
    "Live probe interval": "Как часто обновляется live-проверка уже отобранных прокси в фоне.",
    "Live probe duration": "Длительность одной фоновой live-проверки.",
    "Live probe top N": "Сколько лучших прокси приложение перепроверяет в фоне.",
    "Deep media top N": "Сколько лучших прокси дополнительно проверять через медиа Telegram после основного отбора.",
    "RF whitelist check": "Строгая медиа-проверка для сетей, где чаты Telegram открываются, но фото, голосовые, кружки и файлы через CDN работают нестабильно. Требует вход в Telegram.",
}

GENERAL_SETTING_TIPS = {
    "autostart": (
        "Приложение будет запускаться вместе с macOS."
        if sys.platform == "darwin"
        else "Приложение будет запускаться вместе с Windows."
        if sys.platform == "win32"
        else "Автозапуск на этой платформе не поддерживается."
    ),
    "start_minimized": "При запуске окно не будет открываться поверх рабочего стола, приложение сразу уйдет в трей.",
    "auto_start_local": "Если уже есть рабочий пул, локальный proxy frontend стартует автоматически.",
    "auto_update": "Приложение будет проверять GitHub Releases при запуске.",
    "telegram_sources": "Авторизованные Telegram-источники позволяют читать каналы, группы и ветки, включая случаи, где публичного web-view недостаточно.",
    "deep_media": "Дополнительно проверяет не только открытие чатов, но и загрузку фото, файлов, голосовых и других медиа через Telegram API.",
}


def _format_latency(value: float | None) -> str:
    if value in (None, 0):
        return "n/a"
    return f"{value:.0f} ms"


def _format_rate_kbps(value: float | None) -> str:
    if value is None or value <= 0:
        return "n/a"
    units = ["KB/s", "MB/s", "GB/s"]
    scaled = float(value)
    unit_index = 0
    while scaled >= 1024.0 and unit_index < len(units) - 1:
        scaled /= 1024.0
        unit_index += 1
    precision = 0 if scaled >= 100 else 1
    return f"{scaled:.{precision}f} {units[unit_index]}"


def _format_seed_source(source: str) -> str:
    mapping = {
        "default_list": "Базовый list",
        "legacy_working_list": "Базовый legacy list",
        "cached_report": "Кэш прошлого запуска",
        "legacy_cached_report": "Legacy кэш прошлого запуска",
        "bundled_seed": "Встроенный стартовый пул",
    }
    return mapping.get(source, "Новый результат")


def _format_thread_status(status: str, count: int, *, enabled: bool = False) -> str:
    if not status or status == "not_checked":
        if enabled:
            return "Telegram-источники включены и будут проверены при следующем обновлении"
        return "Telegram-источники еще не проверялись"
    if status == "disabled":
        if enabled:
            return "Telegram-источники включены и ожидают следующего обновления"
        return "Telegram-источники выключены"
    if status.startswith("loaded:"):
        if count <= 0:
            return "Парсинг Telegram-источников завершен, новых proxy не найдено"
        return f"Загружено из Telegram-источников: {count}"
    if status.startswith("skipped:"):
        reason = status.split(":", 1)[1]
        mapping = {
            "telegram_api_credentials_missing": "не указаны API ID / API Hash",
            "telegram_session_not_authorized": "Telegram-сессия не авторизована",
            "no_working_upstream": "нет рабочего upstream для проверки",
        }
        return f"Telegram-источники пропущены: {mapping.get(reason, reason)}"
    return status


def _appearance_mode_to_ctk(mode: str) -> str:
    return {
        "auto": "system",
        "light": "Light",
        "dark": "Dark",
    }.get(mode, "system")


def _appearance_label(mode: str) -> str:
    return APPEARANCE_LABELS.get(mode, APPEARANCE_LABELS["auto"])


def _primary_monitor_workarea(window: tk.Misc | None = None) -> tuple[int, int, int, int]:
    if sys.platform == "win32":
        try:
            class RECT(ctypes.Structure):
                _fields_ = [
                    ("left", ctypes.c_long),
                    ("top", ctypes.c_long),
                    ("right", ctypes.c_long),
                    ("bottom", ctypes.c_long),
                ]

            rect = RECT()
            if ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(rect), 0):
                return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
        except Exception:
            pass
    if window is None:
        return 0, 0, 1280, 720
    return 0, 0, int(window.winfo_screenwidth()), int(window.winfo_screenheight())


def _center_window(window: tk.Misc, width: int, height: int, *, primary_monitor: bool = False) -> None:
    window.update_idletasks()
    if primary_monitor:
        origin_left, origin_top, screen_width, screen_height = _primary_monitor_workarea(window)
    else:
        origin_left, origin_top = 0, 0
        screen_width = int(window.winfo_screenwidth())
        screen_height = int(window.winfo_screenheight())
    left = origin_left + max(0, (screen_width - width) // 2)
    top = origin_top + max(0, (screen_height - height) // 2)
    window.geometry(f"{width}x{height}+{left}+{top}")


def _set_fixed_window_size(window: tk.Misc, width: int, height: int, *, primary_monitor: bool = False) -> None:
    _center_window(window, width, height, primary_monitor=primary_monitor)
    with contextlib.suppress(Exception):
        window.minsize(width, height)
        window.maxsize(width, height)
        window.resizable(False, False)


class _PrimaryMonitorModal(ctk.CTkToplevel):
    def __init__(
        self,
        parent: tk.Misc | None,
        *,
        title: str,
        message: str,
        kind: str,
        buttons: tuple[str, ...],
    ) -> None:
        owner = parent if isinstance(parent, tk.Misc) else tk._default_root
        super().__init__(owner)
        self.result: str | None = None
        self._buttons = buttons
        # Withdraw сразу — не даём CTkToplevel мелькнуть до того как виджеты построены.
        # grab_set() НЕ вызываем здесь: grab на скрытом окне захватывает все события,
        # но окно невидимо → приложение зависает. grab_set() переехал в _show_ready().
        self.withdraw()
        self.title(title)
        with contextlib.suppress(Exception):
            if APP_ICON_PATH.exists():
                self.iconbitmap(str(APP_ICON_PATH))
        _set_fixed_window_size(self, 460, 240 if len(buttons) == 1 else 250, primary_monitor=True)
        self.transient(owner)
        self.configure(fg_color=COLOR_BG)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Escape>", lambda _event: self._cancel(), add="+")

        accent_map = {
            "info": (COLOR_ACCENT_SOFT, COLOR_ACCENT),
            "warning": (COLOR_WARN_BG, COLOR_WARN_TEXT),
            "error": (COLOR_DANGER_BG, COLOR_DANGER_TEXT),
            "question": (COLOR_ACCENT_SOFT, COLOR_ACCENT),
        }
        badge_bg, badge_text = accent_map.get(kind, (COLOR_ACCENT_SOFT, COLOR_ACCENT))

        card = ctk.CTkFrame(self, corner_radius=24, fg_color=COLOR_CARD, border_width=1, border_color=COLOR_BORDER)
        card.pack(fill="both", expand=True, padx=18, pady=18)

        header = ctk.CTkFrame(card, fg_color="transparent")
        header.pack(fill="x", padx=18, pady=(18, 10))
        ctk.CTkLabel(
            header,
            text={"info": "i", "warning": "!", "error": "x", "question": "?"}.get(kind, "i"),
            width=34,
            height=34,
            corner_radius=17,
            fg_color=badge_bg,
            text_color=badge_text,
            font=("Segoe UI Semibold", 18),
        ).pack(side="left")
        ctk.CTkLabel(
            header,
            text=title,
            text_color=COLOR_TEXT,
            font=("Segoe UI Semibold", 18),
        ).pack(side="left", padx=(12, 0))

        ctk.CTkLabel(
            card,
            text=message,
            text_color=COLOR_TEXT,
            font=("Segoe UI", 12),
            justify="left",
            wraplength=388,
        ).pack(fill="x", padx=18, pady=(0, 18))

        actions = ctk.CTkFrame(card, fg_color="transparent")
        actions.pack(fill="x", padx=18, pady=(0, 18))
        for index in range(len(buttons)):
            actions.grid_columnconfigure(index, weight=1, uniform="alert_actions")
        for index, button_name in enumerate(buttons):
            label = {
                "ok": "OK",
                "yes": "Да",
                "no": "Нет",
            }.get(button_name, button_name)
            is_primary = button_name in {"ok", "yes"}
            ctk.CTkButton(
                actions,
                text=label,
                height=40,
                corner_radius=20,
                fg_color=COLOR_ACCENT if is_primary else COLOR_ACCENT_SOFT,
                hover_color=COLOR_ACCENT_HOVER if is_primary else COLOR_ACCENT_SOFT_HOVER,
                text_color="#FFFFFF" if is_primary else COLOR_ACCENT,
                command=lambda value=button_name: self._choose(value),
            ).grid(row=0, column=index, sticky="ew", padx=(0 if index == 0 else 6, 0 if index == len(buttons) - 1 else 6))

        # Небольшая задержка чтобы CTkToplevel завершил внутреннюю инициализацию
        # (масштабирование DPI и т.п.) до того как мы показываем окно и ставим grab.
        self.after(50, self._show_ready)

    def _show_ready(self) -> None:
        with contextlib.suppress(Exception):
            self.deiconify()
            self.update_idletasks()  # позволить Tk отрисовать виджеты
            self.lift()
            self.focus_force()
            self.grab_set()  # grab ТОЛЬКО после того как окно реально видно

    def _choose(self, value: str) -> None:
        self.result = value
        self.destroy()

    def _cancel(self) -> None:
        self.result = "no" if "no" in self._buttons else "ok"
        self.destroy()

    def show(self) -> str | None:
        self.wait_window()
        return self.result


class _PrimaryMonitorMessageBox:
    def __init__(self) -> None:
        self._active_keys: dict[str, _PrimaryMonitorModal] = {}
        self._last_shown_at: dict[str, float] = {}
        self._cooldown_sec = 1.25

    def _resolve_parent(self, parent: tk.Misc | None) -> tk.Misc | None:
        if isinstance(parent, tk.Misc):
            return parent
        return tk._default_root

    def _show_modal(
        self,
        kind: str,
        title: str,
        message: str,
        *,
        parent: tk.Misc | None = None,
        dedupe_key: str | None = None,
    ) -> str | None:
        owner = self._resolve_parent(parent)
        if dedupe_key:
            active = self._active_keys.get(dedupe_key)
            if active is not None:
                with contextlib.suppress(Exception):
                    if active.winfo_exists():
                        active.lift()
                        active.focus_force()
                        return "ok"
            last_shown_at = self._last_shown_at.get(dedupe_key, 0.0)
            if time.monotonic() - last_shown_at < self._cooldown_sec:
                return "ok"

        buttons = ("yes", "no") if kind == "question" else ("ok",)
        dialog = _PrimaryMonitorModal(
            owner,
            title=title,
            message=message,
            kind=kind,
            buttons=buttons,
        )
        if dedupe_key:
            self._active_keys[dedupe_key] = dialog
        try:
            return dialog.show()
        finally:
            if dedupe_key:
                self._last_shown_at[dedupe_key] = time.monotonic()
                active = self._active_keys.get(dedupe_key)
                if active is dialog:
                    self._active_keys.pop(dedupe_key, None)

    def showinfo(self, title: str, message: str, *, parent: tk.Misc | None = None, dedupe_key: str | None = None) -> str:
        self._show_modal("info", title, message, parent=parent, dedupe_key=dedupe_key)
        return "ok"

    def showwarning(self, title: str, message: str, *, parent: tk.Misc | None = None, dedupe_key: str | None = None) -> str:
        self._show_modal("warning", title, message, parent=parent, dedupe_key=dedupe_key)
        return "ok"

    def showerror(self, title: str, message: str, *, parent: tk.Misc | None = None, dedupe_key: str | None = None) -> str:
        self._show_modal("error", title, message, parent=parent, dedupe_key=dedupe_key)
        return "ok"

    def askyesno(self, title: str, message: str, *, parent: tk.Misc | None = None) -> bool:
        return self._show_modal("question", title, message, parent=parent) == "yes"


messagebox = _PrimaryMonitorMessageBox()


def _acquire_single_instance():
    if sys.platform != "win32" or not hasattr(ctypes, "windll"):
        return object()
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, SINGLE_INSTANCE_MUTEX_NAME)
    if not handle:
        return object()
    if kernel32.GetLastError() == 183:
        with contextlib.suppress(Exception):
            ctypes.windll.user32.MessageBoxW(
                None,
                "MTProxy AutoSwitch уже запущен. Закройте существующий экземпляр или откройте окно из трея.",
                APP_NAME,
                0x30,
            )
        kernel32.CloseHandle(handle)
        return None
    return handle


def _release_single_instance(handle) -> None:
    if sys.platform == "win32" and hasattr(ctypes, "windll") and isinstance(handle, int):
        with contextlib.suppress(Exception):
            ctypes.windll.kernel32.CloseHandle(handle)


_CLIPBOARD_CTRL_KEYSYMS = {
    "a": "select_all",
    "c": "copy",
    "v": "paste",
    "x": "cut",
    "ф": "select_all",
    "с": "copy",
    "м": "paste",
    "ч": "cut",
}
_CLIPBOARD_CTRL_KEYCODES = {
    65: "select_all",
    67: "copy",
    86: "paste",
    88: "cut",
}


def _resolve_clipboard_shortcut(event: tk.Event) -> str | None:
    state = int(getattr(event, "state", 0))
    keysym = str(getattr(event, "keysym", "") or "").lower()
    keycode = int(getattr(event, "keycode", -1))
    is_ctrl = bool(state & 0x4)
    is_shift = bool(state & 0x1)

    if is_ctrl:
        if keysym == "insert" or keycode == 45:
            return "copy"
        return _CLIPBOARD_CTRL_KEYSYMS.get(keysym) or _CLIPBOARD_CTRL_KEYCODES.get(keycode)
    if is_shift:
        if keysym == "insert" or keycode == 45:
            return "paste"
        if keysym == "delete" or keycode == 46:
            return "cut"
    return None


def _bind_clipboard_shortcuts(widget: object, *, readonly: bool = False) -> None:
    target = getattr(widget, "_textbox", None) or getattr(widget, "_entry", None) or widget
    bind_targets = [target]
    if widget is not target:
        bind_targets.append(widget)

    def _is_text_widget() -> bool:
        return isinstance(target, tk.Text)

    def _get_selected_text() -> str:
        try:
            if _is_text_widget():
                return str(target.get("sel.first", "sel.last"))
            selection = target.selection_get()
            return str(selection)
        except Exception:
            return ""

    def _delete_selection() -> None:
        try:
            if _is_text_widget():
                target.delete("sel.first", "sel.last")
            else:
                if target.selection_present():
                    start = int(target.index("sel.first"))
                    end = int(target.index("sel.last"))
                    target.delete(start, end)
        except Exception:
            pass

    def _insert_text(value: str) -> None:
        if not value:
            return
        try:
            if _is_text_widget():
                if target.tag_ranges("sel"):
                    target.delete("sel.first", "sel.last")
                target.insert("insert", value)
            else:
                if target.selection_present():
                    start = int(target.index("sel.first"))
                    end = int(target.index("sel.last"))
                    target.delete(start, end)
                target.insert(target.index("insert"), value)
        except Exception:
            pass

    def _select_all(_event=None):
        try:
            if isinstance(target, tk.Text):
                target.tag_add("sel", "1.0", "end-1c")
            else:
                target.select_range(0, "end")
                target.icursor("end")
        except Exception:
            return "break"
        return "break"

    def _copy(_event=None):
        try:
            text = _get_selected_text()
            if text:
                target.clipboard_clear()
                target.clipboard_append(text)
        except Exception:
            return "break"
        return "break"

    def _cut(_event=None):
        if readonly:
            return "break"
        try:
            text = _get_selected_text()
            if text:
                target.clipboard_clear()
                target.clipboard_append(text)
                _delete_selection()
        except Exception:
            return "break"
        return "break"

    def _paste(_event=None):
        try:
            current_state = str(target.cget("state"))
        except Exception:
            current_state = "normal"
        if current_state in {"disabled", "readonly"} or readonly:
            return "break"
        try:
            text = str(target.clipboard_get())
        except Exception:
            return "break"
        if not text:
            return "break"
        try:
            if _is_text_widget():
                if target.tag_ranges("sel"):
                    target.delete("sel.first", "sel.last")
                target.insert("insert", text)
            else:
                try:
                    target.configure(state="normal")
                except Exception:
                    pass
                try:
                    if target.selection_present():
                        target.delete("sel.first", "sel.last")
                except Exception:
                    pass
                target.insert("insert", text)
        except Exception:
            pass
        return "break"

    def _show_context_menu(event=None):
        menu = None
        try:
            menu = tk.Menu(target, tearoff=0)
            menu.add_command(label="Копировать", command=_copy)
            if not readonly:
                menu.add_command(label="Вырезать", command=_cut)
                menu.add_command(label="Вставить", command=_paste)
            menu.add_separator()
            menu.add_command(label="Выделить все", command=_select_all)
            menu.tk_popup(event.x_root, event.y_root)
        except Exception:
            return "break"
        finally:
            with contextlib.suppress(Exception):
                menu.grab_release()
        return "break"

    def _handle_keypress(event=None):
        action = _resolve_clipboard_shortcut(event)
        if action == "copy":
            return _copy(event)
        if action == "cut":
            return _cut(event)
        if action == "paste":
            return _paste(event)
        if action == "select_all":
            return _select_all(event)
        return None

    sequences = (
        ("<Control-a>", _select_all),
        ("<Control-A>", _select_all),
        ("<Control-Key-a>", _select_all),
        ("<Control-Key-A>", _select_all),
        ("<Control-ф>", _select_all),
        ("<Control-Ф>", _select_all),
        ("<Control-c>", _copy),
        ("<Control-C>", _copy),
        ("<Control-Key-c>", _copy),
        ("<Control-Key-C>", _copy),
        ("<Control-с>", _copy),
        ("<Control-С>", _copy),
        ("<Control-x>", _cut),
        ("<Control-X>", _cut),
        ("<Control-Key-x>", _cut),
        ("<Control-Key-X>", _cut),
        ("<Control-ч>", _cut),
        ("<Control-Ч>", _cut),
        ("<Control-v>", _paste),
        ("<Control-V>", _paste),
        ("<Control-Key-v>", _paste),
        ("<Control-Key-V>", _paste),
        ("<Control-м>", _paste),
        ("<Control-М>", _paste),
        ("<<Copy>>", _copy),
        ("<<Cut>>", _cut),
        ("<<Paste>>", _paste),
        ("<Button-3>", _show_context_menu),
    )
    sequences = (
        ("<KeyPress>", _handle_keypress),
        ("<<Copy>>", _copy),
        ("<<Cut>>", _cut),
        ("<<Paste>>", _paste),
        ("<Button-3>", _show_context_menu),
    )
    for bind_target in bind_targets:
        if getattr(bind_target, "_clipboard_shortcuts_bound", False):
            continue
        for sequence, callback in sequences:
            with contextlib.suppress(Exception):
                bind_target.bind(sequence, callback, add="+")
        with contextlib.suppress(Exception):
            setattr(bind_target, "_clipboard_shortcuts_bound", True)


def _add_help_badge(parent: ctk.CTkFrame, text: str) -> ctk.CTkLabel:
    badge = ctk.CTkLabel(
        parent,
        text="?",
        width=22,
        height=22,
        corner_radius=11,
        fg_color=COLOR_ACCENT_SOFT,
        text_color=COLOR_ACCENT,
        font=("Segoe UI Semibold", 11),
    )
    badge.pack(side="left", padx=(8, 0))
    attach_ctk_tooltip(badge, text)
    return badge


# ─── FIX: максимум 60 кадров вместо 140, явный сброс при destroy ─────────────
class LoopingVideoPreview(ctk.CTkFrame):
    def __init__(self, parent: tk.Misc, *, video_path: Path, width: int = 420, height: int = 236) -> None:
        super().__init__(parent, corner_radius=22, fg_color=COLOR_FIELD, border_width=1, border_color=COLOR_FIELD_BORDER)
        self.video_path = video_path
        self.display_width = width
        self.display_height = height
        self.frames: list[ctk.CTkImage] = []
        self._frame_index = 0
        self._frame_delay_ms = 70
        self._frame_job: str | None = None
        self._destroyed = False

        self.grid_columnconfigure(0, weight=1)
        self.video_label = ctk.CTkLabel(self, text="")
        self.video_label.pack(fill="both", expand=True, padx=10, pady=10)
        self.status_label = ctk.CTkLabel(
            self,
            text="Загрузка preview...",
            text_color=COLOR_TEXT_SOFT,
            font=("Segoe UI", 11),
        )
        self.status_label.pack(anchor="center", pady=(0, 10))

        if imageio is None:
            self.status_label.configure(text="Preview недоступен: не установлен imageio")
        elif not self.video_path.exists():
            self.status_label.configure(text="Preview не найден")
        else:
            self.after(40, self._load_frames)

    def _load_frames(self) -> None:
        if self._destroyed:
            return
        import os as _os, sys as _sys, pathlib as _pl
        if getattr(_sys, "frozen", False) and "IMAGEIO_FFMPEG_EXE" not in _os.environ:
            _meipass = _pl.Path(_sys._MEIPASS)
            for _pat in ("ffmpeg*.exe", "ffmpeg*"):
                _c = sorted(_meipass.glob(_pat))
                if _c:
                    _os.environ["IMAGEIO_FFMPEG_EXE"] = str(_c[0])
                    break
        try:
            pil_frames: list[Image.Image] = []
            with imageio.get_reader(str(self.video_path)) as reader:
                meta = reader.get_meta_data() or {}
                fps = float(meta.get("fps") or 18.0)
                # Увеличиваем step чтобы ограничить число кадров до ~60
                step = max(1, int(round(fps / 15.0)))
                for index, frame in enumerate(reader):
                    if index % step != 0:
                        continue
                    image = self._fit_frame(Image.fromarray(frame).convert("RGB"))
                    pil_frames.append(image)
                    if len(pil_frames) >= 60:   # FIX: было 140
                        break
            if not pil_frames:
                raise RuntimeError("no_frames")
            delay_ms = max(50, int(round(1000.0 / min(15.0, max(8.0, fps / step)))))
            if not self._destroyed:
                self._set_frames(pil_frames, delay_ms)
        except Exception:
            if not self._destroyed:
                self.status_label.configure(text="Не удалось загрузить preview")

    def _fit_frame(self, frame: Image.Image) -> Image.Image:
        frame_ratio = frame.width / frame.height
        display_ratio = self.display_width / self.display_height
        if frame_ratio > display_ratio:
            new_h = self.display_height
            new_w = int(frame_ratio * new_h)
        else:
            new_w = self.display_width
            new_h = int(new_w / frame_ratio)
        resized = frame.resize((new_w, new_h), Image.Resampling.LANCZOS)
        left = (new_w - self.display_width) // 2
        top  = (new_h - self.display_height) // 2
        return resized.crop((left, top, left + self.display_width, top + self.display_height))

    def _set_frames(self, pil_frames: list[Image.Image], delay_ms: int) -> None:
        self.frames = [
            ctk.CTkImage(light_image=frame, dark_image=frame, size=(self.display_width, self.display_height))
            for frame in pil_frames
        ]
        self._frame_delay_ms = delay_ms
        self.status_label.configure(text="")
        self._play_next()

    def _play_next(self) -> None:
        self._frame_job = None
        if self._destroyed or not self.frames:
            return
        frame = self.frames[self._frame_index]
        self.video_label.configure(image=frame)
        self._frame_index = (self._frame_index + 1) % len(self.frames)
        self._frame_job = self.after(self._frame_delay_ms, self._play_next)

    def destroy(self) -> None:
        self._destroyed = True
        if self._frame_job is not None:
            with contextlib.suppress(Exception):
                self.after_cancel(self._frame_job)
            self._frame_job = None
        with contextlib.suppress(Exception):
            self.video_label.configure(image=None)
        # Явно освобождаем PIL/tk PhotoImage объекты
        self.frames.clear()
        super().destroy()


def _telegram_web_hosts_block() -> str:
    lines = [HOSTS_BLOCK_BEGIN, *TELEGRAM_WEB_HOSTS_LINES, HOSTS_BLOCK_END]
    return "\n".join(lines)


def _strip_hosts_block(text: str) -> str:
    start = text.find(HOSTS_BLOCK_BEGIN)
    if start < 0:
        return text
    end = text.find(HOSTS_BLOCK_END, start)
    if end < 0:
        return text[:start].rstrip() + "\n"
    end += len(HOSTS_BLOCK_END)
    stripped = (text[:start] + text[end:]).strip()
    return stripped + ("\n" if stripped else "")


def _autostart_command() -> str:
    target = Path(sys.executable).resolve()
    if getattr(sys, "frozen", False):
        if sys.platform == "darwin":
            app_bundle = _macos_app_bundle_path(target)
            if app_bundle is not None:
                return f'/usr/bin/open -a "{app_bundle}"'
        return f'"{target}"'
    script = Path(__file__).resolve()
    return f'"{target}" "{script}"'


def is_autostart_enabled() -> bool:
    if sys.platform == "darwin":
        plist_path = _macos_launch_agent_path()
        if not plist_path.exists():
            return False
        try:
            with plist_path.open("rb") as handle:
                payload = plistlib.load(handle)
        except Exception:
            return False
        return list(payload.get("ProgramArguments") or []) == _macos_launch_agent_payload().get("ProgramArguments")
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, AUTOSTART_VALUE)
        return str(value).strip() == _autostart_command()
    except FileNotFoundError:
        return False
    except OSError:
        return False


def set_autostart_enabled(enabled: bool) -> None:
    if sys.platform == "darwin":
        plist_path = _macos_launch_agent_path()
        if enabled:
            plist_path.parent.mkdir(parents=True, exist_ok=True)
            with plist_path.open("wb") as handle:
                plistlib.dump(_macos_launch_agent_payload(), handle, sort_keys=False)
        else:
            with contextlib.suppress(FileNotFoundError):
                plist_path.unlink()
        return
    if winreg is None:
        if enabled:
            raise RuntimeError("autostart_is_not_supported_on_this_platform")
        return
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as key:
        if enabled:
            winreg.SetValueEx(key, AUTOSTART_VALUE, 0, winreg.REG_SZ, _autostart_command())
        else:
            try:
                winreg.DeleteValue(key, AUTOSTART_VALUE)
            except FileNotFoundError:
                pass


def _macos_app_bundle_path(target: Path | None = None) -> Path | None:
    executable_path = (target or Path(sys.executable)).resolve()
    macos_dir = executable_path.parent
    if macos_dir.name != "MacOS":
        return None
    contents_dir = macos_dir.parent
    if contents_dir.name != "Contents":
        return None
    app_bundle = contents_dir.parent
    if app_bundle.suffix != ".app":
        return None
    return app_bundle


def _macos_launch_agent_path() -> Path:
    bundle_id = "com.mtproxyautoswitch"
    return Path.home() / "Library" / "LaunchAgents" / f"{bundle_id}.plist"


def _macos_launch_agent_payload() -> dict[str, object]:
    target = Path(sys.executable).resolve()
    app_bundle = _macos_app_bundle_path(target)
    if app_bundle is not None:
        arguments = ["/usr/bin/open", "-a", str(app_bundle)]
        working_directory = str(app_bundle.parent)
    elif getattr(sys, "frozen", False):
        arguments = [str(target)]
        working_directory = str(target.parent)
    else:
        script = Path(__file__).resolve()
        arguments = [str(target), str(script)]
        working_directory = str(script.parent)
    return {
        "Label": _macos_launch_agent_path().stem,
        "ProgramArguments": arguments,
        "WorkingDirectory": working_directory,
        "RunAtLoad": True,
        "KeepAlive": False,
    }


class MTProxyAutoSwitchApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.message_queue: queue.Queue[tuple] = queue.Queue()
        self.runtime = AppRuntime(log_sink=self._push_log, event_sink=self._push_event)
        self.title(APP_NAME)
        with contextlib.suppress(Exception):
            if APP_ICON_PATH.exists():
                self.iconbitmap(str(APP_ICON_PATH))
        _center_window(self, 438, 720)
        self.minsize(438, 720)
        self.maxsize(438, 720)
        self.resizable(False, False)
        self.configure(fg_color=COLOR_BG)
        self.protocol("WM_DELETE_WINDOW", self._on_close_requested)
        self.bind("<F11>", lambda _event: "break")
        self.log_lines: list[str] = []
        self.last_upstream: dict[str, object] = {}
        self.last_upload_kbps: float | None = None
        self.last_download_kbps: float | None = None
        self.auth_status: dict[str, object] = {
            "authorized": False,
            "display": "",
            "phone": "",
            "session_exists": False,
        }
        self.snapshot_cache: dict[str, object] = {}
        self.refresh_thread: threading.Thread | None = None
        self.refresh_cancel_event = threading.Event()
        self.refresh_in_progress = False
        self.runtime_call_count = 0
        self.settings_dialog: SettingsDialog | None = None
        self.settings_button: ctk.CTkButton | None = None
        self.qr_dialog: QRAuthDialog | None = None
        self._tray_icon: pystray.Icon | None = None
        self._tray_lock = threading.RLock()
        self._tray_stopping = False
        self._tray_started = False
        self._hidden_to_tray = False
        self._quitting = False
        self.update_info: dict[str, object] = {
            "checking": False,
            "available": False,
            "tag_name": "",
            "html_url": "",
            "status": "",
        }

        self._ensure_config_flags()
        self._apply_appearance()
        self._configure_ttk_style()
        self._create_variables()
        self._build_layout()
        self._refresh_snapshot()
        self.bind("<Configure>", self._on_window_resize)
        # FIX: убраны bind_all clipboard shortcuts — они конфликтовали с локальными
        # обработчиками в SettingsDialog (двойной paste, блокировка вставки).
        # Все entry/textbox уже имеют _bind_clipboard_shortcuts вызванным индивидуально.

        self.after(100, self._process_messages)
        self.after(600, self.refresh_auth_status)
        self.after(900, self._auto_refresh_initial)
        self.after(1500, self._auto_check_updates)
        if self.runtime.config.start_minimized_to_tray:
            self.after(1200, lambda: self._hide_to_tray(notify=False))

    def _ensure_config_flags(self) -> None:
        autostart_enabled = is_autostart_enabled()
        if self.runtime.config.autostart_enabled != autostart_enabled:
            self.runtime.config.autostart_enabled = autostart_enabled
            self.runtime.save_config()

    def _apply_appearance(self) -> None:
        mode = self.runtime.config.appearance if self.runtime.config.appearance in APPEARANCE_LABELS else "auto"
        ctk.set_appearance_mode(_appearance_mode_to_ctk(mode))
        self.configure(fg_color=COLOR_BG)

    def _is_dark_mode(self) -> bool:
        return str(ctk.get_appearance_mode()).lower() == "dark"

    def _configure_ttk_style(self) -> None:
        style = ttk.Style(self)
        with contextlib.suppress(Exception):
            style.theme_use("clam")
        if self._is_dark_mode():
            background = "#0F1620"
            heading = "#162235"
            foreground = "#E5EDF8"
            heading_foreground = "#BDD1EE"
            selection = "#1F3B66"
        else:
            background = "#FFFFFF"
            heading = "#EEF4FF"
            foreground = "#183153"
            heading_foreground = "#35517A"
            selection = "#DDEBFF"
        style.configure(
            "Compact.Treeview",
            background=background,
            fieldbackground=background,
            foreground=foreground,
            rowheight=28,
            borderwidth=0,
            relief="flat",
        )
        style.configure(
            "Compact.Treeview.Heading",
            background=heading,
            foreground=heading_foreground,
            font=("Segoe UI Semibold", 10),
            relief="flat",
        )
        style.map("Compact.Treeview", background=[("selected", selection)], foreground=[("selected", foreground)])

    def _create_variables(self) -> None:
        self.status_var = tk.StringVar(value="Подготовка")
        self.caption_var = tk.StringVar(value="Локальный MTProto frontend")
        self.primary_label_var = tk.StringVar(value="Пуск")
        self.primary_hint_var = tk.StringVar(value="Стартовый список загрузится сразу, полный refresh пройдет в фоне.")
        self.copy_hint_var = tk.StringVar(value="")
        self.link_preview_var = tk.StringVar(value="")
        self.pool_count_var = tk.StringVar(value="0")
        self.ping_var = tk.StringVar(value="n/a")
        self.speed_var = tk.StringVar(value="↑ n/a\n↓ n/a")
        self.active_proxy_var = tk.StringVar(value="Еще не выбран")
        self.thread_var = tk.StringVar(value="Telegram-источники еще не проверялись")
        self.footer_var = tk.StringVar(value="Стартовая инициализация")
        self.progress_text_var = tk.StringVar(value="Готов к обновлению")
        self.update_status_var = tk.StringVar(value=f"Версия {APP_PUBLIC_VERSION}")
        self.refresh_fraction = 0.0
        self.refresh_phase = ""
        self.refresh_phase_total = 0

    def _build_layout(self) -> None:
        shell = ctk.CTkFrame(self, fg_color="transparent")
        self.main_shell = shell
        shell.pack(fill="both", expand=True, padx=16, pady=16)

        top = ctk.CTkFrame(shell, fg_color="transparent")
        top.pack(fill="x")

        ctk.CTkLabel(
            top,
            text="MTProxy",
            text_color=COLOR_TEXT,
            font=("Segoe UI Semibold", 24),
        ).pack(side="left")

        top_actions = ctk.CTkFrame(top, fg_color="transparent")
        top_actions.pack(side="right")
        self.settings_button = ctk.CTkButton(
            top_actions,
            text="Настройки",
            width=96,
            height=34,
            corner_radius=17,
            fg_color=COLOR_CARD,
            hover_color=COLOR_ACCENT_SOFT,
            text_color=COLOR_TEXT,
            border_width=1,
            border_color=COLOR_FIELD_BORDER,
            font=("Segoe UI Semibold", 12),
            command=self.open_settings,
        )
        self.settings_button.pack(side="left")

        self.status_chip = ctk.CTkLabel(
            shell,
            textvariable=self.status_var,
            height=32,
            corner_radius=16,
            fg_color=COLOR_SUCCESS_BG,
            text_color=COLOR_SUCCESS_TEXT,
            padx=14,
            font=("Segoe UI Semibold", 12),
        )
        self.status_chip.pack(anchor="w", pady=(10, 0))

        self.progress_row = ctk.CTkFrame(shell, fg_color="transparent")
        self.progress_row.pack(fill="x", pady=(10, 0))
        self.progress_bar = ctk.CTkProgressBar(
            self.progress_row,
            height=8,
            corner_radius=999,
            progress_color=COLOR_ACCENT,
            fg_color=COLOR_FIELD_BORDER,
        )
        self.progress_bar.pack(fill="x")
        self.progress_bar.set(0.0)
        ctk.CTkLabel(
            self.progress_row,
            textvariable=self.progress_text_var,
            text_color=COLOR_TEXT_SOFT,
            font=("Segoe UI", 11),
        ).pack(anchor="w", pady=(6, 0))
        self.progress_thread_label = ctk.CTkLabel(
            self.progress_row,
            textvariable=self.thread_var,
            text_color=COLOR_TEXT_FAINT,
            font=("Segoe UI", 11),
            justify="left",
            wraplength=360,
        )
        self.progress_thread_label.pack(anchor="w", pady=(4, 0))

        hero = ctk.CTkFrame(shell, corner_radius=26, fg_color=COLOR_CARD, border_width=1, border_color=COLOR_BORDER)
        hero.pack(fill="x", pady=(12, 10))
        self.hero_card = hero

        self.primary_button = ctk.CTkButton(
            hero,
            textvariable=self.primary_label_var,
            command=self._on_primary_action,
            width=144,
            height=144,
            corner_radius=72,
            fg_color=COLOR_ACCENT,
            hover_color=COLOR_ACCENT_HOVER,
            text_color="#FFFFFF",
            font=("Segoe UI Semibold", 22),
        )
        self.primary_button.pack(pady=(16, 8))

        self.primary_hint_label = ctk.CTkLabel(
            hero,
            textvariable=self.primary_hint_var,
            text_color=COLOR_TEXT_SOFT,
            font=("Segoe UI", 12),
            justify="center",
            wraplength=320,
        )
        self.primary_hint_label.pack(padx=16, pady=(0, 6))

        actions = ctk.CTkFrame(hero, fg_color="transparent")
        actions.pack(fill="x", padx=16, pady=(0, 6))
        self.hero_actions = actions
        actions.grid_columnconfigure((0, 1), weight=1)
        self.refresh_button = ctk.CTkButton(
            actions,
            text="Обновить",
            height=38,
            corner_radius=19,
            fg_color=COLOR_ACCENT_SOFT,
            hover_color=COLOR_ACCENT_SOFT_HOVER,
            text_color=COLOR_ACCENT,
            command=self.start_refresh,
        )
        self.refresh_button.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.open_output_button = ctk.CTkButton(
            actions,
            text="Открыть list",
            height=38,
            corner_radius=19,
            fg_color=COLOR_CARD,
            hover_color=COLOR_ACCENT_SOFT,
            text_color=COLOR_TEXT,
            border_width=1,
            border_color=COLOR_FIELD_BORDER,
            command=self.open_output_folder,
        )
        self.open_output_button.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        self.main_action_buttons = [self.refresh_button, self.open_output_button]

        self.copy_button = ctk.CTkButton(
            shell,
            text="Скопировать ссылку подключения",
            height=40,
            corner_radius=20,
            fg_color=COLOR_CARD,
            hover_color=COLOR_ACCENT_SOFT,
            border_width=1,
            border_color=COLOR_FIELD_BORDER,
            text_color=COLOR_TEXT,
            font=("Segoe UI Semibold", 13),
            command=self.copy_local_link,
        )
        self.copy_button.pack(fill="x")

        summary = ctk.CTkFrame(shell, fg_color="transparent")
        summary.pack(fill="x", pady=(8, 0))
        self.summary_frame = summary
        summary.grid_columnconfigure((0, 1, 2), weight=1)
        self.summary_cards = [
            self._create_stat_card(summary, 0, "Рабочих", self.pool_count_var),
            self._create_stat_card(summary, 1, "Пинг", self.ping_var),
            self._create_stat_card(summary, 2, "Скорость", self.speed_var),
        ]

        active = ctk.CTkFrame(
            shell,
            corner_radius=24,
            fg_color=COLOR_CARD,
            border_width=1,
            border_color=COLOR_BORDER,
            height=190,
        )
        active.pack(fill="both", expand=True, pady=(10, 0))
        active.pack_propagate(False)
        self.active_card = active

        ctk.CTkLabel(
            active,
            text="Активный upstream",
            text_color=COLOR_TEXT,
            font=("Segoe UI Semibold", 15),
        ).pack(anchor="w", padx=16, pady=(14, 4))
        self.active_proxy_label = ctk.CTkLabel(
            active,
            textvariable=self.active_proxy_var,
            text_color=COLOR_TEXT,
            font=("Segoe UI", 13),
            justify="left",
            wraplength=308,
        )
        self.active_proxy_label.pack(anchor="w", padx=16)
        self.footer_info_box = ctk.CTkTextbox(
            active,
            height=56,
            corner_radius=0,
            fg_color="transparent",
            border_width=0,
            text_color=COLOR_TEXT_FAINT,
            font=("Segoe UI", 11),
            activate_scrollbars=False,
            wrap="word",
        )
        self.footer_info_box.pack(fill="x", padx=16, pady=(10, 14))
        self.footer_info_box.insert("1.0", self.footer_var.get())
        self.footer_info_box.configure(state="disabled")
        self._refresh_main_layout()

    def _create_stat_card(self, parent: ctk.CTkFrame, column: int, title: str, variable: tk.StringVar) -> ctk.CTkFrame:
        card = ctk.CTkFrame(parent, corner_radius=22, fg_color=COLOR_CARD, border_width=1, border_color=COLOR_BORDER)
        card.grid(row=0, column=column, padx=4, sticky="nsew")
        ctk.CTkLabel(
            card,
            text=title,
            text_color=COLOR_TEXT_SOFT,
            font=("Segoe UI", 10),
        ).pack(anchor="w", padx=12, pady=(10, 0))
        ctk.CTkLabel(
            card,
            textvariable=variable,
            text_color=COLOR_TEXT,
            font=("Segoe UI Semibold", 18),
        ).pack(anchor="w", padx=12, pady=(2, 10))
        return card

    def _push_log(self, message: str) -> None:
        self.message_queue.put(("log", message))

    def _push_event(self, event_name: str, payload: dict[str, object]) -> None:
        self.message_queue.put(("event", event_name, payload))

    def _append_log(self, message: str) -> None:
        self.log_lines.append(message)
        if len(self.log_lines) > 400:
            self.log_lines = self.log_lines[-400:]

    def _set_speed_display(self, upload_kbps: float | None, download_kbps: float | None) -> None:
        self.speed_var.set(f"↑ {_format_rate_kbps(upload_kbps)}\n↓ {_format_rate_kbps(download_kbps)}")

    def _update_speed_display(
        self,
        upload_kbps: float | None,
        download_kbps: float | None,
        *,
        reset: bool = False,
    ) -> None:
        if reset:
            self.last_upload_kbps = upload_kbps
            self.last_download_kbps = download_kbps
        else:
            if upload_kbps is not None and upload_kbps > 0:
                self.last_upload_kbps = upload_kbps
            if download_kbps is not None and download_kbps > 0:
                self.last_download_kbps = download_kbps
        self.speed_var.set(
            f"↑ {_format_rate_kbps(self.last_upload_kbps)}\n↓ {_format_rate_kbps(self.last_download_kbps)}"
        )

    def _set_footer_info_text(self, text: str) -> None:
        self.footer_var.set(text)
        if hasattr(self, "footer_info_box"):
            self.footer_info_box.configure(state="normal")
            self.footer_info_box.delete("1.0", "end")
            self.footer_info_box.insert("1.0", text)
            self.footer_info_box.configure(state="disabled")

    def _set_refresh_progress(self, fraction: float, text: str) -> None:
        self.refresh_fraction = max(0.0, min(1.0, float(fraction)))
        self.progress_bar.set(self.refresh_fraction)
        self.progress_text_var.set(text)

    def _reset_refresh_progress(self) -> None:
        self.refresh_phase = "preparing"
        self.refresh_phase_total = 0
        self._set_refresh_progress(0.02, "Подготовка к обновлению списка")

    def _handle_runtime_event(self, event_name: str, payload: dict[str, object]) -> None:
        if event_name == "phase":
            phase = str(payload.get("phase", ""))
            self.refresh_phase = phase
            if phase == "scraping":
                self.refresh_phase_total = int(payload.get("total_sources", 0))
                self._set_refresh_progress(0.04, f"Сбор сайтов: 0/{self.refresh_phase_total}")
            elif phase == "probing":
                self.refresh_phase_total = int(payload.get("total_proxies", 0))
                self._set_refresh_progress(0.38, f"Проверка прокси: 0/{self.refresh_phase_total}")
            return

        if event_name == "source_started":
            total = max(1, int(payload.get("total", 1)))
            index = max(1, int(payload.get("index", 1)))
            self._set_refresh_progress(0.04 + ((index - 1) / total) * 0.28, f"Сбор сайтов: {index}/{total}")
            return

        if event_name == "source_finished":
            total = max(1, int(payload.get("total", 1)))
            index = max(1, int(payload.get("index", 1)))
            unique_total = int(payload.get("unique_total", 0))
            self._set_refresh_progress(0.04 + (index / total) * 0.28, f"Сбор сайтов: {index}/{total}  |  найдено {unique_total}")
            return

        if event_name == "probe_result":
            total = max(1, int(payload.get("total", 1)))
            completed = max(0, int(payload.get("completed", 0)))
            self._set_refresh_progress(0.36 + (completed / total) * 0.60, f"Проверка прокси: {completed}/{total}")
            return

        if event_name == "telegram_sources_started":
            total_sources = max(1, int(payload.get("total_sources", 1)))
            max_age_days = int(payload.get("max_age_days", 0))
            suffix = f"  |  последние {max_age_days} дн." if max_age_days > 0 else ""
            self._set_refresh_progress(0.965, f"Парс Telegram-источников: 0/{total_sources}{suffix}")
            return

        if event_name == "telegram_source_started":
            total = max(1, int(payload.get("total", 1)))
            index = max(1, int(payload.get("index", 1)))
            source = _trim_middle(str(payload.get("source", "")), 46)
            self._set_refresh_progress(0.965 + ((index - 1) / total) * 0.01, f"Telegram {index}/{total}: {source}")
            return

        if event_name == "telegram_source_progress":
            total = max(1, int(payload.get("total", 1)))
            index = max(1, int(payload.get("index", 1)))
            scanned = max(0, int(payload.get("scanned_messages", 0)))
            proxy_count = max(0, int(payload.get("proxy_count", 0)))
            source = _trim_middle(str(payload.get("source", "")), 42)
            self._set_refresh_progress(
                0.967 + (index / total) * 0.008,
                f"Telegram {index}/{total}: {scanned} сообщений  |  {proxy_count} proxy  |  {source}",
            )
            return

        if event_name == "telegram_source_finished":
            total = max(1, int(payload.get("total", 1)))
            index = max(1, int(payload.get("index", 1)))
            proxy_count = max(0, int(payload.get("proxy_count", 0)))
            scanned = max(0, int(payload.get("scanned_messages", 0)))
            suffix = ""
            if bool(payload.get("hit_age_limit")):
                suffix = "  |  достигнут лимит по давности"
            elif bool(payload.get("timed_out")):
                suffix = "  |  частично по таймауту"
            elif bool(payload.get("hit_limit")):
                suffix = "  |  достигнут лимит сообщений"
            self._set_refresh_progress(0.97 + (index / total) * 0.01, f"Telegram {index}/{total}: {proxy_count} proxy из {scanned} сообщений{suffix}")
            return

        if event_name == "telegram_sources_finished":
            proxy_count = max(0, int(payload.get("proxy_count", 0)))
            self._set_refresh_progress(0.98, f"Парс Telegram завершен: найдено {proxy_count} proxy")
            return

        if event_name == "telegram_sources_probing_started":
            total_proxies = max(0, int(payload.get("total_proxies", 0)))
            self._set_refresh_progress(0.982, f"Допроверка Telegram-proxy: {total_proxies}")
            return

        if event_name == "telegram_sources_probing_finished":
            total_proxies = max(0, int(payload.get("total_proxies", 0)))
            self._set_refresh_progress(0.986, f"Допроверка Telegram-proxy завершена: {total_proxies}")
            return

        if event_name == "deep_media_started":
            total = max(1, int(payload.get("total", 1)))
            mode = "РФ white-list" if bool(payload.get("strict")) else "deep media"
            self._set_refresh_progress(0.988, f"{mode}: 0/{total}")
            return

        if event_name == "deep_media_progress":
            total = max(1, int(payload.get("total", 1)))
            index = max(1, int(payload.get("index", 1)))
            host = str(payload.get("host", "") or "")
            port = str(payload.get("port", "") or "")
            note = str(payload.get("note", "") or "")
            mode = "РФ white-list" if bool(payload.get("strict")) else "deep media"
            label = f"{host}:{port}" if host and port else "proxy"
            self._set_refresh_progress(0.988 + (index / total) * 0.01, f"{mode}: {index}/{total}  |  {label}  |  {note}")
            return

        if event_name == "deep_media_finished":
            mode = "РФ white-list" if bool(payload.get("strict")) else "deep media"
            rejected = max(0, int(payload.get("rejected", 0)))
            self._set_refresh_progress(0.998, f"{mode} завершен  |  отклонено {rejected}")
            return

        if event_name == "files_written":
            self._set_refresh_progress(0.999, "Запись итоговых файлов")
            return

        if event_name == "runtime_refresh_complete":
            working = int(payload.get("working", 0))
            unique = int(payload.get("unique", 0))
            self._set_refresh_progress(1.0, f"Обновление завершено: {working} рабочих из {unique}")
            return

        if event_name == "runtime_refresh_waiting":
            reason = str(payload.get("reason", "") or "")
            active_media = int(payload.get("active_media", 0) or 0)
            active_heavy = int(payload.get("active_heavy", 0) or 0)
            self._set_refresh_progress(
                max(self.refresh_fraction or 0.02, 0.03),
                f"Ждём завершения медиа перед {reason}  |  media={active_media} heavy={active_heavy}",
            )
            return

        if event_name == "runtime_refresh_resumed":
            reason = str(payload.get("reason", "") or "")
            self._set_refresh_progress(
                max(self.refresh_fraction or 0.03, 0.04),
                f"Медиа завершено, продолжаем {reason}",
            )
            return

        if event_name == "runtime_refresh_wait_timeout":
            reason = str(payload.get("reason", "") or "")
            self._set_refresh_progress(
                max(self.refresh_fraction or 0.03, 0.04),
                f"Медиа ещё активно, продолжаем {reason} без ожидания",
            )
            return

        if event_name == "seed_loaded" and not self.refresh_in_progress:
            count = int(payload.get("count", 0))
            source = str(payload.get("source", ""))
            self._set_refresh_progress(1.0, f"Стартовый пул: {count}  |  {_format_seed_source(source)}")
            return

        if event_name == "telegram_qr_ready":
            self._show_qr_dialog(payload)
            return

        if event_name == "telegram_auth_required":
            feature = str(payload.get("feature", "") or "")
            if feature == "deep_media":
                self.progress_text_var.set("Deep media check пропущен: требуется вход в Telegram")
            elif feature == "rf_whitelist":
                self.progress_text_var.set("РФ white-list media check пропущен: требуется вход в Telegram")
            return

        if event_name == "local_server_state":
            error = str(payload.get("error", "") or "")
            if error:
                self.progress_text_var.set(f"Локальный frontend: {error}")
            return

        if event_name == "local_upstream_selected":
            self.last_upstream = dict(payload)
            host = payload.get("host")
            port = payload.get("port")
            if host and port:
                self.active_proxy_var.set(f"{host}:{port}")
            self.ping_var.set(_format_latency(_safe_float(payload.get("latency_ms") or payload.get("connect_latency_ms"))))
            if bool(payload.get("is_media")) and not self.refresh_in_progress:
                self.progress_text_var.set(f"Медиа-сессия через {host}:{port}")
            return

        if event_name == "local_media_activity":
            host = str(payload.get("host", "") or "")
            port = str(payload.get("port", "") or "")
            upload_kbps = _safe_float(payload.get("upload_kbps"))
            download_kbps = _safe_float(payload.get("download_kbps"))
            label = f"{host}:{port}" if host and port else "proxy"
            upload_speed = _format_rate_kbps(upload_kbps)
            download_speed = _format_rate_kbps(download_kbps)
            self._update_speed_display(upload_kbps, download_kbps)
            if not self.refresh_in_progress:
                self.progress_text_var.set(f"Медиа-сессия: {label} | ↑ {upload_speed} | ↓ {download_speed}")
            return

        if event_name == "local_session_closed" and (bool(payload.get("heavy_upload")) or bool(payload.get("is_media"))):
            success = bool(payload.get("success"))
            bytes_up = int(payload.get("bytes_up") or 0)
            bytes_down = int(payload.get("bytes_down") or 0)
            upload_kbps = _safe_float(payload.get("upload_kbps")) if bytes_up >= DISPLAY_SPEED_MIN_TRANSFER_BYTES else None
            download_kbps = _safe_float(payload.get("download_kbps")) if bytes_down >= DISPLAY_SPEED_MIN_TRANSFER_BYTES else None
            speed = _format_rate_kbps(upload_kbps)
            self._update_speed_display(upload_kbps, download_kbps)
            if not self.refresh_in_progress:
                self.progress_text_var.set(f"Выгрузка {'ok' if success else 'fail'} | {speed}")
            return

    def get_runtime_state(self, *, allow_stale: bool = False) -> tuple[AppConfig, dict[str, object]]:
        if allow_stale and self.snapshot_cache and self.refresh_in_progress:
            return self.runtime.config, dict(self.snapshot_cache)
        return self.runtime.config, self.runtime.snapshot()

    def _process_messages(self) -> None:
        refresh_required = False
        while True:
            try:
                item = self.message_queue.get_nowait()
            except queue.Empty:
                break

            kind = item[0]
            if kind == "log":
                self._append_log(str(item[1]))
                continue

            if kind == "event":
                _, event_name, payload = item
                self._handle_runtime_event(event_name, payload)
                if event_name in {
                    "runtime_refresh_complete",
                    "seed_loaded",
                    "local_server_state",
                    "local_media_activity",
                    "local_session_closed",
                    "local_upstream_selected",
                }:
                    refresh_required = True
                continue

            if kind == "refresh_done":
                self.refresh_in_progress = False
                self.refresh_cancel_event.clear()
                refresh_required = True
                continue

            if kind == "refresh_error":
                self.refresh_in_progress = False
                self.refresh_cancel_event.clear()
                refresh_required = True
                _, message, details = item
                if message == "refresh_cancelled":
                    self._append_log("[refresh] cancelled")
                    self._set_refresh_progress(0.0, "Обновление отменено")
                    self.copy_hint_var.set("Обновление отменено")
                else:
                    self._append_log(f"[refresh] failed: {message}")
                    self._append_log(details)
                    self._set_refresh_progress(0.0, f"Ошибка обновления: {message}")
                    self.copy_hint_var.set(f"Refresh error: {message}")
                self.after(5000, lambda: self.copy_hint_var.set(""))

        if refresh_required:
            self._refresh_snapshot()
        self.after(100, self._process_messages)

    def _is_ui_busy(self) -> bool:
        return self.refresh_in_progress or self.runtime_call_count > 0

    def _refresh_snapshot(self) -> None:
        config, snapshot = self.get_runtime_state(allow_stale=self.refresh_in_progress)
        self.snapshot_cache = dict(snapshot)

        rows = list(snapshot.get("pool_rows", []))
        best_row = rows[0] if rows else None
        running = bool(snapshot.get("local_running"))
        working_count = int(snapshot.get("working_count", 0))
        thread_status = str(snapshot.get("thread_status", ""))
        thread_count = int(snapshot.get("thread_proxy_count", 0))
        local_url = str(snapshot.get("local_url", ""))

        ui_busy = self._is_ui_busy()

        if self.refresh_in_progress:
            self.status_var.set("Обновление")
            self.status_chip.configure(fg_color=COLOR_ACCENT_SOFT, text_color=COLOR_ACCENT)
        elif running:
            self.status_var.set("Локальный прокси активен")
            self.status_chip.configure(fg_color=COLOR_SUCCESS_BG, text_color=COLOR_SUCCESS_TEXT)
        elif working_count > 0:
            self.status_var.set("Готов к запуску")
            self.status_chip.configure(fg_color=COLOR_WARN_BG, text_color=COLOR_WARN_TEXT)
        else:
            self.status_var.set("Пул пуст")
            self.status_chip.configure(fg_color=COLOR_IDLE_BG, text_color=COLOR_IDLE_TEXT)

        self.primary_label_var.set("Стоп" if running else "Пуск")
        can_toggle = running or working_count > 0
        self.primary_button.configure(
            state="normal" if (can_toggle and not ui_busy) else "disabled",
            fg_color=COLOR_DANGER_BG if running else COLOR_ACCENT,
            hover_color=COLOR_DANGER_BORDER if running else COLOR_ACCENT_HOVER,
            border_width=0,
        )
        refresh_cancel_requested = self.refresh_cancel_event.is_set()
        if self.refresh_in_progress:
            self.refresh_button.configure(
                text="Отмена..." if refresh_cancel_requested else "Отмена",
                fg_color=COLOR_DANGER_BG,
                hover_color=COLOR_DANGER_BORDER,
                text_color=COLOR_DANGER_TEXT,
                state="disabled" if refresh_cancel_requested else "normal",
            )
        else:
            self.refresh_button.configure(
                text="Обновить",
                fg_color=COLOR_ACCENT_SOFT,
                hover_color=COLOR_ACCENT_SOFT_HOVER,
                text_color=COLOR_ACCENT,
                state="disabled" if ui_busy else "normal",
            )
        self.open_output_button.configure(state="disabled" if ui_busy else "normal")
        self.copy_button.configure(state="normal" if local_url else "disabled")
        if self.settings_button is not None:
            self.settings_button.configure(state="disabled" if self.refresh_in_progress else "normal")

        if local_url:
            self.link_preview_var.set(_trim_middle(local_url, 72))
        else:
            self.link_preview_var.set("Локальная ссылка появится после инициализации")

        if running:
            self.primary_hint_var.set(f"Telegram можно подключать к {config.local_host}:{config.local_port}")
        elif ui_busy and not self.refresh_in_progress:
            self.primary_hint_var.set("Во время обновления списка, авторизации или установки обновления управление временно заблокировано.")
        elif working_count > 0:
            self.primary_hint_var.set("Пул готов. Кнопка по центру запускает и останавливает локальный frontend.")
        else:
            self.primary_hint_var.set("Сначала обновите список, затем запускайте локальный frontend.")

        self.pool_count_var.set(str(working_count))

        display_host = "Еще не выбран"
        display_ping = "n/a"
        display_upload_kbps = None
        display_download_kbps = None
        active_row = None
        now_ts = time.time()
        if self.last_upstream:
            display_host = f"{self.last_upstream.get('host')}:{self.last_upstream.get('port')}"
            last_host = self.last_upstream.get("host")
            last_port = self.last_upstream.get("port")
            active_row = next(
                (
                    row for row in rows
                    if str(row.get("host")) == str(last_host) and str(row.get("port")) == str(last_port)
                ),
                None,
            )
        if active_row is None:
            active_row = best_row
        elif best_row is None:
            best_row = active_row
        if not self.last_upstream and best_row is not None:
            display_host = f"{best_row.get('host')}:{best_row.get('port')}"

        ping_value = None
        if self.last_upstream:
            ping_value = _safe_float(self.last_upstream.get("latency_ms"))
            if ping_value is None:
                ping_value = _safe_float(self.last_upstream.get("connect_latency_ms"))
        if ping_value is None and active_row is not None:
            ping_value = _safe_float(
                active_row.get("live_latency_ms")
                or active_row.get("base_latency_ms")
                or active_row.get("connect_latency_ms")
            )
        if ping_value is None and best_row is not None:
            display_host = f"{best_row.get('host')}:{best_row.get('port')}"
            ping_value = _safe_float(
                best_row.get("live_latency_ms")
                or best_row.get("base_latency_ms")
                or best_row.get("connect_latency_ms")
            )
        display_ping = _format_latency(ping_value)

        fresh_live_rows = [
            row
            for row in rows
            if (now_ts - (_safe_float(row.get("last_live_activity_at")) or 0.0)) <= 2.5
            and (
                int(row.get("active_media_connections") or 0) > 0
                or int(row.get("active_heavy_uploads") or 0) > 0
            )
        ]
        if fresh_live_rows:
            total_upload_kbps = sum(max(0.0, _safe_float(row.get("live_media_upload_kbps")) or 0.0) for row in fresh_live_rows)
            total_download_kbps = sum(max(0.0, _safe_float(row.get("live_media_download_kbps")) or 0.0) for row in fresh_live_rows)
            display_upload_kbps = total_upload_kbps if total_upload_kbps > 0 else None
            display_download_kbps = total_download_kbps if total_download_kbps > 0 else None
        elif active_row is not None:
            last_live_activity_at = _safe_float(active_row.get("last_live_activity_at")) or 0.0
            live_is_fresh = (now_ts - last_live_activity_at) <= 2.5
            if live_is_fresh:
                display_upload_kbps = _safe_float(active_row.get("live_media_upload_kbps"))
                display_download_kbps = _safe_float(active_row.get("live_media_download_kbps"))
            if display_upload_kbps is None:
                display_upload_kbps = _safe_float(active_row.get("recent_media_upload_kbps"))
            if display_download_kbps is None:
                display_download_kbps = _safe_float(active_row.get("recent_media_download_kbps"))

        self.ping_var.set(display_ping)
        self._update_speed_display(display_upload_kbps, display_download_kbps, reset=True)
        self.active_proxy_var.set(display_host)
        telegram_sources_enabled = bool(getattr(config, "telegram_sources_enabled", config.thread_source_enabled))
        self.thread_var.set(_format_thread_status(thread_status, thread_count, enabled=telegram_sources_enabled))

        if self.refresh_in_progress:
            self._set_footer_info_text("Идет полный пересбор и перепроверка прокси")
        elif snapshot.get("last_refresh_finished_at"):
            self._set_footer_info_text(
                f"Уникальных прокси: {snapshot.get('unique_count', 0)}  |  "
                f"Отсеяно: {snapshot.get('rejected_count', 0)}"
            )
        elif snapshot.get("seed_source"):
            self._set_footer_info_text("Загружен стартовый пул. Полный refresh запустится автоматически.")
        else:
            self._set_footer_info_text("Ожидание первого обновления")

        if self.settings_dialog is not None and self.settings_dialog.winfo_exists():
            self.settings_dialog.refresh_from_runtime()
            self.settings_dialog.refresh_interaction_state()
        self._refresh_tray_menu()

    def _auto_refresh_initial(self) -> None:
        if self._quitting or self.refresh_in_progress:
            return
        working_count = int(self.snapshot_cache.get("working_count", 0) or 0)
        if working_count > 0:
            if not bool(self.snapshot_cache.get("local_running")) and self.runtime.config.auto_start_local:
                with contextlib.suppress(Exception):
                    self.runtime.start_local_server()
                    self._refresh_snapshot()
            self.after(15000, self.start_refresh)
            return
        self.start_refresh()

    def _auto_check_updates(self) -> None:
        if self._quitting or not is_public_release():
            return
        if not bool(getattr(self.runtime.config, "auto_update_enabled", True)):
            return
        self.check_for_updates(manual=False)

    def _set_update_status(self, text: str) -> None:
        self.update_info["status"] = text
        self.update_status_var.set(text)
        if self.settings_dialog is not None and self.settings_dialog.winfo_exists():
            self.settings_dialog.refresh_interaction_state()

    def check_for_updates(self, *, manual: bool) -> None:
        if not is_public_release():
            return
        if bool(self.update_info.get("checking")):
            return
        self.update_info["checking"] = True
        self._set_update_status("Проверка обновлений...")

        def ui_call(callback) -> None:
            with contextlib.suppress(Exception):
                if self.winfo_exists():
                    self.after(0, callback)

        def worker() -> None:
            try:
                release = fetch_latest_release()
                available = bool(release.tag_name) and is_update_available(
                    APP_PUBLIC_VERSION,
                    release,
                    install_dir=self.runtime.install_dir,
                )
                info = {
                    "checking": False,
                    "available": available,
                    "tag_name": release.tag_name,
                    "html_url": release.html_url,
                    "status": f"Доступна версия {release.tag_name}" if available else f"Установлена актуальная версия {APP_PUBLIC_VERSION}",
                }
                def on_done() -> None:
                    self.update_info.update(info)
                    self._set_update_status(str(info["status"]))
                    if manual and not available:
                        messagebox.showinfo("Обновления", "Новых версий не найдено.", parent=self)
                    if manual and available:
                        if messagebox.askyesno("Обновления", f"Доступна версия {release.tag_name}. Скачать и установить сейчас?", parent=self):
                            self.install_update()
                    elif available:
                        self.copy_hint_var.set(f"Доступна версия {release.tag_name}")
                        self.after(8000, lambda: self.copy_hint_var.set(""))
                ui_call(on_done)
            except Exception as exc:
                def on_error() -> None:
                    self.update_info["checking"] = False
                    self._set_update_status("Не удалось проверить обновления")
                    if manual:
                        messagebox.showerror("Обновления", str(exc), parent=self)
                ui_call(on_error)

        threading.Thread(target=worker, daemon=True, name="mtproxy-update-check").start()

    def install_update(self) -> None:
        if not is_public_release():
            return
        if bool(self.update_info.get("checking")) or self.refresh_in_progress or self.runtime_call_count > 0:
            return
        if sys.platform != "win32":
            release_url = str(self.update_info.get("html_url") or "") or "https://github.com/pengvench/MTProxyAutoSwitch/releases/latest"
            if webbrowser.open(release_url):
                self._set_update_status("Открыта страница релиза")
            else:
                self._set_update_status("Откройте страницу релиза вручную")
            return
        self.update_info["checking"] = True
        self._set_update_status("Подготовка обновления...")

        def ui_call(callback) -> None:
            with contextlib.suppress(Exception):
                if self.winfo_exists():
                    self.after(0, callback)

        def worker() -> None:
            try:
                prepared_update = prepare_update(
                    install_dir=self.runtime.install_dir,
                    state_dir=self.runtime.state_dir,
                    current_version=APP_PUBLIC_VERSION,
                    progress_sink=lambda message: ui_call(lambda message=message: self._set_update_status(message)),
                )

                def on_ready() -> None:
                    self.update_info["checking"] = False
                    release_tag = prepared_update.release.tag_name or self.update_info.get("tag_name") or "готово"
                    self._set_update_status(f"Обновление {release_tag} загружено")
                    if messagebox.askyesno("Обновления", "Обновление загружено. Перезапустить приложение и установить его сейчас?", parent=self):
                        launch_prepared_update(prepared_update)
                        self._quitting = True
                        self.destroy()
                ui_call(on_ready)
            except Exception as exc:
                def on_error() -> None:
                    self.update_info["checking"] = False
                    if str(exc) == "release_is_current":
                        self._set_update_status(f"Установлена актуальная версия {APP_PUBLIC_VERSION}")
                        messagebox.showinfo("Обновления", "Новых версий не найдено.", parent=self)
                        return
                    self._set_update_status("Не удалось установить обновление")
                    messagebox.showerror("Обновления", str(exc), parent=self)
                ui_call(on_error)

        threading.Thread(target=worker, daemon=True, name="mtproxy-update-install").start()

    def start_local_proxy(self) -> None:
        if self._is_ui_busy():
            return
        if bool(self.snapshot_cache.get("local_running")):
            return
        if int(self.snapshot_cache.get("working_count", 0)) <= 0:
            messagebox.showinfo("Нет рабочего пула", "Сначала нажмите «Обновить», чтобы собрать рабочие прокси.")
            return
        try:
            self.runtime.start_local_server()
        except Exception as exc:
            messagebox.showerror("Запуск не выполнен", str(exc))
            return
        self._refresh_snapshot()
        self.after(250, self.open_local_link_in_telegram)

    def stop_local_proxy(self) -> None:
        if self._is_ui_busy():
            return
        try:
            self.runtime.stop_local_server()
        except Exception as exc:
            messagebox.showerror("Остановка не выполнена", str(exc))
            return
        self._refresh_snapshot()

    def start_refresh(self) -> None:
        if self.refresh_in_progress:
            self.cancel_refresh()
            return
        if self.runtime_call_count > 0:
            return
        self.refresh_cancel_event.clear()
        self.refresh_in_progress = True
        self._reset_refresh_progress()
        self._refresh_snapshot()

        def worker() -> None:
            try:
                self.runtime.run_refresh(cancel_event=self.refresh_cancel_event)
            except Exception as exc:
                self.message_queue.put(("refresh_error", str(exc), traceback.format_exc()))
            finally:
                self.message_queue.put(("refresh_done",))

        self.refresh_thread = threading.Thread(target=worker, daemon=True, name="mtproxy-refresh")
        self.refresh_thread.start()

    def cancel_refresh(self) -> None:
        if not self.refresh_in_progress or self.refresh_cancel_event.is_set():
            return
        self.refresh_cancel_event.set()
        self._set_refresh_progress(self.refresh_fraction or 0.01, "Отмена обновления...")
        self._refresh_snapshot()

    def _on_primary_action(self) -> None:
        if self._is_ui_busy():
            return
        if bool(self.snapshot_cache.get("local_running")):
            self.stop_local_proxy()
        else:
            self.start_local_proxy()

    def copy_local_link(self) -> None:
        local_url = str(self.snapshot_cache.get("local_url", "")).strip()
        if not local_url:
            self.progress_text_var.set("Локальная ссылка пока недоступна")
            return
        self.clipboard_clear()
        self.clipboard_append(local_url)
        self.progress_text_var.set("Ссылка скопирована")

    def open_local_link_in_telegram(self) -> None:
        tg_url = str(self.snapshot_cache.get("local_tg_url", "")).strip()
        web_url = str(self.snapshot_cache.get("local_url", "")).strip()
        if not tg_url and not web_url:
            return
        with contextlib.suppress(Exception):
            if tg_url and webbrowser.open(tg_url):
                return
        with contextlib.suppress(Exception):
            if web_url:
                webbrowser.open(web_url)

    def open_settings(self) -> None:
        if self.refresh_in_progress:
            return
        for child in self.winfo_children():
            if isinstance(child, SettingsDialog):
                try:
                    if child.winfo_exists():
                        self.settings_dialog = child
                        state = str(child.state())
                        if state in {"withdrawn", "iconic"}:
                            child.deiconify()
                        child.lift()
                        child.focus_force()
                        return
                except tk.TclError:
                    pass
        if self.settings_dialog is not None:
            try:
                if self.settings_dialog.winfo_exists():
                    state = str(self.settings_dialog.state())
                    if state in {"withdrawn", "iconic"}:
                        self.settings_dialog.deiconify()
                    self.settings_dialog.lift()
                    self.settings_dialog.focus_force()
                    return
            except tk.TclError:
                self.settings_dialog = None
            self.settings_dialog = None
        # FIX: создаём диалог и больше не вызываем deiconify/lift/focus_force здесь,
        # CTkToplevel сам показывается — дополнительный вызов создавал мигание.
        self.settings_dialog = SettingsDialog(self)

    def apply_config(self, config: AppConfig) -> bool:
        previous_appearance = self.runtime.config.appearance
        changed = self.runtime.apply_config(config)
        if not changed:
            self._refresh_snapshot()
            return False
        set_autostart_enabled(config.autostart_enabled)
        self._apply_appearance()
        self._configure_ttk_style()
        self.configure(fg_color=COLOR_BG)
        if previous_appearance != config.appearance and self.settings_dialog is not None:
            with contextlib.suppress(Exception):
                dialog = self.settings_dialog
                self.settings_dialog = None
                dialog.destroy()
        self._refresh_snapshot()
        return True

    def run_runtime_call(
        self,
        func,
        *,
        on_success=None,
        on_error=None,
        block_ui: bool = True,
    ) -> None:
        if block_ui:
            self.runtime_call_count += 1
            self._refresh_snapshot()

        def ui_call(callback) -> None:
            try:
                if self.winfo_exists():
                    self.after(0, callback)
            except Exception:
                pass

        def worker() -> None:
            try:
                result = func()
            except Exception as exc:
                if block_ui:
                    ui_call(self._runtime_call_finished)
                if on_error is None:
                    ui_call(lambda exc=exc: messagebox.showerror("Ошибка", str(exc)))
                    return
                ui_call(lambda exc=exc: on_error(exc))
                return
            if block_ui:
                ui_call(self._runtime_call_finished)
            if on_success is not None:
                ui_call(lambda result=result: on_success(result))

        threading.Thread(target=worker, daemon=True).start()

    def _runtime_call_finished(self) -> None:
        self.runtime_call_count = max(0, self.runtime_call_count - 1)
        self._refresh_snapshot()

    def refresh_auth_status(self, callback=None, *, block_ui: bool = False) -> None:
        def on_success(result: dict[str, object]) -> None:
            self.auth_status = dict(result)
            if callback is not None:
                callback(result)
            self._refresh_snapshot()

        def on_error(exc: Exception) -> None:
            session_path = (self.runtime.root_dir / self.runtime.config.telegram_session_file).resolve()
            self.auth_status = {
                "authorized": False,
                "display": "",
                "phone": "",
                "session_exists": session_path.exists(),
                "error": str(exc),
            }
            self._append_log(f"[auth] status check failed: {exc}")
            if callback is not None:
                callback(self.auth_status)
            self._refresh_snapshot()

        self.run_runtime_call(
            self.runtime.run_auth_status,
            on_success=on_success,
            on_error=on_error,
            block_ui=block_ui,
        )

    def request_auth_code(self, phone: str, callback=None) -> None:
        def on_success(result: dict[str, object]) -> None:
            if callback is not None:
                callback(result)

        self.run_runtime_call(lambda: self.runtime.request_auth_code(phone), on_success=on_success)

    def complete_auth(self, phone: str, code: str, password: str, callback=None) -> None:
        def on_success(result: dict[str, object]) -> None:
            if result.get("authorized"):
                self.auth_status = dict(result)
            if callback is not None:
                callback(result)
            self._refresh_snapshot()

        self.run_runtime_call(
            lambda: self.runtime.complete_auth(phone, code, password),
            on_success=on_success,
        )

    def logout_auth(self, callback=None) -> None:
        def on_success(_: object) -> None:
            self.auth_status = {
                "authorized": False,
                "display": "",
                "phone": "",
                "session_exists": False,
            }
            if callback is not None:
                callback()
            self._refresh_snapshot()

        self.run_runtime_call(self.runtime.logout_auth, on_success=on_success)

    def start_qr_auth(self, *, password: str = "", callback=None) -> None:
        if self.refresh_in_progress or self.runtime_call_count > 0:
            return

        def on_success(result: dict[str, object]) -> None:
            if result.get("authorized"):
                self.auth_status = dict(result)
                if self.qr_dialog is not None and self.qr_dialog.winfo_exists():
                    self.qr_dialog.close()
            if callback is not None:
                callback(result)
            self._refresh_snapshot()

        self.run_runtime_call(
            lambda: self.runtime.run_qr_login(password=password),
            on_success=on_success,
        )

    def send_proxy_list_to_saved_messages(self, callback=None) -> None:
        def on_success(result: dict[str, object]) -> None:
            if callback is not None:
                callback(result)
            self._refresh_snapshot()

        self.run_runtime_call(self.runtime.send_working_proxies_to_saved_messages, on_success=on_success)

    def _show_qr_dialog(self, payload: dict[str, object]) -> None:
        url = str(payload.get("url", "") or "").strip()
        if not url:
            return
        if self.qr_dialog is not None and self.qr_dialog.winfo_exists():
            self.qr_dialog.update_qr(url, str(payload.get("expires_at", "") or ""))
            self.qr_dialog.focus()
            self.qr_dialog.lift()
            return
        parent = self.settings_dialog if self.settings_dialog is not None and self.settings_dialog.winfo_exists() else self
        self.qr_dialog = QRAuthDialog(
            parent,
            app=self,
            url=url,
            expires_at=str(payload.get("expires_at", "") or ""),
            on_refresh=lambda password="": self.start_qr_auth(password=password),
        )

    def open_output_folder(self) -> None:
        path = (self.runtime.root_dir / self.runtime.config.out_dir).resolve()
        path.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])

    def _on_window_resize(self, _event=None) -> None:
        wrap = max(250, self.winfo_width() - 90)
        hint_wrap = max(240, wrap - 20)
        if hasattr(self, "primary_hint_label"):
            self.primary_hint_label.configure(wraplength=hint_wrap)
        if hasattr(self, "link_preview_label"):
            self.link_preview_label.configure(wraplength=hint_wrap)
        if hasattr(self, "active_proxy_label"):
            self.active_proxy_label.configure(wraplength=wrap)
        if hasattr(self, "progress_thread_label"):
            self.progress_thread_label.configure(wraplength=hint_wrap)
        if hasattr(self, "active_card"):
            self.active_card.configure(height=200)

    def _refresh_main_layout(self) -> None:
        if hasattr(self, "hero_actions"):
            self.hero_actions.grid_columnconfigure(0, weight=1)
            self.hero_actions.grid_columnconfigure(1, weight=1)
            self.refresh_button.grid_forget()
            self.open_output_button.grid_forget()
            self.refresh_button.grid(row=0, column=0, sticky="ew", padx=(0, 6), pady=0)
            self.open_output_button.grid(row=0, column=1, sticky="ew", padx=(6, 0), pady=0)

        if hasattr(self, "summary_cards"):
            self.summary_frame.grid_columnconfigure(0, weight=1)
            self.summary_frame.grid_columnconfigure(1, weight=1)
            self.summary_frame.grid_columnconfigure(2, weight=1)
            for index, card in enumerate(self.summary_cards):
                card.grid_forget()
                card.grid(row=0, column=index, padx=4, pady=0, sticky="nsew")

    def _build_tray_image(self) -> Image.Image:
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((4, 4, 60, 60), radius=16, fill="#2563EB")
        draw.rounded_rectangle((18, 34, 26, 50), radius=3, fill="#FFFFFF")
        draw.rounded_rectangle((30, 26, 38, 50), radius=3, fill="#FFFFFF")
        draw.rounded_rectangle((42, 18, 50, 50), radius=3, fill="#FFFFFF")
        return image

    def _build_tray_menu(self) -> pystray.Menu:
        running = bool(self.snapshot_cache.get("local_running"))
        ui_busy = self._is_ui_busy()
        items = [
            pystray.MenuItem("Открыть", lambda icon, item: self.after(0, self._show_from_tray), default=True),
            pystray.MenuItem("Скопировать ссылку", lambda icon, item: self.after(0, self.copy_local_link)),
        ]
        if not ui_busy and not running:
            items.append(pystray.MenuItem("Запустить", lambda icon, item: self.after(0, self.start_local_proxy)))
        if not ui_busy and running:
            items.append(pystray.MenuItem("Остановить", lambda icon, item: self.after(0, self.stop_local_proxy)))
        if self.refresh_in_progress:
            items.append(pystray.MenuItem("Отменить обновление", lambda icon, item: self.after(0, self.cancel_refresh)))
        elif not ui_busy:
            items.append(pystray.MenuItem("Обновить", lambda icon, item: self.after(0, self.start_refresh)))
        items.append(pystray.MenuItem("Выход", lambda icon, item: self.after(0, lambda: self._quit_application(force=True))))
        return pystray.Menu(*items)

    def _refresh_tray_menu(self) -> None:
        with self._tray_lock:
            if self._tray_icon is None:
                return
            with contextlib.suppress(Exception):
                self._tray_icon.menu = self._build_tray_menu()
                self._tray_icon.update_menu()

    def _ensure_tray_icon(self) -> None:
        with self._tray_lock:
            if self._tray_stopping:
                return
            if self._tray_icon is None:
                self._tray_icon = pystray.Icon(APP_NAME, self._build_tray_image(), APP_NAME, self._build_tray_menu())
            else:
                with contextlib.suppress(Exception):
                    self._tray_icon.menu = self._build_tray_menu()
            if not self._tray_started:
                self._tray_icon.run_detached()
                self._tray_started = True
            with contextlib.suppress(Exception):
                self._tray_icon.visible = True

    def _stop_tray_icon(self) -> None:
        with self._tray_lock:
            if self._tray_icon is None:
                return
            icon = self._tray_icon
            self._tray_stopping = True
        with contextlib.suppress(Exception):
            icon.stop()
        with self._tray_lock:
            if self._tray_icon is icon:
                self._tray_icon = None
            self._tray_stopping = False
            self._tray_started = False

    def _hide_to_tray(self, notify: bool = True) -> None:
        if self._hidden_to_tray:
            return
        self._ensure_tray_icon()
        self._hidden_to_tray = True
        self.withdraw()
        if notify:
            self.copy_hint_var.set("Приложение скрыто в трей")

    def _show_from_tray(self) -> None:
        if not self._hidden_to_tray:
            return
        self._hidden_to_tray = False
        self.deiconify()
        self.after(50, self.lift)
        self.after(100, self.focus_force)
        with self._tray_lock:
            if self._tray_icon is not None:
                with contextlib.suppress(Exception):
                    self._tray_icon.visible = False

    def _on_close_requested(self) -> None:
        if self._quitting:
            return
        behavior = self.runtime.config.close_behavior
        if behavior == "tray":
            self._hide_to_tray()
            return
        if behavior == "exit":
            self._quit_application(force=True)
            return

        dialog = CloseActionDialog(self)
        choice, remember = dialog.show()
        if not choice:
            return
        if remember:
            config = AppConfig(**asdict(self.runtime.config))
            config.close_behavior = choice
            self.apply_config(config)
        if choice == "tray":
            self._hide_to_tray()
        else:
            self._quit_application(force=True)

    def _quit_application(self, force: bool = False) -> None:
        if self._quitting:
            return
        self._quitting = True
        self._stop_tray_icon()
        with contextlib.suppress(Exception):
            self.runtime.shutdown()
        self.destroy()


class SettingsDialog(ctk.CTkToplevel):
    def __init__(self, app: MTProxyAutoSwitchApp) -> None:
        super().__init__(app)
        self.app = app
        self._resize_job: str | None = None   # FIX: для дебаунса resize
        self._dirty_job: str | None = None
        self._dirty = False
        self._refreshing_controls = False
        self._baseline_payload: dict[str, object] | None = None
        self._last_pool_signature: tuple[object, ...] | None = None
        self._last_logs_signature: tuple[object, ...] | None = None
        self._tab_poll_job: str | None = None
        self._active_tab_name = ""
        self.title("Настройки")
        with contextlib.suppress(Exception):
            if APP_ICON_PATH.exists():
                self.iconbitmap(str(APP_ICON_PATH))
        _set_fixed_window_size(self, 960, 760)
        self.configure(fg_color=COLOR_BG)
        self.transient(app)
        with contextlib.suppress(Exception):
            self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Destroy>", self._on_destroy, add="+")
        self.bind("<Configure>", self._on_resize, add="+")

        self._create_variables()
        self._build_layout()
        self._setup_dirty_tracking()
        self.after(0, self._show_window_ready)
        self.after_idle(lambda: self.refresh_from_runtime(force_config=True))
        self.after(50, self._refresh_general_layout)
        self._schedule_tab_poll()

    # FIX: дебаунс resize — обновляем layout не чаще чем раз в 80ms
    def _on_resize(self, event=None) -> None:
        if event is None or event.widget is self:
            if self._resize_job is not None:
                with contextlib.suppress(Exception):
                    self.after_cancel(self._resize_job)
            self._resize_job = self.after(80, self._do_deferred_resize)

    def _show_window_ready(self) -> None:
        """Показать окно сразу, а тяжёлые данные догрузить после первого кадра."""
        with contextlib.suppress(Exception):
            self.lift()
            self.focus_force()

    def _bind_dialog_clipboard(self) -> None:
        return

    def _do_deferred_resize(self) -> None:
        self._resize_job = None
        self._refresh_general_layout()
        self._refresh_wraplengths()

    def _setup_dirty_tracking(self) -> None:
        tracked_vars = [
            self.autostart_var,
            self.start_minimized_var,
            self.auto_start_local_var,
            self.appearance_var,
            self.close_behavior_var,
            self.telegram_sources_enabled_var,
            self.deep_media_enabled_var,
            self.rf_whitelist_check_var,
            self.local_host_var,
            self.local_port_var,
            self.local_secret_var,
            self.telegram_api_id_var,
            self.telegram_api_hash_var,
            self.duration_var,
            self.interval_var,
            self.timeout_var,
            self.workers_var,
            self.fetch_timeout_var,
            self.max_latency_var,
            self.min_success_var,
            self.high_ratio_var,
            self.high_streak_var,
            self.max_proxies_var,
            self.live_probe_interval_var,
            self.live_probe_duration_var,
            self.live_probe_top_n_var,
            self.deep_media_top_n_var,
            self.auto_update_var,
            *self.source_toggle_vars.values(),
            *self.telegram_source_toggle_vars.values(),
        ]
        for variable in tracked_vars:
            with contextlib.suppress(Exception):
                variable.trace_add("write", lambda *_args: self._schedule_dirty_check())

    def _schedule_dirty_check(self, _event=None) -> None:
        if self._refreshing_controls:
            return
        if self._dirty_job is not None:
            with contextlib.suppress(Exception):
                self.after_cancel(self._dirty_job)
        self._dirty_job = self.after(60, self._update_dirty_state)

    def _update_dirty_state(self) -> None:
        self._dirty_job = None
        draft = self._collect_settings_payload(validate=False)
        self._dirty = self._baseline_payload is not None and draft != self._baseline_payload
        self.refresh_interaction_state()

    def _set_baseline_payload(self, payload: dict[str, object]) -> None:
        self._baseline_payload = dict(payload)
        self._dirty = False
        self.refresh_interaction_state()

    def _schedule_tab_poll(self) -> None:
        if self._tab_poll_job is not None:
            with contextlib.suppress(Exception):
                self.after_cancel(self._tab_poll_job)
        self._tab_poll_job = self.after(180, self._poll_active_tab)

    def _poll_active_tab(self) -> None:
        self._tab_poll_job = None
        current_tab = ""
        with contextlib.suppress(Exception):
            current_tab = str(self.tabs.get() or "")
        if current_tab and current_tab != self._active_tab_name:
            self._active_tab_name = current_tab
            self._handle_active_tab_changed(current_tab)
        if self.winfo_exists():
            self._schedule_tab_poll()

    def _handle_active_tab_changed(self, tab_name: str) -> None:
        about_tab_name = getattr(self, "_about_tab_name", "")
        if not about_tab_name:
            return
        if tab_name == about_tab_name:
            self._ensure_about_video_preview()
        else:
            self._release_about_video_preview()

    def _ensure_about_video_preview(self) -> None:
        if not hasattr(self, "about_video_wrap") or self.about_video_preview is not None:
            return
        if hasattr(self, "about_video_placeholder") and self.about_video_placeholder is not None:
            with contextlib.suppress(Exception):
                self.about_video_placeholder.destroy()
            self.about_video_placeholder = None
        self.about_video_preview = LoopingVideoPreview(
            self.about_video_wrap,
            video_path=ABOUT_VIDEO_PATH,
            width=620,
            height=348,
        )
        self.about_video_preview.pack(fill="x", padx=0, pady=0)

    def _release_about_video_preview(self) -> None:
        video_widget = getattr(self, "about_video_preview", None)
        if video_widget is not None:
            with contextlib.suppress(Exception):
                video_widget.destroy()
            self.about_video_preview = None
        if hasattr(self, "about_video_wrap") and getattr(self, "about_video_placeholder", None) is None:
            self.about_video_placeholder = ctk.CTkLabel(
                self.about_video_wrap,
                text="Откройте вкладку «О приложении», чтобы загрузить preview.",
                text_color=COLOR_TEXT_SOFT,
                font=("Segoe UI", 11),
                justify="center",
                wraplength=560,
            )
            self.about_video_placeholder.pack(fill="x", padx=16, pady=22)

    def _create_variables(self) -> None:
        config = self.app.runtime.config
        self.autostart_var = tk.BooleanVar(value=config.autostart_enabled)
        self.start_minimized_var = tk.BooleanVar(value=config.start_minimized_to_tray)
        self.auto_start_local_var = tk.BooleanVar(value=config.auto_start_local)
        self.appearance_var = tk.StringVar(value=_appearance_label(config.appearance))
        self.close_behavior_var = tk.StringVar(value=CLOSE_LABELS.get(config.close_behavior, CLOSE_LABELS["ask"]))
        self.telegram_sources_enabled_var = tk.BooleanVar(
            value=getattr(config, "telegram_sources_enabled", config.thread_source_enabled)
        )
        self.deep_media_enabled_var = tk.BooleanVar(value=config.deep_media_enabled)
        self.rf_whitelist_check_var = tk.BooleanVar(value=getattr(config, "rf_whitelist_check_enabled", False))
        self.show_advanced_probe_var = tk.BooleanVar(value=False)

        self.local_host_var = tk.StringVar(value=config.local_host)
        self.local_port_var = tk.StringVar(value=str(config.local_port))
        self.local_secret_var = tk.StringVar(value=config.local_secret)
        self.phone_var = tk.StringVar(value="")
        self.code_var = tk.StringVar(value="")
        self.password_var = tk.StringVar(value="")
        self.telegram_api_id_var = tk.StringVar(value=str(config.telegram_api_id or ""))
        self.telegram_api_hash_var = tk.StringVar(value=config.telegram_api_hash)
        self.password_visible_var = tk.BooleanVar(value=False)
        self.duration_var = tk.StringVar(value=str(config.duration))
        self.interval_var = tk.StringVar(value=str(config.interval))
        self.timeout_var = tk.StringVar(value=str(config.timeout))
        self.workers_var = tk.StringVar(value=str(config.workers))
        self.fetch_timeout_var = tk.StringVar(value=str(config.fetch_timeout))
        self.max_latency_var = tk.StringVar(value=str(config.max_latency_ms))
        self.min_success_var = tk.StringVar(value=str(config.min_success_rate))
        self.high_ratio_var = tk.StringVar(value=str(config.max_high_latency_ratio))
        self.high_streak_var = tk.StringVar(value=str(config.high_latency_streak))
        self.max_proxies_var = tk.StringVar(value=str(config.max_proxies))
        self.live_probe_interval_var = tk.StringVar(value=str(config.live_probe_interval_sec))
        self.live_probe_duration_var = tk.StringVar(value=str(config.live_probe_duration_sec))
        self.live_probe_top_n_var = tk.StringVar(value=str(config.live_probe_top_n))
        self.deep_media_top_n_var = tk.StringVar(value=str(config.deep_media_top_n))
        self.auto_update_var = tk.BooleanVar(value=getattr(config, "auto_update_enabled", True))
        active_sources = set(config.sources)
        self.source_toggle_vars: dict[str, tk.BooleanVar] = {
            source: tk.BooleanVar(value=source in active_sources)
            for source in DEFAULT_SOURCES
        }
        active_telegram_sources = set(getattr(config, "telegram_sources", []) or ([config.thread_source_url] if config.thread_source_url else []))
        self.telegram_source_toggle_vars: dict[str, tk.BooleanVar] = {
            source: tk.BooleanVar(value=source in active_telegram_sources)
            for source in DEFAULT_TELEGRAM_SOURCE_URLS
        }

    def _build_layout(self) -> None:
        wrapper = ctk.CTkFrame(self, fg_color="transparent")
        wrapper.pack(fill="both", expand=True, padx=18, pady=18)

        ctk.CTkLabel(
            wrapper,
            text="Настройки",
            text_color=COLOR_TEXT,
            font=("Segoe UI Semibold", 26),
        ).pack(anchor="w")
        ctk.CTkLabel(
            wrapper,
            text="Параметры приложения",
            text_color=COLOR_TEXT_SOFT,
            font=("Segoe UI", 12),
        ).pack(anchor="w", pady=(4, 12))

        self.tabs = ctk.CTkTabview(
            wrapper,
            fg_color=COLOR_CARD,
            segmented_button_fg_color=COLOR_ACCENT_SOFT,
            segmented_button_selected_color=COLOR_ACCENT,
            segmented_button_selected_hover_color=COLOR_ACCENT_HOVER,
            segmented_button_unselected_hover_color=COLOR_ACCENT_SOFT_HOVER,
            text_color=COLOR_TEXT,
        )
        self.tabs.pack(fill="both", expand=True)
        general = self.tabs.add("Общие")
        telegram = self.tabs.add("Telegram")
        sources = self.tabs.add("Параметры проверки")
        pool = self.tabs.add("Пул")
        logs = self.tabs.add("Логи")
        authors = self.tabs.add("О приложении")
        self._about_tab_name = "О приложении"

        self._build_general_tab(general)
        self._build_telegram_tab(telegram)
        self._build_sources_tab(sources)
        self._build_pool_tab(pool)
        self._build_logs_tab(logs)
        self._build_authors_tab(authors)

        footer = ctk.CTkFrame(wrapper, fg_color="transparent")
        footer.pack(fill="x", pady=(16, 4))
        footer.grid_columnconfigure((0, 1), weight=1, uniform="settings_footer")
        self.footer_open_button = ctk.CTkButton(
            footer,
            text="Открыть папку list",
            height=44,
            corner_radius=22,
            fg_color=COLOR_CARD,
            hover_color=COLOR_ACCENT_SOFT,
            border_width=1,
            border_color=COLOR_FIELD_BORDER,
            text_color=COLOR_TEXT,
            font=("Segoe UI Semibold", 12),
            command=self.app.open_output_folder,
        )
        self.footer_open_button.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.save_button = ctk.CTkButton(
            footer,
            text="Сохранить",
            height=44,
            corner_radius=22,
            fg_color=COLOR_ACCENT,
            hover_color=COLOR_ACCENT_HOVER,
            text_color="#FFFFFF",
            font=("Segoe UI Semibold", 12),
            command=self._save_settings,
        )
        self.save_button.grid(row=0, column=1, sticky="ew", padx=(6, 0))

    def _build_general_tab(self, tab: ctk.CTkFrame) -> None:
        outer = ctk.CTkFrame(tab, fg_color="transparent")
        outer.pack(fill="both", expand=True, padx=8, pady=12)
        container = ctk.CTkFrame(outer, fg_color="transparent")
        container.pack(fill="both", expand=True)
        container.grid_columnconfigure((0, 1), weight=1, uniform="settings_general")
        self.general_container = container

        left = ctk.CTkFrame(container, corner_radius=22, fg_color=COLOR_CARD, border_width=1, border_color=COLOR_BORDER)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        right = ctk.CTkFrame(container, corner_radius=22, fg_color=COLOR_CARD, border_width=1, border_color=COLOR_BORDER)
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        self.general_left_card = left
        self.general_right_card = right

        ctk.CTkLabel(left, text="Поведение приложения", font=("Segoe UI Semibold", 16), text_color=COLOR_TEXT).pack(anchor="w", padx=18, pady=(16, 10))
        row = ctk.CTkFrame(left, fg_color="transparent")
        row.pack(anchor="w", padx=18, pady=6)
        autostart_checkbox = ctk.CTkCheckBox(row, text=AUTOSTART_LABEL, variable=self.autostart_var)
        autostart_checkbox.pack(side="left")
        if not AUTOSTART_SUPPORTED:
            autostart_checkbox.configure(state="disabled")
        _add_help_badge(row, GENERAL_SETTING_TIPS["autostart"])
        row = ctk.CTkFrame(left, fg_color="transparent")
        row.pack(anchor="w", padx=18, pady=6)
        ctk.CTkCheckBox(row, text="Стартовать свернутым в трей", variable=self.start_minimized_var).pack(side="left")
        _add_help_badge(row, GENERAL_SETTING_TIPS["start_minimized"])
        row = ctk.CTkFrame(left, fg_color="transparent")
        row.pack(anchor="w", padx=18, pady=6)
        ctk.CTkCheckBox(row, text="Автостарт локального proxy frontend", variable=self.auto_start_local_var).pack(side="left")
        _add_help_badge(row, GENERAL_SETTING_TIPS["auto_start_local"])
        ctk.CTkLabel(left, text="Тема", text_color=COLOR_TEXT_SOFT, font=("Segoe UI", 11)).pack(anchor="w", padx=18, pady=(16, 4))
        ctk.CTkOptionMenu(
            left,
            values=list(APPEARANCE_LABELS.values()),
            variable=self.appearance_var,
            width=220,
            height=36,
            corner_radius=18,
            fg_color=COLOR_FIELD,
            button_color=COLOR_FIELD_BORDER,
            button_hover_color=COLOR_ACCENT_SOFT_HOVER,
            dropdown_fg_color=COLOR_CARD,
            dropdown_hover_color=COLOR_ACCENT_SOFT,
            text_color=COLOR_TEXT,
            dropdown_text_color=COLOR_TEXT,
        ).pack(anchor="w", padx=18, pady=(0, 12))
        ctk.CTkLabel(left, text="При закрытии окна", text_color=COLOR_TEXT_SOFT, font=("Segoe UI", 11)).pack(anchor="w", padx=18, pady=(4, 4))
        ctk.CTkOptionMenu(
            left,
            values=list(CLOSE_LABELS.values()),
            variable=self.close_behavior_var,
            width=220,
            height=36,
            corner_radius=18,
            fg_color=COLOR_FIELD,
            button_color=COLOR_FIELD_BORDER,
            button_hover_color=COLOR_ACCENT_SOFT_HOVER,
            dropdown_fg_color=COLOR_CARD,
            dropdown_hover_color=COLOR_ACCENT_SOFT,
            text_color=COLOR_TEXT,
            dropdown_text_color=COLOR_TEXT,
        ).pack(anchor="w", padx=18, pady=(0, 18))
        ctk.CTkLabel(left, text="Обновления", text_color=COLOR_TEXT_SOFT, font=("Segoe UI", 11)).pack(anchor="w", padx=18, pady=(4, 4))
        row = ctk.CTkFrame(left, fg_color="transparent")
        row.pack(anchor="w", padx=18, pady=6)
        ctk.CTkCheckBox(row, text="Проверять обновления при запуске", variable=self.auto_update_var).pack(side="left")
        _add_help_badge(row, GENERAL_SETTING_TIPS["auto_update"])
        self.update_status_label = ctk.CTkLabel(
            left,
            textvariable=self.app.update_status_var,
            text_color=COLOR_TEXT_SOFT,
            font=("Segoe UI", 11),
            justify="left",
            wraplength=300,
        )
        self.update_status_label.pack(anchor="w", padx=18, pady=(4, 8))
        update_actions = ctk.CTkFrame(left, fg_color="transparent")
        update_actions.pack(fill="x", padx=18, pady=(0, 18))
        update_actions.grid_columnconfigure((0, 1), weight=1)
        self.check_updates_button = ctk.CTkButton(
            update_actions,
            text="Проверить",
            height=34,
            corner_radius=17,
            fg_color=COLOR_ACCENT_SOFT,
            hover_color=COLOR_ACCENT_SOFT_HOVER,
            text_color=COLOR_ACCENT,
            command=lambda: self.app.check_for_updates(manual=True),
        )
        self.check_updates_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.install_update_button = ctk.CTkButton(
            update_actions,
            text="Установить",
            height=34,
            corner_radius=17,
            fg_color=COLOR_ACCENT,
            hover_color=COLOR_ACCENT_HOVER,
            text_color="#FFFFFF",
            command=self.app.install_update,
        )
        self.install_update_button.grid(row=0, column=1, sticky="ew", padx=(4, 0))
        ctk.CTkLabel(right, text="Локальный frontend", font=("Segoe UI Semibold", 16), text_color=COLOR_TEXT).pack(anchor="w", padx=18, pady=(16, 10))
        self._entry_row(right, "Host", self.local_host_var)
        self._entry_row(right, "Port", self.local_port_var)
        self._entry_row(right, "Secret", self.local_secret_var)
        ctk.CTkLabel(
            right,
            text="Локальная ссылка формируется из этих параметров и используется клиентом Telegram для подключения к встроенному frontend.",
            text_color=COLOR_TEXT_SOFT,
            font=("Segoe UI", 11),
            justify="left",
            wraplength=320,
        ).pack(anchor="w", padx=18, pady=(2, 10))
        self.regenerate_secret_button = ctk.CTkButton(
            right,
            text="Сгенерировать новый secret",
            width=220,
            height=36,
            corner_radius=18,
            fg_color=COLOR_ACCENT_SOFT,
            hover_color=COLOR_ACCENT_SOFT_HOVER,
            text_color=COLOR_ACCENT,
            command=self._regenerate_secret,
        )
        self.regenerate_secret_button.pack(anchor="w", padx=18, pady=(6, 18))

        ctk.CTkLabel(right, text="Telegram Web", font=("Segoe UI Semibold", 16), text_color=COLOR_TEXT).pack(anchor="w", padx=18, pady=(8, 10))
        ctk.CTkLabel(
            right,
            text="Опционально: hosts-правила помогают нормальной работе Telegram в браузере. Для записи в hosts нужны права администратора.",
            text_color=COLOR_TEXT_SOFT,
            font=("Segoe UI", 11),
            justify="left",
            wraplength=320,
        ).pack(anchor="w", padx=18, pady=(0, 8))
        hosts_actions = ctk.CTkFrame(right, fg_color="transparent")
        hosts_actions.pack(fill="x", padx=18, pady=(0, 18))
        hosts_actions.grid_columnconfigure((0, 1, 2), weight=1)
        self.copy_hosts_button = ctk.CTkButton(
            hosts_actions,
            text="Копировать",
            height=34,
            corner_radius=17,
            fg_color=COLOR_ACCENT_SOFT,
            hover_color=COLOR_ACCENT_SOFT_HOVER,
            text_color=COLOR_ACCENT,
            command=self._copy_hosts_block,
        )
        self.copy_hosts_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.apply_hosts_button = ctk.CTkButton(
            hosts_actions,
            text="Применить",
            height=34,
            corner_radius=17,
            fg_color=COLOR_ACCENT_SOFT,
            hover_color=COLOR_ACCENT_SOFT_HOVER,
            text_color=COLOR_ACCENT,
            command=self._apply_hosts_block,
        )
        self.apply_hosts_button.grid(row=0, column=1, sticky="ew", padx=4)
        self.remove_hosts_button = ctk.CTkButton(
            hosts_actions,
            text="Удалить",
            height=34,
            corner_radius=17,
            fg_color=COLOR_ACCENT_SOFT,
            hover_color=COLOR_ACCENT_SOFT_HOVER,
            text_color=COLOR_ACCENT,
            command=self._remove_hosts_block,
        )
        self.remove_hosts_button.grid(row=0, column=2, sticky="ew", padx=(4, 0))

    def _refresh_wraplengths(self) -> None:
        try:
            w = self.winfo_width()
        except Exception:
            return
        wrap = min(700, max(320, w - 260))
        labels_to_update = [
            getattr(self, attr, None)
            for attr in (
                "thread_requirement_label",
                "thread_status_label",
                "deep_media_requirement_label",
                "auth_status_label",
                "auth_storage_label",
                "update_status_label",
            )
        ]
        for label in labels_to_update:
            if label is not None:
                with contextlib.suppress(Exception):
                    label.configure(wraplength=wrap)

    def _refresh_general_layout(self) -> None:
        if not hasattr(self, "general_container"):
            return
        self.general_container.grid_columnconfigure(0, weight=1, uniform="settings_general")
        self.general_container.grid_columnconfigure(1, weight=1, uniform="settings_general")
        self.general_left_card.grid_forget()
        self.general_right_card.grid_forget()
        self.general_left_card.grid(row=0, column=0, sticky="nsew", padx=(0, 8), pady=0)
        self.general_right_card.grid(row=0, column=1, sticky="nsew", padx=(8, 0), pady=0)

    def _build_telegram_tab(self, tab: ctk.CTkFrame) -> None:
        outer = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        outer.pack(fill="both", expand=True, padx=8, pady=12)

        source_card = ctk.CTkFrame(outer, corner_radius=22, fg_color=COLOR_CARD, border_width=1, border_color=COLOR_BORDER)
        source_card.pack(fill="x", pady=(0, 12))
        ctk.CTkLabel(source_card, text="Источники из Telegram", font=("Segoe UI Semibold", 16), text_color=COLOR_TEXT).pack(anchor="w", padx=18, pady=(16, 10))
        source_toggle_row = ctk.CTkFrame(source_card, fg_color="transparent")
        source_toggle_row.pack(anchor="w", padx=18, pady=(0, 8))
        ctk.CTkCheckBox(
            source_toggle_row,
            text="Использовать авторизованные Telegram-источники",
            variable=self.telegram_sources_enabled_var,
            command=self._handle_thread_source_toggle,
        ).pack(side="left")
        _add_help_badge(source_toggle_row, GENERAL_SETTING_TIPS["telegram_sources"])
        ctk.CTkLabel(
            source_card,
            text="Поддерживаются каналы, группы, сообщения и ветки в формате t.me/.... Для приватных источников нужна авторизованная сессия и доступ к чату.",
            font=("Segoe UI", 11),
            text_color=COLOR_TEXT_SOFT,
            justify="left",
            wraplength=680,
        ).pack(anchor="w", padx=18, pady=(0, 10))
        telegram_actions = ctk.CTkFrame(source_card, fg_color="transparent")
        telegram_actions.pack(fill="x", padx=18, pady=(0, 8))
        self.enable_all_telegram_sources_button = ctk.CTkButton(
            telegram_actions,
            text="Включить все Telegram-источники",
            height=34,
            corner_radius=17,
            fg_color=COLOR_ACCENT_SOFT,
            hover_color=COLOR_ACCENT_SOFT_HOVER,
            text_color=COLOR_ACCENT,
            command=self._set_all_telegram_sources_enabled,
        )
        self.enable_all_telegram_sources_button.pack(side="left")
        self.telegram_source_checkboxes = []
        for source in DEFAULT_TELEGRAM_SOURCE_URLS:
            checkbox = ctk.CTkCheckBox(
                source_card,
                text=source,
                variable=self.telegram_source_toggle_vars[source],
            )
            checkbox.pack(anchor="w", padx=18, pady=4)
            self.telegram_source_checkboxes.append(checkbox)
        ctk.CTkLabel(
            source_card,
            text="Свои Telegram-источники",
            font=("Segoe UI", 11),
            text_color=COLOR_TEXT_SOFT,
        ).pack(anchor="w", padx=18, pady=(12, 6))
        self.telegram_sources_box = ctk.CTkTextbox(
            source_card,
            height=96,
            corner_radius=18,
            fg_color=COLOR_FIELD,
            border_width=1,
            border_color=COLOR_FIELD_BORDER,
            text_color=COLOR_TEXT,
        )
        self.telegram_sources_box.pack(fill="x", padx=18, pady=(0, 10))
        _bind_clipboard_shortcuts(self.telegram_sources_box)
        self.telegram_sources_box.bind("<KeyRelease>", self._schedule_dirty_check, add="+")
        self.telegram_sources_box.bind("<<Paste>>", self._schedule_dirty_check, add="+")
        self.telegram_sources_box.bind("<<Cut>>", self._schedule_dirty_check, add="+")
        self.thread_requirement_label = ctk.CTkLabel(
            source_card,
            text="",
            text_color=COLOR_WARN_TEXT,
            font=("Segoe UI", 12),
            justify="left",
            wraplength=680,
        )
        self.thread_requirement_label.pack(anchor="w", padx=18, pady=(0, 8))
        self.thread_requirement_label.pack_forget()
        self.thread_status_label = ctk.CTkLabel(source_card, text="", text_color=COLOR_TEXT_SOFT, font=("Segoe UI", 12), justify="left", wraplength=680)
        self.thread_status_label.pack(anchor="w", padx=18, pady=(2, 18))

        auth_card = ctk.CTkFrame(outer, corner_radius=22, fg_color=COLOR_CARD, border_width=1, border_color=COLOR_BORDER)
        auth_card.pack(fill="x")
        ctk.CTkLabel(auth_card, text="Авторизация Telegram", font=("Segoe UI Semibold", 16), text_color=COLOR_TEXT).pack(anchor="w", padx=18, pady=(16, 10))
        ctk.CTkLabel(
            auth_card,
            text="Для Telegram API нужны ваши собственные API ID и API Hash.",
            text_color=COLOR_TEXT_SOFT,
            font=("Segoe UI", 11),
            justify="left",
            wraplength=700,
        ).pack(anchor="w", padx=18, pady=(0, 8))
        self._entry_row(auth_card, "API ID", self.telegram_api_id_var)
        self._entry_row(auth_card, "API Hash", self.telegram_api_hash_var)
        api_actions = ctk.CTkFrame(auth_card, fg_color="transparent")
        api_actions.pack(fill="x", padx=18, pady=(0, 10))
        ctk.CTkLabel(
            api_actions,
            text="API ID и API Hash получаются на my.telegram.org/apps",
            text_color=COLOR_TEXT_SOFT,
            font=("Segoe UI", 11),
        ).pack(side="left")
        self.open_telegram_apps_button = ctk.CTkButton(
            api_actions,
            text="Получить API ID / API Hash",
            width=230,
            height=36,
            corner_radius=18,
            fg_color=COLOR_ACCENT_SOFT,
            hover_color=COLOR_ACCENT_SOFT_HOVER,
            text_color=COLOR_ACCENT,
            command=self._open_telegram_apps_page,
        )
        self.open_telegram_apps_button.pack(side="right")
        self._entry_row(auth_card, "Телефон", self.phone_var)
        self._entry_row_with_button(auth_card, "Код", self.code_var, button_text="Запросить код", button_command=self._request_code)
        self._password_entry_row(auth_card, "Пароль 2FA", self.password_var)
        self.auth_status_label = ctk.CTkLabel(auth_card, text="", text_color=COLOR_TEXT_SOFT, font=("Segoe UI", 12), justify="left", wraplength=700)
        self.auth_status_label.pack(anchor="w", padx=18, pady=(4, 10))
        self.auth_storage_label = ctk.CTkLabel(
            auth_card,
            text="Сессия хранится в зашифрованном виде только на этом устройстве и никуда не отправляется, кроме прямого подключения к Telegram.",
            text_color=COLOR_TEXT_FAINT,
            font=("Segoe UI", 11),
            justify="left",
            wraplength=700,
        )
        self.auth_storage_label.pack(anchor="w", padx=18, pady=(0, 10))

        buttons = ctk.CTkFrame(auth_card, fg_color="transparent")
        buttons.pack(fill="x", padx=18, pady=(0, 18))
        buttons.grid_columnconfigure((0, 1), weight=1)
        self.auth_check_button = ctk.CTkButton(
            buttons,
            text="Проверить",
            height=38,
            corner_radius=19,
            fg_color=COLOR_ACCENT_SOFT,
            hover_color=COLOR_ACCENT_SOFT_HOVER,
            text_color=COLOR_ACCENT,
            command=self._check_auth_status,
        )
        self.auth_request_code_button = ctk.CTkButton(
            buttons,
            text="Запросить код",
            height=38,
            corner_radius=19,
            fg_color=COLOR_ACCENT_SOFT,
            hover_color=COLOR_ACCENT_SOFT_HOVER,
            text_color=COLOR_ACCENT,
            command=self._request_code,
        )
        self.auth_login_button = ctk.CTkButton(
            buttons,
            text="Войти",
            height=38,
            corner_radius=19,
            fg_color=COLOR_ACCENT,
            hover_color=COLOR_ACCENT_HOVER,
            text_color="#FFFFFF",
            command=self._complete_auth,
        )
        self.auth_qr_button = ctk.CTkButton(
            buttons,
            text="QR вход",
            height=38,
            corner_radius=19,
            fg_color=COLOR_ACCENT_SOFT,
            hover_color=COLOR_ACCENT_SOFT_HOVER,
            text_color=COLOR_ACCENT,
            command=self._start_qr_auth,
        )
        self.auth_logout_button = ctk.CTkButton(
            buttons,
            text="Выйти",
            height=38,
            corner_radius=19,
            fg_color=COLOR_DANGER_BG,
            hover_color=COLOR_DANGER_BORDER,
            text_color=COLOR_DANGER_TEXT,
            command=self._logout,
        )
        self.auth_send_list_button = ctk.CTkButton(
            buttons,
            text="Отправить прокси в Избранное",
            height=38,
            corner_radius=19,
            fg_color=COLOR_ACCENT,
            hover_color=COLOR_ACCENT_HOVER,
            text_color="#FFFFFF",
            command=self._send_proxy_list_to_saved_messages,
        )
        self.auth_buttons = [
            self.auth_check_button,
            self.auth_login_button,
            self.auth_qr_button,
            self.auth_logout_button,
            self.auth_send_list_button,
        ]

    def _build_sources_tab(self, tab: ctk.CTkFrame) -> None:
        outer = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        outer.pack(fill="both", expand=True, padx=8, pady=12)

        sources_card = ctk.CTkFrame(outer, corner_radius=22, fg_color=COLOR_CARD, border_width=1, border_color=COLOR_BORDER)
        sources_card.pack(fill="x", pady=(0, 12))
        ctk.CTkLabel(sources_card, text="Источники и параметры проверки", font=("Segoe UI Semibold", 16), text_color=COLOR_TEXT).pack(anchor="w", padx=18, pady=(16, 8))
        ctk.CTkLabel(
            sources_card,
            text="Можно включать готовые web-источники и добавлять свои URL. Telegram-источники с авторизацией настраиваются во вкладке Telegram.",
            font=("Segoe UI", 11),
            text_color=COLOR_TEXT_SOFT,
            justify="left",
            wraplength=700,
        ).pack(anchor="w", padx=18, pady=(0, 10))

        actions = ctk.CTkFrame(sources_card, fg_color="transparent")
        actions.pack(fill="x", padx=18, pady=(0, 10))
        self.enable_all_sources_button = ctk.CTkButton(
            actions,
            text="Включить все источники",
            height=34,
            corner_radius=17,
            fg_color=COLOR_ACCENT_SOFT,
            hover_color=COLOR_ACCENT_SOFT_HOVER,
            text_color=COLOR_ACCENT,
            command=self._set_all_sources_enabled,
        )
        self.enable_all_sources_button.pack(side="left")

        preset_sources = ctk.CTkFrame(sources_card, fg_color="transparent")
        preset_sources.pack(fill="x", padx=18, pady=(0, 8))
        for source in DEFAULT_SOURCES:
            ctk.CTkCheckBox(
                preset_sources,
                text=source,
                variable=self.source_toggle_vars[source],
            ).pack(anchor="w", pady=4)

        ctk.CTkLabel(
            sources_card,
            text="Свои веб-источники",
            font=("Segoe UI Semibold", 13),
            text_color=COLOR_TEXT,
        ).pack(anchor="w", padx=18, pady=(8, 6))
        self.custom_sources_box = ctk.CTkTextbox(
            sources_card,
            height=92,
            corner_radius=18,
            fg_color=COLOR_FIELD,
            border_width=1,
            border_color=COLOR_FIELD_BORDER,
            text_color=COLOR_TEXT,
        )
        self.custom_sources_box.pack(fill="x", padx=18, pady=(0, 18))
        _bind_clipboard_shortcuts(self.custom_sources_box)
        self.custom_sources_box.bind("<KeyRelease>", self._schedule_dirty_check, add="+")
        self.custom_sources_box.bind("<<Paste>>", self._schedule_dirty_check, add="+")
        self.custom_sources_box.bind("<<Cut>>", self._schedule_dirty_check, add="+")

        tuning = ctk.CTkFrame(outer, corner_radius=22, fg_color=COLOR_CARD, border_width=1, border_color=COLOR_BORDER)
        tuning.pack(fill="x", pady=(0, 12))
        ctk.CTkLabel(tuning, text="Параметры проверки", font=("Segoe UI Semibold", 16), text_color=COLOR_TEXT).pack(anchor="w", padx=18, pady=(16, 10))
        deep_media_row = ctk.CTkFrame(tuning, fg_color="transparent")
        deep_media_row.pack(fill="x", padx=18, pady=(0, 8))
        ctk.CTkCheckBox(
            deep_media_row,
            text="Включить deep media check",
            variable=self.deep_media_enabled_var,
            command=self._handle_deep_media_toggle,
        ).pack(side="left")
        _add_help_badge(deep_media_row, GENERAL_SETTING_TIPS["deep_media"])
        whitelist_row = ctk.CTkFrame(tuning, fg_color="transparent")
        whitelist_row.pack(fill="x", padx=18, pady=(0, 8))
        ctk.CTkCheckBox(
            whitelist_row,
            text="Включить РФ white-list media check",
            variable=self.rf_whitelist_check_var,
            command=self._handle_rf_whitelist_toggle,
        ).pack(side="left")
        whitelist_tip = ctk.CTkLabel(
            whitelist_row,
            text="?",
            width=22,
            height=22,
            corner_radius=11,
            fg_color=COLOR_ACCENT_SOFT,
            text_color=COLOR_ACCENT,
            font=("Segoe UI Semibold", 11),
        )
        whitelist_tip.pack(side="left", padx=(8, 0))
        attach_ctk_tooltip(whitelist_tip, ADVANCED_PROBE_TIPS["RF whitelist check"])
        self.deep_media_requirement_label = ctk.CTkLabel(
            tuning,
            text="",
            text_color=COLOR_WARN_TEXT,
            font=("Segoe UI", 12),
            justify="left",
            wraplength=700,
        )
        self.deep_media_requirement_label.pack(anchor="w", padx=18, pady=(0, 10))
        self.deep_media_requirement_label.pack_forget()

        advanced_row = ctk.CTkFrame(tuning, fg_color="transparent")
        advanced_row.pack(fill="x", padx=18, pady=(0, 8))
        ctk.CTkCheckBox(
            advanced_row,
            text="Показать расширенные параметры",
            variable=self.show_advanced_probe_var,
            command=self._refresh_advanced_probe_visibility,
        ).pack(side="left")
        tip_badge = ctk.CTkLabel(
            advanced_row,
            text="?",
            width=22,
            height=22,
            corner_radius=11,
            fg_color=COLOR_ACCENT_SOFT,
            text_color=COLOR_ACCENT,
            font=("Segoe UI Semibold", 11),
        )
        tip_badge.pack(side="left", padx=(8, 0))
        attach_ctk_tooltip(
            tip_badge,
            "Расширенные параметры влияют на скорость и строгость отбора. Если не уверены, оставьте значения по умолчанию.",
        )

        self.advanced_probe_frame = ctk.CTkFrame(tuning, fg_color="transparent")
        self.advanced_probe_frame.pack(fill="x", padx=12, pady=(0, 12))
        self._grid_entries(
            self.advanced_probe_frame,
            [
                ("Duration", self.duration_var, ADVANCED_PROBE_TIPS["Duration"]),
                ("Interval", self.interval_var, ADVANCED_PROBE_TIPS["Interval"]),
                ("Timeout", self.timeout_var, ADVANCED_PROBE_TIPS["Timeout"]),
                ("Workers", self.workers_var, ADVANCED_PROBE_TIPS["Workers"]),
                ("Fetch timeout", self.fetch_timeout_var, ADVANCED_PROBE_TIPS["Fetch timeout"]),
                ("Max latency", self.max_latency_var, ADVANCED_PROBE_TIPS["Max latency"]),
                ("Min success rate", self.min_success_var, ADVANCED_PROBE_TIPS["Min success rate"]),
                ("High latency ratio", self.high_ratio_var, ADVANCED_PROBE_TIPS["High latency ratio"]),
                ("High latency streak", self.high_streak_var, ADVANCED_PROBE_TIPS["High latency streak"]),
                ("Max proxies", self.max_proxies_var, ADVANCED_PROBE_TIPS["Max proxies"]),
                ("Live probe interval", self.live_probe_interval_var, ADVANCED_PROBE_TIPS["Live probe interval"]),
                ("Live probe duration", self.live_probe_duration_var, ADVANCED_PROBE_TIPS["Live probe duration"]),
                ("Live probe top N", self.live_probe_top_n_var, ADVANCED_PROBE_TIPS["Live probe top N"]),
                ("Deep media top N", self.deep_media_top_n_var, ADVANCED_PROBE_TIPS["Deep media top N"]),
            ],
        )
        self._refresh_advanced_probe_visibility()

    def _build_pool_tab(self, tab: ctk.CTkFrame) -> None:
        card = ctk.CTkFrame(tab, corner_radius=22, fg_color=COLOR_CARD, border_width=1, border_color=COLOR_BORDER)
        card.pack(fill="both", expand=True, padx=8, pady=12)
        ctk.CTkLabel(card, text="Рабочий пул", font=("Segoe UI Semibold", 16), text_color=COLOR_TEXT).pack(anchor="w", padx=18, pady=(16, 10))

        table_frame = ctk.CTkFrame(card, fg_color="transparent")
        table_frame.pack(fill="both", expand=True, padx=18, pady=(0, 12))
        columns = ("host", "port", "ping", "score", "media", "state")
        self.pool_tree = ttk.Treeview(table_frame, columns=columns, show="headings", style="Compact.Treeview")
        self.pool_tree.heading("host", text="Host")
        self.pool_tree.heading("port", text="Port")
        self.pool_tree.heading("ping", text="Ping")
        self.pool_tree.heading("score", text="Score")
        self.pool_tree.heading("media", text="Media")
        self.pool_tree.heading("state", text="State")
        self.pool_tree.column("host", width=200, anchor="w")
        self.pool_tree.column("port", width=60, anchor="center")
        self.pool_tree.column("ping", width=80, anchor="center")
        self.pool_tree.column("score", width=80, anchor="center")
        self.pool_tree.column("media", width=80, anchor="center")
        self.pool_tree.column("state", width=120, anchor="w")
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.pool_tree.yview)
        self.pool_tree.configure(yscrollcommand=scrollbar.set)
        self.pool_tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        actions = ctk.CTkFrame(card, fg_color="transparent")
        actions.pack(fill="x", padx=18, pady=(0, 18))
        self.copy_pool_button = ctk.CTkButton(
            actions,
            text="Скопировать выбранный upstream",
            width=220,
            height=36,
            corner_radius=18,
            fg_color=COLOR_ACCENT_SOFT,
            hover_color=COLOR_ACCENT_SOFT_HOVER,
            text_color=COLOR_ACCENT,
            command=self._copy_selected_pool_proxy,
        )
        self.copy_pool_button.pack(side="left")

    def _build_logs_tab(self, tab: ctk.CTkFrame) -> None:
        card = ctk.CTkFrame(tab, corner_radius=22, fg_color=COLOR_CARD, border_width=1, border_color=COLOR_BORDER)
        card.pack(fill="both", expand=True, padx=8, pady=12)
        ctk.CTkLabel(card, text="Журнал", font=("Segoe UI Semibold", 16), text_color=COLOR_TEXT).pack(anchor="w", padx=18, pady=(16, 10))
        self.logs_box = ctk.CTkTextbox(card, corner_radius=18, fg_color=COLOR_FIELD, border_width=1, border_color=COLOR_FIELD_BORDER, text_color=COLOR_TEXT)
        self.logs_box.pack(fill="both", expand=True, padx=18, pady=(0, 12))
        _bind_clipboard_shortcuts(self.logs_box, readonly=True)
        actions = ctk.CTkFrame(card, fg_color="transparent")
        actions.pack(fill="x", padx=18, pady=(0, 18))
        self.copy_logs_button = ctk.CTkButton(
            actions,
            text="Скопировать логи",
            width=160,
            height=36,
            corner_radius=18,
            fg_color=COLOR_ACCENT_SOFT,
            hover_color=COLOR_ACCENT_SOFT_HOVER,
            text_color=COLOR_ACCENT,
            command=self._copy_logs,
        )
        self.copy_logs_button.pack(side="left")

    def _build_authors_tab(self, tab: ctk.CTkFrame) -> None:
        outer = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        outer.pack(fill="both", expand=True, padx=8, pady=12)

        card = ctk.CTkFrame(outer, corner_radius=22, fg_color=COLOR_CARD, border_width=1, border_color=COLOR_BORDER)
        card.pack(fill="x", pady=(0, 12))
        ctk.CTkLabel(card, text="О приложении", font=("Segoe UI Semibold", 16), text_color=COLOR_TEXT).pack(anchor="w", padx=18, pady=(16, 10))
        intro = ctk.CTkFrame(card, corner_radius=20, fg_color=COLOR_FIELD, border_width=1, border_color=COLOR_FIELD_BORDER)
        intro.pack(fill="x", padx=18, pady=(0, 16))
        ctk.CTkLabel(
            intro,
            text="MTProxy AutoSwitch — приложение для сбора, проверки и отбора MTProto-прокси с локальным подключением для Telegram.",
            font=("Segoe UI", 12),
            text_color=COLOR_TEXT,
            justify="left",
            wraplength=620,
        ).pack(anchor="w", padx=18, pady=(16, 8))
        ctk.CTkLabel(
            intro,
            text="Ниже размещены ссылки на исходный проект, автора сборки и репозиторий этого форка.",
            font=("Segoe UI", 11),
            text_color=COLOR_TEXT_SOFT,
            justify="left",
            wraplength=620,
        ).pack(anchor="w", padx=18, pady=(0, 16))

        self.about_video_wrap = ctk.CTkFrame(card, corner_radius=22, fg_color=COLOR_FIELD, border_width=1, border_color=COLOR_FIELD_BORDER)
        self.about_video_wrap.pack(fill="x", padx=18, pady=(0, 16))
        self.about_video_preview: LoopingVideoPreview | None = None
        self.about_video_placeholder = ctk.CTkLabel(
            self.about_video_wrap,
            text="Откройте вкладку «О приложении», чтобы загрузить preview.",
            text_color=COLOR_TEXT_SOFT,
            font=("Segoe UI", 11),
            justify="center",
            wraplength=560,
        )
        self.about_video_placeholder.pack(fill="x", padx=16, pady=22)

        self._about_link_row(
            card,
            title="Оригинальный проект Flowseal",
            body="Базовый клиент, на основе которого сделан этот форк.",
            link_text="https://github.com/Flowseal/tg-ws-proxy",
            url="https://github.com/Flowseal/tg-ws-proxy",
            button_attr="author_origin_button",
        )
        self._about_link_row(
            card,
            title="Telegram автора",
            body="Для меня было бы очень приятно, если бы вы подписались.",
            link_text="https://t.me/peppe_poppo",
            url="https://t.me/peppe_poppo",
            button_attr="author_user_button",
        )
        self._about_link_row(
            card,
            title="Репозиторий этого форка",
            body="Исходники, публичные сборки и история изменений проекта.",
            link_text="https://github.com/pengvench/MTProxyAutoSwitch",
            url="https://github.com/pengvench/MTProxyAutoSwitch",
            last=True,
        )

    def _about_link_row(
        self,
        parent: ctk.CTkFrame,
        *,
        title: str,
        body: str,
        link_text: str,
        url: str,
        last: bool = False,
        button_attr: str | None = None,
    ) -> None:
        row = ctk.CTkFrame(parent, corner_radius=18, fg_color=COLOR_FIELD, border_width=1, border_color=COLOR_FIELD_BORDER)
        row.pack(fill="x", padx=18, pady=(0, 10 if last else 12))
        ctk.CTkLabel(
            row,
            text=title,
            font=("Segoe UI Semibold", 14),
            text_color=COLOR_TEXT,
        ).pack(anchor="w", padx=14, pady=(14, 0))
        ctk.CTkLabel(
            row,
            text=body,
            font=("Segoe UI", 11),
            text_color=COLOR_TEXT_SOFT,
            justify="left",
            wraplength=620,
        ).pack(anchor="w", padx=14, pady=(4, 10))
        button = ctk.CTkButton(
            row,
            text=link_text,
            height=38,
            corner_radius=19,
            fg_color=COLOR_ACCENT_SOFT,
            hover_color=COLOR_ACCENT_SOFT_HOVER,
            text_color=COLOR_ACCENT,
            anchor="w",
            command=lambda target=url: webbrowser.open(target),
        )
        button.pack(fill="x", padx=14, pady=(0, 14))
        if button_attr:
            setattr(self, button_attr, button)

    def _entry_row(
        self,
        parent: ctk.CTkFrame,
        label: str,
        variable: tk.StringVar,
        *,
        width: int = 220,
        show: str | None = None,
    ) -> None:
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=18, pady=(0, 10))
        ctk.CTkLabel(row, text=label, width=120, anchor="w", text_color=COLOR_TEXT_SOFT, font=("Segoe UI", 12)).pack(side="left")
        entry = ctk.CTkEntry(
            row,
            textvariable=variable,
            width=width,
            height=36,
            corner_radius=18,
            show=show,
            fg_color=COLOR_FIELD,
            border_color=COLOR_FIELD_BORDER,
            text_color=COLOR_TEXT,
        )
        entry.pack(side="left", fill="x", expand=True)
        _bind_clipboard_shortcuts(entry)

    def _entry_row_with_button(
        self,
        parent: ctk.CTkFrame,
        label: str,
        variable: tk.StringVar,
        *,
        button_text: str,
        button_command,
        show: str | None = None,
    ) -> None:
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=18, pady=(0, 10))
        ctk.CTkLabel(row, text=label, width=120, anchor="w", text_color=COLOR_TEXT_SOFT, font=("Segoe UI", 12)).pack(side="left")
        entry = ctk.CTkEntry(
            row,
            textvariable=variable,
            height=36,
            corner_radius=18,
            show=show,
            fg_color=COLOR_FIELD,
            border_color=COLOR_FIELD_BORDER,
            text_color=COLOR_TEXT,
        )
        entry.pack(side="left", fill="x", expand=True)
        _bind_clipboard_shortcuts(entry)
        button = ctk.CTkButton(
            row,
            text=button_text,
            width=132,
            height=36,
            corner_radius=18,
            fg_color=COLOR_ACCENT_SOFT,
            hover_color=COLOR_ACCENT_SOFT_HOVER,
            text_color=COLOR_ACCENT,
            command=button_command,
        )
        button.pack(side="left", padx=(8, 0))
        self.code_request_inline_button = button

    def _password_entry_row(
        self,
        parent: ctk.CTkFrame,
        label: str,
        variable: tk.StringVar,
    ) -> None:
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=18, pady=(0, 10))
        self.password_row = row
        ctk.CTkLabel(row, text=label, width=120, anchor="w", text_color=COLOR_TEXT_SOFT, font=("Segoe UI", 12)).pack(side="left")
        self.password_entry = ctk.CTkEntry(
            row,
            textvariable=variable,
            height=36,
            corner_radius=18,
            show="*",
            fg_color=COLOR_FIELD,
            border_color=COLOR_FIELD_BORDER,
            text_color=COLOR_TEXT,
        )
        self.password_entry.pack(side="left", fill="x", expand=True)
        _bind_clipboard_shortcuts(self.password_entry)
        self.password_toggle_button = ctk.CTkButton(
            row,
            text="👁",
            width=42,
            height=36,
            corner_radius=18,
            fg_color=COLOR_ACCENT_SOFT,
            hover_color=COLOR_ACCENT_SOFT_HOVER,
            text_color=COLOR_ACCENT,
            command=self._toggle_password_visibility,
        )
        self.password_toggle_button.pack(side="left", padx=(8, 0))
        self._set_password_row_visible(False)

    def _grid_entries(self, parent: ctk.CTkFrame, items: list[tuple[str, tk.StringVar, str]]) -> None:
        grid = ctk.CTkFrame(parent, fg_color="transparent")
        grid.pack(fill="x", padx=18, pady=(0, 12))
        for index, (label, variable, tip_text) in enumerate(items):
            row = index // 2
            column = index % 2
            cell = ctk.CTkFrame(grid, fg_color="transparent")
            cell.grid(row=row, column=column, sticky="ew", padx=6, pady=6)
            header = ctk.CTkFrame(cell, fg_color="transparent")
            header.pack(fill="x")
            ctk.CTkLabel(header, text=label, text_color=COLOR_TEXT_SOFT, font=("Segoe UI", 12)).pack(side="left", anchor="w")
            tip = ctk.CTkLabel(
                header,
                text="?",
                width=18,
                height=18,
                corner_radius=9,
                fg_color=COLOR_ACCENT_SOFT,
                text_color=COLOR_ACCENT,
                font=("Segoe UI Semibold", 10),
            )
            tip.pack(side="left", padx=(6, 0))
            attach_ctk_tooltip(tip, tip_text)
            entry = ctk.CTkEntry(
                cell,
                textvariable=variable,
                height=36,
                corner_radius=18,
                fg_color=COLOR_FIELD,
                border_color=COLOR_FIELD_BORDER,
                text_color=COLOR_TEXT,
            )
            entry.pack(fill="x", pady=(4, 0))
            _bind_clipboard_shortcuts(entry)
            grid.grid_columnconfigure(column, weight=1)

    def _collect_settings_payload(self, *, validate: bool) -> dict[str, object]:
        payload = asdict(self.app.runtime.config)

        def _invalid(field: str, value: object) -> str:
            return f"__invalid__:{field}:{str(value).strip()}"

        def _read_int_maybe(value: object, field: str, *, allow_zero: bool = False) -> int | str:
            try:
                return _read_int(str(value).strip(), field, allow_zero=allow_zero)
            except Exception:
                if validate:
                    raise
                return _invalid(field, value)

        def _read_float_maybe(value: object, field: str) -> float | str:
            try:
                return _read_float(str(value).strip(), field)
            except Exception:
                if validate:
                    raise
                return _invalid(field, value)

        payload["autostart_enabled"] = bool(self.autostart_var.get()) if AUTOSTART_SUPPORTED else False
        payload["start_minimized_to_tray"] = bool(self.start_minimized_var.get())
        payload["auto_start_local"] = bool(self.auto_start_local_var.get())
        payload["appearance"] = next((code for code, label in APPEARANCE_LABELS.items() if label == self.appearance_var.get()), "auto")
        payload["close_behavior"] = _close_code(self.close_behavior_var.get())
        payload["local_host"] = self.local_host_var.get().strip() or "127.0.0.1"
        payload["local_port"] = _read_int_maybe(self.local_port_var.get(), "local_port")
        payload["local_secret"] = self.local_secret_var.get().strip().lower()
        payload["telegram_sources_enabled"] = bool(self.telegram_sources_enabled_var.get())
        payload["telegram_sources"] = self._collect_telegram_sources_from_controls()
        payload["thread_source_enabled"] = payload["telegram_sources_enabled"]
        payload["thread_source_url"] = payload["telegram_sources"][0] if payload["telegram_sources"] else ""
        payload["telegram_phone"] = ""
        payload["duration"] = _read_float_maybe(self.duration_var.get(), "duration")
        payload["interval"] = _read_float_maybe(self.interval_var.get(), "interval")
        payload["timeout"] = _read_float_maybe(self.timeout_var.get(), "timeout")
        payload["workers"] = _read_int_maybe(self.workers_var.get(), "workers")
        payload["fetch_timeout"] = _read_float_maybe(self.fetch_timeout_var.get(), "fetch_timeout")
        payload["max_latency_ms"] = _read_float_maybe(self.max_latency_var.get(), "max_latency_ms")
        payload["min_success_rate"] = _read_float_maybe(self.min_success_var.get(), "min_success_rate")
        payload["max_high_latency_ratio"] = _read_float_maybe(self.high_ratio_var.get(), "max_high_latency_ratio")
        payload["high_latency_streak"] = _read_int_maybe(self.high_streak_var.get(), "high_latency_streak")
        payload["max_proxies"] = _read_int_maybe(self.max_proxies_var.get() or "0", "max_proxies", allow_zero=True)
        payload["live_probe_interval_sec"] = _read_int_maybe(self.live_probe_interval_var.get(), "live_probe_interval_sec")
        payload["live_probe_duration_sec"] = _read_float_maybe(self.live_probe_duration_var.get(), "live_probe_duration_sec")
        payload["live_probe_top_n"] = _read_int_maybe(self.live_probe_top_n_var.get(), "live_probe_top_n")
        payload["deep_media_enabled"] = bool(self.deep_media_enabled_var.get())
        payload["rf_whitelist_check_enabled"] = bool(self.rf_whitelist_check_var.get())
        payload["deep_media_top_n"] = _read_int_maybe(self.deep_media_top_n_var.get(), "deep_media_top_n")
        payload["auto_update_enabled"] = bool(self.auto_update_var.get())
        payload["sources"] = self._collect_sources_from_controls()
        payload["telegram_api_id"] = _read_int_maybe(self.telegram_api_id_var.get() or "0", "telegram_api_id", allow_zero=True)
        payload["telegram_api_hash"] = self.telegram_api_hash_var.get().strip()

        if validate:
            if not payload["local_secret"]:
                raise ValueError("local_secret is required")
            if not payload["sources"]:
                raise ValueError("sources list is empty")
            if bool(payload["telegram_sources_enabled"] or payload["deep_media_enabled"] or payload["rf_whitelist_check_enabled"]):
                if not payload["telegram_api_id"] or not str(payload["telegram_api_hash"]).strip():
                    raise ValueError("Для функций Telegram необходимо указать API ID и API Hash")
        return payload

    def _refresh_config_controls_from_runtime(self, config: AppConfig) -> None:
        self._refreshing_controls = True
        try:
            self.autostart_var.set(bool(config.autostart_enabled))
            self.start_minimized_var.set(bool(config.start_minimized_to_tray))
            self.auto_start_local_var.set(bool(config.auto_start_local))
            self.appearance_var.set(_appearance_label(config.appearance))
            self.close_behavior_var.set(CLOSE_LABELS.get(config.close_behavior, CLOSE_LABELS["ask"]))
            self.telegram_sources_enabled_var.set(bool(getattr(config, "telegram_sources_enabled", config.thread_source_enabled)))
            self.deep_media_enabled_var.set(bool(getattr(config, "deep_media_enabled", False)))
            self.rf_whitelist_check_var.set(bool(getattr(config, "rf_whitelist_check_enabled", False)))
            self.local_host_var.set(config.local_host)
            self.local_port_var.set(str(config.local_port))
            self.local_secret_var.set(config.local_secret)
            self.telegram_api_id_var.set(str(config.telegram_api_id or ""))
            self.telegram_api_hash_var.set(config.telegram_api_hash)
            self.duration_var.set(str(config.duration))
            self.interval_var.set(str(config.interval))
            self.timeout_var.set(str(config.timeout))
            self.workers_var.set(str(config.workers))
            self.fetch_timeout_var.set(str(config.fetch_timeout))
            self.max_latency_var.set(str(config.max_latency_ms))
            self.min_success_var.set(str(config.min_success_rate))
            self.high_ratio_var.set(str(config.max_high_latency_ratio))
            self.high_streak_var.set(str(config.high_latency_streak))
            self.max_proxies_var.set(str(config.max_proxies))
            self.live_probe_interval_var.set(str(config.live_probe_interval_sec))
            self.live_probe_duration_var.set(str(config.live_probe_duration_sec))
            self.live_probe_top_n_var.set(str(config.live_probe_top_n))
            self.deep_media_top_n_var.set(str(config.deep_media_top_n))
            self.auto_update_var.set(bool(getattr(config, "auto_update_enabled", True)))

            configured_sources = list(config.sources)
            for source, variable in self.source_toggle_vars.items():
                variable.set(source in configured_sources)
            self.custom_sources_box.delete("1.0", "end")
            custom_sources = [source for source in configured_sources if source not in self.source_toggle_vars]
            if custom_sources:
                self.custom_sources_box.insert("1.0", "\n".join(custom_sources))

            configured_telegram_sources = list(getattr(config, "telegram_sources", []) or [])
            for source, variable in self.telegram_source_toggle_vars.items():
                variable.set(source in configured_telegram_sources)
            telegram_textbox = getattr(self.telegram_sources_box, "_textbox", self.telegram_sources_box)
            telegram_box_state = str(telegram_textbox.cget("state"))
            if telegram_box_state == "disabled":
                telegram_textbox.configure(state="normal")
            self.telegram_sources_box.delete("1.0", "end")
            custom_telegram_sources = [source for source in configured_telegram_sources if source not in self.telegram_source_toggle_vars]
            if custom_telegram_sources:
                self.telegram_sources_box.insert("1.0", "\n".join(custom_telegram_sources))
            if telegram_box_state == "disabled":
                telegram_textbox.configure(state="disabled")
        finally:
            self._refreshing_controls = False
        self._set_baseline_payload(self._collect_settings_payload(validate=False))

    def _refresh_runtime_status(self, config: AppConfig, snapshot: dict[str, object]) -> None:
        auth = self.app.auth_status
        if auth.get("authorized"):
            status_text = f"Сессия активна: {auth.get('display') or auth.get('phone')}"
        elif auth.get("error"):
            status_text = f"Статус сессии не проверен: {auth.get('error')}"
        elif auth.get("session_exists"):
            status_text = "Сессия найдена, но авторизация не подтверждена"
        else:
            status_text = "Сессия Telegram не подключена"
        if not (config.telegram_api_id and config.telegram_api_hash.strip()):
            status_text = "Для входа в Telegram укажите свои API ID и API Hash"
        self.auth_status_label.configure(text=status_text)
        if auth.get("authorized"):
            self._set_password_row_visible(False)

        thread_text = _format_thread_status(
            str(snapshot.get("thread_status", "")),
            int(snapshot.get("thread_proxy_count", 0)),
            enabled=bool(getattr(config, "telegram_sources_enabled", config.thread_source_enabled)),
        )
        if auth.get("authorized") and str(snapshot.get("thread_status", "")) == "skipped:telegram_session_not_authorized":
            thread_text = "Сессия активна. Обновите список, чтобы заново проверить Telegram-источники."
        self.thread_status_label.configure(text=thread_text)

        pool_rows = list(snapshot.get("pool_rows", [])[:100])
        pool_signature = tuple(
            (
                row.get("host"),
                row.get("port"),
                row.get("live_latency_ms"),
                row.get("base_latency_ms"),
                row.get("score"),
                row.get("media_score"),
                row.get("last_error"),
                row.get("reason"),
                row.get("url"),
            )
            for row in pool_rows
        )
        if pool_signature != self._last_pool_signature:
            self._last_pool_signature = pool_signature
            for item in self.pool_tree.get_children():
                self.pool_tree.delete(item)
            for row in pool_rows:
                state = row.get("last_error") or row.get("reason") or "ok"
                self.pool_tree.insert(
                    "",
                    "end",
                    values=(
                        row.get("host"),
                        row.get("port"),
                        _format_latency(_safe_float(row.get("live_latency_ms") or row.get("base_latency_ms"))),
                        f"{_safe_float(row.get('score')) or 0:.0f}",
                        _format_media(row.get("media_score")),
                        state,
                    ),
                    tags=(str(row.get("url", "")),),
                )

        logs_text = "\n".join(self.app.log_lines[-250:])
        logs_signature = (len(self.app.log_lines), logs_text)
        if logs_signature != self._last_logs_signature:
            self._last_logs_signature = logs_signature
            self.logs_box.delete("1.0", "end")
            self.logs_box.insert("1.0", logs_text)

        self._refresh_requirement_hints()

    def refresh_from_runtime(self, *, force_config: bool = False) -> None:
        config, snapshot = self.app.get_runtime_state(allow_stale=True)
        self._refresh_runtime_status(config, snapshot)
        if force_config or not self._dirty or self._baseline_payload is None:
            self._refresh_config_controls_from_runtime(config)
        self.refresh_interaction_state()
        return
        self.appearance_var.set(_appearance_label(config.appearance))
        self.deep_media_enabled_var.set(bool(getattr(config, "deep_media_enabled", False)))
        self.rf_whitelist_check_var.set(bool(getattr(config, "rf_whitelist_check_enabled", False)))
        self.auto_update_var.set(bool(getattr(config, "auto_update_enabled", True)))
        self.telegram_api_id_var.set(str(config.telegram_api_id or ""))
        self.telegram_api_hash_var.set(config.telegram_api_hash)

        auth = self.app.auth_status
        if auth.get("authorized"):
            status_text = f"Сессия активна: {auth.get('display') or auth.get('phone')}"
        elif auth.get("error"):
            status_text = f"Статус сессии не проверен: {auth.get('error')}"
        elif auth.get("session_exists"):
            status_text = "Сессия найдена, но авторизация не подтверждена"
        else:
            status_text = "Сессия Telegram не подключена"
        if not (config.telegram_api_id and config.telegram_api_hash.strip()):
            status_text = "Для входа в Telegram укажите свои API ID и API Hash"
        self.auth_status_label.configure(text=status_text)
        self._refresh_requirement_hints()
        if auth.get("authorized"):
            self._set_password_row_visible(False)

        configured_sources = list(config.sources)
        for source, variable in self.source_toggle_vars.items():
            variable.set(source in configured_sources)
        custom_sources = [source for source in configured_sources if source not in self.source_toggle_vars]
        current_custom = [line.strip() for line in self.custom_sources_box.get("1.0", "end").splitlines() if line.strip()]
        if current_custom != custom_sources:
            self.custom_sources_box.delete("1.0", "end")
            if custom_sources:
                self.custom_sources_box.insert("1.0", "\n".join(custom_sources))

        configured_telegram_sources = list(getattr(config, "telegram_sources", []) or [])
        for source, variable in self.telegram_source_toggle_vars.items():
            variable.set(source in configured_telegram_sources)
        telegram_textbox = getattr(self.telegram_sources_box, "_textbox", self.telegram_sources_box)
        telegram_box_state = str(telegram_textbox.cget("state"))
        if telegram_box_state == "disabled":
            telegram_textbox.configure(state="normal")
        current_custom_telegram = [line.strip() for line in self.telegram_sources_box.get("1.0", "end").splitlines() if line.strip()]
        custom_telegram_sources = [source for source in configured_telegram_sources if source not in self.telegram_source_toggle_vars]
        if current_custom_telegram != custom_telegram_sources:
            self.telegram_sources_box.delete("1.0", "end")
            if custom_telegram_sources:
                self.telegram_sources_box.insert("1.0", "\n".join(custom_telegram_sources))
        if telegram_box_state == "disabled":
            telegram_textbox.configure(state="disabled")
        self.telegram_sources_enabled_var.set(bool(getattr(config, "telegram_sources_enabled", config.thread_source_enabled)))

        thread_text = _format_thread_status(
            str(snapshot.get("thread_status", "")),
            int(snapshot.get("thread_proxy_count", 0)),
            enabled=bool(getattr(config, "telegram_sources_enabled", config.thread_source_enabled)),
        )
        if auth.get("authorized") and str(snapshot.get("thread_status", "")) == "skipped:telegram_session_not_authorized":
            thread_text = "Сессия активна. Обновите список, чтобы заново проверить Telegram-источники."
        self.thread_status_label.configure(text=thread_text)

        for item in self.pool_tree.get_children():
            self.pool_tree.delete(item)
        for row in snapshot.get("pool_rows", [])[:100]:
            state = row.get("last_error") or row.get("reason") or "ok"
            self.pool_tree.insert(
                "",
                "end",
                values=(
                    row.get("host"),
                    row.get("port"),
                    _format_latency(_safe_float(row.get("live_latency_ms") or row.get("base_latency_ms"))),
                    f"{_safe_float(row.get('score')) or 0:.0f}",
                    _format_media(row.get("media_score")),
                    state,
                ),
                tags=(str(row.get("url", "")),),
            )

        self.logs_box.delete("1.0", "end")
        self.logs_box.insert("1.0", "\n".join(self.app.log_lines[-250:]))
        self.refresh_interaction_state()

    def refresh_interaction_state(self) -> None:
        busy = self.app._is_ui_busy()
        auth_required_disabled = not bool(self.app.auth_status.get("authorized"))
        for button in getattr(self, "auth_buttons", []):
            button.configure(state="disabled" if busy else "normal")
        for widget in (
            getattr(self, "save_button", None),
            getattr(self, "footer_open_button", None),
            getattr(self, "regenerate_secret_button", None),
            getattr(self, "check_updates_button", None),
            getattr(self, "install_update_button", None),
            getattr(self, "open_telegram_apps_button", None),
            getattr(self, "copy_hosts_button", None),
            getattr(self, "apply_hosts_button", None),
            getattr(self, "remove_hosts_button", None),
            getattr(self, "copy_pool_button", None),
            getattr(self, "copy_logs_button", None),
            getattr(self, "enable_all_sources_button", None),
            getattr(self, "author_user_button", None),
            getattr(self, "author_origin_button", None),
        ):
            if widget is not None:
                widget.configure(state="disabled" if busy else "normal")
        if hasattr(self, "save_button"):
            if self._dirty:
                self.save_button.configure(
                    state="disabled" if busy else "normal",
                    text="Сохранить",
                    fg_color=COLOR_ACCENT,
                    hover_color=COLOR_ACCENT_HOVER,
                    text_color="#FFFFFF",
                    command=self._save_settings,
                )
            else:
                # Изменений нет — кнопка становится красной «Закрыть»
                self.save_button.configure(
                    state="normal",
                    text="Закрыть",
                    fg_color=COLOR_DANGER_BG,
                    hover_color=COLOR_DANGER_BORDER,
                    text_color=COLOR_DANGER_TEXT,
                    command=self._close,
                )
        if hasattr(self, "install_update_button"):
            update_available = bool(self.app.update_info.get("available"))
            update_busy = bool(self.app.update_info.get("checking"))
            self.install_update_button.configure(
                state="normal" if (is_public_release() and update_available and not busy and not update_busy) else "disabled"
            )
        if hasattr(self, "check_updates_button"):
            update_busy = bool(self.app.update_info.get("checking"))
            self.check_updates_button.configure(state="normal" if (is_public_release() and not busy and not update_busy) else "disabled")
        for checkbox in getattr(self, "telegram_source_checkboxes", []):
            checkbox.configure(state="disabled" if (busy or auth_required_disabled) else "normal")
        for widget in (
            getattr(self, "enable_all_telegram_sources_button", None),
        ):
            if widget is not None:
                widget.configure(state="disabled" if (busy or auth_required_disabled) else "normal")
        telegram_box = getattr(self, "telegram_sources_box", None)
        if telegram_box is not None:
            textbox = getattr(telegram_box, "_textbox", telegram_box)
            textbox.configure(state="disabled" if (busy or auth_required_disabled) else "normal")
        self._refresh_auth_controls(busy=busy)

    def _refresh_requirement_hints(self) -> None:
        auth = self.app.auth_status
        is_authorized = bool(auth.get("authorized"))
        thread_enabled = bool(self.telegram_sources_enabled_var.get())
        deep_media_enabled = bool(self.deep_media_enabled_var.get())
        rf_whitelist_enabled = bool(self.rf_whitelist_check_var.get())

        if hasattr(self, "thread_requirement_label"):
            try:
                if thread_enabled and not is_authorized:
                    self.thread_requirement_label.configure(
                        text="Для парса Telegram-источников нужна авторизованная Telegram-сессия. "
                             "Без входа каналы, группы и ветки через Telegram API будут пропущены."
                    )
                    if not self.thread_requirement_label.winfo_manager():
                        self.thread_requirement_label.pack(
                            anchor="w", padx=18, pady=(0, 8),
                            before=self.thread_status_label,
                        )
                else:
                    self.thread_requirement_label.configure(text="")
                    if self.thread_requirement_label.winfo_manager():
                        self.thread_requirement_label.pack_forget()
            except Exception:
                pass

        if hasattr(self, "deep_media_requirement_label"):
            try:
                if (deep_media_enabled or rf_whitelist_enabled) and not is_authorized:
                    self.deep_media_requirement_label.configure(
                        text="Media-проверки требуют авторизованную Telegram-сессию. "
                             "Без входа deep media check и РФ white-list check будут пропущены."
                    )
                    if not self.deep_media_requirement_label.winfo_manager():
                        self.deep_media_requirement_label.pack(
                            anchor="w", padx=18, pady=(0, 10),
                            before=self.advanced_probe_frame,
                        )
                else:
                    self.deep_media_requirement_label.configure(text="")
                    if self.deep_media_requirement_label.winfo_manager():
                        self.deep_media_requirement_label.pack_forget()
            except Exception:
                pass

    def _set_all_sources_enabled(self) -> None:
        for variable in self.source_toggle_vars.values():
            variable.set(True)
        if bool(self.app.auth_status.get("authorized")):
            self.telegram_sources_enabled_var.set(True)
            for variable in self.telegram_source_toggle_vars.values():
                variable.set(True)
        self._refresh_requirement_hints()

    def _set_all_telegram_sources_enabled(self) -> None:
        if not bool(self.app.auth_status.get("authorized")):
            self.telegram_sources_enabled_var.set(False)
            self._show_auth_required_alert("Telegram-источники")
            return
        self.telegram_sources_enabled_var.set(True)
        for variable in self.telegram_source_toggle_vars.values():
            variable.set(True)
        self._refresh_requirement_hints()

    def _refresh_advanced_probe_visibility(self) -> None:
        if not hasattr(self, "advanced_probe_frame"):
            return
        if bool(self.show_advanced_probe_var.get()):
            if not self.advanced_probe_frame.winfo_manager():
                self.advanced_probe_frame.pack(fill="x", padx=12, pady=(0, 12))
        elif self.advanced_probe_frame.winfo_manager():
            self.advanced_probe_frame.pack_forget()

    def _collect_sources_from_controls(self) -> list[str]:
        selected = [
            source
            for source, variable in self.source_toggle_vars.items()
            if bool(variable.get())
        ]
        if hasattr(self, "custom_sources_box"):
            custom_lines = [
                line.strip()
                for line in self.custom_sources_box.get("1.0", "end").splitlines()
                if line.strip()
            ]
            for source in custom_lines:
                if source not in selected:
                    selected.append(source)
        return selected

    def _collect_telegram_sources_from_controls(self) -> list[str]:
        selected = [
            source
            for source, variable in self.telegram_source_toggle_vars.items()
            if bool(variable.get())
        ]
        if hasattr(self, "telegram_sources_box"):
            custom_lines = [
                line.strip()
                for line in self.telegram_sources_box.get("1.0", "end").splitlines()
                if line.strip()
            ]
            for source in custom_lines:
                if source not in selected:
                    selected.append(source)
        return selected

    def _refresh_auth_controls(self, *, busy: bool) -> None:
        is_authorized = bool(self.app.auth_status.get("authorized"))
        if hasattr(self, "auth_check_button"):
            self.auth_check_button.grid_forget()
            self.auth_login_button.grid_forget()
            self.auth_qr_button.grid_forget()
            self.auth_logout_button.grid_forget()
            self.auth_send_list_button.grid_forget()

            if is_authorized:
                self.auth_check_button.grid(row=0, column=0, sticky="ew", padx=(0, 6), pady=(0, 0))
                self.auth_logout_button.grid(row=0, column=1, sticky="ew", padx=(6, 0), pady=(0, 0))
                self.auth_send_list_button.grid(row=1, column=0, columnspan=2, sticky="ew", padx=0, pady=(8, 0))
                self.auth_check_button.configure(state="disabled" if busy else "normal")
                self.auth_logout_button.configure(state="disabled" if busy else "normal")
                self.auth_send_list_button.configure(
                    state="disabled" if (busy or int(self.app.snapshot_cache.get("working_count", 0)) <= 0) else "normal"
                )
            else:
                self.auth_check_button.grid(row=0, column=0, sticky="ew", padx=(0, 6), pady=(0, 0))
                self.auth_qr_button.grid(row=0, column=1, sticky="ew", padx=(6, 0), pady=(0, 0))
                self.auth_login_button.grid(row=1, column=0, columnspan=2, sticky="ew", padx=0, pady=(8, 0))
                self.auth_check_button.configure(state="disabled" if busy else "normal")
                self.auth_qr_button.configure(state="disabled" if busy else "normal")
                self.auth_login_button.configure(state="disabled" if busy else "normal")
            if hasattr(self, "code_request_inline_button"):
                self.code_request_inline_button.configure(state="disabled" if (busy or is_authorized) else "normal")

    def _show_auth_required_alert(self, feature_name: str) -> None:
        messagebox.showwarning(
            "Требуется вход",
            f"Для функции «{feature_name}» нужна авторизованная Telegram-сессия. Сначала войдите в Telegram в этом окне.",
            parent=self.app,
        )

    def _open_telegram_apps_page(self) -> None:
        webbrowser.open("https://my.telegram.org/apps")

    def _handle_thread_source_toggle(self) -> None:
        if self.telegram_sources_enabled_var.get() and not bool(self.app.auth_status.get("authorized")):
            self.telegram_sources_enabled_var.set(False)
            self._refresh_requirement_hints()
            self._show_auth_required_alert("Telegram-источники")
            return
        self._refresh_requirement_hints()

    def _handle_deep_media_toggle(self) -> None:
        if self.deep_media_enabled_var.get() and not bool(self.app.auth_status.get("authorized")):
            self.deep_media_enabled_var.set(False)
            self._refresh_requirement_hints()
            self._show_auth_required_alert("Deep media check")
            return
        self._refresh_requirement_hints()

    def _handle_rf_whitelist_toggle(self) -> None:
        if self.rf_whitelist_check_var.get() and not bool(self.app.auth_status.get("authorized")):
            self.rf_whitelist_check_var.set(False)
            self._refresh_requirement_hints()
            self._show_auth_required_alert("РФ white-list media check")
            return
        self._refresh_requirement_hints()

    def _toggle_password_visibility(self) -> None:
        if not hasattr(self, "password_entry"):
            return
        visible = not bool(self.password_visible_var.get())
        self.password_visible_var.set(visible)
        self.password_entry.configure(show="" if visible else "*")
        if hasattr(self, "password_toggle_button"):
            self.password_toggle_button.configure(text="🙈" if visible else "👁")

    def _clear_auth_inputs(self) -> None:
        self.phone_var.set("")
        self.code_var.set("")
        self.password_var.set("")
        self.password_visible_var.set(False)
        if hasattr(self, "password_entry"):
            self.password_entry.configure(show="*")
        if hasattr(self, "password_toggle_button"):
            self.password_toggle_button.configure(text="👁")
        self._set_password_row_visible(False)

    def _set_password_row_visible(self, visible: bool) -> None:
        if not hasattr(self, "password_row"):
            return
        managed = bool(self.password_row.winfo_manager())
        if visible and not managed:
            self.password_row.pack(fill="x", padx=18, pady=(0, 10), before=self.auth_status_label)
        elif not visible and managed:
            self.password_row.pack_forget()

    def _copy_selected_pool_proxy(self) -> None:
        selection = self.pool_tree.selection()
        if not selection:
            return
        item_id = selection[0]
        tags = self.pool_tree.item(item_id, "tags")
        if not tags:
            return
        self.clipboard_clear()
        self.clipboard_append(tags[0])

    def _copy_logs(self) -> None:
        logs = self.logs_box.get("1.0", "end-1c").strip()
        if not logs:
            return
        self.clipboard_clear()
        self.clipboard_append(logs)

    def _copy_hosts_block(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(_telegram_web_hosts_block())
        messagebox.showinfo("Telegram Web", "Блок hosts скопирован в буфер обмена", parent=self.app)

    def _apply_hosts_block(self) -> None:
        try:
            existing = HOSTS_PATH.read_text(encoding="utf-8")
            stripped = _strip_hosts_block(existing).rstrip()
            block = _telegram_web_hosts_block()
            combined = f"{stripped}\n\n{block}\n" if stripped else f"{block}\n"
            HOSTS_PATH.write_text(combined, encoding="utf-8")
        except PermissionError:
            messagebox.showerror("Telegram Web", "Нет доступа к hosts. Запустите приложение от имени администратора.", parent=self.app)
            return
        except Exception as exc:
            messagebox.showerror("Telegram Web", str(exc), parent=self.app)
            return
        messagebox.showinfo("Telegram Web", "Hosts-правила применены", parent=self.app)

    def _remove_hosts_block(self) -> None:
        try:
            if not HOSTS_PATH.exists():
                return
            existing = HOSTS_PATH.read_text(encoding="utf-8")
            HOSTS_PATH.write_text(_strip_hosts_block(existing), encoding="utf-8")
        except PermissionError:
            messagebox.showerror("Telegram Web", "Нет доступа к hosts. Запустите приложение от имени администратора.", parent=self.app)
            return
        except Exception as exc:
            messagebox.showerror("Telegram Web", str(exc), parent=self.app)
            return
        messagebox.showinfo("Telegram Web", "Hosts-правила удалены", parent=self.app)

    def _regenerate_secret(self) -> None:
        self.local_secret_var.set(secrets.token_hex(16))

    def _save_settings(self) -> None:
        try:
            payload = self._collect_settings_payload(validate=True)
            if self._baseline_payload is not None and payload == self._baseline_payload:
                self._set_baseline_payload(payload)
                return
            appearance_changed = str(payload.get("appearance")) != self.app.runtime.config.appearance
            changed = self.app.apply_config(AppConfig(**payload))
        except Exception as exc:
            messagebox.showerror("Настройки не сохранены", str(exc), parent=self.app)
            return
        if self.winfo_exists():
            self._set_baseline_payload(payload)
        if not changed:
            return
        if not appearance_changed and self.winfo_exists():
            self.refresh_from_runtime(force_config=True)
        messagebox.showinfo("Настройки", "Параметры сохранены", parent=self.app, dedupe_key="settings_saved")
        return
        try:
            payload = asdict(self.app.runtime.config)
            appearance_code = next((code for code, label in APPEARANCE_LABELS.items() if label == self.appearance_var.get()), "auto")
            appearance_changed = appearance_code != self.app.runtime.config.appearance
            payload["autostart_enabled"] = bool(self.autostart_var.get())
            payload["start_minimized_to_tray"] = bool(self.start_minimized_var.get())
            payload["auto_start_local"] = bool(self.auto_start_local_var.get())
            payload["appearance"] = appearance_code
            payload["close_behavior"] = _close_code(self.close_behavior_var.get())
            payload["local_host"] = self.local_host_var.get().strip() or "127.0.0.1"
            payload["local_port"] = _read_int(self.local_port_var.get(), "local_port")
            payload["local_secret"] = self.local_secret_var.get().strip().lower()
            payload["telegram_sources_enabled"] = bool(self.telegram_sources_enabled_var.get())
            payload["telegram_sources"] = self._collect_telegram_sources_from_controls()
            payload["thread_source_enabled"] = payload["telegram_sources_enabled"]
            payload["thread_source_url"] = payload["telegram_sources"][0] if payload["telegram_sources"] else ""
            payload["telegram_phone"] = ""
            payload["duration"] = _read_float(self.duration_var.get(), "duration")
            payload["interval"] = _read_float(self.interval_var.get(), "interval")
            payload["timeout"] = _read_float(self.timeout_var.get(), "timeout")
            payload["workers"] = _read_int(self.workers_var.get(), "workers")
            payload["fetch_timeout"] = _read_float(self.fetch_timeout_var.get(), "fetch_timeout")
            payload["max_latency_ms"] = _read_float(self.max_latency_var.get(), "max_latency_ms")
            payload["min_success_rate"] = _read_float(self.min_success_var.get(), "min_success_rate")
            payload["max_high_latency_ratio"] = _read_float(self.high_ratio_var.get(), "max_high_latency_ratio")
            payload["high_latency_streak"] = _read_int(self.high_streak_var.get(), "high_latency_streak")
            payload["max_proxies"] = _read_int(self.max_proxies_var.get() or "0", "max_proxies", allow_zero=True)
            payload["live_probe_interval_sec"] = _read_int(self.live_probe_interval_var.get(), "live_probe_interval_sec")
            payload["live_probe_duration_sec"] = _read_float(self.live_probe_duration_var.get(), "live_probe_duration_sec")
            payload["live_probe_top_n"] = _read_int(self.live_probe_top_n_var.get(), "live_probe_top_n")
            payload["deep_media_enabled"] = bool(self.deep_media_enabled_var.get())
            payload["rf_whitelist_check_enabled"] = bool(self.rf_whitelist_check_var.get())
            payload["deep_media_top_n"] = _read_int(self.deep_media_top_n_var.get(), "deep_media_top_n")
            payload["auto_update_enabled"] = bool(self.auto_update_var.get())
            payload["sources"] = self._collect_sources_from_controls()
            payload["telegram_api_id"] = _read_int(self.telegram_api_id_var.get() or "0", "telegram_api_id", allow_zero=True)
            payload["telegram_api_hash"] = self.telegram_api_hash_var.get().strip()
            if not payload["local_secret"]:
                raise ValueError("local_secret is required")
            if not payload["sources"]:
                raise ValueError("sources list is empty")
            if bool(payload["telegram_sources_enabled"] or payload["deep_media_enabled"] or payload["rf_whitelist_check_enabled"]):
                if not payload["telegram_api_id"] or not payload["telegram_api_hash"]:
                    raise ValueError("Для функций Telegram необходимо указать API ID и API Hash")
            self.app.apply_config(AppConfig(**payload))
        except Exception as exc:
            messagebox.showerror("Настройки не сохранены", str(exc), parent=self.app)
            return
        if not appearance_changed and self.winfo_exists():
            self.refresh_from_runtime()
        messagebox.showinfo("Настройки", "Параметры сохранены", parent=self.app)

    def _save_without_message(self) -> bool:
        return self._save_before_auth()
        try:
            self._save_settings()
            return True
        except Exception:
            return False

    def _check_auth_status(self) -> None:
        if not self._save_before_auth():
            return
        self.auth_status_label.configure(text="Проверка статуса Telegram...")
        self.app.refresh_auth_status(callback=lambda _: self.refresh_from_runtime(), block_ui=True)

    def _request_code(self) -> None:
        if not self._save_before_auth():
            return
        phone = normalize_telegram_phone(self.phone_var.get())
        digits = "".join(ch for ch in phone if ch.isdigit())
        if not phone or len(digits) < 11:
            messagebox.showerror("Телефон не указан", "Введите телефон в формате +79990000000, 89990000000 или 9990000000", parent=self.app)
            return
        self.phone_var.set(phone)
        self.auth_status_label.configure(text="Запрос кода авторизации...")
        self.app.request_auth_code(phone, callback=lambda _: messagebox.showinfo("Telegram", "Код отправлен", parent=self.app))

    def _complete_auth(self) -> None:
        if not self._save_before_auth():
            return
        phone = normalize_telegram_phone(self.phone_var.get())
        digits = "".join(ch for ch in phone if ch.isdigit())
        code = self.code_var.get().strip()
        password = self.password_var.get().strip()
        if not phone or len(digits) < 11 or not code:
            messagebox.showerror("Нет данных", "Нужны телефон и код подтверждения", parent=self.app)
            return
        self.phone_var.set(phone)
        self.auth_status_label.configure(text="Выполняется вход в Telegram...")
        self.app.complete_auth(
            phone,
            code,
            password,
            callback=lambda result: self._handle_auth_result(result),
        )

    def _start_qr_auth(self) -> None:
        if not self._save_before_auth():
            return
        password = self.password_var.get().strip()
        self.auth_status_label.configure(text="Подготовка QR-входа...")
        self.app.start_qr_auth(password=password, callback=lambda result: self._handle_auth_result(result))

    def _logout(self) -> None:
        if not self._save_before_auth():
            return
        self.auth_status_label.configure(text="Выход из Telegram...")
        self.app.logout_auth(callback=self._handle_logout_complete)

    def _send_proxy_list_to_saved_messages(self) -> None:
        self.auth_status_label.configure(text="Отправка рабочего списка в Избранное...")
        self.app.send_proxy_list_to_saved_messages(callback=self._handle_send_proxy_list_result)

    def _handle_auth_result(self, result: dict[str, object]) -> None:
        self.refresh_from_runtime()
        if result.get("password_required"):
            self._set_password_row_visible(True)
            self.auth_status_label.configure(text="Требуется пароль 2FA для завершения входа")
            if self.app.qr_dialog is not None and self.app.qr_dialog.winfo_exists():
                self.app.qr_dialog.show_password_prompt()
            return
        if result.get("timeout"):
            if self.app.qr_dialog is not None and self.app.qr_dialog.winfo_exists():
                self.app.qr_dialog.mark_expired()
            messagebox.showwarning("Telegram", "QR-код истек или не был подтвержден вовремя", parent=self.app)
            return
        if result.get("authorized"):
            self._clear_auth_inputs()
            self.refresh_from_runtime()
            messagebox.showinfo("Telegram", "Сессия авторизована", parent=self.app)
        else:
            messagebox.showwarning("Telegram", str(result), parent=self.app)

    def _handle_logout_complete(self) -> None:
        self._clear_auth_inputs()
        self.refresh_from_runtime()

    def _handle_send_proxy_list_result(self, result: dict[str, object]) -> None:
        self.refresh_from_runtime()
        messagebox.showinfo(
            "Telegram",
            f"Отправлено прокси: {int(result.get('sent', 0))}\nСообщений: {int(result.get('messages', 0))}",
            parent=self.app,
        )

    def _save_before_auth(self) -> bool:
        try:
            payload = self._collect_settings_payload(validate=True)
            if not payload["telegram_api_id"] or not payload["telegram_api_hash"]:
                raise ValueError("Для входа в Telegram необходимо указать API ID и API Hash")
            changed = self.app.apply_config(AppConfig(**payload))
            if self.winfo_exists():
                self._set_baseline_payload(payload)
            if changed and self.winfo_exists():
                self.refresh_from_runtime(force_config=True)
            return True
        except Exception as exc:
            messagebox.showerror("Настройки не сохранены", str(exc), parent=self.app)
            return False
        try:
            payload = asdict(self.app.runtime.config)
            payload["autostart_enabled"] = bool(self.autostart_var.get())
            payload["start_minimized_to_tray"] = bool(self.start_minimized_var.get())
            payload["auto_start_local"] = bool(self.auto_start_local_var.get())
            payload["appearance"] = next((code for code, label in APPEARANCE_LABELS.items() if label == self.appearance_var.get()), "auto")
            payload["close_behavior"] = _close_code(self.close_behavior_var.get())
            payload["local_host"] = self.local_host_var.get().strip() or "127.0.0.1"
            payload["local_port"] = _read_int(self.local_port_var.get(), "local_port")
            payload["local_secret"] = self.local_secret_var.get().strip().lower()
            payload["telegram_sources_enabled"] = bool(self.telegram_sources_enabled_var.get())
            payload["telegram_sources"] = self._collect_telegram_sources_from_controls()
            payload["thread_source_enabled"] = payload["telegram_sources_enabled"]
            payload["thread_source_url"] = payload["telegram_sources"][0] if payload["telegram_sources"] else ""
            payload["telegram_phone"] = ""
            payload["sources"] = self._collect_sources_from_controls()
            payload["rf_whitelist_check_enabled"] = bool(self.rf_whitelist_check_var.get())
            payload["auto_update_enabled"] = bool(self.auto_update_var.get())
            payload["telegram_api_id"] = _read_int(self.telegram_api_id_var.get() or "0", "telegram_api_id", allow_zero=True)
            payload["telegram_api_hash"] = self.telegram_api_hash_var.get().strip()
            if not payload["telegram_api_id"] or not payload["telegram_api_hash"]:
                raise ValueError("Для входа в Telegram необходимо указать API ID и API Hash")
            self.app.apply_config(AppConfig(**payload))
            self.refresh_from_runtime()
            return True
        except Exception as exc:
            messagebox.showerror("Настройки не сохранены", str(exc), parent=self.app)
            return False

    def _close(self) -> None:
        self._release_about_video_preview()
        if self._dirty_job is not None:
            with contextlib.suppress(Exception):
                self.after_cancel(self._dirty_job)
            self._dirty_job = None
        if self._tab_poll_job is not None:
            with contextlib.suppress(Exception):
                self.after_cancel(self._tab_poll_job)
            self._tab_poll_job = None
        with contextlib.suppress(Exception):
            self.grab_release()
        # FIX: явно останавливаем видео-виджет при закрытии окна
        video_widget = getattr(self, "about_video_preview", None)
        if video_widget is not None:
            with contextlib.suppress(Exception):
                video_widget.destroy()
            self.about_video_preview = None
        # Отменяем отложенный resize если есть
        if self._resize_job is not None:
            with contextlib.suppress(Exception):
                self.after_cancel(self._resize_job)
            self._resize_job = None
        self._clear_auth_inputs()
        self.app.settings_dialog = None
        self.destroy()

    def _on_destroy(self, event=None) -> None:
        if event is None or event.widget is self:
            self.app.settings_dialog = None


class QRAuthDialog(ctk.CTkToplevel):
    def __init__(self, parent: tk.Misc, *, app: MTProxyAutoSwitchApp, url: str, expires_at: str, on_refresh) -> None:
        super().__init__(parent)
        self.app = app
        self.on_refresh = on_refresh
        self.qr_url = ""
        self.qr_image: ctk.CTkImage | None = None
        self._expires_epoch: float | None = None
        self._countdown_job: str | None = None
        self.title("QR вход Telegram")
        with contextlib.suppress(Exception):
            if APP_ICON_PATH.exists():
                self.iconbitmap(str(APP_ICON_PATH))
        _set_fixed_window_size(self, 470, 720)
        self.transient(parent)
        self.grab_set()
        self.configure(fg_color=COLOR_BG)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Destroy>", self._on_destroy, add="+")
        self.after(40, self._bring_to_front)

        wrapper = ctk.CTkFrame(self, fg_color="transparent")
        wrapper.pack(fill="both", expand=True, padx=14, pady=14)
        card = ctk.CTkFrame(wrapper, corner_radius=24, fg_color=COLOR_CARD, border_width=1, border_color=COLOR_BORDER)
        card.pack(fill="both", expand=True)

        ctk.CTkLabel(card, text="QR вход Telegram", text_color=COLOR_TEXT, font=("Segoe UI Semibold", 18)).pack(anchor="w", padx=18, pady=(18, 6))
        self.info_label = ctk.CTkLabel(
            card,
            text="Отсканируйте код из любого уже авторизованного клиента Telegram.",
            text_color=COLOR_TEXT_SOFT,
            font=("Segoe UI", 12),
            justify="left",
            wraplength=300,
        )
        self.info_label.pack(anchor="w", padx=18)
        self.qr_image_label = ctk.CTkLabel(card, text="")
        self.qr_image_label.pack(pady=(18, 12))
        self.expires_label = ctk.CTkLabel(card, text="", text_color=COLOR_TEXT_FAINT, font=("Segoe UI", 11))
        self.expires_label.pack(pady=(0, 10))
        actions = ctk.CTkFrame(card, fg_color="transparent")
        actions.pack(fill="x", padx=18, pady=(0, 10))
        self.copy_qr_button = ctk.CTkButton(
            actions,
            text="Скопировать ссылку",
            height=36,
            corner_radius=18,
            fg_color=COLOR_ACCENT_SOFT,
            hover_color=COLOR_ACCENT_SOFT_HOVER,
            text_color=COLOR_ACCENT,
            command=self._copy_url,
        )
        self.copy_qr_button.pack(fill="x")
        self.refresh_qr_button = ctk.CTkButton(
            actions,
            text="Обновить QR",
            height=36,
            corner_radius=18,
            fg_color=COLOR_ACCENT,
            hover_color=COLOR_ACCENT_HOVER,
            text_color="#FFFFFF",
            command=self._refresh_qr,
        )
        self.refresh_qr_button.pack(fill="x", pady=(8, 0))
        self.password_wrap = ctk.CTkFrame(card, fg_color="transparent")
        self.password_wrap.pack(fill="x", padx=18, pady=(4, 0))
        self.password_hint_label = ctk.CTkLabel(
            self.password_wrap,
            text="",
            text_color=COLOR_WARN_TEXT,
            font=("Segoe UI", 11),
            justify="left",
            wraplength=300,
        )
        self.password_hint_label.pack(anchor="w", pady=(0, 8))
        self.qr_password_var = tk.StringVar(value="")
        self.qr_password_visible_var = tk.BooleanVar(value=False)
        password_row = ctk.CTkFrame(self.password_wrap, fg_color="transparent")
        password_row.pack(fill="x")
        self.qr_password_entry = ctk.CTkEntry(
            password_row,
            textvariable=self.qr_password_var,
            height=36,
            corner_radius=18,
            show="*",
            fg_color=COLOR_FIELD,
            border_color=COLOR_FIELD_BORDER,
            text_color=COLOR_TEXT,
        )
        self.qr_password_entry.pack(side="left", fill="x", expand=True)
        _bind_clipboard_shortcuts(self.qr_password_entry)
        self.qr_password_toggle_button = ctk.CTkButton(
            password_row,
            text="👁",
            width=42,
            height=36,
            corner_radius=18,
            fg_color=COLOR_ACCENT_SOFT,
            hover_color=COLOR_ACCENT_SOFT_HOVER,
            text_color=COLOR_ACCENT,
            command=self._toggle_password,
        )
        self.qr_password_toggle_button.pack(side="left", padx=(8, 0))
        self.qr_password_submit_button = ctk.CTkButton(
            self.password_wrap,
            text="Продолжить с 2FA",
            height=36,
            corner_radius=18,
            fg_color=COLOR_ACCENT,
            hover_color=COLOR_ACCENT_HOVER,
            text_color="#FFFFFF",
            command=self._submit_password,
        )
        self.qr_password_submit_button.pack(fill="x", pady=(8, 0))
        self.password_wrap.pack_forget()
        self.note_label = ctk.CTkLabel(card, text="Окно закроется автоматически после успешного входа.", text_color=COLOR_TEXT_FAINT, font=("Segoe UI", 11), wraplength=300, justify="left")
        self.note_label.pack(anchor="w", padx=18, pady=(4, 18))
        self.update_qr(url, expires_at)

    def update_qr(self, url: str, expires_at: str) -> None:
        self.qr_url = url
        image = qrcode.make(url).resize((220, 220))
        self.qr_image = ctk.CTkImage(light_image=image, dark_image=image, size=(220, 220))
        self.qr_image_label.configure(image=self.qr_image)
        self._set_expiry(expires_at)
        self.password_wrap.pack_forget()
        self.qr_password_var.set("")

    def show_password_prompt(self) -> None:
        self.password_hint_label.configure(text="Telegram запросил пароль 2FA. Введите пароль и нажмите продолжить.")
        if not self.password_wrap.winfo_manager():
            self.password_wrap.pack(fill="x", padx=18, pady=(4, 0))
        with contextlib.suppress(Exception):
            settings_dialog = self.app.settings_dialog
            if settings_dialog is not None and settings_dialog.winfo_exists():
                preset = settings_dialog.password_var.get().strip()
                if preset and not self.qr_password_var.get().strip():
                    self.qr_password_var.set(preset)
        with contextlib.suppress(Exception):
            self.qr_password_entry.focus_force()
        self._bring_to_front()

    def mark_expired(self) -> None:
        self.expires_label.configure(text="QR-код истек. Выполняется обновление...")
        self.after(250, self._refresh_qr)

    def _copy_url(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self.qr_url)

    def _refresh_qr(self) -> None:
        if callable(self.on_refresh):
            self.password_wrap.pack_forget()
            self.expires_label.configure(text="Получение нового QR...")
            self.on_refresh("")

    def _submit_password(self) -> None:
        password = self.qr_password_var.get().strip()
        if not password:
            messagebox.showwarning("Telegram", "Введите пароль 2FA", parent=self)
            return
        if callable(self.on_refresh):
            self.expires_label.configure(text="Повторная авторизация с 2FA...")
            self.on_refresh(password)

    def _toggle_password(self) -> None:
        visible = not bool(self.qr_password_visible_var.get())
        self.qr_password_visible_var.set(visible)
        self.qr_password_entry.configure(show="" if visible else "*")
        self.qr_password_toggle_button.configure(text="🙈" if visible else "👁")

    def _set_expiry(self, expires_at: str) -> None:
        self._cancel_countdown()
        self._expires_epoch = None
        try:
            self._expires_epoch = datetime.datetime.fromisoformat(expires_at).timestamp() if expires_at else None
        except Exception:
            self._expires_epoch = None
        self._tick_expiry()

    def _tick_expiry(self) -> None:
        self._countdown_job = None
        if self._expires_epoch is None:
            self.expires_label.configure(text="Срок действия QR неизвестен")
            return
        remaining = int(self._expires_epoch - datetime.datetime.now().timestamp())
        if remaining <= 0:
            self.mark_expired()
            return
        self.expires_label.configure(text=f"QR активен еще {remaining} сек.")
        self._countdown_job = self.after(1000, self._tick_expiry)

    def _cancel_countdown(self) -> None:
        if self._countdown_job is not None:
            with contextlib.suppress(Exception):
                self.after_cancel(self._countdown_job)
            self._countdown_job = None

    def _bring_to_front(self) -> None:
        with contextlib.suppress(Exception):
            self.deiconify()
            self.lift()
            self.focus_force()
            self.attributes("-topmost", True)
            self.after(300, lambda: self.attributes("-topmost", False))

    def close(self) -> None:
        self._cancel_countdown()
        self.app.qr_dialog = None
        self.destroy()

    def _on_destroy(self, _event=None) -> None:
        self._cancel_countdown()
        self.app.qr_dialog = None


class CloseActionDialog(ctk.CTkToplevel):
    def __init__(self, parent: MTProxyAutoSwitchApp) -> None:
        super().__init__(parent)
        self.result: str | None = None
        self.remember_var = tk.BooleanVar(value=False)
        self.title("Закрытие приложения")
        _set_fixed_window_size(self, 470, 250)
        self.transient(parent)
        self.grab_set()
        self.configure(fg_color=COLOR_BG)
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        card = ctk.CTkFrame(self, corner_radius=24, fg_color=COLOR_CARD, border_width=1, border_color=COLOR_BORDER)
        card.pack(fill="both", expand=True, padx=18, pady=18)

        ctk.CTkLabel(
            card,
            text="Что делать при закрытии?",
            text_color=COLOR_TEXT,
            font=("Segoe UI Semibold", 18),
        ).pack(anchor="w", padx=18, pady=(18, 8))
        ctk.CTkLabel(
            card,
            text="Можно полностью закрыть приложение или убрать его в трей.",
            text_color=COLOR_TEXT_SOFT,
            font=("Segoe UI", 12),
            justify="left",
            wraplength=380,
        ).pack(anchor="w", padx=18)
        ctk.CTkCheckBox(card, text="Запомнить выбор", variable=self.remember_var).pack(anchor="w", padx=18, pady=(14, 14))

        actions = ctk.CTkFrame(card, fg_color="transparent")
        actions.pack(fill="x", padx=18, pady=(0, 18))
        actions.grid_columnconfigure((0, 1), weight=1, uniform="close_actions")
        ctk.CTkButton(
            actions,
            text="Скрыть в трей",
            height=42,
            corner_radius=21,
            fg_color=COLOR_ACCENT_SOFT,
            hover_color=COLOR_ACCENT_SOFT_HOVER,
            text_color=COLOR_ACCENT,
            command=lambda: self._choose("tray"),
        ).grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ctk.CTkButton(
            actions,
            text="Закрыть",
            height=42,
            corner_radius=21,
            fg_color=COLOR_ACCENT,
            hover_color=COLOR_ACCENT_HOVER,
            text_color="#FFFFFF",
            command=lambda: self._choose("exit"),
        ).grid(row=0, column=1, sticky="ew", padx=(8, 0))

    def _choose(self, value: str) -> None:
        self.result = value
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()

    def show(self) -> tuple[str | None, bool]:
        self.wait_window()
        return self.result, bool(self.remember_var.get())


def _safe_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_media(value: object) -> str:
    number = _safe_float(value)
    if number is None or number < 0:
        return "n/a"
    return f"{number:.2f}"


def _read_int(value: str, field: str, *, allow_zero: bool = False) -> int:
    parsed = int(str(value).strip())
    if parsed < 0 or (parsed == 0 and not allow_zero):
        raise ValueError(f"{field} must be greater than zero")
    return parsed


def _read_float(value: str, field: str) -> float:
    parsed = float(str(value).strip())
    if parsed <= 0:
        raise ValueError(f"{field} must be greater than zero")
    return parsed


def _close_code(label: str) -> str:
    for code, display in CLOSE_LABELS.items():
        if display == label:
            return code
    return "ask"


def _trim_middle(value: str, max_len: int) -> str:
    if len(value) <= max_len:
        return value
    keep = max(8, (max_len - 3) // 2)
    return f"{value[:keep]}...{value[-keep:]}"


def main() -> None:
    single_instance_handle = _acquire_single_instance()
    if single_instance_handle is None:
        return
    try:
        app = MTProxyAutoSwitchApp()
        app.mainloop()
    finally:
        _release_single_instance(single_instance_handle)


if __name__ == "__main__":
    main()
