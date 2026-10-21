"""
Launchpad Mapper - map a Novation Launchpad Mini (MK1) to shortcuts on Windows.

No pip packages needed: only Python 3 (with tkinter, included in the standard
Windows installer). MIDI goes through winmm.dll, keystrokes through SendInput,
the tray icon through shell32 - all built into Windows.

Features: 8 pages (top buttons), press / hold / double-press actions, hotkey
recorder, smart app launching, volume + brightness, press feedback, startup
light show with scrolling text, photo overlay, tray icon, auto-reconnect.
"""

import ctypes
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import unicodedata
import webbrowser
from ctypes import wintypes
from tkinter import colorchooser, filedialog, messagebox, ttk

try:
    import winreg
except ImportError:  # not on Windows (only matters for testing)
    winreg = None

APP_NAME = "LaunchpadMapper"
WINDOW_TITLE = "Launchpad Mapper  -  Mini MK1"
CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
CONFIG_PATH = os.path.join(CONFIG_DIR, "mapping.json")
SETTINGS_PATH = os.path.join(CONFIG_DIR, "settings.json")
NUM_PAGES = 8

ACTIONS = ["None", "Hotkey", "Launch app / file", "Open URL", "Type text", "Run command",
           "Volume", "Brightness", "Go to page"]
HINTS = {
    "None": "This does nothing.",
    "Hotkey": "Keys joined by +, e.g.  ctrl+shift+m   alt+tab   win+d   playpause.  Or press Record.",
    "Launch app / file": "Path to an .exe, document or folder (or a name like notepad).",
    "Open URL": "A web address, e.g.  https://youtube.com",
    "Type text": "Text is pasted into the active window (Greek / Unicode works).",
    "Run command": "Any command line, run in the background.",
    "Volume": "up, down, mute, 'up 10' / 'down 10' (percent) or 'set 40' (go to 40%).",
    "Brightness": "up, down, 'up 10' / 'down 10' or 'set 60'. Works on external monitors (DDC/CI) and laptop screens.",
    "Go to page": "A page number 1-8, or next / prev.",
}
VALUE_CHOICES = {
    "Volume": ["up", "down", "mute", "up 10", "down 10", "set 30", "set 50", "set 100"],
    "Brightness": ["up", "down", "up 10", "down 10", "set 30", "set 70", "set 100"],
    "Go to page": [str(i) for i in range(1, NUM_PAGES + 1)] + ["next", "prev"],
}

# (name, red level 0-3, green level 0-3)
PRESETS = [
    ("Off", 0, 0), ("Red", 3, 0), ("Orange", 3, 1), ("Amber", 3, 3),
    ("Yellow", 2, 3), ("Lime", 1, 3), ("Green", 0, 3), ("Dim red", 1, 0), ("Dim green", 0, 1),
]
SHOW_COLORS = {"Red": (3, 0), "Orange": (3, 1), "Amber": (3, 3), "Yellow": (2, 3),
               "Lime": (1, 3), "Green": (0, 3)}
RAINBOW = [(3, 0), (3, 1), (3, 3), (1, 3), (0, 3), (3, 1)]

DEFAULT_SETTINGS = {
    "theme": "Light", "custom_bg": "", "last_page": 1,
    "auto_connect": True, "close_to_tray": True, "press_feedback": True,
    "hold_ms": 500, "double_ms": 350,
    "show_enabled": True, "show_text": "HELLO", "show_color": "Amber", "show_speed": 90,
    "photo": None, "photo_size_pct": 85,
}

OFF_COLOR = "#2b2b2b"
PAD_KEYS = [f"n{16 * r + c}" for r in range(8) for c in range(9)]   # 8x8 grid + side column
TOP_KEYS = [f"c{104 + i}" for i in range(8)]                         # round buttons on top


# =====================================================================
#  Windows MIDI (winmm.dll)
# =====================================================================
winmm = ctypes.WinDLL("winmm")

MIDIINPROC = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint,
                                ctypes.c_size_t, ctypes.c_size_t, ctypes.c_size_t)
CALLBACK_FUNCTION = 0x00030000
MIM_DATA = 0x3C3
MIM_CLOSE = 0x3C2


class MIDIINCAPSW(ctypes.Structure):
    _fields_ = [("wMid", wintypes.WORD), ("wPid", wintypes.WORD),
                ("vDriverVersion", wintypes.UINT), ("szPname", ctypes.c_wchar * 32),
                ("dwSupport", wintypes.DWORD)]


class MIDIOUTCAPSW(ctypes.Structure):
    _fields_ = [("wMid", wintypes.WORD), ("wPid", wintypes.WORD),
                ("vDriverVersion", wintypes.UINT), ("szPname", ctypes.c_wchar * 32),
                ("wTechnology", wintypes.WORD), ("wVoices", wintypes.WORD),
                ("wNotes", wintypes.WORD), ("wChannelMask", wintypes.WORD),
                ("dwSupport", wintypes.DWORD)]


winmm.midiInOpen.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint,
                             ctypes.c_size_t, ctypes.c_size_t, ctypes.c_uint32]
winmm.midiInStart.argtypes = [ctypes.c_void_p]
winmm.midiInStop.argtypes = [ctypes.c_void_p]
winmm.midiInReset.argtypes = [ctypes.c_void_p]
winmm.midiInClose.argtypes = [ctypes.c_void_p]
winmm.midiOutOpen.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint,
                              ctypes.c_size_t, ctypes.c_size_t, ctypes.c_uint32]
winmm.midiOutShortMsg.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
winmm.midiOutReset.argtypes = [ctypes.c_void_p]
winmm.midiOutClose.argtypes = [ctypes.c_void_p]


def midi_error(code):
    if code == 4:
        return "device is in use by another program (close it and try again)"
    return f"Windows MIDI error code {code}"


def list_midi_inputs():
    names = []
    for i in range(winmm.midiInGetNumDevs()):
        caps = MIDIINCAPSW()
        winmm.midiInGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps))
        names.append(caps.szPname)
    return names


def list_midi_outputs():
    names = []
    for i in range(winmm.midiOutGetNumDevs()):
        caps = MIDIOUTCAPSW()
        winmm.midiOutGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps))
        names.append(caps.szPname)
    return names


class MidiIn:
    """on_msg receives ("midi", status, d1, d2) or ("closed",)."""

    def __init__(self, index, on_msg):
        self.on_msg = on_msg
        self.closing = False
        self.handle = ctypes.c_void_p()
        self._cb = MIDIINPROC(self._proc)  # keep a reference so it isn't garbage collected
        cb_ptr = ctypes.cast(self._cb, ctypes.c_void_p).value
        r = winmm.midiInOpen(ctypes.byref(self.handle), index, cb_ptr, 0, CALLBACK_FUNCTION)
        if r:
            raise OSError(midi_error(r))
        winmm.midiInStart(self.handle)

    def _proc(self, hmi, msg, instance, p1, p2):
        if msg == MIM_DATA:
            self.on_msg(("midi", p1 & 0xFF, (p1 >> 8) & 0xFF, (p1 >> 16) & 0xFF))
        elif msg == MIM_CLOSE and not self.closing:
            self.on_msg(("closed",))

    def close(self):
        self.closing = True
        for fn in (winmm.midiInStop, winmm.midiInReset, winmm.midiInClose):
            try:
                fn(self.handle)
            except Exception:
                pass


class MidiOut:
    def __init__(self, index):
        self.failed = False
        self.handle = ctypes.c_void_p()
        r = winmm.midiOutOpen(ctypes.byref(self.handle), index, 0, 0, 0)
        if r:
            raise OSError(midi_error(r))

    def send(self, status, d1, d2):
        msg = status | (d1 << 8) | (d2 << 16)
        r = 0
        for _ in range(25):  # retry briefly if the driver is busy (MIDIERR_NOTREADY = 67)
            r = winmm.midiOutShortMsg(self.handle, msg)
            if r != 67:
                break
            time.sleep(0.002)
        if r not in (0, 67):
            self.failed = True  # device probably unplugged
        return r == 0

    def close(self):
        for fn in (winmm.midiOutReset, winmm.midiOutClose):
            try:
                fn(self.handle)
            except Exception:
                pass


# =====================================================================
#  Keyboard (SendInput) + hotkey recorder (low-level keyboard hook)
# =====================================================================
user32 = ctypes.WinDLL("user32")
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
ULONG_PTR = ctypes.c_size_t


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_ssize_t)

user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, ctypes.c_void_p, wintypes.DWORD]
user32.SetWindowsHookExW.restype = ctypes.c_void_p
user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = ctypes.c_ssize_t
user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), ctypes.c_void_p, wintypes.UINT,
                                wintypes.UINT, wintypes.UINT]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = ctypes.c_void_p

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002

VK = {
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B, "windows": 0x5B,
    "cmd": 0x5B, "super": 0x5B, "enter": 0x0D, "return": 0x0D, "esc": 0x1B, "escape": 0x1B,
    "tab": 0x09, "space": 0x20, "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "insert": 0x2D,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22, "up": 0x26, "down": 0x28,
    "left": 0x25, "right": 0x27, "capslock": 0x14, "printscreen": 0x2C, "pause": 0x13,
    "volumemute": 0xAD, "volumedown": 0xAE, "volumeup": 0xAF, "nexttrack": 0xB0,
    "prevtrack": 0xB1, "stop": 0xB2, "playpause": 0xB3,
    ",": 0xBC, ".": 0xBE, "-": 0xBD, "=": 0xBB, ";": 0xBA, "/": 0xBF, "`": 0xC0,
    "[": 0xDB, "]": 0xDD, "\\": 0xDC, "'": 0xDE, "plus": 0xBB, "minus": 0xBD,
}
for _i in range(1, 25):
    VK[f"f{_i}"] = 0x70 + _i - 1
EXTENDED = {0x2E, 0x2D, 0x24, 0x23, 0x21, 0x22, 0x26, 0x28, 0x25, 0x27, 0x5B, 0x2C}
VK_NAMES = {}
for _name, _code in VK.items():
    VK_NAMES.setdefault(_code, _name)  # first alias wins ("ctrl", "enter", ...)
MOD_VKS = {0x10: "shift", 0xA0: "shift", 0xA1: "shift", 0x11: "ctrl", 0xA2: "ctrl", 0xA3: "ctrl",
           0x12: "alt", 0xA4: "alt", 0xA5: "alt", 0x5B: "win", 0x5C: "win"}


def vk_for(name):
    name = name.strip().lower()
    if name in VK:
        return VK[name]
    if name.startswith("vk") and name[2:].isdigit():
        return int(name[2:])
    if len(name) == 1 and name.isalnum():
        return ord(name.upper())
    raise ValueError(f"Unknown key '{name}'")


def vk_name(vk):
    if vk in VK_NAMES:
        return VK_NAMES[vk]
    if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
        return chr(vk).lower()
    return None


