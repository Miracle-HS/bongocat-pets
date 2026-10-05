# -*- coding: utf-8 -*-
"""
BongoCat 点击数(Pets)定位与修改工具

原理(来自 Assembly-CSharp.dll 反编译, BongoCat.Pets):
    _currentGained      <- Steam Stat "BongoTap"
    _currentAchievement <- Steam Stat "BongoBeat"
    _totalSpent         <- Steam Stat "BongoMinus"

    Current(屏幕可用点击数) = Max(_currentGained - _totalSpent, 0)
    AddPet(n)       : _currentGained += n ; _currentAchievement += n
    TrySpendPets(n) : if Current >= n  ->  _totalSpent += n     <- 开箱扣 1000 就在这
    每 60 秒 Client.StoreStats() 写回 Steam, 所以改动可持久化

三个字段在内存里连续: [base]=_currentGained [base+4]=_currentAchievement [base+8]=_totalSpent

用法:
    python pets_tool.py auto              0. 免输入自动定位(首选, 重启后也能用)
    python pets_tool.py snap              1. 拍快照
    python pets_tool.py findauto [min]    2. 敲几下键盘后运行(推荐, 不用数次数)
    python pets_tool.py find N            2. 或: 已知恰好拍了 N 次
    python pets_tool.py findval C [L]     2. 或: 已知屏幕数字 C=可用, L=总计
    python pets_tool.py show  <addr>      查看三个字段
    python pets_tool.py spent <addr> <v>  设置 _totalSpent (设 0 = 拿回全部点击数)
    python pets_tool.py hold  <addr> [v]  持续锁定 _totalSpent, 开箱永不扣 (Ctrl+C 停)
"""
import os, sys, struct, pickle, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gamemem import Mem, find_pid

SNAP = os.path.join(os.environ.get("TEMP") or os.environ.get("TMP") or ".",
                    "bongocat_pets_snapshot.bin")
MAXR = 64 * 1024 * 1024
NL = chr(10)


def attach():
    pid = find_pid("BongoCat.exe")
    if not pid:
        print("[!] BongoCat.exe 没在运行")
        sys.exit(1)
    print("[i] PID = %d" % pid)
    return Mem(pid)


def dump(m):
    out, total = {}, 0
    for base, size in m.regions(writable_only=True):
        if size > MAXR:
            continue
        b = m.rd(base, size)
        if b:
            out[base] = b
            total += len(b)
    return out, total


def read3(m, addr):
    return (m.read_value(addr, 'i32'),
            m.read_value(addr + 4, 'i32'),
            m.read_value(addr + 8, 'i32'))


def report(addr, g, ach, sp, extra=""):
    print("  Pets 三元组基址 = 0x%016X%s" % (addr, extra))
    print("      _currentGained      = %d" % g)
    print("      _currentAchievement = %d" % ach)
    print("      _totalSpent         = %d" % sp)
    print("      => 屏幕可用点击数     = %d" % max(g - sp, 0))


def nextstep(addr):
    print("")
    print("[+] 唯一命中, 就是它。下一步任选:")
    print("    python pets_tool.py spent 0x%X 0    # 已花费清零, 立刻拿回全部点击数" % addr)
    print("    python pets_tool.py hold  0x%X 0    # 锁定为 0, 之后开箱永不扣" % addr)


def cmd_snap():
    m = attach()
    snap, total = dump(m)
    with open(SNAP, "wb") as f:
        pickle.dump(snap, f, 2)
    print("[+] 快照已保存 (%d 个区域, %.1f MB) -> %s" % (len(snap), total / 1048576.0, SNAP))
    print("[>] 现在去游戏窗口敲几十下键盘或点几十下鼠标, 然后运行:")
    print("      python pets_tool.py findauto")


def _loadsnap():
    if not os.path.exists(SNAP):
        print("[!] 先跑 snap")
        sys.exit(1)
    with open(SNAP, "rb") as f:
        return pickle.load(f)


def cmd_find(n, minv=1000):
    m = attach()
    old = _loadsnap()
    new, _ = dump(m)
    hits = []
    for base, ob in old.items():
        nb = new.get(base)
        if nb is None or len(nb) != len(ob) or ob == nb:
            continue
        for off in range(0, len(ob) - 12, 4):
            o0, o1 = struct.unpack_from("<ii", ob, off)
            v0, v1 = struct.unpack_from("<ii", nb, off)
            if v0 - o0 != n or v1 - o1 != n or o0 < minv or o1 < minv:
                continue
            o2 = struct.unpack_from("<i", ob, off + 8)[0]
            v2 = struct.unpack_from("<i", nb, off + 8)[0]
            hits.append((base + off, v0, v1, v2, o2))
    print("")
    print("=== 命中 (相邻两个 int32 同时 +%d) ===" % n)
    for addr, g, ach, sp, sp_old in hits:
        report(addr, g, ach, sp, "" if sp == sp_old else "   [第三个字段也变了]")
    print("总计 %d 个候选" % len(hits))
    if len(hits) == 1:
        nextstep(hits[0][0])


