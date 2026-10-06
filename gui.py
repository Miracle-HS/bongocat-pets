# -*- coding: utf-8 -*-
"""
BongoCat 点击数(Pets) 图形工具
双击 "启动GUI.bat" 即可运行, 只用标准库, 无需安装任何东西。

原理(Assembly-CSharp.dll / BongoCat.Pets 反编译):
    可用点击数 Current = Max(_currentGained - _totalSpent, 0)
    开箱 TrySpendPets(1000)  ->  _totalSpent += 1000
    内存连续: [A]=_currentGained [A+4]=_currentAchievement [A+8]=_totalSpent [A+12]=_init
"""
import os, sys, struct, bisect, threading, queue, time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gamemem import Mem, find_pid
import autoclick
import gameinfo, version

OFF_READY = 0x0D      # ChestIsReady   
OFF_OPENING = 0x0F    # _openingChest

PROC = "BongoCat.exe"
APP_TITLE = "BongoCat 点击数工具"
INSTANCE_MUTEX = r"Local\BongoCatPetsGUI"
MAXR = 64 * 1024 * 1024

REG_PATH = r"Software\Irox Games\BongoCat"
REG_NORMAL = "TIME_LEFT_h1828194100"
REG_EMOTE = "EMOTE_CHEST_TIME_LEFT_h1089283883"

# ---------- 深色主题配色 ----------
BG       = "#171922"      # 窗口底色
CARD     = "#1f2230"      # 卡片背景
BORDER   = "#313548"      # 卡片描边
TEXT     = "#e9ebf4"      # 主文字
MUTED    = "#9aa0b5"      # 次级文字
ACCENT   = "#7db0ff"      # 高亮蓝
ACCENT_D = "#4f85e8"      # 主按钮蓝
GREEN    = "#4fd18b"      # 成功 / 数值
RED      = "#ff6b81"      # 错误
AMBER    = "#ffc861"      # 警示
BTN      = "#2b2f42"      # 普通按钮底
BTN_BD   = "#3b4059"      # 普通按钮描边
FIELD    = "#14161f"      # 输入框底
FIELD_BD = "#3a3f55"      # 输入框描边
FONT     = "微软雅黑 UI"


def _read_timers():
    """读 PlayerPrefs 里的两个倒计时(游戏每秒写一次), 返回 (普通, 表情)"""
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_PATH)
    except Exception:
        return (None, None)
    out = []
    for name in (REG_NORMAL, REG_EMOTE):
        try:
            out.append(int(winreg.QueryValueEx(k, name)[0]))
        except Exception:
            out.append(None)
    return tuple(out)


def _dark_titlebar(root):

    try:
        import ctypes
        root.update_idletasks()
        user32 = ctypes.windll.user32
        user32.GetParent.restype = ctypes.c_void_p
        user32.GetParent.argtypes = [ctypes.c_void_p]
        hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
        dwm = ctypes.windll.dwmapi
        dwm.DwmSetWindowAttribute.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                              ctypes.c_void_p, ctypes.c_ulong]

        def set_int(attr, value):
            v = ctypes.c_int(value)
            dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v))

        def set_color(attr, hexcolor):
            r, g, b = int(hexcolor[1:3], 16), int(hexcolor[3:5], 16), int(hexcolor[5:7], 16)
            v = ctypes.c_int(r | (g << 8) | (b << 16))   # COLORREF 0x00BBGGRR
            dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v))

        for attr in (20, 19):          
            try:
                set_int(attr, 1)
            except Exception:
                pass
        try:
            set_color(35, BG)          
            set_color(36, TEXT)        
            set_color(34, BORDER)      
        except Exception:
            pass
    except Exception:
        pass


class Core(object):
    def __init__(self):
        self.mem = None
        self.pid = None
        self.addr = None
        self.shops = []
        self.hwnd = None

    def connect(self):
        pid = find_pid(PROC)
        if not pid:
            raise RuntimeError("没有找到 %s, 请先启动游戏" % PROC)
        self.mem = Mem(pid)
        self.pid = pid
        self.addr = None
        self.shops = []
        self.hwnd = autoclick.find_hwnd(pid)
        return pid

    def read3(self):
        m, a = self.mem, self.addr
        return (m.read_value(a, 'i32'),
                m.read_value(a + 4, 'i32'),
                m.read_value(a + 8, 'i32'))

    def write_spent(self, v):
        self.mem.write_value(self.addr + 8, int(v), 'i32')

    def valid(self):
        if self.addr is None or self.mem is None:
            return False
        try:
            g, ach, sp = self.read3()
            init = self.mem.read_value(self.addr + 12, 'i32')
        except Exception:
            return False
        return g == ach and init == 1 and 0 <= sp <= g

    def locate(self, progress=None, minv=5000):
        m = self.mem
        rs = []
        for b, sz in m.regions(writable_only=False):
            rs.append((b, b + sz))
        rs.sort()
        st = [a for a, _ in rs]

        def isptr(p):
            if p < 0x10000 or p > 0x7FFFFFFFFFFF:
                return False
            i = bisect.bisect_right(st, p) - 1
            return i >= 0 and rs[i][0] <= p < rs[i][1]

        regs = [(b, s) for b, s in m.regions(writable_only=True) if s <= MAXR]
        hits = []
        for n, (base, size) in enumerate(regs):
            if progress:
                progress(n, len(regs))
            b = m.rd(base, size)
            if not b:
                continue
            for off in range(0x18, len(b) - 16, 4):
                g, ach, sp, init = struct.unpack_from("<iiii", b, off)
                if g != ach or init != 1:
                    continue
                if g < minv or g >= 500000000 or sp < 0 or sp > g:
                    continue
                p1, p2, p3 = struct.unpack_from("<QQQ", b, off - 24)
                if not (isptr(p1) and isptr(p2) and isptr(p3)):
                    continue
                hits.append((base + off, g, ach, sp))
        if progress:
            progress(len(regs), len(regs))
        return hits

    def locate_shops(self):
        """定位 BongoCat.Shop 两个实例 (普通宝箱 / 表情宝箱)
           X-4  _isEmoteShop(0/1)   X+0 _stockRefreshTime   X+4 StockRefreshTimeLeft
           X+8  _delayDefocusOnStartum = 4.0f (0x40800000)
           X+0C _canDefocus  X+0D ChestIsReady  X+0E _sentChestReady  X+0F _openingChest
        只接受「一普通 + 一表情、冷却上限相同、地址相邻」的唯一一对, 不做模糊兜底。
        """
        m = self.mem
        raw = []
        for base, size in m.regions(writable_only=True):
            if size > MAXR:
                continue
            b = m.rd(base, size)
            if not b:
                continue
            for off in range(4, len(b) - 16, 4):
                if struct.unpack_from("<I", b, off + 8)[0] != 0x40800000:
                    continue
                emote = struct.unpack_from("<i", b, off - 4)[0]
                if emote not in (0, 1):
                    continue
                tot, left = struct.unpack_from("<ii", b, off)
                if tot <= 0 or tot > 86400 or left < 0 or left > tot:
                    continue
                if max(b[off + 0x0C], b[off + 0x0D], b[off + 0x0E], b[off + 0x0F]) > 1:
                    continue
                raw.append({"addr": base + off, "emote": bool(emote),
                            "total": tot, "left": left})
        for x in raw:
            if x["emote"]:
                continue
            for y in raw:
                if not y["emote"]:
                    continue
                if x["total"] == y["total"] and 0 < abs(x["addr"] - y["addr"]) < 0x400:
                    self.shops = [x, y]
                    return self.shops
        self.shops = []
        return []

    def read_shop(self, s):
        return (self.mem.read_value(s["addr"], 'i32'),
                self.mem.read_value(s["addr"] + 4, 'i32'))

    def chest_ready(self, s):
        try:
            return self.mem.read_value(s["addr"] + OFF_READY, 'u8') == 1
        except Exception:
            return False


