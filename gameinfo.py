# -*- coding: utf-8 -*-
"""BongoCat 游戏目录定位 + 版本信息 —— 纯标准库, 不依赖 dnfile。

换机后 Steam 库位置会变, 所以游戏 DLL 路径必须运行时解析, 不能写死。
查找顺序 (命中的第一个即返回):
    环境变量 -> config.json -> 运行中的进程 -> Steam 注册表/vdf -> 常见路径兜底

GUI 需要在"还没 import dnfile"时就能判断游戏在不在, 所以这部分逻辑独立成模块。
"""
import os, re, glob, sys
import datetime

GAME_NAME = "BongoCat"
PROC_NAME = "BongoCat.exe"
DLL_NAME = "Assembly-CSharp.dll"
DLL_REL = os.path.join("BongoCat_Data", "Managed", DLL_NAME)

_BS = "\\"
_cache = {"done": False, "path": None, "trail": []}


class GameNotFound(RuntimeError):
    """所有来源都没能定位到游戏 DLL。"""


# ---------- 基础路径换算 ----------

def dll_from_dir(root):
    """游戏根目录 -> DLL 绝对路径; 文件不存在则 None"""
    if not root:
        return None
    p = os.path.join(os.path.normpath(root), DLL_REL)
    return p if os.path.isfile(p) else None


def game_root_from_dll(dll):
    """<root>/BongoCat_Data/Managed/Assembly-CSharp.dll -> <root>"""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(dll))))


# ---------- Steam 库定位 ----------

def _steam_registry_libs():
    """从注册表读 Steam 安装目录 (三个键值都试, 失败静默跳过)"""
    try:
        import winreg
    except ImportError:
        return []
    out = []
    for hive, sub, name in (
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath")):
        try:
            k = winreg.OpenKey(hive, sub)
            try:
                v = winreg.QueryValueEx(k, name)[0]
            finally:
                winreg.CloseKey(k)
            if v:
                out.append(os.path.normpath(str(v)))
        except Exception:
            pass
    return out


def vdf_libraries(steam_root):
    """解析 steamapps/libraryfolders.vdf, 返回其中所有库根目录"""
    vdf = os.path.join(steam_root, "steamapps", "libraryfolders.vdf")
    try:
        with open(vdf, "r", encoding="utf-8", errors="replace") as f:
            txt = f.read()
    except OSError:
        return []
    out = []
    for m in re.finditer(r'"path"\s*"([^"]*)"', txt):
        # VDF 里反斜杠是双写转义。这里只还原 \\ -> \, 不能用 unicode_escape
        # (那会把中文用户名路径解坏)。apps 子块的键是数字, 不会误抓。
        p = os.path.normpath(m.group(1).replace(_BS + _BS, _BS))
        if p and p not in out:
            out.append(p)
    return out


# ---------- 运行中的进程 ----------

def _exe_path_of(pid):
    """取进程 exe 完整路径; 失败返回 None"""
    if not pid:
        return None
    try:
        import gamemem
        for name, (_base, _size, path) in gamemem.modules(pid).items():
            if name.lower() == PROC_NAME.lower() and path:
                return path
    except Exception:
        pass
    # 兜底: QueryFullProcessImageNameW 不受快照 32/64 位限制
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k32.OpenProcess(0x1000, False, pid)      # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return None
        try:
            buf = ctypes.create_unicode_buffer(32768)
            n = ctypes.c_ulong(32768)
            ok = k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n))
            return buf.value if ok else None
        finally:
            k32.CloseHandle(h)
    except Exception:
        return None


# ---------- 候选路径枚举 ----------

