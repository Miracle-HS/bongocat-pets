# -*- coding: utf-8 -*-
"""
BongoCat 自动开箱 —— DLL 等长 IL 补丁 (不依赖鼠标模拟)

把 BongoCat.Shop.OnChestReady() 尾部的「宝箱就绪 -> 显示图标等你点」改成「直接买」:

  游戏 2026-09 版:  if (_shopItem.CanBuy())  _shopVisuals.SetActive(true);
  游戏 2026-10 版:  if (_shopItem.CanBuy())  _shopVisuals.Show();           // dev 重构了视觉组件
  补丁后:           if (_shopItem.CanBuy())  _shopItem.Buy();

IL 字节数 12 -> 12, 方法长度不变, 所需 token (字段/方法) 都已存在于程序集。
两个代次的模式都自动识别, 匹配不到就拒绝写入 (等游戏再更新坏了也不会写坏文件)。
宝箱一就绪, 游戏自己就买掉, 全程零点击、与坐标无关。

前提: 游戏内「宝箱弹窗」开关保持开启 (OnChestReady 里 ShowChestPopup 为 false 会提前 return)。

用法:
    python patch_dll.py                 只检查并显示将要修改的字节 (不写盘)
    python patch_dll.py --apply         打补丁 (需先关闭游戏)
    python patch_dll.py --apply --wait  等你关闭游戏后自动打补丁
    python patch_dll.py --restore       从最近的备份还原
    python patch_dll.py --status        查看当前是原版还是已打补丁
"""
import sys, os, struct, shutil, time, glob
import dnfile
import gameinfo, version

# 游戏 DLL 路径不写死: 换台电脑 Steam 库位置就变了, 由 gameinfo 运行时定位。
# 需要路径时调 gameinfo.find_dll() / find_dll_or_raise(), 不要缓存到常量。


def game_running():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        from gamemem import find_pid
        return find_pid("BongoCat.exe")
    except Exception:
        return None


def _tokname(pe, tok):
    """解析 token 的引用名: 0x0A=MemberRef, 0x06=MethodDef; 解不出返回 ''"""
    tbl, rid = tok >> 24, tok & 0xFFFFFF
    try:
        if tbl == 0x0A:
            return str(pe.net.mdtables.MemberRef.rows[rid - 1].Name)
        if tbl == 0x06:
            return str(pe.net.mdtables.MethodDef.rows[rid - 1].Name)
    except Exception:
        pass
    return ""