class App(object):
    def __init__(self, root):
        self.root = root
        self.core = Core()
        self.q = queue.Queue()
        self.hold_on = tk.BooleanVar(value=False)
        self.hold_target = None
        self.hold_count = 0
        self.fast_on = tk.BooleanVar(value=False)
        self.pause_on = tk.BooleanVar(value=False)
        self.cfg = autoclick.load_cfg()
        self.shop_last = {0: None, 1: None}
        self.shop_retry = {0: 0, 1: 0}
        self.suppress = {0: 0.0, 1: 0.0}
        self.opened = {0: int(self.cfg.get("opened_normal", 0) or 0),
                       1: int(self.cfg.get("opened_emote", 0) or 0)}
        self.scanning = False
        self.retry = 0            # 定位失败后的自动重试次数
        self.patch_state = None
        self._build()
        self._refresh_opened()
        self.refresh_patch_state()
        self.root.after(200, self._pump)
        self.root.after(600, self.do_connect)

    def _build(self):
        r = self.root
        r.title(APP_TITLE)
        r.configure(background=BG)
        sw, sh = r.winfo_screenwidth(), r.winfo_screenheight()
        r.geometry("%dx%d" % (min(800, sw - 40), min(920, sh - 80)))
        r.minsize(600, min(440, sh - 80))

        st = ttk.Style(r)
        st.theme_use("clam")
        st.configure(".", background=BG, foreground=TEXT, bordercolor=BORDER,
                     lightcolor=CARD, darkcolor=CARD, focuscolor=ACCENT,
                     font=(FONT, 9))
        st.configure("TFrame", background=BG)
        st.configure("Card.TFrame", background=CARD)
        st.configure("Card.TLabelframe", background=CARD, bordercolor=BORDER,
                     lightcolor=CARD, darkcolor=CARD, relief="solid",
                     borderwidth=1)
        st.configure("Card.TLabelframe.Label", background=CARD,
                     foreground=ACCENT, font=(FONT, 10, "bold"))
        st.configure("TLabel", background=CARD, foreground=TEXT)
        st.configure("Head.TLabel", background=BG, foreground=TEXT,
                     font=(FONT, 13, "bold"))
        st.configure("Sub.TLabel", background=BG, foreground=MUTED,
                     font=(FONT, 8))
        st.configure("Logo.TLabel", background=BG)
        st.configure("Muted.TLabel", background=CARD, foreground=MUTED)
        st.configure("Sec.TLabel", background=CARD, foreground=TEXT,
                     font=(FONT, 9, "bold"))
        st.configure("Num.TLabel", background=CARD, foreground=TEXT,
                     font=("Consolas", 10, "bold"))
        st.configure("Key.TLabel", background=CARD, foreground=MUTED,
                     font=("Consolas", 8))
        st.configure("Proc.TLabel", background=CARD, foreground=RED,
                     font=(FONT, 9, "bold"))
        st.configure("Big.TLabel", background=CARD, foreground=GREEN,
                     font=("Consolas", 22, "bold"))
        st.configure("Ok.TLabel", background=CARD, foreground=GREEN,
                     font=("Consolas", 10, "bold"))
        st.configure("TButton", background=BTN, foreground=TEXT,
                     bordercolor=BTN_BD, lightcolor=BTN, darkcolor=BTN,
                     focuscolor=BTN, padding=(12, 5), font=(FONT, 9))
        st.map("TButton",
               background=[("pressed", "#22263a"), ("active", "#3a4059"),
                           ("disabled", "#242736")],
               foreground=[("disabled", "#5a5f76")],
               bordercolor=[("active", ACCENT_D), ("disabled", "#2b2e3f")])
        st.configure("Accent.TButton", background=ACCENT_D,
                     foreground="#0c0f16", bordercolor=ACCENT_D,
                     lightcolor=ACCENT_D, darkcolor=ACCENT_D,
                     font=(FONT, 9, "bold"), padding=(12, 5))
        st.map("Accent.TButton",
               background=[("pressed", "#4272c9"), ("active", ACCENT),
                           ("disabled", "#33415e")],
               foreground=[("disabled", "#65708c")])
        st.configure("Warn.TButton", background=BTN, foreground=AMBER,
                     bordercolor=BTN_BD, lightcolor=BTN, darkcolor=BTN,
                     padding=(12, 5))
        st.map("Warn.TButton",
               background=[("active", "#3a4059"), ("disabled", "#242736")],
               foreground=[("active", AMBER), ("disabled", "#5a5f76")])
        st.configure("TCheckbutton", background=CARD, foreground=TEXT,
                     indicatorcolor=FIELD, indicatormargin=(3, 3, 6, 3),
                     focuscolor=CARD)
        st.map("TCheckbutton",
               background=[("active", CARD), ("disabled", CARD)],
               foreground=[("disabled", "#5a5f76")],
               indicatorcolor=[("selected", ACCENT_D), ("active", FIELD_BD)])
        st.configure("TEntry", fieldbackground=FIELD, foreground=TEXT,
                     bordercolor=FIELD_BD, lightcolor=FIELD_BD,
                     darkcolor=FIELD_BD, insertcolor=TEXT,
                     selectbackground=ACCENT_D, padding=4)
        st.map("TEntry",
               bordercolor=[("focus", ACCENT_D)],
               lightcolor=[("focus", ACCENT_D)],
               darkcolor=[("focus", ACCENT_D)])
        st.configure("Horizontal.TProgressbar", background=ACCENT_D,
                     troughcolor="#12141c", bordercolor="#12141c",
                     lightcolor=ACCENT_D, darkcolor=ACCENT_D, thickness=8)
        st.configure("TSeparator", background=BORDER, bordercolor=BORDER)
        st.configure("Vertical.TScrollbar", background=BTN, troughcolor=CARD,
                     bordercolor=CARD, arrowcolor=MUTED,
                     lightcolor=BTN, darkcolor=BTN)
        st.map("Vertical.TScrollbar", background=[("active", "#41475f")])

        # ================= 可滚动内容区 =================
        # 所有内容塞进 Canvas 里的 Frame: 内容超高时用鼠标滚轮上下滚动, 不常驻滚动条。
        # 宽度永远跟随窗口; 高度 = max(内容自然高度, 窗口高度) -> 窗口够高时日志框自动撑满。
        self.canvas = tk.Canvas(r, bg=BG, highlightthickness=0, bd=0,
                                yscrollincrement=18)   # units=像素, 滚动手感更细
        self.canvas.pack(fill="both", expand=True)
        self.inner = ttk.Frame(self.canvas)
        self._win = self.canvas.create_window((0, 0), window=self.inner,
                                              anchor="nw")

        def _inner_cfg(_e=None):
            # 内容高度变了 -> 更新可滚动区域
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))

        def _canvas_cfg(e):
            if not self._win:
                return
            self.canvas.itemconfigure(self._win, width=e.width)
            # 注意用 reqheight (自然高), 不要用 bbox 高度, 否则会「棘轮式」只增不减
            self.canvas.itemconfigure(
                self._win, height=max(self.inner.winfo_reqheight(), e.height))

        self.inner.bind("<Configure>", _inner_cfg)
        self.canvas.bind("<Configure>", _canvas_cfg)

        # ---- 鼠标滚轮 ----
        def _on_wheel(e):
            w = e.widget
            d = getattr(e, "delta", 0)
            if not d:
                return
            step = -int(d / 120) * 3      # Windows delta = ±120, 一格滚 3 个单位
            if isinstance(w, tk.Text):
                return                     # 日志框自己滚, 别两边一起滚
            if isinstance(w, ttk.Scrollbar):
                self.txt.yview_scroll(step, "units")   # 悬在日志滚动条上 -> 滚日志
                return
            if not self.canvas.winfo_ismapped():
                return                     # messagebox / filedialog 上不滚主界面
            self.canvas.yview_scroll(step, "units")
            return "break"

        r.bind_all("<MouseWheel>", _on_wheel)

        def _on_close():
            try:
                r.unbind_all("<MouseWheel>")
            except Exception:
                pass
            r.destroy()

        r.protocol("WM_DELETE_WINDOW", _on_close)

        # ---------------- 标题 + 图标 ----------------
        self._logo = self._icon = None
        try:
            if getattr(sys, "frozen", False):
                # PyInstaller onefile: 资源解包在 sys._MEIPASS
                _a = os.path.join(getattr(sys, "_MEIPASS",
                                          os.path.dirname(sys.executable)), "assets")
            else:
                _a = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
            self._logo = tk.PhotoImage(file=os.path.join(_a, "logo48.png"))
            self._icon = tk.PhotoImage(file=os.path.join(_a, "icon32.png"))
            r.iconphoto(True, self._icon)
        except Exception:
            self._logo = self._icon = None

        head = ttk.Frame(self.inner)
        head.pack(fill="x", padx=14, pady=(12, 0))
        if self._logo is not None:
            ttk.Label(head, image=self._logo, style="Logo.TLabel").pack(
                side="left", padx=(0, 10))
            title = "BongoCat 点击数工具"
        else:
            title = "🐾  BongoCat 点击数工具"
        ttk.Label(head, text=title, style="Head.TLabel").pack(side="left")
        ttk.Label(head, text="v%s" % version.__version__, style="Sub.TLabel").pack(
            side="right", pady=(7, 0))
        ttk.Label(head, text="适配游戏 %s" % " / ".join(version.ADAPTED),
                  style="Sub.TLabel").pack(side="right", padx=(0, 8), pady=(7, 0))

        # ============ 连接 · 定位 · 数值 ============
        f1 = ttk.LabelFrame(self.inner, text=" ①  连接 · 定位 · 数值 ",
                            style="Card.TLabelframe")
        f1.pack(fill="x", padx=12, pady=(10, 4))
        c1 = ttk.Frame(f1, style="Card.TFrame")
        c1.pack(fill="x", padx=12, pady=(8, 10))

        row = ttk.Frame(c1, style="Card.TFrame")
        row.pack(fill="x")
        self.lb_proc = ttk.Label(row, text="●  未连接", style="Proc.TLabel")
        self.lb_proc.pack(side="left")
        self.lb_addr = ttk.Label(row, text="基址: —", style="Key.TLabel",
                                 font=("Consolas", 10))
        self.lb_addr.pack(side="left", padx=(18, 0))
        self.bt_scan = ttk.Button(row, text="自动定位", width=8,
                                  command=self.do_locate)
        self.bt_scan.pack(side="right")
        ttk.Button(row, text="重新连接", width=8, style="Accent.TButton",
                   command=self.do_connect).pack(side="right", padx=(0, 8))
        self.pb = ttk.Progressbar(c1, mode="determinate")
        self.pb.pack(fill="x", pady=(8, 0))

        ttk.Separator(c1).pack(fill="x", pady=10)

        g = ttk.Frame(c1, style="Card.TFrame")
        g.pack(fill="x")
        self.v_gain = tk.StringVar(value="—")
        self.v_ach = tk.StringVar(value="—")
        self.v_spent = tk.StringVar(value="—")
        self.v_cur = tk.StringVar(value="—")
        stats = [("累计获得", "BongoTap", self.v_gain),
                 ("成就计数", "BongoBeat", self.v_ach),
                 ("累计花费", "BongoMinus", self.v_spent)]
        for i, (t, k, var) in enumerate(stats):
            ttk.Label(g, text=t, style="Muted.TLabel").grid(
                row=i, column=0, sticky="w", pady=2)
            ttk.Label(g, text=k, style="Key.TLabel").grid(
                row=i, column=1, sticky="w", padx=(8, 0))
            ttk.Label(g, textvariable=var, style="Num.TLabel").grid(
                row=i, column=2, sticky="w", padx=(12, 0), pady=2)
        g.columnconfigure(3, weight=1)      # 右侧留白列, 数值左对齐, 互不挤压

        ttk.Separator(c1).pack(fill="x", pady=(10, 8))

        gc = ttk.Frame(c1, style="Card.TFrame")
        gc.pack(fill="x")
        ttk.Label(gc, text="可用点击数", style="Muted.TLabel").grid(
            row=0, column=0, sticky="w")
        ttk.Label(gc, text="BongoTap - BongoMinus", style="Key.TLabel").grid(
            row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Label(gc, textvariable=self.v_cur, style="Big.TLabel").grid(
            row=0, column=2, sticky="e", padx=(16, 0))
        gc.columnconfigure(1, weight=1)     # 伸展的是说明列, 大数字贴右边缘

        # ============  操作面板  ============
        f2 = ttk.LabelFrame(self.inner, text=" ②  操作面板 ",
                            style="Card.TLabelframe")
        f2.pack(fill="x", padx=12, pady=4)
        c2 = ttk.Frame(f2, style="Card.TFrame")
        c2.pack(fill="x", padx=12, pady=(6, 10))

        # ---- 2a 修改点击数  ----
        ttk.Label(c2, text="▎修改点击数", style="Sec.TLabel").pack(anchor="w")
        a = ttk.Frame(c2, style="Card.TFrame")
        a.pack(fill="x", pady=(4, 0))
        a.columnconfigure(2, weight=1)        # 末尾留白列吸收多余宽度, 前两列保持靠左
        ttk.Label(a, text="把「累计花费」设为:", style="Muted.TLabel").grid(
            row=0, column=0, sticky="w")
        # 输入框与「应用」放进同一格: 两者永远紧邻, 不会被伸展列推到远处
        av = ttk.Frame(a, style="Card.TFrame")
        av.grid(row=0, column=1, sticky="w", padx=(10, 0))
        self.e_val = ttk.Entry(av, width=14, font=("Consolas", 10))
        self.e_val.pack(side="left")
        self.e_val.insert(0, "0")
        self.bt_apply = ttk.Button(av, text="应用", command=self.do_apply,
                                   state="disabled")
        self.bt_apply.pack(side="left", padx=(8, 0))
        self.bt_zero = ttk.Button(a, text="一键清零 (拿回全部点击数)",
                                  style="Accent.TButton",
                                  command=lambda: self.do_apply(0),
                                  state="disabled")
        # 长按钮独占一行通栏: 无论窗口多窄都不会挤到上一行
        self.bt_zero.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(7, 0))
        self.cb_hold = ttk.Checkbutton(
            c2, text="锁定累计花费 (开箱永不扣)",
            variable=self.hold_on, command=self.on_hold, state="disabled")
        self.cb_hold.pack(anchor="w", pady=(6, 0))

        ttk.Separator(c2).pack(fill="x", pady=10)

        # ---- 2b 宝箱冷却  ----
        ttk.Label(c2, text="▎宝箱冷却  ·  BongoCat.Shop",
                  style="Sec.TLabel").pack(anchor="w")
        sg = ttk.Frame(c2, style="Card.TFrame")
        sg.pack(fill="x", pady=(4, 0))
        self.v_shop_n = tk.StringVar(value="—")
        self.v_shop_e = tk.StringVar(value="—")
        for i, (cap, var) in enumerate((("普通宝箱  剩余/上限", self.v_shop_n),
                                        ("表情宝箱  剩余/上限", self.v_shop_e))):
            ttk.Label(sg, text=cap, style="Muted.TLabel").grid(
                row=i, column=0, sticky="w", pady=1)
            ttk.Label(sg, textvariable=var, style="Num.TLabel").grid(
                row=i, column=1, sticky="w", padx=(12, 0), pady=1)
        # 尾部文字可能很长("等 Steam 发令牌 (重试 N 次)"), 靠右侧留白列吸收
        sg.columnconfigure(2, weight=1)
        sa = ttk.Frame(c2, style="Card.TFrame")
        sa.pack(fill="x", pady=(6, 0))
        sa.columnconfigure(2, weight=1)
        ttk.Label(sa, text="冷却上限(秒):", style="Muted.TLabel").grid(
            row=0, column=0, sticky="w")
        # 输入框与「应用」同格, 始终紧邻
        cd = ttk.Frame(sa, style="Card.TFrame")
        cd.grid(row=0, column=1, sticky="w", padx=(10, 0))
        self.e_cd = ttk.Entry(cd, width=10, font=("Consolas", 10))
        self.e_cd.pack(side="left")
        self.e_cd.insert(0, "5")
        self.bt_cd = ttk.Button(cd, text="应用", command=self.do_cooldown,
                                state="disabled")
        self.bt_cd.pack(side="left", padx=(8, 0))
        self.bt_now = ttk.Button(sa, text="立刻上架", style="Accent.TButton",
                                 command=self.do_ready, state="disabled")
        self.bt_now.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(7, 0))
        self.cb_fast = ttk.Checkbutton(
            c2, text="保持随时可开 (倒计时一超过上限就压回去)",
            variable=self.fast_on, state="disabled")
        self.cb_fast.pack(anchor="w", pady=(6, 0))

        ttk.Separator(c2).pack(fill="x", pady=10)

        # ---- 2c 自动开箱  ----
        ttk.Label(c2, text="▎自动开箱", style="Sec.TLabel").pack(anchor="w")
        rc = ttk.Frame(c2, style="Card.TFrame")
        rc.pack(fill="x", pady=(4, 0))
        # col0 = 标签列(固定) / col1 = 内容列(伸展) / col2 = 操作按钮列(固定)
        # 操作列宽度锁死: 任何一行文字变长只挤内容列, 按钮永远不会被顶走
        rc.columnconfigure(0, minsize=76)
        rc.columnconfigure(1, weight=1)
        rc.columnconfigure(2, minsize=100)   # 宽度 = 「选择游戏目录」自然宽

        def _cap(row, text):
            ttk.Label(rc, text=text, style="Muted.TLabel", width=8,
                      anchor="w").grid(row=row, column=0, sticky="nw",
                                        pady=(4, 0))

        # 补丁状态 —— 去掉固定 width, 整条内容列独占, 永不裁字
        _cap(0, "补丁状态")
        self.v_patch = tk.StringVar(value="检测中…")
        self.lb_patch = ttk.Label(rc, textvariable=self.v_patch,
                                  font=("Consolas", 10), anchor="w",
                                  justify="left", wraplength=520)
        self.lb_patch.grid(row=0, column=1, columnspan=2, sticky="ew")

        # 补丁按钮 —— 两者相邻靠左, 不被伸展列拆散
        pb = ttk.Frame(rc, style="Card.TFrame")
        pb.grid(row=1, column=1, sticky="w", pady=(7, 0))
        self.bt_patch = ttk.Button(pb, text="打补丁", command=self.do_patch)
        self.bt_patch.pack(side="left")
        self.bt_unpatch = ttk.Button(pb, text="还原", style="Warn.TButton",
                                     command=self.do_unpatch)
        self.bt_unpatch.pack(side="left", padx=(8, 0))

        # 游戏版本 —— 值独占一行, 不再和按钮抢空间
        _cap(2, "游戏版本")
        self.v_game = tk.StringVar(value="检测中…")
        ttk.Label(rc, textvariable=self.v_game, style="Key.TLabel",
                  anchor="w").grid(row=2, column=1, columnspan=2,
                                    sticky="ew", pady=(4, 0))

        # 选择目录 —— 默认不显示, 仅当自动定位找不到游戏时才由 refresh_patch_state 放出
        self.bt_dir = ttk.Button(rc, text="选择游戏目录",
                                 command=self.do_choose_dir)

        # 已开箱 + 清零
        _cap(4, "已开箱")
        self.v_opened = tk.StringVar(value="—")
        ttk.Label(rc, textvariable=self.v_opened, style="Ok.TLabel",
                  anchor="w").grid(row=4, column=1, sticky="w", pady=(8, 0))
        self.bt_reset = ttk.Button(rc, text="清零", style="Warn.TButton",
                                   command=self.do_reset_count)
        self.bt_reset.grid(row=4, column=2, sticky="e", pady=(4, 0))

        # 暂停复选框 —— 长文本独占一整行通栏
        self.cb_auto = ttk.Checkbutton(
            rc, text="暂停自动开箱 (勾上 = 压住倒计时, 游戏就不会自动买)",
            variable=self.pause_on, command=self.on_pause, state="disabled")
        self.cb_auto.grid(row=5, column=0, columnspan=3, sticky="w", pady=(9, 0))

        # 状态文字 —— 挪到下一行右对齐, 不与长复选框争同一行
        self.v_auto = tk.StringVar(value="")
        ttk.Label(rc, textvariable=self.v_auto, style="Muted.TLabel",
                  anchor="e").grid(row=6, column=1, columnspan=2,
                                   sticky="e", pady=(2, 0))

        # ============ 日志 ============
        f3 = ttk.LabelFrame(self.inner, text=" 日志 ", style="Card.TLabelframe")
        f3.pack(fill="both", expand=True, padx=12, pady=(4, 12))
        wrap = ttk.Frame(f3, style="Card.TFrame")
        wrap.pack(fill="both", expand=True, padx=10, pady=(6, 10))
        self.txt = tk.Text(wrap, height=7, font=("Consolas", 9), wrap="word",
                           bg="#12141d", fg="#c9cddb", bd=0, relief="flat",
                           padx=8, pady=6, insertbackground=TEXT,
                           selectbackground="#3a4a7a", state="disabled")
        sb = ttk.Scrollbar(wrap, command=self.txt.yview)
        sb.pack(side="right", fill="y")
        self.txt.pack(side="left", fill="both", expand=True)
        self.txt.config(yscrollcommand=sb.set)
        self.txt.tag_configure("ts", foreground="#595f78")
        self.txt.tag_configure("warn", foreground=AMBER)

        # 标题栏跟随深色主题 (打包时窗口句柄就绪后生效)
        r.after(10, lambda: _dark_titlebar(r))
        # 首屏按内容实际高度开窗, 超出屏幕就截断(靠滚轮)
        r.after(80, self._fit_height)

    def _fit_height(self):
        """一次性: 把窗口高度撑到刚好装下内容, 但不超过屏幕, 也不小于 minsize。"""
        try:
            r = self.root
            r.update_idletasks()
            need = self.inner.winfo_reqheight() + 34      # 34 ≈ Windows 标题栏
            sh = r.winfo_screenheight()
            want = max(min(need, sh - 80), 440)
            r.geometry("%dx%d" % (max(r.winfo_width(), 800), want))
        except tk.TclError:
            pass

    def log(self, s):
        self.txt.configure(state="normal")
        self.txt.insert("end", "[" + time.strftime("%H:%M:%S") + "] ", "ts")
        if any(k in s for k in ("⚠", "失败", "出错", "未找到", "失效", "未定位")):
            self.txt.insert("end", s + chr(10), "warn")
        else:
            self.txt.insert("end", s + chr(10))
        self.txt.configure(state="disabled")
        self.txt.see("end")

    def _enable(self, on):
        st = "normal" if on else "disabled"
        for w in (self.bt_apply, self.bt_zero, self.cb_hold):
            w.config(state=st)

    def _enable_shop(self, on):
        st = "normal" if on else "disabled"
        for w in (self.bt_cd, self.bt_now, self.cb_fast, self.cb_auto):
            w.config(state=st)


    def _save_opened(self):
        self.cfg["opened_normal"] = self.opened[0]
        self.cfg["opened_emote"] = self.opened[1]
        autoclick.save_cfg(self.cfg)
        self._refresh_opened()

    def _refresh_opened(self):
        n, e = self.opened[0], self.opened[1]
        self.v_opened.set("普通 %d   表情 %d   合计 %d" % (n, e, n + e))

    def do_reset_count(self):
        if not messagebox.askyesno("确认", "把开箱计数清零?"):
            return
        self.opened = {0: 0, 1: 0}
        self._save_opened()
        self.log("开箱计数已清零")

    def refresh_patch_state(self):
        """定位游戏 DLL, 判断是原版还是已打补丁"""
        dll = gameinfo.find_dll()
        if not dll:
            self.patch_state = None
            self.v_patch.set("未找到游戏")
            self.lb_patch.config(foreground=AMBER)
            self.v_game.set("—")
            # 只有自动定位失败时才给出手动指定目录的入口
            self.bt_dir.grid(row=3, column=2, sticky="e", pady=(6, 0))
            self.log("未找到 BongoCat 游戏目录。请点「选择游戏目录」指定, 或确认游戏已安装")
            return None
        try:
            import patch_dll          # 放在 try 里: 没装 dnfile 时不该让界面崩掉
            off, old, new, state = patch_dll.compute(dll)
        except Exception as e:
            self.patch_state = None
            self.v_patch.set("无法读取: %s" % str(e)[:22])
            self.lb_patch.config(foreground=RED)
            self.v_game.set("—")
            self.bt_dir.grid_remove()
            return None
        self.bt_dir.grid_remove()
        self.patch_state = state
        self.v_patch.set(state + ("  (偏移 0x%X)" % off))
        self.lb_patch.config(foreground=GREEN if state.startswith("已打补丁") else TEXT)
        self._set_game_version(dll, state)
        return state

    def _set_game_version(self, dll, state):
        """拼「代次 · build · 更新日期」, 缺哪项省哪项 (非 Steam 安装只剩代次)"""
        parts = []
        gen = gameinfo.gen_from_state(state)
        if gen:
            parts.append(gen)
        build, updated = gameinfo.steam_build(dll)
        if build:
            parts.append("build %s" % build)
        if updated:
            parts.append("%s 更新" % updated)
        self.v_game.set("  ·  ".join(parts) or "—")

    def do_choose_dir(self):
        """手动指定游戏目录 (自动定位全落空时的兜底)"""
        d = filedialog.askdirectory(title="选择 BongoCat 安装目录 (含 BongoCat.exe)")
        if not d:
            return
        d = os.path.normpath(d)
        if not gameinfo.dll_from_dir(d):
            messagebox.showwarning(
                "目录不对",
                "该目录下没找到:\nBongoCat_Data\\Managed\\Assembly-CSharp.dll\n\n"
                "请选择包含 BongoCat.exe 的游戏安装目录 (通常名为 BongoCat)。")
            return
        self.cfg["game_dir"] = d          # 读-改-写, 不覆盖已存的开箱计数
        autoclick.save_cfg(self.cfg)
        gameinfo.find_dll(refresh=True)
        self.log("游戏目录已设为: %s" % d)
        self.refresh_patch_state()

    def do_patch(self):
        import patch_dll
        st = self.refresh_patch_state()
        if st and st.startswith("已打补丁"):
            messagebox.showinfo("提示", "已经是补丁状态了")
            return
        if st is None:
            messagebox.showwarning(
                "提示", "没能读取游戏 DLL。\n若上面显示「未找到游戏」, "
                        "请点「选择游戏目录」指定游戏安装位置。")
            return
        try:
            off, old, new, _ = patch_dll.compute()
        except Exception as e:
            self.log("计算补丁位置失败: %s" % e)
            messagebox.showerror("失败", str(e))
            return

        def _write():
            if not patch_dll.write_bytes(off, new):
                raise RuntimeError("写入补丁失败 (DLL 可能被占用)")
            self.q.put(("rlog", "补丁已写入 (偏移 0x%X), 宝箱就绪将自动购买" % off))

        self._apply_with_restart("打补丁", _write)

    def do_unpatch(self):
        import patch_dll
        try:
            dll = gameinfo.find_dll_or_raise()
        except gameinfo.GameNotFound:
            messagebox.showwarning("提示", "未找到 BongoCat 游戏目录, 请先点「选择游戏目录」。")
            return
        b = patch_dll.newest_backup(dll)
        if not b:
            messagebox.showwarning("提示", "没找到备份文件")
            return
        name = os.path.basename(b)

        def _restore():
            import shutil
            shutil.copy2(b, dll)
            self.q.put(("rlog", "已从 %s 还原" % name))

        self._apply_with_restart("还原原版", _restore)

    def _drop_conn(self):
        """作废当前内存连接 (进程可能已被结束), 界面退回未连接态"""
        self.core.mem = None
        self.core.addr = None
        self.core.pid = None
        self.core.shops = []
        self.core.hwnd = None
        self.hold_target = None
        self._enable(False)
        self._enable_shop(False)
        self.lb_addr.config(text="基址: —")

    def _game_exe(self):
        """从已定位的 DLL 反推 BongoCat.exe 路径"""
        dll = gameinfo.find_dll()
        if not dll:
            return None
        cand = os.path.join(gameinfo.game_root_from_dll(dll), gameinfo.PROC_NAME)
        return cand if os.path.isfile(cand) else None

    def _apply_with_restart(self, why, work):
        """改 DLL 的统一流程: 关闭游戏 -> 写盘 -> 重新启动 -> 主线程重连

        必须先关游戏: DLL 被进程加载着时覆写会抛 WinError 1224
        (「请求的操作无法在使用用户映射区域打开的文件上执行」),
        那个码不是 PermissionError, 所以不能靠 except PermissionError 兜。

        work 是写盘回调, 只在游戏已停、进程外执行。
        """
        self._drop_conn()
        self.bt_patch.config(state="disabled")
        self.bt_unpatch.config(state="disabled")
        self.lb_proc.config(text="●  正在重启游戏…", foreground=MUTED)
        threading.Thread(target=self._apply_worker, args=(why, work),
                         daemon=True).start()

    def _game_procs(self, root):
        """列出 exe 位于 root 目录下的进程 -> [(pid, name), ...]

        只按 exe 路径筛, 所以别的 Unity 游戏的 UnityCrashHandler64 不会被算进来。
        """
        import gamemem
        root = os.path.normpath(root).lower()
        out = []
        for pid, _name in gamemem.list_processes():
            try:
                p = gameinfo._exe_path_of(pid)
            except Exception:
                continue
            if p and os.path.dirname(os.path.normpath(p)).lower() == root:
                out.append((pid, os.path.basename(p)))
        return out

    def _kill_helpers(self, root):
        """结束游戏目录里的残留进程 (UnityCrashHandler64 等), 它们映射着游戏 DLL"""
        import subprocess
        for pid, name in self._game_procs(root):
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                               capture_output=True, timeout=10)
                self.q.put(("rlog", "已结束残留进程 %s (PID=%d)" % (name, pid)))
            except Exception:
                pass

    def _apply_worker(self, why, work):
        """后台线程: 停游戏 -> work() 写盘 -> 拉起游戏 -> 等它起来 -> 通知主线程重连

        走队列而不是直接碰界面, 因为 tkinter 只能在主线程操作。
        """
        import subprocess, time
        import gamemem

        exe = self._game_exe()
        if not exe:
            self.q.put(("rlog", "找不到 BongoCat.exe, 无法自动重启"))
            self.q.put(("rdone", False))
            return

        pid = gamemem.find_pid(PROC)

        # 游戏开着就先关掉。按镜像名杀而不是 /PID —— 可能开了多个实例,
        # /PID 只杀一棵进程树, 漏掉的那些照样占着 DLL。
        if pid:
            try:
                subprocess.run(["taskkill", "/F", "/T", "/IM", PROC],
                               capture_output=True, timeout=15)
            except Exception as e:
                self.q.put(("rlog", "结束游戏进程失败: %s" % e))
                self.q.put(("rdone", False))
                return
            # 只杀游戏目录内的进程: UnityCrashHandler64.exe 会映射游戏的 managed
            # DLL, 不清掉它写盘必然撞 WinError 1224。按路径筛, 免得误杀别的
            # Unity 游戏的崩溃处理器。
            self._kill_helpers(os.path.dirname(exe))
            # taskkill 是异步的, 发出去不等于进程已经没了。这里必须先等它
            # 真正退出, 否则下面的写盘重试会把「正在退出」误判成「游戏复活」。
            t0 = time.time()
            while time.time() - t0 < 15 and self._game_procs(os.path.dirname(exe)):
                time.sleep(0.3)
            if self._game_procs(os.path.dirname(exe)):
                self.q.put(("rlog", "游戏没能退出, 取消%s" % why))
                self.q.put(("rdone", False))
                return

        # 写盘重试: 进程退出后它的内存映射不会立刻释放, 这段时间里覆写 DLL
        # 会撞 WinError 1224 (或 32)。等一小会儿再试就能写进去。
        work_err = None
        for attempt in range(24):          # 24 * 0.5s ≈ 12s
            try:
                work()
                work_err = None
                break
            except OSError as e:
                # 1224 = 使用用户映射区域, 32 = 文件被另一进程占用
                # winerror 与 errno 择一, 不同调用路径填的字段不一样
                code = getattr(e, "winerror", None) or getattr(e, "errno", None)
                if code not in (32, 1224):
                    work_err = e
                    break
                work_err = e
                if attempt == 0:
                    self.q.put(("rlog", "游戏已退出, 等待文件解锁…"))
                if gamemem.find_pid(PROC):
                    self.q.put(("rlog", "游戏又被拉起来了, 取消%s" % why))
                    self.q.put(("rdone", False))
                    return
                time.sleep(0.5)

        if work_err is not None:
            self.q.put(("rlog", "%s失败: %s" % (why, work_err)))
            # 重试 12 秒还是写不进去, 说明不是延迟释放。列出还活着的进程,
            # 免得只能对着一个错误码瞎猜。
            try:
                left = self._game_procs(os.path.dirname(exe))
                if left:
                    self.q.put(("rlog", "仍占用游戏目录的进程: %s"
                                % ", ".join("%s(PID %d)" % (n, p)
                                            for p, n in left)))
                else:
                    self.q.put(("rlog", "没有残留进程, 可能是安全软件在扫描锁定 "
                                        "(可把游戏目录加进 Windows Defender 排除项)"))
            except Exception:
                pass
            # 是我们把游戏关掉的, 得拉回去, 免得用户还得自己再点一次启动
            if pid:
                try:
                    subprocess.Popen([exe], cwd=os.path.dirname(exe))
                except Exception:
                    pass
            self.q.put(("rdone", False))
            return

        # 游戏本来就没开, 不用替用户启动
        if not pid:
            self.q.put(("rdone", True))
            return

        try:
            subprocess.Popen([exe], cwd=os.path.dirname(exe))
        except Exception as e:
            self.q.put(("rlog", "%s完成, 但启动游戏失败: %s" % (why, e)))
            self.q.put(("rdone", False))
            return

        # 先等进程真的出现
        t0, up = time.time(), False
        while time.time() - t0 < 30:
            pid2 = gamemem.find_pid(PROC)
            if pid2:
                up = True
                break
            time.sleep(0.5)
        if not up:
            self.q.put(("rlog", "已拉起 BongoCat, 但 30 秒内没检测到进程, 请检查游戏"))
            self.q.put(("rdone", False))
            return

        # 检测到进程就通知主线程重连, 不再等待窗口可见。
        self.q.put(("rlog", "BongoCat 已重新启动, 检测到进程, 正在重新连接…"))
        self.q.put(("rdone", True))

    def on_pause(self):
        if self.pause_on.get():
            self.log("已暂停: 倒计时被压在 2 秒不归零, OnChestReady 不触发, 游戏不会自动买")
        else:
            self.log("已恢复: 倒计时可以归零, 补丁会让游戏自己买掉宝箱")
            if not (self.patch_state or "").startswith("已打补丁"):
                self.log("⚠ 当前 DLL 是原版, 不会自动买。请先「打补丁」并重启游戏")

    def _auto_tick(self):
        """补丁版开关的实现:
        打了补丁后, 是 TimerUpdate 里 StockRefreshTimeLeft<=0 触发 OnChestReady -> 自动买。
        所以只要把倒计时按住不让它归零, 就等于关掉自动开箱; 放开就是打开。
        """
        if not self.pause_on.get():
            if self.v_auto.get():
                self.v_auto.set("")
            return
        held = 0
        for s in self.core.shops:
            try:
                left = self.core.mem.read_value(s["addr"] + 4, 'i32')
                if left is not None and left < 2:
                    self.core.mem.write_value(s["addr"] + 4, 2, 'i32')
                    held += 1
            except Exception:
                pass
        self.v_auto.set("已暂停")

    def do_cooldown(self):
        if not self.core.shops:
            messagebox.showwarning("提示", "还没定位到 Shop, 请先点自动定位")
            return
        try:
            v = int(self.e_cd.get())
        except ValueError:
            messagebox.showwarning("提示", "请输入整数秒数")
            return
        if v < 1 or v > 86400:
            messagebox.showwarning("提示", "秒数需在 1 ~ 86400 之间")
            return
        for s in self.core.shops:
            self.core.mem.write_value(s["addr"], v, 'i32')
            left = self.core.mem.read_value(s["addr"] + 4, 'i32')
            if left > v:
                self.core.mem.write_value(s["addr"] + 4, v, 'i32')
            self.suppress[1 if s["emote"] else 0] = time.time() + 1.5
        self.log("冷却上限已改为 %d 秒 (原 1800). 每次开箱后只需等 %d 秒" % (v, v))

    def do_ready(self):
        if not self.core.shops:
            messagebox.showwarning("提示", "还没定位到 Shop, 请先点自动定位")
            return
        for s in self.core.shops:
            self.core.mem.write_value(s["addr"] + 4, 1, 'i32')
        self.log("已把两个宝箱的倒计时压到 1 秒")
        self.log("若 1 秒后归 0 -> 宝箱上架; 若跳到 60 -> Steam 还没发放 Chest_Token, 需再等")

    def do_connect(self):
        try:
            pid = self.core.connect()
        except Exception as e:
            self.lb_proc.config(text="●  " + str(e), foreground=RED)
            self.log("连接失败: " + str(e))
            self._enable(False)
            return
        self.lb_proc.config(text="●  %s   PID=%d   已连接" % (PROC, pid), foreground=GREEN)
        self.lb_addr.config(text="基址: —")
        self.core.addr = None
        self._enable(False)
        self.retry = 0
        self.log("已连接 PID=%d, 正在自动定位…" % pid)
        self.root.after(400, self.do_locate)

    def do_locate(self, manual=True):
        if self.core.mem is None:
            messagebox.showwarning("提示", "请先连接进程")
            return
        if self.scanning:
            return
        if manual:
            self.retry = 0      # 用户主动点的, 重新给满重试次数
        self.scanning = True
        self.bt_scan.config(state="disabled", text="扫描中…")
        self.pb.config(value=0, maximum=100)
        self.log("开始扫描内存, 约需十几秒…")
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self):
        try:
            hits = self.core.locate(progress=lambda i, n: self.q.put(("prog", (i, n))))
            self.q.put(("hits", hits))
            shops = self.core.locate_shops()
            self.q.put(("shops", shops))
        except Exception as e:
            self.q.put(("err", str(e)))

    def do_apply(self, forced=None):
        if not self.core.valid():
            messagebox.showwarning("提示", "基址失效, 请重新定位")
            return
        try:
            v = int(self.e_val.get()) if forced is None else int(forced)
        except ValueError:
            messagebox.showwarning("提示", "请输入整数")
            return
        g, ach, sp = self.core.read3()
        if v < 0 or v > g:
            messagebox.showwarning("提示", "累计花费必须在 0 ~ %d 之间" % g)
            return
        self.core.write_spent(v)
        if self.hold_on.get():
            self.hold_target = v
        self.log("累计花费 %d -> %d ,  可用点击数 %d -> %d"
                 % (sp, v, max(g - sp, 0), max(g - v, 0)))
        self.log("拍一下猫即可刷新游戏内显示; 60 秒内 StoreStats() 写回 Steam")

    def on_hold(self):
        if self.hold_on.get():
            if not self.core.valid():
                self.hold_on.set(False)
                messagebox.showwarning("提示", "基址失效, 请重新定位")
                return
            self.hold_target = self.core.read3()[2]
            self.hold_count = 0
            self.log("已锁定累计花费 = %d , 开箱扣除会被即时复位" % self.hold_target)
        else:
            self.log("已解除锁定, 本次共拦截 %d 次扣除" % self.hold_count)

    def _pump(self):
        while True:
            try:
                kind, data = self.q.get_nowait()
            except queue.Empty:
                break
            if kind == "prog":
                i, n = data
                self.pb.config(value=i * 100.0 / max(n, 1))
            elif kind == "err":
                self.scanning = False
                self.bt_scan.config(state="normal", text="自动定位")
                self.log("扫描出错: " + data)
            elif kind == "hits":
                self.scanning = False
                self.bt_scan.config(state="normal", text="自动定位")
                if not data:
                    # 分两种情况, 别一律盲等:
                    #  1) 游戏窗口都没了 —— 游戏挂了或被关了, 等也没用, 直接放弃
                    #  2) 游戏还在跑, 只是数据没进内存 —— 退避重试兜底
                    if not autoclick.find_hwnd(self.core.pid or 0):
                        self.retry = 0
                        self.log("游戏窗口不存在(游戏可能已退出), 放弃定位。"
                                 "重新启动游戏后再点「自动定位」")
                        continue
                    if self.retry < 5:
                        self.retry += 1
                        wait = self.retry * 6
                        self.log("游戏数据未就绪, %d 秒后自动重试 (%d/5)…"
                                 % (wait, self.retry))
                        self.root.after(wait * 1000,
                                        lambda: self.do_locate(manual=False))
                        # 必须 continue 而不是 return —— _pump 末尾靠
                        # root.after(50, self._pump) 自我调度, return 会让
                        # 整条链断掉, 之后界面再也不刷新。
                        continue
                    self.log("未找到。确认游戏在运行, 且累计点击数 > 5000")
                else:
                    self.retry = 0
                    a, g, ach, sp = data[0]
                    self.core.addr = a
                    self.lb_addr.config(text="基址: 0x%X" % a)
                    self._enable(True)
                    if len(data) == 1:
                        self.log("定位成功: 0x%X   (累计获得 %d, 累计花费 %d)" % (a, g, sp))
                    else:
                        self.log("找到 %d 个候选, 采用第一个: 0x%X" % (len(data), a))

            elif kind == "rlog":
                self.log(data)
            elif kind == "rdone":
                # 重启线程收工: 恢复按钮, 进程已起来就重新连接 + 自动定位
                self.bt_patch.config(state="normal")
                self.bt_unpatch.config(state="normal")
                # DLL 可能在后台被改过, 重新判定补丁状态
                self.refresh_patch_state()
                if data:
                    self.do_connect()
                else:
                    self.lb_proc.config(text="●  未连接", style="Proc.TLabel")

            elif kind == "shops":
                if not data:
                    # 重试期间不重复刷屏, 等定位彻底失败时再报一次
                    if self.retry == 0:
                        self.log("未定位到 Shop 对象 (宝箱冷却功能不可用)")
                else:
                    self._enable_shop(True)
                    for s in data:
                        self.log("Shop[%s] @0x%X  上限=%ds  剩余=%ds"
                                 % ("表情" if s["emote"] else "普通", s["addr"],
                                    s["total"], s["left"]))

        if self.core.addr is not None and self.core.mem is not None:
            try:
                g, ach, sp = self.core.read3()
                if self.hold_on.get() and self.hold_target is not None and sp != self.hold_target:
                    self.core.write_spent(self.hold_target)
                    self.hold_count += 1
                    self.log("拦截扣除 %d -> 复位 %d   (累计 %d 次)"
                             % (sp, self.hold_target, self.hold_count))
                    sp = self.hold_target
                self.v_gain.set("{:,}".format(g))
                self.v_ach.set("{:,}".format(ach))
                self.v_spent.set("{:,}".format(sp))
                self.v_cur.set("{:,}".format(max(g - sp, 0)))
            except Exception:
                pass

        if self.core.shops and self.core.mem is not None:
            try:
                lim = None
                if self.fast_on.get():
                    try:
                        lim = int(self.e_cd.get())
                    except ValueError:
                        lim = None
                for s in self.core.shops:
                    tot, left = self.core.read_shop(s)
                    if lim and (tot > lim or left > lim):
                        if tot > lim:
                            self.core.mem.write_value(s["addr"], lim, 'i32')
                            tot = lim
                        if left > lim:
                            self.core.mem.write_value(s["addr"] + 4, lim, 'i32')
                            left = lim
                        self.suppress[1 if s["emote"] else 0] = time.time() + 1.5
                    txt = "%d / %d 秒" % (left, tot)
                    idx = 1 if s["emote"] else 0
                    # 游戏自带逻辑: 倒计时归零时若 Steam 库里没有 Chest_Token,
                    # 就把剩余时间设成 60 再问一次 -> 表现为 60→0→60 无限循环。
                    # 这不是故障, 是在等 Steam 发货, 这里显式标出来。
                    last = self.shop_last[idx]
                    if last is not None and last <= 1 and left == 60 and tot > 60:
                        self.shop_retry[idx] += 1
                    if left > 60 or self.core.chest_ready(s):
                        self.shop_retry[idx] = 0
                    # --- 开箱计数 ---
                    # Shop.ItemGotBought() 是购买成功后唯一会把
                    #   StockRefreshTimeLeft = _stockRefreshTime
                    # 的地方, 所以「剩余时间跳回上限」就等于成功开了一个箱。
                    # self.suppress[idx] 用来屏蔽我们自己写内存造成的假跳变。
                    if (last is not None and left == tot and last != tot
                            and tot >= 3 and time.time() > self.suppress[idx]):
                        self.opened[idx] += 1
                        self._save_opened()
                        self.log("[%s] 开箱 +1  (普通 %d / 表情 %d / 合计 %d)"
                                 % ("表情宝箱" if idx else "普通宝箱",
                                    self.opened[0], self.opened[1],
                                    self.opened[0] + self.opened[1]))
                    self.shop_last[idx] = left
                    if self.core.chest_ready(s):
                        txt += "   已上架"
                    elif self.shop_retry[idx]:
                        txt += "   等 Steam 发令牌 (重试 %d 次)" % self.shop_retry[idx]
                    if s["emote"]:
                        self.v_shop_e.set(txt)
                    else:
                        self.v_shop_n.set(txt)
                self._auto_tick()
            except Exception:
                pass
        self.root.after(50, self._pump)