def _candidates(trail):
    """按可靠性依次产出候选 DLL 路径, 同时把来源记进 trail 供排查"""
    def emit(src, path):
        if not path:
            return None
        path = os.path.normpath(path)
        trail.append("%s: %s" % (src, path))
        return path

    # 1) 环境变量 (指向不存在的路径时不算命中, 会继续往下找)
    yield emit("环境变量 BONGOCAT_DLL", os.environ.get("BONGOCAT_DLL"))
    d = os.environ.get("BONGOCAT_DIR")
    if d:
        yield emit("环境变量 BONGOCAT_DIR", os.path.join(d, DLL_REL))

    # 2) 上次在界面里选过的目录
    try:
        import autoclick
        gd = autoclick.load_cfg().get("game_dir")
    except Exception:
        gd = None
    if gd:
        yield emit("上次选择的目录", os.path.join(gd, DLL_REL))

    # 3) 正在运行的游戏进程
    try:
        import gamemem
        pid = gamemem.find_pid(PROC_NAME)
    except Exception:
        pid = None
    exe = _exe_path_of(pid)
    if exe:
        yield emit("运行中的游戏", os.path.join(os.path.dirname(exe), DLL_REL))

    # 4) Steam 注册表 + libraryfolders.vdf
    libs = _steam_registry_libs()
    for lib in libs:
        for root in [lib] + vdf_libraries(lib):
            yield emit("Steam 库", os.path.join(root, "steamapps", "common",
                                                GAME_NAME, DLL_REL))

    # 5) 兜底: 已知盘符下的常见安装位置 (不做整盘递归)
    subs = ("SteamLibrary", "Steam", "Games",
            os.path.join("Program Files (x86)", "Steam"),
            os.path.join("Program Files", "Steam"))
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        drive = letter + ":" + os.sep
        if not os.path.isdir(drive):
            continue
        for sub in subs:
            yield emit("兜底扫描", os.path.join(drive, sub, "steamapps", "common",
                                                GAME_NAME, DLL_REL))


# ---------- 对外接口 ----------

def find_dll(refresh=False):
    """定位游戏 DLL, 命中后缓存。返回绝对路径, 全找不到返回 None。"""
    if _cache["done"] and not refresh:
        return _cache["path"]
    trail, hit = [], None
    for p in _candidates(trail):
        if p and os.path.isfile(p):
            hit = p
            break
    _cache.update(done=True, path=hit, trail=trail)
    return hit


def find_dll_or_raise(refresh=False):
    p = find_dll(refresh)
    if not p:
        raise GameNotFound(
            "未找到 BongoCat 游戏目录。请在界面点「选择游戏目录」指定, "
            "或设置环境变量 BONGOCAT_DIR 指向含 BongoCat.exe 的目录")
    return p


def search_trail():
    """上一次定位尝试过的来源列表 (仅在 find_dll 跑过之后有内容)"""
    return list(_cache.get("trail") or [])


# ---------- 版本信息 ----------

def steam_build(dll=None):
    """从 Steam 清单读游戏版本 -> (buildid, 更新日期) ; 非 Steam 安装返回 (None, None)

    按 installdir 匹配而不是硬编码 AppID —— 商店页 AppID 与本地清单实测不一致。
    """
    dll = dll or find_dll()
    if not dll:
        return (None, None)
    root = game_root_from_dll(dll)
    steamapps = os.path.dirname(os.path.dirname(root))   # <root>/.. = common, 再上一层
    target = os.path.basename(root).lower()
    for acf in glob.glob(os.path.join(steamapps, "appmanifest_*.acf")):
        try:
            with open(acf, "r", encoding="utf-8", errors="replace") as f:
                txt = f.read()
        except OSError:
            continue
        m = re.search(r'"installdir"\s+"([^"]*)"', txt)
        if not m or m.group(1).lower() != target:
            continue
        b = re.search(r'"buildid"\s+"([^"]*)"', txt)
        u = re.search(r'"lastupdated"\s+"([^"]*)"', txt)
        date = None
        if u:
            try:
                date = datetime.datetime.fromtimestamp(int(u.group(1))).strftime("%Y-%m-%d")
            except (ValueError, OSError, OverflowError):
                date = None
        return (b.group(1) if b else None, date)
    return (None, None)


def gen_from_state(state):
    """补丁状态串 -> 括号内的代次标记
       '原版(10月新版 Show)' -> '10月新版 Show'"""
    m = re.search(r"[（(]([^）)]+)[）)]", state or "")
    return m.group(1) if m else None
