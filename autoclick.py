# -*- coding: utf-8 -*-
"""
Win32 鼠标点击 / 窗口穿透状态探测 / 配置持久化   (仅标准库)

BongoCat 是透明穿透窗口: TransparentWindow.Update() 每帧执行
    SetClickthrough(!MouseOnObject())
而 SetClickthrough(true) 就是给窗口加上 WS_EX_TRANSPARENT。
=> 读窗口 ex-style 的 WS_EX_TRANSPARENT 位, 就能确切知道
   "此刻光标位置能不能点中游戏里的东西", 不用猜延时。
"""
import os, sys, json, time, ctypes
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)


def _enable_dpi_awareness():
    """必须在任何窗口/光标 API 之前调用。
    否则在多显示器混合 DPI 环境下, GetCursorPos / GetSystemMetrics / SendInput
    拿到的都是被系统缩放过的虚拟坐标, 与 DPI 感知的 Unity 窗口对不上,
    表现为「光标移过去了但位置不对, 点不中东西」。"""
    for ctx in (-4, -3):          # PER_MONITOR_AWARE_V2, PER_MONITOR_AWARE
        try:
            if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(ctx)):
                return "PerMonitorV2" if ctx == -4 else "PerMonitor"
        except Exception:
            pass
    try:
        shcore = ctypes.WinDLL("shcore")
        if shcore.SetProcessDpiAwareness(2) == 0:
            return "PerMonitor(shcore)"
    except Exception:
        pass
    try:
        if user32.SetProcessDPIAware():
            return "System"
    except Exception:
        pass
    return "FAILED"


DPI_MODE = _enable_dpi_awareness()

# 打包成 exe (PyInstaller) 后 __file__ 指向临时解包目录, 配置必须跟着 exe 走
if getattr(sys, "frozen", False):
    _BASE = os.path.dirname(sys.executable)
else:
    _BASE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(_BASE, "config.json")

GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x20
INPUT_MOUSE = 0
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000
SM_XV, SM_YV, SM_CXV, SM_CYV = 76, 77, 78, 79


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _IU(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _IU)]


def get_cursor():
    p = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return int(p.x), int(p.y)


def _send(flags, ax=0, ay=0):
    mi = MOUSEINPUT(ax, ay, 0, flags, 0, None)
    user32.SendInput(1, ctypes.byref(INPUT(INPUT_MOUSE, _IU(mi))), ctypes.sizeof(INPUT))


def _raw_move(x, y):
    vx = user32.GetSystemMetrics(SM_XV)
    vy = user32.GetSystemMetrics(SM_YV)
    vw = max(user32.GetSystemMetrics(SM_CXV) - 1, 1)
    vh = max(user32.GetSystemMetrics(SM_CYV) - 1, 1)
    f = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
    _send(f, int(round((x - vx) * 65535.0 / vw)), int(round((y - vy) * 65535.0 / vh)))


def move_to(x, y, tries=4):
    """移动光标到虚拟桌面绝对坐标, 移动后回读校验并纠偏。
    绝对坐标换算受 DPI / 多屏排布影响, 闭环纠偏比死磕公式可靠。"""
    tx, ty = int(x), int(y)
    _raw_move(tx, ty)
    ax, ay = tx, ty
    for _ in range(tries):
        time.sleep(0.012)
        gx, gy = get_cursor()
        dx, dy = tx - gx, ty - gy
        if dx == 0 and dy == 0:
            return True
        ax += dx
        ay += dy
        _raw_move(ax, ay)
    return get_cursor() == (tx, ty)


def press():
    _send(MOUSEEVENTF_LEFTDOWN)
    time.sleep(0.05)
    _send(MOUSEEVENTF_LEFTUP)


def find_hwnd(pid):
    """找出该进程可见的 Unity 主窗口"""
    found = []
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(h, _):
        p = wintypes.DWORD()
        user32.GetWindowThreadProcessId(h, ctypes.byref(p))
        if p.value == pid and user32.IsWindowVisible(h):
            cls = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(h, cls, 64)
            if "Unity" in cls.value:
                found.append(h)
        return True

    user32.EnumWindows(proto(cb), 0)
    return found[0] if found else None


def is_clickthrough(hwnd):
    return bool(user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TRANSPARENT)


def hoverable(hwnd, x, y, dwell=0.09):
    """把光标放到 (x,y), 等游戏处理一帧, 返回它是否认为这里可点"""
    move_to(x, y)
    time.sleep(dwell)
    return not is_clickthrough(hwnd)


def smart_click(hwnd, x, y, timeout=0.8, radius=0, step=12, restore=True):
    """
    先确认游戏已摘掉 WS_EX_TRANSPARENT 再按下, 避免点穿到桌面。
    radius>0 时, 原位置点不中就在附近螺旋找一个能点的点。
    返回 (是否点了, 实际点击坐标 or None)
    """
    old = get_cursor()
    try:
        move_to(x, y)
        t0 = time.time()
        while time.time() - t0 < timeout:
            if not is_clickthrough(hwnd):
                press()
                return True, (x, y)
            time.sleep(0.02)
        if radius > 0:
            for r in range(step, radius + 1, step):
                for dx, dy in ((0, -r), (r, 0), (0, r), (-r, 0),
                               (r, -r), (r, r), (-r, r), (-r, -r)):
                    if hoverable(hwnd, x + dx, y + dy):
                        press()
                        return True, (x + dx, y + dy)
        return False, None
    finally:
        if restore:
            move_to(old[0], old[1])


def scan_clickable(hwnd, rect, step=40, dwell=0.05, restore=True):
    """在窗口范围内网格扫描, 返回所有游戏认为可点的坐标"""
    l, t, w, h = rect
    old = get_cursor()
    hits = []
    try:
        for y in range(t, t + h, step):
            for x in range(l, l + w, step):
                if hoverable(hwnd, x, y, dwell):
                    hits.append((x, y))
    finally:
        if restore:
            move_to(old[0], old[1])
    return hits


def window_rect(hwnd):
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return (r.left, r.top, r.right - r.left, r.bottom - r.top)


def load_cfg():
    try:
        with open(CFG, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cfg(d):
    try:
        with open(CFG, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def cluster(points, gap=80):
    """把散点按邻近度聚成若干簇, 返回 [(中心x, 中心y, 点数), ...] 按点数降序"""
    pts = list(points)
    out = []
    while pts:
        seed = pts.pop(0)
        grp = [seed]
        changed = True
        while changed:
            changed = False
            for p in pts[:]:
                if any(abs(p[0] - q[0]) <= gap and abs(p[1] - q[1]) <= gap for q in grp):
                    grp.append(p)
                    pts.remove(p)
                    changed = True
        cx = sum(p[0] for p in grp) // len(grp)
        cy = sum(p[1] for p in grp) // len(grp)
        out.append((cx, cy, len(grp)))
    out.sort(key=lambda c: -c[2])
    return out


def scan_progress(hwnd, rect, step=60, dwell=0.05, on_row=None, stop=None):
    """逐行扫描可点区域, on_row(y, 行字符串) 用于回报进度"""
    l, t, w, h = rect
    old = get_cursor()
    hits = []
    try:
        for y in range(t, t + h, step):
            row = ""
            for x in range(l, l + w, step):
                if stop and stop():
                    return hits
                move_to(x, y)
                time.sleep(dwell)
                ok = not is_clickthrough(hwnd)
                row += "#" if ok else "."
                if ok:
                    hits.append((x, y))
            if on_row:
                on_row(y, row)
    finally:
        move_to(old[0], old[1])
    return hits