def _instance_mutex():
    """创建会话内的单实例锁, 句柄必须保留到窗口关闭。"""
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.CreateMutexW(None, False, INSTANCE_MUTEX)
    error = ctypes.get_last_error()
    if not handle:
        raise ctypes.WinError(error)
    return kernel32, handle, error == 183    # ERROR_ALREADY_EXISTS


def _activate_existing():
    """重复启动时尽量唤回原窗口, 不创建新的 Tk 窗口。"""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowW.restype = wintypes.HWND
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    hwnd = user32.FindWindowW("TkTopLevel", APP_TITLE)
    if hwnd:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)       # SW_RESTORE
        user32.SetForegroundWindow(hwnd)


def main():
    try:
        kernel32, handle, exists = _instance_mutex()
    except OSError as e:
        # 锁创建失败时停止启动, 避免失去单实例保护。
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR,
                                      wintypes.LPCWSTR, wintypes.UINT]
        user32.MessageBoxW.restype = ctypes.c_int
        user32.MessageBoxW(None, "无法检查程序是否已运行: %s" % e, APP_TITLE, 0x10)
        return
    if exists:
        kernel32.CloseHandle(handle)
        _activate_existing()
        return
    try:
        root = tk.Tk()
        App(root)
        root.mainloop()
    finally:
        kernel32.CloseHandle(handle)


if __name__ == "__main__":
    main()