def send_key(vk, up):
    flags = (KEYEVENTF_KEYUP if up else 0) | (KEYEVENTF_EXTENDEDKEY if vk in EXTENDED else 0)
    inp = INPUT(type=1)
    inp.u.ki = KEYBDINPUT(vk, 0, flags, 0, 0)
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def tap_key(vk):
    send_key(vk, False)
    send_key(vk, True)


def send_hotkey(combo):
    vks = [vk_for(k) for k in combo.split("+") if k.strip()]
    for vk in vks:
        send_key(vk, False)
    time.sleep(0.02)
    for vk in reversed(vks):
        send_key(vk, True)


class HotkeyRecorder(threading.Thread):
    """Captures the next key combination system-wide (and swallows it while recording).
    Calls on_done(combo_string_or_None). Esc cancels; times out after 12 seconds."""

    def __init__(self, on_done, timeout=12):
        super().__init__(daemon=True)
        self.on_done, self.timeout = on_done, timeout
        self.down, self.seen = set(), []
        self.result, self.cancel, self.finished = None, False, False

    def _combine(self, key):
        mods = [m for m in ("ctrl", "alt", "shift", "win") if m in self.seen]
        return "+".join(mods + ([key] if key else []))

    def _hook(self, n_code, w_param, l_param):
        if n_code < 0:
            return user32.CallNextHookEx(None, n_code, w_param, l_param)
        info = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
        vk = info.vkCode
        is_down = w_param in (0x100, 0x104)
        mod = MOD_VKS.get(vk)
        if is_down:
            if mod:
                self.down.add(mod)
                if mod not in self.seen:
                    self.seen.append(mod)
            elif not self.finished:
                name = vk_name(vk) or f"vk{vk}"
                if name == "esc" and not self.seen:
                    self.cancel = True
                else:
                    self.result = self._combine(name)
                self.finished = True
        elif mod:
            self.down.discard(mod)
            if not self.down and self.seen and not self.finished:  # only modifiers were pressed
                self.result = self._combine(None)
                self.finished = True
        return 1  # swallow

    def run(self):
        callback = HOOKPROC(self._hook)
        hook = user32.SetWindowsHookExW(13, callback, kernel32.GetModuleHandleW(None), 0)
        if not hook:
            self.on_done(None)
            return
        msg = wintypes.MSG()
        deadline = time.time() + self.timeout
        try:
            while not self.finished and time.time() < deadline:
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                time.sleep(0.01)
        finally:
            user32.UnhookWindowsHookEx(hook)
        self.on_done(None if self.cancel else self.result)


# =====================================================================
#  Volume and brightness
# =====================================================================
def do_volume(spec):
    parts = spec.lower().split()
    if not parts:
        return
    cmd = parts[0]
    n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    if cmd == "mute":
        tap_key(0xAD)
    elif cmd in ("up", "down"):
        presses = max(1, round((n if n is not None else 2) / 2))  # one key press = 2%
        for _ in range(presses):
            tap_key(0xAF if cmd == "up" else 0xAE)
            time.sleep(0.005)
    elif cmd == "set" and n is not None:
        for _ in range(50):  # make sure we are at 0 first
            tap_key(0xAE)
            time.sleep(0.003)
        for _ in range(round(min(n, 100) / 2)):
            tap_key(0xAF)
            time.sleep(0.003)
    else:
        raise ValueError("Volume value: up, down, mute, 'up 10', 'down 10' or 'set 40'")


class PHYSICAL_MONITOR(ctypes.Structure):
    _fields_ = [("hPhysicalMonitor", ctypes.c_void_p), ("szDescription", ctypes.c_wchar * 128)]


MONITORENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                                     ctypes.c_void_p, ctypes.c_ssize_t)
dxva2 = ctypes.WinDLL("dxva2")
user32.EnumDisplayMonitors.argtypes = [ctypes.c_void_p, ctypes.c_void_p, MONITORENUMPROC, ctypes.c_ssize_t]
dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
dxva2.GetPhysicalMonitorsFromHMONITOR.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(PHYSICAL_MONITOR)]
dxva2.GetMonitorBrightness.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(wintypes.DWORD)] * 3
dxva2.SetMonitorBrightness.argtypes = [ctypes.c_void_p, wintypes.DWORD]
dxva2.DestroyPhysicalMonitors.argtypes = [wintypes.DWORD, ctypes.POINTER(PHYSICAL_MONITOR)]


def parse_level(spec):
    parts = spec.lower().split()
    if not parts or parts[0] not in ("up", "down", "set"):
        raise ValueError("Brightness value: up, down, 'up 10', 'down 10' or 'set 60'")
    n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    if parts[0] == "set" and n is None:
        raise ValueError("Use e.g. 'set 60'")
    return parts[0], (10 if n is None else n)


def new_level(mode, n, cur, lo=0, hi=100):
    span = hi - lo
    if mode == "up":
        return min(hi, cur + max(1, n * span // 100))
    if mode == "down":
        return max(lo, cur - max(1, n * span // 100))
    return lo + min(n, 100) * span // 100


def brightness_ddc(mode, n):
    """External monitors via DDC/CI. Returns how many monitors were changed."""
    handles = []

    def cb(hmon, hdc, rect, lparam):
        handles.append(hmon)
        return 1

    proc = MONITORENUMPROC(cb)
    user32.EnumDisplayMonitors(None, None, proc, 0)
    changed = 0
    for hmon in handles:
        count = wintypes.DWORD()
        if not dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(hmon, ctypes.byref(count)) or not count.value:
            continue
        arr = (PHYSICAL_MONITOR * count.value)()
        if not dxva2.GetPhysicalMonitorsFromHMONITOR(hmon, count.value, arr):
            continue
        for mon in arr:
            lo, cur, hi = wintypes.DWORD(), wintypes.DWORD(), wintypes.DWORD()
            if dxva2.GetMonitorBrightness(mon.hPhysicalMonitor, ctypes.byref(lo), ctypes.byref(cur), ctypes.byref(hi)):
                target = new_level(mode, n, cur.value, lo.value, hi.value)
                if dxva2.SetMonitorBrightness(mon.hPhysicalMonitor, target):
                    changed += 1
        dxva2.DestroyPhysicalMonitors(count.value, arr)
    return changed


def brightness_wmi(mode, n):
    """Laptop built-in screen via WMI (PowerShell). Returns True on success."""
    expr = {"up": f"$cur+{n}", "down": f"$cur-{n}", "set": f"{n}"}[mode]
    script = (
        "$cur=(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness | Select-Object -First 1).CurrentBrightness;"
        "if($cur -eq $null){exit 1};"
        f"$new=[Math]::Max(0,[Math]::Min(100,{expr}));"
        "Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods | "
        "Invoke-CimMethod -MethodName WmiSetBrightness -Arguments @{Timeout=[uint32]1;Brightness=[byte]$new} | Out-Null"
    )
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
                           creationflags=0x08000000, capture_output=True, timeout=15)
        return r.returncode == 0
    except Exception:
        return False


def set_brightness(spec):
    """Returns a status message if nothing could be changed, otherwise None."""
    mode, n = parse_level(spec)
    if brightness_ddc(mode, n) or brightness_wmi(mode, n):
        return None
    return "Brightness: no controllable display found (monitor may not support DDC/CI)."


# =====================================================================
#  Smart app launching (bring an already running app to the front)
# =====================================================================
user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
user32.GetWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]
user32.GetWindow.restype = ctypes.c_void_p
user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
user32.EnumWindows.argtypes = [WNDENUMPROC, ctypes.c_ssize_t]
user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.IsIconic.argtypes = [ctypes.c_void_p]
user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
user32.BringWindowToTop.argtypes = [ctypes.c_void_p]
user32.SwitchToThisWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = ctypes.c_void_p
kernel32.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD)]
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]


def norm_path(p):
    return os.path.normcase(os.path.abspath(p))


def exe_of_pid(pid):
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return norm_path(buf.value)
    finally:
        kernel32.CloseHandle(handle)
    return ""


def windows_for_exe(exe):
    found = []

    def cb(hwnd, lparam):
        if user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, 4) \
                and user32.GetWindowTextLengthW(hwnd) > 0:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if exe_of_pid(pid.value) == exe:
                found.append(hwnd)
        return 1

    proc = WNDENUMPROC(cb)
    user32.EnumWindows(proc, 0)
    return found





user32.GetForegroundWindow.restype = ctypes.c_void_p
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
kernel32.GetCurrentThreadId.restype = wintypes.DWORD


def focus_window(hwnd):
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    fg = user32.GetForegroundWindow()
    fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    this_thread = kernel32.GetCurrentThreadId()
    attached = bool(fg_thread and fg_thread != this_thread
                    and user32.AttachThreadInput(this_thread, fg_thread, True))
    try:
        user32.BringWindowToTop(hwnd)
        if not user32.SetForegroundWindow(hwnd):
            user32.SwitchToThisWindow(hwnd, True)
    finally:
        if attached:
            user32.AttachThreadInput(this_thread, fg_thread, False)


def resolve_exe(value):
    """Return the .exe path if `value` is just one program (no arguments), else None."""
    v = value.strip().strip('"')
    if os.path.isfile(v) and v.lower().endswith(".exe"):
        return norm_path(v)
    if v and not any(ch.isspace() for ch in v):
        found = shutil.which(v)
        if found and found.lower().endswith(".exe"):
            return norm_path(found)
    return None


def launch(value, bring_to_front=True):
    value = value.strip()
    if bring_to_front:
        exe = resolve_exe(value)
        if exe:
            wins = windows_for_exe(exe)
            if wins:
                focus_window(wins[0])
                return
    if os.path.exists(value.strip('"')):
        os.startfile(value.strip('"'))
    else:
        subprocess.Popen(value, shell=True)


# =====================================================================
#  Start with Windows + single instance
# =====================================================================
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_mutex_handle = None


def autostart_enabled():
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except OSError:
        return False


def set_autostart(on):
    if winreg is None:
        return
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pyw):
        exe = pyw
    cmd = f'"{exe}" "{os.path.abspath(__file__)}" --tray'
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except OSError:
                pass


def another_instance_running():
    global _mutex_handle
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    _mutex_handle = kernel32.CreateMutexW(None, False, "Local\\LaunchpadMapperSingleInstance")
    return ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS


# =====================================================================
#  Tray icon (hidden window + Shell_NotifyIcon, runs in its own thread)
# =====================================================================
shell32 = ctypes.WinDLL("shell32")
gdi32 = ctypes.WinDLL("gdi32")

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint,
                             wintypes.WPARAM, wintypes.LPARAM)
WM_TRAY = 0x0400 + 20


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", ctypes.c_void_p),
                ("hIcon", ctypes.c_void_p), ("hCursor", ctypes.c_void_p),
                ("hbrBackground", ctypes.c_void_p), ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", ctypes.c_void_p), ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT),
                ("hIcon", ctypes.c_void_p), ("szTip", ctypes.c_wchar * 128),
                ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                ("szInfo", ctypes.c_wchar * 256), ("uTimeout", wintypes.UINT),
                ("szInfoTitle", ctypes.c_wchar * 64), ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", ctypes.c_ubyte * 16), ("hBalloonIcon", ctypes.c_void_p)]


class ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD), ("yHotspot", wintypes.DWORD),
                ("hbmMask", ctypes.c_void_p), ("hbmColor", ctypes.c_void_p)]


user32.DefWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
user32.CreateWindowExW.restype = ctypes.c_void_p
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), ctypes.c_void_p, wintypes.UINT, wintypes.UINT]
user32.PostMessageW.argtypes = [ctypes.c_void_p, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DestroyWindow.argtypes = [ctypes.c_void_p]
user32.CreatePopupMenu.restype = ctypes.c_void_p
user32.AppendMenuW.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
user32.TrackPopupMenu.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p]
user32.DestroyMenu.argtypes = [ctypes.c_void_p]
user32.CreateIconIndirect.argtypes = [ctypes.POINTER(ICONINFO)]
user32.CreateIconIndirect.restype = ctypes.c_void_p
user32.DestroyIcon.argtypes = [ctypes.c_void_p]
user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
gdi32.CreateBitmap.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.UINT, wintypes.UINT, ctypes.c_void_p]
gdi32.CreateBitmap.restype = ctypes.c_void_p
gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]


def make_icon(accent):
    """32x32 icon: dark rounded square with a 4x4 grid of pads in the accent color."""
    size = 32
    r, g, b = accent
    bgra = bytearray(size * size * 4)
    mask = bytearray(size * size // 8)  # 1 bits = transparent
    for y in range(size):
        for x in range(size):
            corner = min(x, size - 1 - x) + min(y, size - 1 - y) < 4
            cell_x, cell_y = (x - 3) // 7, (y - 3) // 7
            in_pad = 3 <= x < 31 and 3 <= y < 31 and 0 <= cell_x < 4 and 0 <= cell_y < 4 \
                and (x - 3) % 7 < 5 and (y - 3) % 7 < 5
            i = (y * size + x) * 4
            if corner:
                mask[(y * size + x) // 8] |= 0x80 >> (x % 8)
                continue
            col = (r, g, b) if in_pad else (30, 30, 34)
            bgra[i:i + 4] = bytes((col[2], col[1], col[0], 255))
    color_bits = (ctypes.c_ubyte * len(bgra)).from_buffer(bgra)
    mask_bits = (ctypes.c_ubyte * len(mask)).from_buffer(mask)
    hbm_color = gdi32.CreateBitmap(size, size, 1, 32, ctypes.addressof(color_bits))
    hbm_mask = gdi32.CreateBitmap(size, size, 1, 1, ctypes.addressof(mask_bits))
    info = ICONINFO(True, 0, 0, hbm_mask, hbm_color)
    icon = user32.CreateIconIndirect(ctypes.byref(info))
    gdi32.DeleteObject(hbm_color)
    gdi32.DeleteObject(hbm_mask)
    return icon


class Tray(threading.Thread):
    """System tray icon. on_event receives "toggle", "connect" or "quit"."""

    def __init__(self, on_event):
        super().__init__(daemon=True)
        self.on_event = on_event
        self.hwnd = None
        self.icons = {}
        self.ready = threading.Event()
        self.connected = False
        self._wndproc = WNDPROC(self._proc)  # keep reference
        self._taskbar_msg = 0

    def _nid(self):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = 1 | 2 | 4  # NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self.icons[self.connected]
        nid.szTip = "Launchpad Mapper - " + ("connected" if self.connected else "not connected")
        return nid

    def set_connected(self, connected):
        self.connected = bool(connected)
        if self.hwnd and self.icons:
            shell32.Shell_NotifyIconW(1, ctypes.byref(self._nid()))  # NIM_MODIFY

    def _menu(self):
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, 0, 1, "Show / hide window")
        user32.AppendMenuW(menu, 0, 2, "Disconnect" if self.connected else "Connect")
        user32.AppendMenuW(menu, 0x800, 0, None)
        user32.AppendMenuW(menu, 0, 3, "Quit")
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(self.hwnd)
        cmd = user32.TrackPopupMenu(menu, 0x100 | 0x2, pt.x, pt.y, 0, self.hwnd, None)
        user32.PostMessageW(self.hwnd, 0, 0, 0)
        user32.DestroyMenu(menu)
        return {1: "toggle", 2: "connect", 3: "quit"}.get(cmd)

    def _proc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAY:
                if lparam == 0x203:      # double-click
                    self.on_event("toggle")
                elif lparam == 0x205:    # right-click
                    ev = self._menu()
                    if ev:
                        self.on_event(ev)
                return 0
            if self._taskbar_msg and msg == self._taskbar_msg:  # Explorer restarted
                shell32.Shell_NotifyIconW(0, ctypes.byref(self._nid()))
                return 0
            if msg == 0x0002:            # WM_DESTROY
                shell32.Shell_NotifyIconW(2, ctypes.byref(self._nid()))
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            pass
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def run(self):
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinst
        wc.lpszClassName = "LaunchpadMapperTray"
        user32.RegisterClassW(ctypes.byref(wc))
        self.hwnd = user32.CreateWindowExW(0, "LaunchpadMapperTray", "LaunchpadMapperTray", 0,
                                           0, 0, 0, 0, None, None, hinst, None)
        if not self.hwnd:
            return
        self._taskbar_msg = user32.RegisterWindowMessageW("TaskbarCreated")
        self.icons = {True: make_icon((34, 197, 94)), False: make_icon((239, 68, 68))}
        shell32.Shell_NotifyIconW(0, ctypes.byref(self._nid()))  # NIM_ADD
        self.ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        for icon in self.icons.values():
            user32.DestroyIcon(icon)

    def stop(self):
        if self.hwnd:
            user32.PostMessageW(self.hwnd, 0x0010, 0, 0)  # WM_CLOSE -> DefWindowProc destroys window


# =====================================================================
#  Data model (pages -> pads -> press / hold / double actions)
# =====================================================================
def new_act():
    return {"action": "None", "value": "", "focus": True}


def norm_act(a):
    out = new_act()
    if isinstance(a, dict):
        out["action"] = a.get("action") if a.get("action") in ACTIONS else "None"
        out["value"] = str(a.get("value", ""))
        out["focus"] = bool(a.get("focus", True))
    return out


def _level(v):
    try:
        return max(0, min(3, int(v)))
    except (TypeError, ValueError):
        return 0


def norm_rec(rec):
    press = rec.get("press")
    if press is None and "action" in rec:  # old single-action format
        press = {"action": rec.get("action"), "value": rec.get("value", "")}
    return {"label": str(rec.get("label", "")), "red": _level(rec.get("red")),
            "green": _level(rec.get("green")), "press": norm_act(press),
            "hold": norm_act(rec.get("hold")), "double": norm_act(rec.get("double"))}


def migrate(data):
    """Accepts the old flat format or the new paged format; always returns the paged one."""
    if isinstance(data, dict) and isinstance(data.get("pages"), dict):
        src = data["pages"]
    elif isinstance(data, dict):  # old format: everything belonged to page 1
        src = {"1": {k: v for k, v in data.items() if isinstance(v, dict) and k.startswith("n")}}
    else:
        src = {}
    pages = {}
    for num, page in src.items():
        if str(num).isdigit() and 1 <= int(num) <= NUM_PAGES and isinstance(page, dict):
            pages[str(int(num))] = {k: norm_rec(v) for k, v in page.items()
                                    if k in PAD_KEYS and isinstance(v, dict)}
    return {"version": 2, "pages": pages}


def has_act(a):
    return isinstance(a, dict) and a.get("action", "None") != "None" \
        and str(a.get("value", "")).strip() != ""


def rec_is_empty(rec):
    if rec.get("label") or rec.get("red") or rec.get("green"):
        return False
    return all(rec[n]["action"] == "None" and not rec[n]["value"].strip()
               for n in ("press", "hold", "double"))


def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# =====================================================================
#  Scrolling-text font (5x7) for the startup light show
# =====================================================================
_F = {
    "A": ".###./#...#/#...#/#####/#...#/#...#/#...#",
    "B": "####./#...#/#...#/####./#...#/#...#/####.",
    "C": ".###./#...#/#..../#..../#..../#...#/.###.",
    "D": "###../#..#./#...#/#...#/#...#/#..#./###..",
    "E": "#####/#..../#..../####./#..../#..../#####",
    "F": "#####/#..../#..../####./#..../#..../#....",
    "G": ".###./#...#/#..../#.###/#...#/#...#/.####",
    "H": "#...#/#...#/#...#/#####/#...#/#...#/#...#",
    "I": ".###./..#../..#../..#../..#../..#../.###.",
    "J": "..###/...#./...#./...#./...#./#..#./.##..",
    "K": "#...#/#..#./#.#../##.../#.#../#..#./#...#",
    "L": "#..../#..../#..../#..../#..../#..../#####",
    "M": "#...#/##.##/#.#.#/#.#.#/#...#/#...#/#...#",
    "N": "#...#/##..#/#.#.#/#..##/#...#/#...#/#...#",
    "O": ".###./#...#/#...#/#...#/#...#/#...#/.###.",
    "P": "####./#...#/#...#/####./#..../#..../#....",
    "Q": ".###./#...#/#...#/#...#/#.#.#/#..#./.##.#",
    "R": "####./#...#/#...#/####./#.#../#..#./#...#",
    "S": ".####/#..../#..../.###./....#/....#/####.",
    "T": "#####/..#../..#../..#../..#../..#../..#..",
    "U": "#...#/#...#/#...#/#...#/#...#/#...#/.###.",
    "V": "#...#/#...#/#...#/#...#/#...#/.#.#./..#..",
    "W": "#...#/#...#/#...#/#.#.#/#.#.#/##.##/#...#",
    "X": "#...#/#...#/.#.#./..#../.#.#./#...#/#...#",
    "Y": "#...#/#...#/.#.#./..#../..#../..#../..#..",
    "Z": "#####/....#/...#./..#../.#.../#..../#####",
    "0": ".###./#...#/#..##/#.#.#/##..#/#...#/.###.",
    "1": "..#../.##../..#../..#../..#../..#../.###.",
    "2": ".###./#...#/....#/...#./..#../.#.../#####",
    "3": ".###./#...#/....#/..##./....#/#...#/.###.",
    "4": "...#./..##./.#.#./#..#./#####/...#./...#.",
    "5": "#####/#..../####./....#/....#/#...#/.###.",
    "6": "..##./.#.../#..../####./#...#/#...#/.###.",
    "7": "#####/....#/...#./..#../.#.../.#.../.#...",
    "8": ".###./#...#/#...#/.###./#...#/#...#/.###.",
    "9": ".###./#...#/#...#/.####/....#/...#./.##..",
    " ": ".../.../.../.../.../.../...",
    "!": "..#../..#../..#../..#../..#../...../..#..",
    "?": ".###./#...#/....#/...#./..#../...../..#..",
    ".": "../../../../../##/##",
    ",": "../../../../.#/.#/#.",
    "-": "..../..../..../####/..../..../....",
    ":": ".../.../.#./.../.#./.../...",
    "+": ".../.../.#./###/.#./.../...",
    "'": ".#/.#/#./../../../..",
    "/": "....#/....#/...#./..#../.#.../#..../#....",
    "(": "..#/.#./#../#../#../.#./..#",
    ")": "#../.#./..#/..#/..#/.#./#..",
    "\u2665": ".#.#./#####/#####/#####/.###./..#../.....",
    "*": "..#../#.#.#/.###./#####/.###./#.#.#/..#..",
    # Greek capitals that do not look like Latin letters
    "\u0393": "#####/#..../#..../#..../#..../#..../#....",
    "\u0394": "..#../..#../.#.#./.#.#./#...#/#...#/#####",
    "\u0398": ".###./#...#/#...#/#####/#...#/#...#/.###.",
    "\u039b": "..#../..#../.#.#./.#.#./#...#/#...#/#...#",
    "\u039e": "#####/...../...../.###./...../...../#####",
    "\u03a0": "#####/#...#/#...#/#...#/#...#/#...#/#...#",
    "\u03a3": "#####/#..../.#.../..#../.#.../#..../#####",
    "\u03a6": "..#../.###./#.#.#/#.#.#/.###./..#../..#..",
    "\u03a8": "#.#.#/#.#.#/#.#.#/.###./..#../..#../..#..",
    "\u03a9": ".###./#...#/#...#/#...#/.#.#./.#.#./##.##",
}
FONT = {ch: rows.split("/") for ch, rows in _F.items()}
# Greek capitals that look exactly like a Latin letter
GREEK_LOOKALIKE = {"\u0391": "A", "\u0392": "B", "\u0395": "E", "\u0396": "Z", "\u0397": "H",
                   "\u0399": "I", "\u039a": "K", "\u039c": "M", "\u039d": "N", "\u039f": "O",
                   "\u03a1": "P", "\u03a4": "T", "\u03a5": "Y", "\u03a7": "X"}


def clean_text(text):
    """Upper-case, strip accents (so Greek tonos works), map Greek look-alikes to Latin."""
    out = []
    for ch in unicodedata.normalize("NFD", text):
        if unicodedata.category(ch) == "Mn":
            continue
        out.append("".join(GREEK_LOOKALIKE.get(c, c) for c in ch.upper()))
    return "".join(out)


def text_columns(text):
    """List of columns (each a list of 7 booleans) for the whole scrolling banner."""
    blank = lambda: [False] * 7
    cols = [blank() for _ in range(8)]
    for ch in clean_text(text):
        rows = FONT.get(ch)
        if not rows:
            continue
        cols += [[rows[r][c] == "#" for r in range(7)] for c in range(len(rows[0]))]
        cols.append(blank())
    cols += [blank() for _ in range(7)]
    return cols


# =====================================================================
#  Colors and themes
# =====================================================================
def display_color(red, green):
    """Approximate on-screen color for an MK1 red/green LED level."""
    if red == 0 and green == 0:
        return OFF_COLOR
    r = int(255 * red / 3)
    g = int(200 * green / 3)
    return f"#{max(r, 40 if red else 0):02x}{max(g, 40 if green else 0):02x}00"


def led_velocity(red, green):
    # +12 = "copy/clear" flags, the normal setting when double-buffering is off
    return 16 * green + red + 12


def nearest_led(hex_color):
    """Snap any hex color to the closest red/green LED combination (blue is ignored)."""
    h = hex_color.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        raise ValueError("bad hex")
    tr, tg = int(h[0:2], 16), int(h[2:4], 16) * 200 / 255  # green LED displays a bit dimmer
    best, best_d = (0, 0), None
    for red in range(4):
        for green in range(4):
            d = (tr - 255 * red / 3) ** 2 + (tg - 200 * green / 3) ** 2
            if best_d is None or d < best_d:
                best, best_d = (red, green), d
    return best


THEMES = {
    "Light": dict(bg="#f0f0f0", fg="#111111", field="#ffffff", btn="#e1e1e1",
                  border="#adadad", muted="#666666", select="#cce4f7"),
    "Dark": dict(bg="#1e1e1e", fg="#e6e6e6", field="#2d2d2d", btn="#3a3a3a",
                 border="#555555", muted="#9a9a9a", select="#3d5a80"),
    "Midnight": dict(bg="#0f172a", fg="#e2e8f0", field="#1e293b", btn="#334155",
                     border="#475569", muted="#94a3b8", select="#2563eb"),
}


def _rgb(h):
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def luminance(h):
    r, g, b = _rgb(h)
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255


def shade(h, f):
    """f > 0 lightens toward white, f < 0 darkens toward black."""
    r, g, b = _rgb(h)
    t = 255 if f > 0 else 0
    f = abs(f)
    return "#%02x%02x%02x" % tuple(int(c + (t - c) * f) for c in (r, g, b))


def custom_theme(bg):
    if len(bg.lstrip("#")) != 6:
        bg = "#2b2b3c"
    if luminance(bg) < 0.5:
        return dict(bg=bg, fg="#f2f2f2", field=shade(bg, 0.10), btn=shade(bg, 0.20),
                    border=shade(bg, 0.35), muted="#b0b0b0", select=shade(bg, 0.35))
    return dict(bg=bg, fg="#111111", field=shade(bg, 0.65), btn=shade(bg, -0.08),
                border=shade(bg, -0.30), muted="#555555", select=shade(bg, -0.15))


def walk(widget):
    for child in widget.winfo_children():
        yield child
        yield from walk(child)


# =====================================================================
#  Widgets
# =====================================================================
def rounded_points(x1, y1, x2, y2, r):
    return [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
            x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]


class PadButton(tk.Canvas):
    """A Launchpad-style button: round (top row / side column) or rounded square (grid pads)."""

    OUTER = "#111111"

    def __init__(self, parent, shape, size, command):
        super().__init__(parent, width=size, height=size, bg=self.OUTER,
                         highlightthickness=0, bd=0, cursor="hand2")
        self.shape, self.size, self.command = shape, size, command
        self._fill, self._fg, self._text, self._selected = OFF_COLOR, "white", "", False
        self.bind("<Button-1>", lambda _e: self.command())
        self._draw()

    def _draw(self):
        self.delete("all")
        s = self.size
        outline = "white" if self._selected else "#000000"
        width = 3 if self._selected else 1
        if self.shape == "round":
            m = 7
            self.create_oval(m, m, s - m, s - m, fill=self._fill, outline=outline, width=width)
            wrap = s - 18
        else:
            m = 3
            self.create_polygon(rounded_points(m, m, s - m, s - m, 8), smooth=True,
                                fill=self._fill, outline=outline, width=width)
            wrap = s - 12
        if self._text:
            self.create_text(s / 2, s / 2, text=self._text, fill=self._fg, width=wrap,
                             justify="center", font=("Segoe UI", 7))

    def configure(self, cnf=None, **kw):
        if cnf:
            kw.update(cnf)
        redraw = False
        for key in ("text", "bg", "fg", "highlightbackground"):
            if key in kw:
                value = kw.pop(key)
                if key == "text":
                    self._text = value
                elif key == "bg":
                    self._fill = value
                elif key == "fg":
                    self._fg = value
                else:
                    self._selected = value not in (self.OUTER, "#111")
                redraw = True
        kw.pop("activebackground", None)
        kw.pop("activeforeground", None)
        if kw:
            super().configure(**kw)
        if redraw:
            self._draw()

    config = configure


class PhotoView(tk.Canvas):
    """Shows a photo of the Launchpad with the buttons drawn on top as overlays."""

    def __init__(self, parent, image):
        super().__init__(parent, width=image.width(), height=image.height(),
                         highlightthickness=0, bd=0)
        self.image = image
        self.pads = []
        self.calib = None  # callable(x, y) while calibrating
        self.create_image(0, 0, anchor="nw", image=image)
        self.bind("<Button-1>", self._click)

    def register(self, pad):
        self.pads.append(pad)

    def _click(self, e):
        if self.calib:
            self.calib(e.x, e.y)
            return
        for pad in self.pads:
            if abs(e.x - pad.cx) <= pad.half and abs(e.y - pad.cy) <= pad.half:
                pad.command()
                return

    def banner(self, text):
        self.delete("banner")
        t = self.create_text(self.winfo_reqwidth() / 2, 18, text=text, fill="white",
                             width=self.winfo_reqwidth() - 24, justify="center",
                             font=("Segoe UI", 10, "bold"), tags="banner")
        x1, y1, x2, y2 = self.bbox(t)
        box = self.create_rectangle(x1 - 8, y1 - 5, x2 + 8, y2 + 5, fill="#000000",
                                    outline="#ffffff", tags="banner")
        self.tag_lower(box, t)

    def marker(self, x, y):
        self.create_oval(x - 7, y - 7, x + 7, y + 7, outline="#ff3030", width=2)
        self.create_line(x - 12, y, x + 12, y, fill="#ff3030", width=2)
        self.create_line(x, y - 12, x, y + 12, fill="#ff3030", width=2)


class PhotoPad:
    """Same interface as PadButton, but drawn as an overlay on a PhotoView."""

    def __init__(self, view, key, cx, cy, half, shape, command):
        self.view, self.key, self.cx, self.cy, self.half = view, key, cx, cy, half
        self.shape, self.command = shape, command
        self._fill, self._fg, self._text, self._selected = OFF_COLOR, "white", "", False
        self._draw()

    def _draw(self):
        v, tag = self.view, f"p_{self.key}"
        v.delete(tag)
        x1, y1, x2, y2 = self.cx - self.half, self.cy - self.half, self.cx + self.half, self.cy + self.half
        off = self._fill == OFF_COLOR
        kw = dict(tags=(tag,), fill="" if off else self._fill,
                  outline="white" if self._selected else ("#bbbbbb" if off else "#000000"),
                  width=3 if self._selected else (1 if off else 2))
        if self.shape == "round":
            v.create_oval(x1, y1, x2, y2, **kw)
        else:
            v.create_polygon(rounded_points(x1, y1, x2, y2, max(3, self.half * 0.25)), smooth=True, **kw)
        if self._text:
            v.create_text(self.cx, self.cy, text=self._text, fill=self._fg, width=max(10, self.half * 2 - 6),
                          justify="center", font=("Segoe UI", 7, "bold"), tags=(tag,))

    def configure(self, cnf=None, **kw):
        if cnf:
            kw.update(cnf)
        for key in ("text", "bg", "fg", "highlightbackground"):
            if key in kw:
                value = kw.pop(key)
                if key == "text":
                    self._text = value
                elif key == "bg":
                    self._fill = value
                elif key == "fg":
                    self._fg = value
                else:
                    self._selected = value not in (PadButton.OUTER, "#111")
        self._draw()

    config = configure


class ActionEditor(ttk.Frame):
    """Editor for one action: type, value, and the helper buttons that fit that type."""

    def __init__(self, parent, on_change, on_record):
        super().__init__(parent, padding=8)
        self.on_change = on_change
        self.loading = False
        self.action_var = tk.StringVar(value="None")
        self.value_var = tk.StringVar()
        self.focus_var = tk.BooleanVar(value=True)

        ttk.Label(self, text="Action").grid(row=0, column=0, sticky="w")
        ttk.Combobox(self, textvariable=self.action_var, values=ACTIONS, state="readonly",
                     width=26).grid(row=0, column=1, columnspan=2, sticky="w", pady=2)
        ttk.Label(self, text="Value").grid(row=1, column=0, sticky="w")
        self.value_box = ttk.Combobox(self, textvariable=self.value_var, width=28)
        self.value_box.grid(row=1, column=1, sticky="w", pady=2)
        self.browse_btn = ttk.Button(self, text="Browse", width=8, command=self.browse)
        self.record_btn = ttk.Button(self, text="Record", width=8, command=lambda: on_record(self))
        self.browse_btn.grid(row=1, column=2, padx=(4, 0))
        self.record_btn.grid(row=1, column=2, padx=(4, 0))
        self.focus_chk = ttk.Checkbutton(self, text="Bring to front if already running",
                                         variable=self.focus_var)
        self.focus_chk.grid(row=2, column=0, columnspan=3, sticky="w")
        self.hint = ttk.Label(self, wraplength=340, style="Muted.TLabel")
        self.hint.grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))

        self.action_var.trace_add("write", lambda *_: self._action_changed())
        self.value_var.trace_add("write", lambda *_: self._changed())
        self.focus_var.trace_add("write", lambda *_: self._changed())
        self._update_visual()

    def _update_visual(self):
        action = self.action_var.get()
        self.hint.configure(text=HINTS.get(action, ""))
        self.value_box.configure(values=VALUE_CHOICES.get(action, []))
        for widget, show in ((self.browse_btn, action == "Launch app / file"),
                             (self.record_btn, action == "Hotkey"),
                             (self.focus_chk, action == "Launch app / file")):
            if show:
                widget.grid()
            else:
                widget.grid_remove()

    def _action_changed(self):
        self._update_visual()
        self._changed()

    def _changed(self):
        if not self.loading:
            self.on_change()

    def browse(self):
        path = filedialog.askopenfilename()
        if path:
            self.action_var.set("Launch app / file")
            self.value_var.set(path)

    def set_hotkey(self, combo):
        self.action_var.set("Hotkey")
        self.value_var.set(combo)

    def get(self):
        return {"action": self.action_var.get(), "value": self.value_var.get(),
                "focus": bool(self.focus_var.get())}

    def set(self, act):
        act = norm_act(act)
        self.loading = True
        self.action_var.set(act["action"])
        self.value_var.set(act["value"])
        self.focus_var.set(act["focus"])
        self._update_visual()
        self.loading = False


