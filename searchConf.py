import os
import sys
import json
import threading
import queue
import fnmatch
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import webbrowser
from pathlib import Path
import ctypes
from ctypes import wintypes

import importlib

try:
    import winreg  # type: ignore
except Exception:  # pragma: no cover - 다른 OS 대체용
    winreg = None

APP_NAME_DEFAULT = "SearchConf Finder"
CONFIG_DIR = Path(os.getenv("APPDATA", Path.home())) / "SearchConfFinder"
CONFIG_PATH = CONFIG_DIR / "settings.json"
RUN_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_ENTRY_NAME = "SearchConfFinder"

DEFAULT_SETTINGS: dict[str, object] = {
    "program_name": APP_NAME_DEFAULT,
    "default_folder": str(Path.home()),
    "folder_history": [],
    "last_folder": "",
    "last_query": "",
    "last_extension": ".conf",
    "recursive": True,
    "case_sensitive": False,
    "autorun": False,
    "global_hotkey_enabled": True,
    "window_geometry": "500x420",
}


def load_settings() -> dict[str, object]:
    if not CONFIG_PATH.exists():
        return {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULT_SETTINGS.items()}
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        merged = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULT_SETTINGS.items()}
        merged.update({k: v for k, v in data.items() if k in DEFAULT_SETTINGS})
        return merged
    except Exception:
        return {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULT_SETTINGS.items()}


def save_settings(settings: dict[str, object]) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with CONFIG_PATH.open("w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def get_executable_path() -> str:
    if getattr(sys, "frozen", False):
        return sys.executable
    return os.path.abspath(__file__)


def set_autorun(enabled: bool) -> bool:
    if winreg is None or os.name != "nt":  # pragma: no cover
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_REG_PATH, 0, winreg.KEY_ALL_ACCESS) as key:
            if enabled:
                winreg.SetValueEx(key, RUN_ENTRY_NAME, 0, winreg.REG_SZ, f'"{get_executable_path()}"')
            else:
                try:
                    winreg.DeleteValue(key, RUN_ENTRY_NAME)
                except FileNotFoundError:
                    pass
        return True
    except FileNotFoundError:
        try:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_REG_PATH) as key:
                if enabled:
                    winreg.SetValueEx(key, RUN_ENTRY_NAME, 0, winreg.REG_SZ, f'"{get_executable_path()}"')
        except Exception:
            return False
        return True
    except Exception:
        return False

# 선택 기능 플래그 (지연 로딩으로 용량 최소화)
HAS_TRAY = False
HAS_KB = False


class HotkeyThread(threading.Thread):
    WM_HOTKEY = 0x0312
    WM_QUIT = 0x0012
    MOD_ALT = 0x0001
    MOD_CONTROL = 0x0002
    MOD_SHIFT = 0x0004
    MOD_WIN = 0x0008
    MOD_NOREPEAT = 0x4000

    def __init__(self, callback) -> None:
        super().__init__(daemon=True)
        self.callback = callback
        self.user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        self.kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        self.hotkey_ids: list[int] = []
        self.stop_event = threading.Event()
        self.ready_event = threading.Event()
        self.failed = False
        self.thread_id: int | None = None

    def run(self) -> None:
        self.thread_id = self.kernel32.GetCurrentThreadId()
        self._register_hotkeys()
        self.ready_event.set()
        if self.failed:
            return

        msg = wintypes.MSG()
        while True:
            ret = self.user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret == 0:  # WM_QUIT
                break
            if ret == -1:
                break
            if msg.message == self.WM_HOTKEY:
                try:
                    self.callback()
                except Exception:
                    pass
            if self.stop_event.is_set():
                break

        self._unregister_hotkeys()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread_id:
            try:
                self.user32.PostThreadMessageW(self.thread_id, self.WM_QUIT, 0, 0)
            except Exception:
                pass
        self._unregister_hotkeys()
        self.ready_event.set()

    def _register_hotkeys(self) -> None:
        combos = [
            (self.MOD_CONTROL | self.MOD_SHIFT | self.MOD_NOREPEAT, 0x38),  # Ctrl+Shift+8
            (self.MOD_CONTROL | self.MOD_NOREPEAT, 0x6A),  # Ctrl+Multiply
        ]
        base_id = 0xA000
        for idx, (mods, key) in enumerate(combos):
            hotkey_id = base_id + idx
            if not self.user32.RegisterHotKey(None, hotkey_id, mods, key):
                self.failed = True
                break
            self.hotkey_ids.append(hotkey_id)
        if self.failed:
            self._unregister_hotkeys()

    def _unregister_hotkeys(self) -> None:
        for hotkey_id in list(self.hotkey_ids):
            try:
                self.user32.UnregisterHotKey(None, hotkey_id)
            except Exception:
                pass
        self.hotkey_ids.clear()


class FileSearchGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.settings: dict[str, object] = load_settings()
        self.hotkeys_registered = False
        self.tray_icon = None
        self.window_hidden = False
        self.hotkey_thread: HotkeyThread | None = None
        self.focus_results_after_search = False
        self.settings_win: tk.Toplevel | None = None

        self.root.title(str(self.settings.get("program_name", APP_NAME_DEFAULT)))
        geometry = self.settings.get("window_geometry")
        if isinstance(geometry, str) and geometry:
            try:
                self.root.geometry(geometry)
            except Exception:
                self.root.geometry(str(DEFAULT_SETTINGS["window_geometry"]))
        else:
            self.root.geometry(str(DEFAULT_SETTINGS["window_geometry"]))

        self.result_queue: "queue.Queue[str]" = queue.Queue()
        self.search_thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.folder_history: list[str] = list(self.settings.get("folder_history", []))

        self._build_ui()
        self._apply_initial_values()
        self._poll_queue()
        self._bind_shortcuts()
        self._setup_tray_icon()
        self._setup_global_hotkeys()
        self._ensure_autorun_state()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        main = ttk.Frame(self.root, padding=12)
        main.pack(fill=tk.BOTH, expand=True)

        # 폴더 선택
        row1 = ttk.Frame(main)
        row1.pack(fill=tk.X, pady=4)
        ttk.Label(row1, text="폴더:").pack(side=tk.LEFT)
        self.folder_var = tk.StringVar()
        self.folder_combo = ttk.Combobox(row1, textvariable=self.folder_var, values=self.folder_history)
        self.folder_combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        self.folder_combo.bind("<Return>", self._on_folder_enter)
        ttk.Button(row1, text="찾아보기...", command=self._browse_folder).pack(side=tk.LEFT)

        # 검색어 + 확장자
        row2 = ttk.Frame(main)
        row2.pack(fill=tk.X, pady=4)
        ttk.Label(row2, text="검색어:").pack(side=tk.LEFT)
        self.query_var = tk.StringVar()
        self.query_entry = ttk.Entry(row2, textvariable=self.query_var)
        self.query_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        self.query_entry.bind("<Return>", self._on_query_enter)

        ttk.Label(row2, text="확장자:").pack(side=tk.LEFT, padx=(8, 0))
        self.ext_var = tk.StringVar(value=".conf")
        self.ext_entry = ttk.Entry(row2, width=10, textvariable=self.ext_var)
        self.ext_entry.pack(side=tk.LEFT, padx=4)

        # 옵션
        row3 = ttk.Frame(main)
        row3.pack(fill=tk.X, pady=2)
        self.recursive_var = tk.BooleanVar(value=True)
        self.case_sensitive_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row3, text="하위 폴더 포함", variable=self.recursive_var).pack(side=tk.LEFT)
        ttk.Checkbutton(row3, text="대/소문자 구분", variable=self.case_sensitive_var).pack(side=tk.LEFT, padx=(10, 0))

        # 실행 버튼
        row4 = ttk.Frame(main)
        row4.pack(fill=tk.X, pady=6)
        self.search_btn = ttk.Button(row4, text="검색", command=lambda: self._on_search_clicked(False))
        self.search_btn.pack(side=tk.LEFT)
        self.stop_btn = ttk.Button(row4, text="정지", command=self._on_stop_clicked, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(row4, text="지우기", command=self._clear_results).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(row4, text="설정", command=self._open_settings).pack(side=tk.LEFT, padx=(6, 0))

        # 결과 리스트
        list_frame = ttk.Frame(main)
        list_frame.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
        self.results = tk.Listbox(list_frame, selectmode=tk.EXTENDED)
        self.results.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.results.yview)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.results.config(yscrollcommand=scroll.set)

        # 상태줄
        self.status_var = tk.StringVar(value="대상 폴더와 검색어를 입력하세요.")
        status = ttk.Label(main, textvariable=self.status_var, anchor="w")
        status.pack(fill=tk.X, pady=(6, 0))

        # 바인딩
        self.results.bind("<Double-Button-1>", self._open_selected)
        self.results.bind("<Return>", self._open_selected)
        self.root.bind("<Control-c>", self._copy_selected_paths)

        # 컨텍스트 메뉴
        self._build_context_menu()

    def _apply_initial_values(self) -> None:
        default_folder = str(self.settings.get("default_folder") or Path.home())
        last_folder = str(self.settings.get("last_folder") or "") or default_folder
        if last_folder:
            if last_folder in self.folder_history:
                self.folder_history.remove(last_folder)
            self.folder_history.insert(0, last_folder)
        elif default_folder:
            if default_folder in self.folder_history:
                self.folder_history.remove(default_folder)
            self.folder_history.insert(0, default_folder)
        self.folder_var.set(last_folder)
        self.folder_combo["values"] = tuple(self.folder_history)
        self.settings["folder_history"] = list(self.folder_history)

        self.query_var.set(str(self.settings.get("last_query") or ""))
        ext = str(self.settings.get("last_extension") or ".conf")
        if not ext.strip():
            ext = ".conf"
        self.ext_var.set(ext)

        self.recursive_var.set(bool(self.settings.get("recursive", True)))
        self.case_sensitive_var.set(bool(self.settings.get("case_sensitive", False)))

        # 기본 상태 메시지
        self.status_var.set("대상 폴더와 검색어를 입력하세요.")
        self._focus_query_entry(select_all=True)

    def _ensure_autorun_state(self) -> None:
        desired = bool(self.settings.get("autorun", False))
        success = set_autorun(desired)
        if not success and desired:
            self.settings["autorun"] = False

    def _save_settings(self) -> None:
        self.settings["program_name"] = str(self.root.title() or APP_NAME_DEFAULT)
        self.settings["default_folder"] = str(self.settings.get("default_folder") or self.folder_var.get() or Path.home())
        self.settings["last_folder"] = self.folder_var.get()
        self.settings["last_query"] = self.query_var.get()
        self.settings["last_extension"] = self.ext_var.get()
        self.settings["recursive"] = bool(self.recursive_var.get())
        self.settings["case_sensitive"] = bool(self.case_sensitive_var.get())
        self.settings["folder_history"] = list(self.folder_history)
        try:
            self.settings["window_geometry"] = self.root.winfo_geometry()
        except Exception:
            pass
        save_settings(self.settings)

    def _on_close(self) -> None:
        try:
            self._save_settings()
        finally:
            self._cleanup()
            self.root.destroy()
    def _build_context_menu(self) -> None:
        self.ctx_menu = tk.Menu(self.root, tearoff=False)
        self.ctx_menu.add_command(label="파일 열기", command=self._open_selected)
        self.ctx_menu.add_command(label="폴더 열기", command=self._reveal_in_explorer)
        self.ctx_menu.add_separator()
        self.ctx_menu.add_command(label="경로 복사", command=self._copy_selected_paths)
        self.results.bind("<Button-3>", self._show_context_menu)  # Windows 우클릭

    def _focus_query_entry(self, select_all: bool = False) -> None:
        try:
            self.query_entry.focus_set()
            if select_all:
                self.query_entry.select_range(0, tk.END)
        except Exception:
            pass

    def _on_folder_enter(self, event=None):
        self._focus_query_entry(select_all=True)
        return "break"

    def _on_query_enter(self, event=None):
        self.focus_results_after_search = True
        self._on_search_clicked(from_query=True)
        return "break"

    def _show_context_menu(self, event) -> None:
        try:
            self.results.selection_clear(0, tk.END)
            idx = self.results.nearest(event.y)
            self.results.selection_set(idx)
            self.results.activate(idx)
            self.ctx_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.ctx_menu.grab_release()

    def _browse_folder(self) -> None:
        folder = filedialog.askdirectory()
        if folder:
            self.folder_var.set(folder)
            self.settings["last_folder"] = folder
            self._update_folder_history(folder)

    def _on_search_clicked(self, from_query: bool = False) -> None:
        if self.search_thread and self.search_thread.is_alive():
            messagebox.showinfo("알림", "이미 검색이 진행 중입니다. 정지 후 다시 시도하세요.")
            return

        folder = self.folder_var.get().strip()
        query = self.query_var.get()
        ext = self.ext_var.get().strip()

        if not folder or not os.path.isdir(folder):
            messagebox.showerror("오류", "유효한 폴더를 선택하세요.")
            return
        if not query:
            messagebox.showerror("오류", "검색어를 입력하세요.")
            return
        if not ext:
            ext = ".conf"
            self.ext_var.set(ext)

        self.focus_results_after_search = from_query

        self._clear_results()
        self.status_var.set("검색 중...")
        self.search_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.stop_event.clear()
        self._update_folder_history(folder)

        # 검색 시점 정보 저장
        self.settings["last_folder"] = folder
        self.settings["last_query"] = query
        self.settings["last_extension"] = ext
        self.settings["recursive"] = bool(self.recursive_var.get())
        self.settings["case_sensitive"] = bool(self.case_sensitive_var.get())

        self.search_thread = threading.Thread(
            target=self._search_worker,
            args=(folder, query, ext, self.recursive_var.get(), self.case_sensitive_var.get()),
            daemon=True,
        )
        self.search_thread.start()

    def _update_folder_history(self, folder: str) -> None:
        if folder in self.folder_history:
            self.folder_history.remove(folder)
        self.folder_history.insert(0, folder)
        # 기록은 최대 15개 유지
        self.folder_history = self.folder_history[:15]
        self.folder_combo["values"] = self.folder_history
        self.settings["folder_history"] = list(self.folder_history)

    def _on_stop_clicked(self) -> None:
        self.stop_event.set()
        self.status_var.set("정지 요청됨...")

    def _clear_results(self) -> None:
        self.results.delete(0, tk.END)
        self.status_var.set("결과가 지워졌습니다.")

    def _poll_queue(self) -> None:
        # 결과 큐 폴링하여 리스트 갱신
        updated = False
        try:
            while True:
                item = self.result_queue.get_nowait()
                if item == "__SEARCH_DONE__":
                    self.search_btn.config(state=tk.NORMAL)
                    self.stop_btn.config(state=tk.DISABLED)
                    if self.results.size() == 0:
                        self.status_var.set("검색 완료: 일치하는 파일 없음.")
                    else:
                        self.status_var.set(f"검색 완료: {self.results.size()}개 파일")
                    if self.focus_results_after_search:
                        try:
                            self.results.focus_set()
                            if self.results.size() > 0:
                                self.results.selection_clear(0, tk.END)
                                self.results.selection_set(0)
                                self.results.activate(0)
                        except Exception:
                            pass
                        self.focus_results_after_search = False
                else:
                    self.results.insert(tk.END, item)
                    updated = True
        except queue.Empty:
            pass

        if updated:
            self.status_var.set(f"진행 중... 현재 {self.results.size()}개 발견")

        self.root.after(100, self._poll_queue)

    def _search_worker(self, folder: str, query: str, ext: str, recursive: bool, case_sensitive: bool) -> None:
        try:
            normalized_ext = ext if ext.startswith(".") or "*" in ext or "?" in ext else f".{ext}"
            pattern = f"*{normalized_ext}" if not any(ch in normalized_ext for ch in "*?") else normalized_ext

            files_iter = self._iter_files(folder, pattern, recursive)
            for path in files_iter:
                if self.stop_event.is_set():
                    break
                try:
                    if self._file_contains_text(path, query, case_sensitive):
                        self.result_queue.put(path)
                except Exception:
                    # 읽기 실패 파일은 건너뜀
                    continue
        finally:
            self.result_queue.put("__SEARCH_DONE__")

    def _bind_shortcuts(self) -> None:
        # Ctrl+* 토글 (일반 키보드와 숫자키패드 지원)
        self.root.bind_all("<Control-Key-asterisk>", lambda e: self._toggle_visibility())
        self.root.bind_all("<Control-Shift-8>", lambda e: self._toggle_visibility())
        self.root.bind_all("<Control-Key-KP_Multiply>", lambda e: self._toggle_visibility())

    def _toggle_visibility(self) -> None:
        try:
            if self.window_hidden or self.root.state() in ("withdrawn", "iconic"):
                self._show_window()
                self._focus_query_entry(select_all=True)
            else:
                self._hide_window()
        except Exception:
            self._hide_window()

    # ---- 시스템 트레이 -------------------------------------------------------
    def _setup_tray_icon(self) -> None:
        if self.tray_icon is not None:
            return
        try:
            # 지연 로딩: pystray, PIL을 실행 중에만 import
            pystray = importlib.import_module("pystray")  # type: ignore
            pil_image = importlib.import_module("PIL.Image")  # type: ignore
            pil_draw = importlib.import_module("PIL.ImageDraw")  # type: ignore
            global HAS_TRAY
            HAS_TRAY = True

            # 간단한 아이콘 생성 (파랑 원)
            img = pil_image.new("RGBA", (64, 64), (0, 0, 0, 0))
            draw = pil_draw.Draw(img)
            draw.ellipse((8, 8, 56, 56), fill=(52, 120, 246, 255))

            def on_clicked(icon, item):
                label = str(item)
                if "열기/복원" in label:
                    self._show_window()
                elif "숨기기/최소화" in label:
                    self._hide_window()
                elif "종료" in label:
                    self.root.after(0, self._on_close)

            menu = pystray.Menu(
                pystray.MenuItem("열기/복원", lambda: on_clicked(self.tray_icon, "열기/복원")),
                pystray.MenuItem("숨기기/최소화", lambda: on_clicked(self.tray_icon, "숨기기/최소화")),
                pystray.MenuItem("종료", lambda: on_clicked(self.tray_icon, "종료")),
            )
            tooltip = str(self.settings.get("program_name", APP_NAME_DEFAULT))
            self.tray_icon = pystray.Icon("searchConf", img, tooltip, menu)

            def run_tray():
                try:
                    self.tray_icon.run()
                except Exception:
                    pass

            threading.Thread(target=run_tray, daemon=True).start()
        except Exception:
            pass

    def _show_window(self) -> None:
        try:
            geometry = self.settings.get("window_geometry")
            if isinstance(geometry, str) and geometry:
                try:
                    self.root.geometry(geometry)
                except Exception:
                    pass
            self.root.deiconify()
            self.root.update_idletasks()
            self.root.lift()
            self.root.focus_force()
            self._focus_query_entry(select_all=False)
            self.window_hidden = False
        except Exception:
            pass

    def _hide_window(self) -> None:
        try:
            try:
                self.settings["window_geometry"] = self.root.winfo_geometry()
            except Exception:
                pass
            self.root.withdraw()
            self.window_hidden = True
        except Exception:
            try:
                self.root.iconify()
                self.window_hidden = True
            except Exception:
                pass

    def _open_settings(self) -> None:
        if self.settings_win and tk.Toplevel.winfo_exists(self.settings_win):
            self.settings_win.deiconify()
            self.settings_win.lift()
            self.settings_win.focus_force()
            return

        win = tk.Toplevel(self.root)
        win.title("설정")
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()
        self.settings_win = win

        program_var = tk.StringVar(value=self.root.title())
        default_folder_var = tk.StringVar(value=str(self.settings.get("default_folder") or Path.home()))
        autorun_var = tk.BooleanVar(value=bool(self.settings.get("autorun", False)))
        hotkey_var = tk.BooleanVar(value=bool(self.settings.get("global_hotkey_enabled", True)))

        body = ttk.Frame(win, padding=12)
        body.pack(fill=tk.BOTH, expand=True)

        row_program = ttk.Frame(body)
        row_program.pack(fill=tk.X, pady=4)
        ttk.Label(row_program, text="프로그램 이름:").pack(side=tk.LEFT)
        program_entry = ttk.Entry(row_program, textvariable=program_var)
        program_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)

        row_folder = ttk.Frame(body)
        row_folder.pack(fill=tk.X, pady=4)
        ttk.Label(row_folder, text="기본 폴더:").pack(side=tk.LEFT)
        folder_entry = ttk.Entry(row_folder, textvariable=default_folder_var)
        folder_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)

        def browse_default_folder() -> None:
            folder = filedialog.askdirectory()
            if folder:
                default_folder_var.set(folder)

        ttk.Button(row_folder, text="찾기", command=browse_default_folder).pack(side=tk.LEFT)

        ttk.Checkbutton(body, text="Windows 시작 시 자동 실행", variable=autorun_var).pack(anchor="w", pady=4)
        ttk.Checkbutton(body, text="전역 단축키(Ctrl+*) 활성화", variable=hotkey_var).pack(anchor="w")

        info = ttk.Label(
            body,
            text="※ 전역 단축키 기능은 관리자 권한이나 keyboard 모듈 설치가 필요할 수 있습니다.",
            wraplength=360,
            foreground="#555555",
        )
        info.pack(fill=tk.X, pady=(6, 0))

        btn_row = ttk.Frame(body)
        btn_row.pack(fill=tk.X, pady=(12, 0))
        ttk.Button(btn_row, text="저장", command=lambda: apply_settings()).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(btn_row, text="닫기", command=lambda: close_settings()).pack(side=tk.RIGHT)

        def apply_settings() -> None:
            name = program_var.get().strip() or APP_NAME_DEFAULT
            self.root.title(name)
            self.settings["program_name"] = name

            default_folder = default_folder_var.get().strip() or str(Path.home())
            self.settings["default_folder"] = default_folder
            if not self.folder_var.get().strip():
                self.folder_var.set(default_folder)

            desired_autorun = bool(autorun_var.get())
            if desired_autorun != bool(self.settings.get("autorun", False)):
                success = set_autorun(desired_autorun)
                if success:
                    self.settings["autorun"] = desired_autorun
                else:
                    messagebox.showwarning("알림", "자동 실행 설정에 실패했습니다. 관리자 권한이 필요할 수 있습니다.")
                    autorun_var.set(self.settings.get("autorun", False))

            desired_hotkey = bool(hotkey_var.get())
            if desired_hotkey != bool(self.settings.get("global_hotkey_enabled", True)):
                self.settings["global_hotkey_enabled"] = desired_hotkey
                if desired_hotkey:
                    if not self._setup_global_hotkeys():
                        messagebox.showwarning("알림", "전역 단축키 등록에 실패했습니다. 관리자 권한이 필요하거나 다른 프로그램과 충돌했을 수 있습니다.")
                        self.settings["global_hotkey_enabled"] = False
                        hotkey_var.set(False)
                else:
                    self._unregister_global_hotkeys()
            self._save_settings()
            close_settings()

        def close_settings() -> None:
            if self.settings_win and tk.Toplevel.winfo_exists(self.settings_win):
                self.settings_win.grab_release()
                self.settings_win.destroy()
            self.settings_win = None

        win.protocol("WM_DELETE_WINDOW", close_settings)
        program_entry.focus_set()

    # ---- 전역 단축키 ---------------------------------------------------------
    def _setup_global_hotkeys(self) -> bool:
        if self.hotkeys_registered:
            return True
        if os.name != "nt":
            return False
        if not bool(self.settings.get("global_hotkey_enabled", True)):
            return False
        try:
            thread = HotkeyThread(lambda: self.root.after(0, self._toggle_visibility))
        except Exception:
            return False

        thread.start()
        if not thread.ready_event.wait(timeout=1.5):
            thread.stop()
            return False
        if thread.failed:
            thread.stop()
            return False

        self.hotkey_thread = thread
        self.hotkeys_registered = True
        return True

    def _unregister_global_hotkeys(self) -> None:
        if self.hotkey_thread:
            try:
                self.hotkey_thread.stop()
            except Exception:
                pass
            self.hotkey_thread = None
        self.hotkeys_registered = False

    def _cleanup(self) -> None:
        self.stop_event.set()
        if self.search_thread and self.search_thread.is_alive():
            try:
                self.search_thread.join(timeout=0.5)
            except Exception:
                pass
        if self.tray_icon is not None:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
            self.tray_icon = None
        self._unregister_global_hotkeys()
    def _iter_files(self, folder: str, pattern: str, recursive: bool):
        if recursive:
            for root, _, files in os.walk(folder):
                for name in files:
                    if fnmatch.fnmatch(name, pattern) or (pattern.startswith("*.") and name.endswith(pattern[1:])):
                        yield os.path.join(root, name)
        else:
            try:
                for name in os.listdir(folder):
                    path = os.path.join(folder, name)
                    if os.path.isfile(path) and (fnmatch.fnmatch(name, pattern) or (pattern.startswith("*.") and name.endswith(pattern[1:]))):
                        yield path
            except PermissionError:
                return

    def _file_contains_text(self, path: str, query: str, case_sensitive: bool) -> bool:
        # 다양한 인코딩 시도
        encodings = ["utf-8", "cp949", "euc-kr", "latin-1"]
        for enc in encodings:
            try:
                with open(path, "r", encoding=enc, errors="replace") as f:
                    if case_sensitive:
                        for line in f:
                            if query in line:
                                return True
                    else:
                        q = query.lower()
                        for line in f:
                            if q in line.lower():
                                return True
                return False
            except Exception:
                continue
        return False

    def _open_selected(self, event=None) -> None:
        selection = self.results.curselection()
        if not selection:
            return
        for idx in selection:
            path = self.results.get(idx)
            try:
                # Windows 기본 앱으로 열기
                os.startfile(path)  # type: ignore[attr-defined]
            except AttributeError:
                # 다른 OS 대비
                webbrowser.open(f"file://{path}")
            except Exception as e:
                messagebox.showerror("오류", f"파일 열기 실패:\n{path}\n\n{e}")

    def _reveal_in_explorer(self) -> None:
        selection = self.results.curselection()
        if not selection:
            return
        path = self.results.get(selection[0])
        folder = os.path.dirname(path)
        try:
            if os.name == "nt":
                os.startfile(folder)  # type: ignore[attr-defined]
            else:
                webbrowser.open(f"file://{folder}")
        except Exception as e:
            messagebox.showerror("오류", f"폴더 열기 실패:\n{folder}\n\n{e}")

    def _copy_selected_paths(self, event=None) -> None:
        selection = self.results.curselection()
        if not selection:
            return
        paths = [self.results.get(i) for i in selection]
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(paths))
        self.status_var.set(f"경로 복사됨: {len(paths)}개")


def main() -> None:
    root = tk.Tk()
    # Tk 8.6+에서 가용 시 ttk 테마 개선
    try:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
        elif "clam" in style.theme_names():
            style.theme_use("clam")
    except Exception:
        pass

    app = FileSearchGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()