def cmd_findauto(minv=1000):
    m = attach()
    old = _loadsnap()
    new, _ = dump(m)
    hits = []
    for base, ob in old.items():
        nb = new.get(base)
        if nb is None or len(nb) != len(ob) or ob == nb:
            continue
        for off in range(0, len(ob) - 12, 4):
            o0, o1, o2 = struct.unpack_from("<iii", ob, off)
            v0, v1, v2 = struct.unpack_from("<iii", nb, off)
            d0, d1 = v0 - o0, v1 - o1
            if d0 <= 0 or d0 != d1:
                continue
            if o0 < minv or o1 < minv or v2 != o2 or v2 < 0 or v0 - v2 < 0:
                continue
            hits.append((base + off, v0, v1, v2, d0))
    print("")
    print("=== 命中 (相邻两个 int32 同时增加相同量, 第三个不变) ===")
    for addr, g, ach, sp, d in hits:
        report(addr, g, ach, sp, "   (本次增量 +%d)" % d)
    print("总计 %d 个候选" % len(hits))
    if len(hits) == 1:
        nextstep(hits[0][0])
    elif len(hits) > 1:
        print("[i] 多个候选: 再跑一轮 snap + findauto 即可排除噪声")


def cmd_findval(cur, life=None):
    m = attach()
    hits = []
    for base, size in m.regions(writable_only=True):
        if size > MAXR:
            continue
        b = m.rd(base, size)
        if not b:
            continue
        for off in range(0, len(b) - 12, 4):
            g, ach, sp = struct.unpack_from("<iii", b, off)
            if g <= 0 or ach <= 0 or sp < 0 or sp > g or g - sp != cur:
                continue
            if life is not None and ach != life:
                continue
            hits.append((base + off, g, ach, sp))
    print("")
    print("=== 命中 (gained - spent == %d%s) ===" %
          (cur, "" if life is None else ", ach == %d" % life))
    for addr, g, ach, sp in hits:
        report(addr, g, ach, sp)
    print("总计 %d 个候选" % len(hits))
    if len(hits) == 1:
        nextstep(hits[0][0])


def cmd_show(addr):
    m = attach()
    g, ach, sp = read3(m, addr)
    report(addr, g, ach, sp)


def cmd_spent(addr, val):
    m = attach()
    g, ach, sp = read3(m, addr)
    print("[i] 改前: gained=%d  ach=%d  spent=%d  current=%d" % (g, ach, sp, max(g - sp, 0)))
    m.write_value(addr + 8, val, 'i32')
    g2, a2, s2 = read3(m, addr)
    print("[+] 改后: gained=%d  ach=%d  spent=%d  current=%d" % (g2, a2, s2, max(g2 - s2, 0)))
    if s2 != val:
        print("[!] 写入未生效, 检查地址或以管理员身份运行")
    else:
        print("[i] 界面会在下一次 UpdateStats 刷新(随便拍一下即可触发),")
        print("    并在 60 秒内由 StoreStats() 写回 Steam。")


def cmd_hold(addr, val=None):
    m = attach()
    if val is None:
        val = m.read_value(addr + 8, 'i32')
    print("[i] 锁定 _totalSpent @ 0x%X = %d   (Ctrl+C 停止)" % (addr + 8, val))
    n = 0
    try:
        while True:
            cur = m.read_value(addr + 8, 'i32')
            if cur != val:
                m.write_value(addr + 8, val, 'i32')
                n += 1
                g, a, s = read3(m, addr)
                print("  [%s] 拦截扣除 %d -> 复位 %d  (累计 %d 次, current=%d)"
                      % (time.strftime("%H:%M:%S"), cur, val, n, max(g - s, 0)))
            time.sleep(0.02)
    except KeyboardInterrupt:
        print("")
        print("[i] 已停止, 共拦截 %d 次扣除" % n)


def cmd_auto(minv=5000):
    """免输入结构化定位: 依据 BongoCat.Pets 对象实测布局
         A-0x18 _petsText(ptr)  A-0x10 _lifetimePetsText(ptr)  A-0x08 _onStatsUpdated(ptr)
         A+0x00 _currentGained  A+0x04 _currentAchievement(==gained)
         A+0x08 _totalSpent     A+0x0C _init(==1)
    """
    import bisect
    m = attach()
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

    hits = []
    for base, size in m.regions(writable_only=True):
        if size > MAXR:
            continue
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
    print("")
    print("=== 结构化命中 (Pets 对象布局) ===")
    for a, g, ach, sp in hits:
        report(a, g, ach, sp)
    print("总计 %d 个候选" % len(hits))
    if len(hits) == 1:
        nextstep(hits[0][0])
    return hits


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    c = sys.argv[1]
    if c == "auto":
        cmd_auto(int(sys.argv[2]) if len(sys.argv) > 2 else 5000)
    elif c == "snap":
        cmd_snap()
    elif c == "find":
        cmd_find(int(sys.argv[2]), int(sys.argv[3]) if len(sys.argv) > 3 else 1000)
    elif c == "findauto":
        cmd_findauto(int(sys.argv[2]) if len(sys.argv) > 2 else 1000)
    elif c == "findval":
        cmd_findval(int(sys.argv[2]), int(sys.argv[3]) if len(sys.argv) > 3 else None)
    elif c == "show":
        cmd_show(int(sys.argv[2], 0))
    elif c == "spent":
        cmd_spent(int(sys.argv[2], 0), int(sys.argv[3]))
    elif c == "hold":
        cmd_hold(int(sys.argv[2], 0), int(sys.argv[3]) if len(sys.argv) > 3 else None)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