# =====================================================================
#  The application
# =====================================================================
class App:
    def __init__(self, root, start_hidden=False):
        self.root = root
        root.title(WINDOW_TITLE)
        root.configure(padx=10, pady=10)

        self.settings = dict(DEFAULT_SETTINGS)
        loaded = load_json(SETTINGS_PATH)
        if isinstance(loaded, dict):
            self.settings.update(loaded)
        self.data = self.load_mapping(CONFIG_PATH)
        try:
            self.page = max(1, min(NUM_PAGES, int(self.settings.get("last_page") or 1)))
        except (TypeError, ValueError):
            self.page = 1

        self.sel = "n0"
        self.loading = False
        self.inport = self.outport = None
        self.q = queue.Queue()
        self.btns = {}
        self.led_cache = {}
        self.connected = False
        self.want_connection = bool(self.settings.get("auto_connect"))
        self.lost_flag = False
        self.conn_in_name = ""
        self.showing = False
        self.show_cols, self.show_pos = [], 0
        self.states = {}
        self.photo_image = None
        self.photo_view = None
        self.calib_points = []
        self.recorder = self.rec_dialog = self.rec_target = None
        self.settings_win = None
        self.tray = None
        self.cur_theme = THEMES["Light"]

        self.build_ui()
        self.refresh_ports()
        self.build_pad_area()
        self.apply_theme()
        self.load_editor()
        self.update_connection_ui()
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        try:
            self.tray = Tray(lambda ev: self.q.put(("tray", ev)))
            self.tray.start()
            if not self.tray.ready.wait(3):
                self.tray = None
        except Exception:
            self.tray = None

        if self.want_connection:
            self.try_connect()
        else:
            self.status.set("Not connected - press Connect.")
        self.poll()
        self.tick()
        if start_hidden and self.tray:
            root.withdraw()

    # ------------------------------------------------------------ settings & storage
    def set_setting(self, key, value):
        self.settings[key] = value
        self.save_settings()

    def save_settings(self):
        try:
            save_json(SETTINGS_PATH, self.settings)
        except Exception:
            pass

    def load_mapping(self, path):
        data = load_json(path)
        if data is None:
            return migrate({})
        if path == CONFIG_PATH and not (isinstance(data, dict) and "pages" in data):
            try:  # keep a copy of the old single-page file, just in case
                shutil.copyfile(path, os.path.join(CONFIG_DIR, "mapping.v1.backup.json"))
            except Exception:
                pass
        return migrate(data)

    def save(self, path=CONFIG_PATH):
        try:
            save_json(path, self.data)
        except Exception as e:
            self.status.set(f"Could not save: {e}")

    def open_file(self):
        path = filedialog.askopenfilename(filetypes=[("Mapping", "*.json")])
        if path:
            self.data = self.load_mapping(path)
            self.refresh_all_buttons()
            self.refresh_pages_ui()
            self.load_editor()
            if not self.showing:
                self.render_leds()
            self.save()

    def save_as(self):
        path = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("Mapping", "*.json")])
        if path:
            self.save(path)

    def page_map(self, n=None):
        return self.data["pages"].setdefault(str(n or self.page), {})

    def rec(self, key, page=None):
        return self.data["pages"].get(str(page or self.page), {}).get(key) or {}

    # ------------------------------------------------------------ theme
    def apply_theme(self):
        name = self.theme_var.get()
        t = custom_theme(self.settings.get("custom_bg", "")) if name == "Custom" \
            else THEMES.get(name, THEMES["Light"])
        self.cur_theme = t
        dark = luminance(t["bg"]) < 0.5
        hover = shade(t["btn"], 0.12 if dark else -0.06)

        self.root.configure(bg=t["bg"])
        st = ttk.Style(self.root)
        st.theme_use("clam")
        st.configure(".", background=t["bg"], foreground=t["fg"], bordercolor=t["border"],
                     lightcolor=t["bg"], darkcolor=t["bg"], troughcolor=t["field"],
                     focuscolor=t["btn"], insertcolor=t["fg"], fieldbackground=t["field"])
        st.configure("TButton", background=t["btn"], foreground=t["fg"], lightcolor=t["btn"],
                     darkcolor=t["btn"], bordercolor=t["border"], padding=3)
        st.map("TButton", background=[("active", hover)])
        st.configure("TEntry", fieldbackground=t["field"], foreground=t["fg"], insertcolor=t["fg"],
                     lightcolor=t["field"], darkcolor=t["field"],
                     selectbackground=t["select"], selectforeground=t["fg"])
        st.configure("TCombobox", fieldbackground=t["field"], background=t["btn"], foreground=t["fg"],
                     arrowcolor=t["fg"], lightcolor=t["field"], darkcolor=t["field"],
                     selectbackground=t["field"], selectforeground=t["fg"])
        st.map("TCombobox", fieldbackground=[("readonly", t["field"])],
               foreground=[("readonly", t["fg"])], selectbackground=[("readonly", t["field"])],
               selectforeground=[("readonly", t["fg"])])
        st.configure("TLabelframe", background=t["bg"], bordercolor=t["border"],
                     lightcolor=t["bg"], darkcolor=t["bg"])
        st.configure("TLabelframe.Label", background=t["bg"], foreground=t["fg"])
        st.configure("Muted.TLabel", background=t["bg"], foreground=t["muted"])
        st.configure("TNotebook", background=t["bg"], bordercolor=t["border"],
                     lightcolor=t["bg"], darkcolor=t["bg"])
        st.configure("TNotebook.Tab", background=t["btn"], foreground=t["fg"], padding=(10, 3),
                     lightcolor=t["btn"], darkcolor=t["btn"], bordercolor=t["border"])
        st.map("TNotebook.Tab", background=[("selected", t["field"])], foreground=[("selected", t["fg"])])
        st.configure("TCheckbutton", background=t["bg"], foreground=t["fg"],
                     indicatorbackground=t["field"], indicatorforeground=t["fg"])
        st.map("TCheckbutton", background=[("active", t["bg"])], foreground=[("active", t["fg"])])

        self.root.option_add("*TCombobox*Listbox.background", t["field"])
        self.root.option_add("*TCombobox*Listbox.foreground", t["fg"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", t["select"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", t["fg"])
        self.led_canvas.configure(bg=t["bg"])
        self.theme_widgets(self.root)

        self.settings["theme"] = name
        self.save_settings()

    def theme_widgets(self, top):
        """Colors the plain-Tk widgets (sliders, combobox drop-downs, dialogs) of `top`."""
        t = self.cur_theme
        if isinstance(top, tk.Toplevel):
            top.configure(bg=t["bg"])
        for w in walk(top):
            cls = w.winfo_class()
            try:
                if cls == "Scale":
                    w.configure(bg=t["bg"], fg=t["fg"], troughcolor=t["field"],
                                highlightbackground=t["bg"], activebackground=t["btn"])
                elif cls == "TCombobox":
                    popdown = self.root.tk.call("ttk::combobox::PopdownWindow", str(w))
                    self.root.tk.call(f"{popdown}.f.l", "configure", "-background", t["field"],
                                      "-foreground", t["fg"], "-selectbackground", t["select"],
                                      "-selectforeground", t["fg"])
            except tk.TclError:
                pass

    def on_theme_selected(self):
        if self.theme_var.get() == "Custom" and not self.settings.get("custom_bg"):
            self.pick_background()
            return
        self.apply_theme()

    def pick_background(self):
        current = self.settings.get("custom_bg") or "#2b2b3c"
        picked = colorchooser.askcolor(color=current, parent=self.root, title="Background color")
        if picked and picked[1]:
            self.settings["custom_bg"] = picked[1]
            self.theme_var.set("Custom")
            self.apply_theme()
        elif self.theme_var.get() == "Custom" and not self.settings.get("custom_bg"):
            self.theme_var.set(self.settings.get("theme", "Light"))

    # ------------------------------------------------------------ UI
    def build_ui(self):
        bar = ttk.Frame(self.root)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Label(bar, text="In:").pack(side="left")
        self.in_var = tk.StringVar()
        self.in_box = ttk.Combobox(bar, textvariable=self.in_var, width=20, state="readonly")
        self.in_box.pack(side="left", padx=(2, 8))
        ttk.Label(bar, text="Out:").pack(side="left")
        self.out_var = tk.StringVar()
        self.out_box = ttk.Combobox(bar, textvariable=self.out_var, width=20, state="readonly")
        self.out_box.pack(side="left", padx=(2, 8))
        ttk.Button(bar, text="Refresh", command=self.refresh_ports).pack(side="left")
        self.connect_btn = ttk.Button(bar, text="Connect", width=11, command=self.toggle_connection)
        self.connect_btn.pack(side="left", padx=8)
        self.led_canvas = tk.Canvas(bar, width=16, height=16, highlightthickness=0, bd=0)
        self.led_canvas.pack(side="left")
        self.conn_text = tk.StringVar()
        ttk.Label(bar, textvariable=self.conn_text, width=14).pack(side="left", padx=(4, 0))
        ttk.Button(bar, text="Open...", command=self.open_file).pack(side="right")
        ttk.Button(bar, text="Save as...", command=self.save_as).pack(side="right", padx=4)

        body = ttk.Frame(self.root)
        body.pack()
        self.pad_holder = ttk.Frame(body)
        self.pad_holder.grid(row=0, column=0, sticky="n")

        ed = ttk.LabelFrame(body, text="Selected button", padding=10)
        ed.grid(row=0, column=1, sticky="n", padx=(12, 0))
        self.title_var = tk.StringVar()
        ttk.Label(ed, textvariable=self.title_var, font=("Segoe UI", 10, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        ttk.Label(ed, text="Label").grid(row=1, column=0, sticky="w")
        self.label_var = tk.StringVar()
        ttk.Entry(ed, textvariable=self.label_var, width=40).grid(row=1, column=1, pady=2, sticky="w")

        self.notebook = ttk.Notebook(ed)
        self.notebook.grid(row=2, column=0, columnspan=2, pady=(8, 0))
        self.editors = {}
        for name, title in (("press", "Press"), ("hold", "Hold"), ("double", "Double-press")):
            editor = ActionEditor(self.notebook, self.on_change, self.record_hotkey)
            self.notebook.add(editor, text=title)
            self.editors[name] = editor
        color_tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(color_tab, text="Color")
        self.build_color_tab(color_tab)

        ttk.Label(ed, style="Muted.TLabel", wraplength=380,
                  text="Press = quick tap, Hold = keep it pressed, Double-press = tap twice. "
                       "Leave Hold and Double-press empty to keep taps instant.").grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
        actions = ttk.Frame(ed)
        actions.grid(row=4, column=0, columnspan=2, pady=(8, 0), sticky="w")
        ttk.Button(actions, text="Test action", command=self.test_action).pack(side="left")
        ttk.Button(actions, text="Clear button", command=self.clear_button).pack(side="left", padx=6)
        self.label_var.trace_add("write", lambda *_: self.on_change())

        self.status = tk.StringVar(value="Starting...")
        foot = ttk.Frame(self.root)
        foot.pack(fill="x", pady=(8, 0))
        ttk.Label(foot, textvariable=self.status).pack(side="left")
        ttk.Button(foot, text="Settings...", command=self.open_settings).pack(side="right")
        self.photo_btn = ttk.Button(foot, text="Photo...", command=self.show_photo_menu)
        self.photo_btn.pack(side="right", padx=4)
        ttk.Button(foot, text="Background...", command=self.pick_background).pack(side="right")
        self.theme_var = tk.StringVar(value=self.settings.get("theme", "Light"))
        theme_box = ttk.Combobox(foot, textvariable=self.theme_var, values=list(THEMES) + ["Custom"],
                                 state="readonly", width=10)
        theme_box.pack(side="right", padx=4)
        theme_box.bind("<<ComboboxSelected>>", lambda _e: self.on_theme_selected())
        ttk.Label(foot, text="Theme").pack(side="right")

        self.photo_pct = tk.IntVar(value=int(self.settings.get("photo_size_pct", 85)))
        self.photo_menu = tk.Menu(self.root, tearoff=0)
        self.photo_menu.add_command(label="Load photo...", command=self.photo_load)
        self.photo_menu.add_command(label="Recalibrate", command=self.photo_recalibrate)
        self.photo_menu.add_separator()
        for pct in (60, 70, 80, 90, 100):
            self.photo_menu.add_radiobutton(label=f"Button size {pct}%", variable=self.photo_pct,
                                            value=pct, command=self.photo_resize)
        self.photo_menu.add_separator()
        self.photo_menu.add_command(label="Remove photo", command=self.photo_remove)

    def build_color_tab(self, ct):
        self.red_var = tk.IntVar()
        self.green_var = tk.IntVar()
        row = ttk.Frame(ct)
        row.grid(row=0, column=0, sticky="w")
        ttk.Label(row, text="Red").pack(side="left")
        tk.Scale(row, from_=0, to=3, orient="horizontal", variable=self.red_var, length=120,
                 command=lambda _=None: self.on_change()).pack(side="left", padx=(2, 14))
        ttk.Label(row, text="Green").pack(side="left")
        tk.Scale(row, from_=0, to=3, orient="horizontal", variable=self.green_var, length=120,
                 command=lambda _=None: self.on_change()).pack(side="left", padx=2)

        presets = ttk.Frame(ct)
        presets.grid(row=1, column=0, pady=6, sticky="w")
        for i, (name, r, g) in enumerate(PRESETS):
            tk.Button(presets, text=name, bg=display_color(r, g), fg="white" if (r + g) < 4 else "black",
                      width=8, relief="flat",
                      command=lambda r=r, g=g: self.set_color(r, g)).grid(row=i // 3, column=i % 3, padx=2, pady=2)

        ttk.Label(ct, text="All 16 LED colors  (rows = red 0-3, columns = green 0-3)").grid(
            row=2, column=0, sticky="w", pady=(4, 0))
        palette = ttk.Frame(ct)
        palette.grid(row=3, column=0, pady=4, sticky="w")
        for r in range(4):
            for g in range(4):
                bright = r + g >= 3
                tk.Button(palette, text="Off" if r + g == 0 else f"R{r} G{g}", width=7, relief="flat",
                          bg=display_color(r, g), activebackground=display_color(r, g),
                          fg="black" if bright else "white",
                          command=lambda r=r, g=g: self.set_color(r, g)).grid(row=r, column=g, padx=2, pady=1)

        hexf = ttk.Frame(ct)
        hexf.grid(row=4, column=0, sticky="w", pady=(6, 0))
        ttk.Label(hexf, text="Hex color").pack(side="left")
        self.hex_var = tk.StringVar(value="#")
        hex_entry = ttk.Entry(hexf, textvariable=self.hex_var, width=10)
        hex_entry.pack(side="left", padx=4)
        hex_entry.bind("<Return>", lambda _e: self.apply_hex())
        ttk.Button(hexf, text="Apply", width=7, command=self.apply_hex).pack(side="left")
        ttk.Button(hexf, text="Pick...", width=7, command=self.pick_color).pack(side="left", padx=4)
        self.hex_note = tk.StringVar()
        ttk.Label(ct, textvariable=self.hex_note, wraplength=380, style="Muted.TLabel").grid(
            row=5, column=0, sticky="w", pady=(2, 0))

    # ------------------------------------------------------------ pad area (normal or photo)
    def build_pad_area(self):
        for w in self.pad_holder.winfo_children():
            w.destroy()
        self.btns.clear()
        self.photo_view = None
        photo = self.settings.get("photo")
        if photo and photo.get("path") and self.load_photo_image(photo["path"]):
            self.build_photo_pads(photo)
        else:
            self.build_grid_pads()
        self.refresh_all_buttons()
        self.refresh_pages_ui()
        if self.sel in self.btns:
            self.btns[self.sel].configure(highlightbackground="white")

    def build_grid_pads(self):
        pad = tk.Frame(self.pad_holder, bg=PadButton.OUTER, padx=12, pady=12)
        pad.pack()
        for i in range(8):
            self.make_btn(pad, f"c{104 + i}", 0, i, small=True)
        for r in range(8):
            for c in range(9):
                self.make_btn(pad, f"n{16 * r + c}", r + 1, c)

    def make_btn(self, parent, key, row, col, small=False):
        shape = "round" if (small or col == 8) else "square"
        b = PadButton(parent, shape, 50, self.button_command(key))
        b.grid(row=row, column=col, padx=2, pady=(2, 8) if small else 2)
        self.btns[key] = b

    def button_command(self, key):
        if key.startswith("c"):
            return lambda n=int(key[1:]) - 103: self.goto_page(n)
        return lambda k=key: self.select(k)

    def load_photo_image(self, path):
        try:
            img = tk.PhotoImage(file=path)
        except tk.TclError:
            return False
        w, h = img.width(), img.height()
        factor = max(1, -(-w // 720), -(-h // 560))
        if factor > 1:
            img = img.subsample(factor)
        self.photo_image = img
        return True

    def build_photo_pads(self, photo):
        view = PhotoView(self.pad_holder, self.photo_image)
        view.pack()
        self.photo_view = view
        p1, p2 = photo.get("p1"), photo.get("p2")
        if not (p1 and p2):
            self.begin_calibration()
            return
        dx, dy = (p2[0] - p1[0]) / 7, (p2[1] - p1[1]) / 7
        half = min(abs(dx), abs(dy)) * int(self.settings.get("photo_size_pct", 85)) / 200
        for r in range(8):
            for c in range(9):
                key = f"n{16 * r + c}"
                pad = PhotoPad(view, key, p1[0] + c * dx, p1[1] + r * dy, half,
                               "round" if c == 8 else "square", self.button_command(key))
                view.register(pad)
                self.btns[key] = pad
        for i in range(8):
            key = f"c{104 + i}"
            pad = PhotoPad(view, key, p1[0] + i * dx, p1[1] - dy, half, "round", self.button_command(key))
            view.register(pad)
            self.btns[key] = pad

    def begin_calibration(self):
        view = self.photo_view
        self.calib_points = []
        view.banner("Click the CENTER of the TOP-LEFT pad (row 1, column 1)")
        self.status.set("Photo setup: follow the instructions shown on the photo.")

        def on_click(x, y):
            self.calib_points.append((x, y))
            view.marker(x, y)
            if len(self.calib_points) == 1:
                view.banner("Now click the CENTER of the BOTTOM-RIGHT pad (row 8, column 8)")
                return
            (x1, y1), (x2, y2) = self.calib_points
            if abs(x2 - x1) < 30 or abs(y2 - y1) < 30 or x2 < x1 or y2 < y1:
                self.calib_points = []
                view.delete("marker")
                view.banner("That didn't look right. Click the CENTER of the TOP-LEFT pad again.")
                return
            self.settings["photo"]["p1"] = [x1, y1]
            self.settings["photo"]["p2"] = [x2, y2]
            self.save_settings()
            view.calib = None
            self.build_pad_area()
            self.status.set("Photo ready. Use Photo... > Button size to adjust the overlay.")

        view.calib = on_click

    def show_photo_menu(self):
        x, y = self.photo_btn.winfo_rootx(), self.photo_btn.winfo_rooty() + self.photo_btn.winfo_height()
        try:
            self.photo_menu.tk_popup(x, y)
        finally:
            self.photo_menu.grab_release()

    def photo_load(self):
        path = filedialog.askopenfilename(title="Choose a photo of your Launchpad (PNG or GIF)",
                                          filetypes=[("PNG / GIF images", "*.png *.gif"), ("All files", "*.*")])
        if not path:
            return
        if not self.load_photo_image(path):
            messagebox.showerror("Photo", "Could not open that image.\n\nPlease use a PNG or GIF file "
                                          "(you can convert a JPG to PNG with Paint).")
            return
        self.settings["photo"] = {"path": path, "p1": None, "p2": None}
        self.save_settings()
        self.build_pad_area()

    def photo_recalibrate(self):
        photo = self.settings.get("photo")
        if not photo:
            messagebox.showinfo("Photo", "Load a photo first.")
            return
        photo["p1"] = photo["p2"] = None
        self.build_pad_area()

    def photo_resize(self):
        self.set_setting("photo_size_pct", int(self.photo_pct.get()))
        if self.settings.get("photo"):
            self.build_pad_area()

    def photo_remove(self):
        self.settings["photo"] = None
        self.save_settings()
        self.build_pad_area()

    # ------------------------------------------------------------ editing
    def key_name(self, key):
        if key.startswith("c"):
            return f"Page button {int(key[1:]) - 103}"
        n = int(key[1:])
        row, col = divmod(n, 16)
        return f"Side button {row + 1}" if col == 8 else f"Pad  row {row + 1}, column {col + 1}"

    def select(self, key):
        if self.sel in self.btns:
            self.btns[self.sel].configure(highlightbackground=PadButton.OUTER)
        self.sel = key
        if key in self.btns:
            self.btns[key].configure(highlightbackground="white")
        self.load_editor()

    def load_editor(self):
        r = self.rec(self.sel)
        self.loading = True
        self.title_var.set(f"Page {self.page}  -  {self.key_name(self.sel)}")
        self.label_var.set(r.get("label", ""))
        self.red_var.set(r.get("red", 0))
        self.green_var.set(r.get("green", 0))
        for name, editor in self.editors.items():
            editor.set(r.get(name))
        self.hex_note.set("")
        self.loading = False

    def on_change(self):
        if self.loading or not self.sel:
            return
        rec = {"label": self.label_var.get(), "red": int(self.red_var.get()),
               "green": int(self.green_var.get()),
               "press": self.editors["press"].get(), "hold": self.editors["hold"].get(),
               "double": self.editors["double"].get()}
        page = self.page_map()
        if rec_is_empty(rec):
            page.pop(self.sel, None)
        else:
            page[self.sel] = rec
        self.refresh_button(self.sel)
        self.refresh_pages_ui()
        if not self.showing:
            self.render_leds()
        self.save()

    def set_color(self, r, g):
        self.red_var.set(r)
        self.green_var.set(g)
        self.on_change()

    def apply_hex(self):
        try:
            r, g = nearest_led(self.hex_var.get())
        except ValueError:
            self.hex_note.set("Enter a hex color such as #FF8800.")
            return
        self.hex_note.set(f"Closest LED color: red {r}, green {g}. The pads have no blue LEDs, "
                          "so only red/green mixes can be shown.")
        self.set_color(r, g)

    def pick_color(self):
        current = display_color(int(self.red_var.get()), int(self.green_var.get()))
        picked = colorchooser.askcolor(color=current, parent=self.root, title="Pick a color")
        if picked and picked[1]:
            self.hex_var.set(picked[1])
            self.apply_hex()

    def clear_button(self):
        self.page_map().pop(self.sel, None)
        self.refresh_button(self.sel)
        self.refresh_pages_ui()
        self.load_editor()
        if not self.showing:
            self.render_leds()
        self.save()

    def test_action(self):
        index = self.notebook.index(self.notebook.select())
        name = ("press", "hold", "double", "press")[index]
        self.run_action(self.rec(self.sel).get(name))

    # ------------------------------------------------------------ refreshing the pads
    def led_color(self, key):
        if key.startswith("c"):
            return self.page_color(int(key[1:]) - 103)
        r = self.rec(key)
        return r.get("red", 0), r.get("green", 0)

    def page_color(self, n):
        if n == self.page:
            return (3, 3)
        return (0, 1) if self.data["pages"].get(str(n)) else (0, 0)

    def refresh_button(self, key):
        b = self.btns.get(key)
        if not b:
            return
        r = self.rec(key)
        red, green = r.get("red", 0), r.get("green", 0)
        color = display_color(red, green)
        fg = "black" if red + green >= 3 else "white"
        b.configure(text=r.get("label", ""), bg=color, activebackground=color, fg=fg, activeforeground=fg)

    def refresh_all_buttons(self):
        for key in PAD_KEYS:
            self.refresh_button(key)

    def refresh_pages_ui(self):
        for i in range(NUM_PAGES):
            b = self.btns.get(f"c{104 + i}")
            if not b:
                continue
            current = (i + 1 == self.page)
            red, green = self.page_color(i + 1)
            b.configure(text=str(i + 1), bg=display_color(red, green), fg="black" if current else "white",
                        highlightbackground="white" if current else PadButton.OUTER)

    def goto_page(self, n):
        n = max(1, min(NUM_PAGES, int(n)))
        if n != self.page:
            self.page = n
            self.set_setting("last_page", n)
            self.refresh_all_buttons()
            self.load_editor()
            self.status.set(f"Page {n}")
        self.refresh_pages_ui()
        if not self.showing:
            self.render_leds()

    # ------------------------------------------------------------ LEDs on the device
    def set_led(self, key, red, green):
        if not self.outport:
            return
        if self.led_cache.get(key, (0, 0)) == (red, green):
            return
        vel = led_velocity(red, green)
        if key.startswith("c"):
            self.outport.send(0xB0, int(key[1:]), vel)
        else:
            self.outport.send(0x90, int(key[1:]), vel)
        self.led_cache[key] = (red, green)

    def render_leds(self):
        if not self.outport:
            return
        for key in PAD_KEYS + TOP_KEYS:
            self.set_led(key, *self.led_color(key))

    def reset_leds(self):
        if self.outport:
            self.outport.send(0xB0, 0, 0)
        self.led_cache.clear()

    # ------------------------------------------------------------ connection (auto-reconnect)
    def refresh_ports(self):
        try:
            ins, outs = list_midi_inputs(), list_midi_outputs()
        except Exception as e:
            self.status.set(f"MIDI error: {e}")
            return
        self.in_box["values"], self.out_box["values"] = ins, outs
        for var, names in ((self.in_var, ins), (self.out_var, outs)):
            if var.get() not in names:
                var.set(next((n for n in names if "launchpad" in n.lower()), ""))

    def update_connection_ui(self):
        c = self.led_canvas
        c.delete("all")
        c.create_oval(2, 2, 14, 14, fill="#22c55e" if self.connected else "#ef4444", outline="")
        if self.connected:
            self.conn_text.set("Connected")
        else:
            self.conn_text.set("Searching..." if self.want_connection else "Disconnected")
        self.connect_btn.configure(text="Disconnect" if self.want_connection else "Connect")
        if self.tray:
            self.tray.set_connected(self.connected)

    def toggle_connection(self):
        if self.want_connection:
            self.want_connection = False
            self.disconnect("Disconnected.")
        else:
            self.want_connection = True
            self.try_connect()
        self.update_connection_ui()

    def try_connect(self):
        self.refresh_ports()
        i, o = self.in_box.current(), self.out_box.current()
        if i < 0 or o < 0:
            self.status.set("Waiting for the Launchpad - plug it in (or choose ports and press Connect).")
            self.update_connection_ui()
            return False
        try:
            out = MidiOut(o)
            try:
                inp = MidiIn(i, self.q.put)
            except Exception:
                out.close()
                raise
        except Exception as e:
            self.status.set(f"Could not open the Launchpad: {e}")
            return False
        self.outport, self.inport = out, inp
        self.connected = True
        self.lost_flag = False
        self.conn_in_name = self.in_var.get()
        self.states.clear()
        self.reset_leds()
        self.update_connection_ui()
        self.status.set(f"Connected to {self.conn_in_name}.")
        self.start_show()
        if not self.showing:
            self.render_leds()
        return True

    def disconnect(self, message=None):
        try:
            if self.outport and not self.outport.failed:
                self.outport.send(0xB0, 0, 0)
        except Exception:
            pass
        for port in (self.inport, self.outport):
            try:
                if port:
                    port.close()
            except Exception:
                pass
        self.inport = self.outport = None
        self.connected = False
        self.showing = False
        self.led_cache.clear()
        for st in self.states.values():
            for timer in (st.get("hold_t"), st.get("tap_t")):
                if timer:
                    self.root.after_cancel(timer)
        self.states.clear()
        self.update_connection_ui()
        if message:
            self.status.set(message)

    def tick(self):
        try:
            if self.connected:
                gone = self.lost_flag or (self.outport and self.outport.failed)
                if not gone and self.conn_in_name:
                    gone = self.conn_in_name not in list_midi_inputs()
                if gone:
                    self.disconnect("Launchpad disconnected - waiting for it to come back...")
            elif self.want_connection:
                self.try_connect()
        except Exception as e:
            self.status.set(f"Connection check failed: {e}")
        self.root.after(1500, self.tick)

    # ------------------------------------------------------------ incoming events
    def poll(self):
        while True:
            try:
                ev = self.q.get_nowait()
            except queue.Empty:
                break
            try:
                self.handle_event(ev)
            except Exception as e:
                self.status.set(f"Error: {e}")
        self.root.after(10, self.poll)

    def handle_event(self, ev):
        kind = ev[0]
        if kind == "midi":
            self.on_midi(ev[1], ev[2], ev[3])
        elif kind == "closed":
            self.lost_flag = True
        elif kind == "status":
            self.status.set(ev[1])
        elif kind == "tray":
            self.on_tray(ev[1])
        elif kind == "rec":
            self.on_recorded(ev[1])

    def on_midi(self, status, d1, d2):
        kind = status & 0xF0
        key, pressed = None, False
        if kind == 0x90:
            key, pressed = f"n{d1}", d2 > 0
        elif kind == 0x80:
            key, pressed = f"n{d1}", False
        elif kind == 0xB0 and 104 <= d1 <= 111:
            key, pressed = f"c{d1}", d2 > 0
        if key is None:
            return
        if key.startswith("c"):
            if pressed:
                self.goto_page(d1 - 103)
            return
        if key not in PAD_KEYS or self.showing:
            return
        if pressed:
            self.on_press(key)
        else:
            self.on_release(key)

    # ------------------------------------------------------------ press / hold / double-press
    def state(self, key):
        return self.states.setdefault(key, {"hold_t": None, "tap_t": None})

    def flash(self, key, on):
        r, g = self.led_color(key)
        b = self.btns.get(key)
        if on:
            fr, fg = (0, 3) if (r, g) == (3, 3) else (3, 3)
            if b:
                b.configure(bg=display_color(fr, fg), fg="black")
            self.set_led(key, fr, fg)
        else:
            self.refresh_button(key)
            self.set_led(key, r, g)

    def on_press(self, key):
        rec = self.rec(key)
        self.status.set(f"Pressed: page {self.page}, {self.key_name(key)}")
        if self.settings.get("press_feedback"):
            self.flash(key, True)
        st = self.state(key)
        if has_act(rec.get("hold")):
            st["hold_t"] = self.root.after(int(self.settings.get("hold_ms", 500)),
                                           lambda k=key: self.fire_hold(k))
        else:
            self.tap(key)

    def on_release(self, key):
        st = self.state(key)
        if self.settings.get("press_feedback"):
            self.flash(key, False)
        if st["hold_t"] is not None:  # released before the hold time: it was a tap
            self.root.after_cancel(st["hold_t"])
            st["hold_t"] = None
            self.tap(key)

    def fire_hold(self, key):
        self.state(key)["hold_t"] = None
        self.run_action(self.rec(key).get("hold"))

    def tap(self, key):
        st = self.state(key)
        rec = self.rec(key)
        if has_act(rec.get("double")):
            if st["tap_t"] is not None:  # second tap inside the window
                self.root.after_cancel(st["tap_t"])
                st["tap_t"] = None
                self.run_action(rec.get("double"))
            else:
                st["tap_t"] = self.root.after(int(self.settings.get("double_ms", 350)),
                                              lambda k=key: self.single(k))
        else:
            self.run_action(rec.get("press"))

    def single(self, key):
        self.state(key)["tap_t"] = None
        self.run_action(self.rec(key).get("press"))

    # ------------------------------------------------------------ actions
    def run_action(self, act):
        if not has_act(act):
            return
        action, raw = act["action"], act["value"]
        value = raw.strip()
        try:
            if action == "Hotkey":
                send_hotkey(value)
            elif action == "Launch app / file":
                launch(value, bool(act.get("focus", True)))
            elif action == "Open URL":
                webbrowser.open(value if "://" in value else "https://" + value)
            elif action == "Type text":
                self.root.clipboard_clear()
                self.root.clipboard_append(raw)
                self.root.update()
                time.sleep(0.05)
                send_hotkey("ctrl+v")
            elif action == "Run command":
                subprocess.Popen(value, shell=True)
            elif action == "Volume":
                self.in_background(lambda: do_volume(value))
            elif action == "Brightness":
                parse_level(value)  # validate now so mistakes show up immediately
                self.in_background(lambda: set_brightness(value))
            elif action == "Go to page":
                v = value.lower()
                if v == "next":
                    self.goto_page(self.page % NUM_PAGES + 1)
                elif v == "prev":
                    self.goto_page((self.page - 2) % NUM_PAGES + 1)
                else:
                    self.goto_page(int(v))
        except Exception as e:
            self.status.set(f"Action failed: {e}")

    def in_background(self, fn):
        def work():
            try:
                msg = fn()
                if msg:
                    self.q.put(("status", msg))
            except Exception as e:
                self.q.put(("status", f"Action failed: {e}"))
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ startup light show
    def start_show(self, text=None, force=False):
        if not self.outport or self.showing:
            return
        if not force and not self.settings.get("show_enabled"):
            return
        text = self.settings.get("show_text", "") if text is None else text
        if not clean_text(text).strip():
            return
        self.show_cols, self.show_pos = text_columns(text), 0
        self.showing = True
        self.reset_leds()
        self.show_step()

    def show_step(self):
        if not self.showing or not self.outport:
            self.showing = False
            return
        cols, pos = self.show_cols, self.show_pos
        if pos > len(cols) - 8:
            self.showing = False
            self.reset_leds()
            self.render_leds()
            return
        name = self.settings.get("show_color", "Amber")
        for c in range(8):
            column = cols[pos + c]
            for r in range(7):
                if column[r]:
                    color = RAINBOW[(pos + c) % len(RAINBOW)] if name == "Rainbow" \
                        else SHOW_COLORS.get(name, (3, 3))
                else:
                    color = (0, 0)
                self.set_led(f"n{16 * r + c}", *color)
        self.show_pos += 1
        self.root.after(int(self.settings.get("show_speed", 90)), self.show_step)

    # ------------------------------------------------------------ hotkey recorder
    def record_hotkey(self, editor):
        if self.recorder:
            return
        self.rec_target = editor
        win = tk.Toplevel(self.root)
        win.title("Record hotkey")
        win.resizable(False, False)
        win.transient(self.root)
        ttk.Label(win, text="Press your shortcut now...\n(Esc cancels)", justify="center",
                  padding=28).pack()
        self.rec_dialog = win
        self.theme_widgets(win)

        def cancel():
            if self.recorder:
                self.recorder.cancel = self.recorder.finished = True

        win.protocol("WM_DELETE_WINDOW", cancel)
        self.recorder = HotkeyRecorder(lambda combo: self.q.put(("rec", combo)))
        self.recorder.start()

    def on_recorded(self, combo):
        self.recorder = None
        if self.rec_dialog is not None:
            self.rec_dialog.destroy()
            self.rec_dialog = None
        if combo and self.rec_target is not None:
            self.rec_target.set_hotkey(combo)
        self.rec_target = None

    # ------------------------------------------------------------ settings dialog
    def open_settings(self):
        if self.settings_win is not None and self.settings_win.winfo_exists():
            self.settings_win.lift()
            return
        win = tk.Toplevel(self.root)
        win.title("Settings")
        win.resizable(False, False)
        self.settings_win = win
        f = ttk.Frame(win, padding=14)
        f.pack(fill="both", expand=True)

        def check(parent, text, key):
            var = tk.BooleanVar(value=bool(self.settings.get(key)))
            ttk.Checkbutton(parent, text=text, variable=var,
                            command=lambda: self.set_setting(key, var.get())).pack(anchor="w", pady=1)

        def slider(parent, text, key, lo, hi):
            row = ttk.Frame(parent)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=text, width=26).pack(side="left")
            s = tk.Scale(row, from_=lo, to=hi, orient="horizontal", length=170, resolution=10)
            s.set(int(self.settings.get(key, lo)))
            s.configure(command=lambda v: self.set_setting(key, int(float(v))))
            s.pack(side="left")
            ttk.Label(row, text="ms").pack(side="left", padx=4)

        general = ttk.LabelFrame(f, text="General", padding=8)
        general.pack(fill="x")
        check(general, "Connect to the Launchpad automatically when the app starts", "auto_connect")
        auto_var = tk.BooleanVar(value=autostart_enabled())

        def auto_changed():
            try:
                set_autostart(auto_var.get())
            except Exception as e:
                messagebox.showerror("Start with Windows", f"Could not change this setting:\n{e}")
                auto_var.set(autostart_enabled())

        ttk.Checkbutton(general, text="Start with Windows (opens hidden in the tray)",
                        variable=auto_var, command=auto_changed).pack(anchor="w", pady=1)
        check(general, "Close button hides the window to the tray (Quit is in the tray menu)", "close_to_tray")
        check(general, "Flash pads when they are pressed", "press_feedback")

        gestures = ttk.LabelFrame(f, text="Hold and double-press timing", padding=8)
        gestures.pack(fill="x", pady=(10, 0))
        slider(gestures, "Hold time", "hold_ms", 200, 1500)
        slider(gestures, "Double-press window", "double_ms", 150, 700)

        show = ttk.LabelFrame(f, text="Startup light show", padding=8)
        show.pack(fill="x", pady=(10, 0))
        check(show, "Play when the Launchpad connects", "show_enabled")
        row = ttk.Frame(show)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text="Text", width=8).pack(side="left")
        text_var = tk.StringVar(value=self.settings.get("show_text", ""))
        text_var.trace_add("write", lambda *_: self.set_setting("show_text", text_var.get()))
        ttk.Entry(row, textvariable=text_var, width=34).pack(side="left")
        row = ttk.Frame(show)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text="Color", width=8).pack(side="left")
        color_var = tk.StringVar(value=self.settings.get("show_color", "Amber"))
        color_box = ttk.Combobox(row, textvariable=color_var, values=list(SHOW_COLORS) + ["Rainbow"],
                                 state="readonly", width=12)
        color_box.pack(side="left")
        color_box.bind("<<ComboboxSelected>>", lambda _e: self.set_setting("show_color", color_var.get()))
        slider(show, "Time per step (lower = faster)", "show_speed", 30, 250)

        def preview():
            if not self.outport:
                self.status.set("Connect the Launchpad to preview the light show.")
            else:
                self.start_show(text_var.get(), force=True)

        ttk.Button(show, text="Preview now", command=preview).pack(anchor="w", pady=(6, 0))
        ttk.Label(show, style="Muted.TLabel", wraplength=420,
                  text="Supports letters (Latin and Greek capitals), digits and ! ? . , - : + ' / ( ) * and the heart symbol. "
                       "Accents are ignored.").pack(anchor="w", pady=(4, 0))
        ttk.Button(f, text="Close", command=win.destroy).pack(anchor="e", pady=(12, 0))
        self.theme_widgets(win)

    # ------------------------------------------------------------ tray and window
    def on_tray(self, event):
        if event == "toggle":
            self.toggle_window()
        elif event == "connect":
            self.toggle_connection()
        elif event == "quit":
            self.quit_app()

    def toggle_window(self):
        if self.root.state() in ("withdrawn", "iconic"):
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        else:
            self.root.withdraw()

    def on_close(self):
        if self.tray and self.settings.get("close_to_tray"):
            self.root.withdraw()
        else:
            self.quit_app()

    def quit_app(self):
        try:
            self.save()
            self.save_settings()
        except Exception:
            pass
        self.want_connection = False
        self.disconnect(None)
        if self.tray:
            self.tray.stop()
        self.root.destroy()


def main():
    if os.name != "nt":
        raise SystemExit("This app works on Windows only.")
    if another_instance_running():
        hidden = tk.Tk()
        hidden.withdraw()
        messagebox.showinfo("Launchpad Mapper", "Launchpad Mapper is already running.\n\n"
                                                "Look for its icon in the system tray (near the clock).")
        return
    root = tk.Tk()
    App(root, start_hidden="--tray" in sys.argv)
    root.mainloop()


if __name__ == "__main__":
    main()