def compute(path=None):
    """返回 (绝对文件偏移, 原12字节, 新12字节, 状态)。两代游戏模式自适应:
      G2 (2026-10 起): 原版  ldarg.0/ldfld _shopVisuals/callvirt Show/ret
                        补丁  ldarg.0/ldfld _shopItem/callvirt Buy/ret
      G1 (2026-09 及更早): 原版  .../ldc.i4.1/callvirt GameObject::SetActive
                        补丁  .../callvirt Buy/nop   (旧版工具写的形态)
    """
    path = path or gameinfo.find_dll_or_raise()
    pe = dnfile.dnPE(path)
    raw = open(path, "rb").read()

    def tn(r):
        ns = str(getattr(r, "TypeNamespace", "") or "")
        return (ns + "." if ns else "") + str(r.TypeName)

    shop = shopitem = None
    for td in pe.net.mdtables.TypeDef.rows:
        if tn(td) == "BongoCat.Shop":
            shop = td
        elif tn(td) == "BongoCat.ShopItem":
            shopitem = td
    if shop is None or shopitem is None:
        raise RuntimeError("找不到 BongoCat.Shop / BongoCat.ShopItem")

    frows = pe.net.mdtables.Field.rows
    mrows = pe.net.mdtables.MethodDef.rows

    def ftok(td, name):
        for fr in td.FieldList:
            if str(fr.row.Name) == name:
                return 0x04000000 | (frows.index(fr.row) + 1)
        raise KeyError(name)

    def mtok(td, name):
        for mr in td.MethodList:
            if str(mr.row.Name) == name:
                return 0x06000000 | (mrows.index(mr.row) + 1)
        raise KeyError(name)

    tok_vis = ftok(shop, "_shopVisuals")
    tok_item = ftok(shop, "_shopItem")
    tok_buy = mtok(shopitem, "Buy")

    pre_vis = b"\x02\x7b" + struct.pack("<I", tok_vis)
    pre_item = b"\x02\x7b" + struct.pack("<I", tok_item)
    buy_tail = b"\x6f" + struct.pack("<I", tok_buy)

    target = None
    for mr in shop.MethodList:
        if str(mr.row.Name) == "OnChestReady":
            target = mr.row
    if target is None or not target.Rva:
        raise RuntimeError("找不到 Shop.OnChestReady 方法体")
    off = pe.get_offset_from_rva(target.Rva)
    h = raw[off]
    if (h & 3) == 2:
        code_off, code_len = off + 1, h >> 2
    else:
        code_len = struct.unpack_from("<I", raw, off + 4)[0]
        code_off = off + 12
    body = raw[code_off:code_off + code_len]

    # --- G2 (新): ldarg.0 ldfld _shopVisuals | callvirt <Show> | ret ---
    g2_old = g2_new = None
    for i in [j for j in range(len(body) - 11) if body.startswith(pre_vis, j) and len(body) - j >= 12]:
        b = body
        if b[i + 6] == 0x6F and b[i + 11] == 0x2A and \
           _tokname(pe, struct.unpack_from("<I", b, i + 7)[0]) == "Show":
            g2_old = (code_off + i, b[i:i + 12], pre_item + buy_tail + b"\x2a")
    # --- G1 (旧): ldarg.0 ldfld _shopVisuals ldc.i4.1 callvirt <SetActive> ---
    g1_old = g1_new = None
    for i in [j for j in range(len(body) - 11) if body.startswith(pre_vis, j) and len(body) - j >= 12]:
        b = body
        if b[i + 6] == 0x17 and b[i + 7] == 0x6F and \
           _tokname(pe, struct.unpack_from("<I", b, i + 8)[0]) == "SetActive":
            g1_old = (code_off + i, b[i:i + 12], pre_item + buy_tail + b"\x00")
    # --- 已打补丁形态 (两代各自的 new) ---
    patched = None
    for pat_len, seq in ((12, pre_item + buy_tail + b"\x2a"), (12, pre_item + buy_tail + b"\x00")):
        if body.count(seq) == 1:
            patched = (code_off + body.find(seq), seq)
            break

    if g2_old and body.count(g2_old[1]) > 1:
        g2_old = None   # 不唯一, 不敢动
    if g1_old and body.count(g1_old[1]) > 1:
        g1_old = None

    if g2_old:
        i, old, new = g2_old
        return i, old, new, "原版(10月新版 Show)"
    if g1_old:
        i, old, new = g1_old
        return i, old, new, "原版(9月旧版 SetActive)"
    if patched:
        i, seq = patched
        state = "已打补丁" + ("(10月 Show 代)" if seq.endswith(b"\x2a") else "(9月 SetActive 代)")
        disp_old = seq.replace(pre_item, pre_vis, 1)  # 仅展示用
        return i, disp_old, seq, state
    raise RuntimeError("方法体里既没找到原版字节也没找到补丁字节 (body %d 字节), "
                       "游戏可能又更新了, 放弃" % code_len)


def newest_backup(path=None):
    dll = path or gameinfo.find_dll()
    if not dll:
        return None
    b = sorted(glob.glob(dll + ".bak-*"))
    return b[-1] if b else None


def do_status():
    dll = gameinfo.find_dll()
    if not dll:
        print("目标文件 : (未找到游戏目录)")
        for t in gameinfo.search_trail():
            print("    尝试: %s" % t)
        print("[!] 请在 GUI 点「选择游戏目录」, 或设环境变量 BONGOCAT_DIR 指向游戏根目录")
        return None
    off, old, new, state = compute(dll)
    print("目标文件 : %s" % dll)
    print("补丁位置 : 文件偏移 0x%X" % off)
    print("原  字节 : %s" % old.hex())
    print("新  字节 : %s" % new.hex())
    print("当前状态 : %s" % state)
    b = newest_backup(dll)
    print("最近备份 : %s" % (os.path.basename(b) if b else "无"))
    build, updated = gameinfo.steam_build(dll)
    if build or updated:
        print("游戏版本 : build %s%s" % (build or "?", "  (%s 更新)" % updated if updated else ""))
    pid = game_running()
    print("游戏进程 : %s" % ("运行中 PID=%d (补丁在下次启动游戏后生效)" % pid if pid else "未运行"))
    return state


def write_bytes(off, data, make_backup=True, path=None):
    dll = path or gameinfo.find_dll_or_raise()
    if make_backup:
        bak = dll + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
        shutil.copy2(dll, bak)
        print("已备份 -> %s" % os.path.basename(bak))
    with open(dll, "r+b") as f:
        f.seek(off)
        f.write(data)
    with open(dll, "rb") as f:
        f.seek(off)
        chk = f.read(len(data))
    ok = chk == data
    print("回读校验 : %s  %s" % (chk.hex(), "OK" if ok else "失败"))
    return ok


def wait_for_exit(limit=900):
    pid = game_running()
    if not pid:
        return True
    print("检测到 BongoCat 正在运行 (PID=%d)。" % pid)
    print("请手动关闭 BongoCat, 本脚本会自动继续 (最多等 %d 秒)…" % limit)
    t0 = time.time()
    while time.time() - t0 < limit:
        if not game_running():
            print("游戏已退出, 继续。")
            time.sleep(1.0)
            return True
        time.sleep(1.0)
    print("[!] 等待超时, 未做修改。")
    return False


def main():
    args = sys.argv[1:]
    print("BongoCat 自动开箱补丁器 v%s  (适配游戏 %s)"
          % (version.__version__, " / ".join(version.ADAPTED)))
    if "--status" in args:
        do_status()
        return
    if "--restore" in args:
        dll = gameinfo.find_dll()
        if not dll:
            print("[!] 未找到 BongoCat 游戏目录, 无法还原。")
            for t in gameinfo.search_trail():
                print("    尝试: %s" % t)
            print("    可设环境变量 BONGOCAT_DIR 指向游戏根目录, 或先运行 GUI 选目录")
            return
        b = newest_backup(dll)
        if not b:
            print("[!] 没有找到备份文件")
            return
        if game_running() and "--wait" in args:
            if not wait_for_exit():
                return
        try:
            shutil.copy2(b, dll)
        except PermissionError:
            print("[!] DLL 被进程锁住, 请关闭 BongoCat 再还原 (--restore --wait 可等待)")
            return
        print("[+] 已从 %s 还原" % os.path.basename(b))
        do_status()
        return

    try:
        off, old, new, state = compute()
    except gameinfo.GameNotFound as e:
        print("[!] %s" % e)
        for t in gameinfo.search_trail():
            print("    尝试: %s" % t)
        return
    print("补丁位置 : 文件偏移 0x%X" % off)
    print("原  字节 : %s" % old.hex())
    print("新  字节 : %s" % new.hex())
    print("当前状态 : %s" % state)
    if "--apply" not in args:
        print()
        print("== 干跑, 未写盘。加 --apply 才会真正修改 ==")
        return
    if state.startswith("已打补丁"):
        print("已经是补丁状态, 无需重复操作。")
        return
    running = game_running()
    if running and "--wait" in args:
        if not wait_for_exit():
            return
        running = None
    # 实测: 游戏运行时 Assembly-CSharp.dll 并未被锁定, 直接尝试写入
    try:
        ok = write_bytes(off, new)
    except PermissionError:
        print("[!] 这次 DLL 确实被进程锁住了。")
        print("    请关闭 BongoCat 后重试, 或改用:  python patch_dll.py --apply --wait")
        return
    if ok:
        if running:
            print("[i] 检测到 BongoCat 正在运行 (PID=%s): 补丁已写盘, 重启游戏后生效。" % running)
        print("[+] 补丁完成。启动 BongoCat 后, 宝箱一就绪会自动购买, 无需任何点击。")
        print("    前提: 游戏内「宝箱弹窗」选项保持开启 (关着时 OnChestReady 会提前返回)")
        print("    想还原:  python patch_dll.py --restore")


if __name__ == "__main__":
    main()
