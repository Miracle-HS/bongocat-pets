#!/usr/bin/env python
"""
gamemem -- reusable Windows game-memory toolkit (standard library only).

No third-party packages: everything is ctypes + bytes.find (memchr), which is
both faster than numpy for this workload and avoids "ModuleNotFoundError" on a
user's bare system Python.

    from gamemem import Mem, find_pid, list_processes, Il2Cpp

    m = Mem(find_pid("Game.exe"))
    hits = m.scan_exact(1000, 'i32')
    hits = m.scan_next(1200)                # refine after the value changed
    m.write_value(hits[0], 99999, 'i32')

IL2CPP metadata (Unity games shipping GameAssembly.dll):

    il = Il2Cpp(m)
    il.resolve_image("SomeKnownClassName")
    kl = il.find_class("CurrencyManager")
    for off, name, static, tcode in il.fields(kl): ...
    for code, name in il.methods_of(kl): ...
"""
import ctypes
import struct
import threading
import time

CHUNK = 8 * 1024 * 1024
PAGE_OK = {0x02, 0x04, 0x08, 0x20, 0x40, 0x80}
PAGE_RW = {0x04, 0x08, 0x40, 0x80}
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100

k32 = ctypes.WinDLL('kernel32', use_last_error=True)
ntdll = ctypes.WinDLL('ntdll')

FMT = {'i8': '<b', 'u8': '<B', 'i16': '<h', 'u16': '<H',
       'i32': '<i', 'u32': '<I', 'i64': '<q', 'u64': '<Q',
       'f32': '<f', 'f64': '<d'}
SIZE = {t: struct.calcsize(f) for t, f in FMT.items()}


class MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_ulonglong), ("AllocationBase", ctypes.c_ulonglong),
                ("AllocationProtect", ctypes.c_uint32), ("__a", ctypes.c_uint32),
                ("RegionSize", ctypes.c_ulonglong), ("State", ctypes.c_uint32),
                ("Protect", ctypes.c_uint32), ("Type", ctypes.c_uint32), ("__b", ctypes.c_uint32)]


class PE32(ctypes.Structure):
    _fields_ = [("dwSize", ctypes.c_uint32), ("cntUsage", ctypes.c_uint32),
                ("th32ProcessID", ctypes.c_uint32), ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", ctypes.c_uint32), ("cntThreads", ctypes.c_uint32),
                ("th32ParentProcessID", ctypes.c_uint32), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", ctypes.c_uint32), ("szExeFile", ctypes.c_char * 260)]


class ME32(ctypes.Structure):
    _fields_ = [("dwSize", ctypes.c_uint32), ("th32ModuleID", ctypes.c_uint32),
                ("th32ProcessID", ctypes.c_uint32), ("GlblcntUsage", ctypes.c_uint32),
                ("ProccntUsage", ctypes.c_uint32), ("modBaseAddr", ctypes.c_void_p),
                ("modBaseSize", ctypes.c_uint32), ("hModule", ctypes.c_void_p),
                ("szModule", ctypes.c_char * 256), ("szExePath", ctypes.c_char * 260)]


def list_processes():
    """-> [(pid, name), ...]"""
    out = []
    snap = k32.CreateToolhelp32Snapshot(2, 0)
    pe = PE32(); pe.dwSize = ctypes.sizeof(PE32)
    ok = k32.Process32First(snap, ctypes.byref(pe))
    while ok:
        out.append((pe.th32ProcessID, pe.szExeFile.decode(errors='replace')))
        ok = k32.Process32Next(snap, ctypes.byref(pe))
    k32.CloseHandle(snap)
    return out


def find_pid(name):
    for pid, nm in list_processes():
        if nm.lower() == name.lower():
            return pid
    return None


def modules(pid):
    """-> {module_name: (base, size, path)}  (needs matching bitness)"""
    out = {}
    snap = k32.CreateToolhelp32Snapshot(0x08 | 0x10, pid)   # MODULE | MODULE32
    if snap == -1:
        return out
    me = ME32(); me.dwSize = ctypes.sizeof(ME32)
    ok = k32.Module32First(snap, ctypes.byref(me))
    while ok:
        out[me.szModule.decode(errors='replace')] = (
            ctypes.cast(me.modBaseAddr, ctypes.c_void_p).value or 0,
            me.modBaseSize, me.szExePath.decode(errors='replace'))
        ok = k32.Module32Next(snap, ctypes.byref(me))
    k32.CloseHandle(snap)
    return out


class Mem(object):
    def __init__(self, pid):
        if not pid:
            raise ValueError("no pid")
        self.pid = pid
        self.h = k32.OpenProcess(0x0400 | 0x0010 | 0x0020 | 0x0008, False, pid)
        self.hs = k32.OpenProcess(0x0800, False, pid)
        if not self.h:
            raise OSError("OpenProcess failed (%d) -- try running as administrator"
                          % ctypes.get_last_error())
        self.last_hits = []
        self.last_type = None

    # ---------- raw access ----------
    def rd(self, addr, n):
        buf = ctypes.create_string_buffer(n)
        got = ctypes.c_size_t(0)
        if not k32.ReadProcessMemory(self.h, ctypes.c_void_p(addr), buf, n, ctypes.byref(got)):
            return None
        return buf.raw[:got.value]

    def wr(self, addr, data):
        got = ctypes.c_size_t(0)
        return bool(k32.WriteProcessMemory(self.h, ctypes.c_void_p(addr),
                                           data, len(data), ctypes.byref(got)))

    def read_value(self, addr, t='i32'):
        d = self.rd(addr, SIZE[t])
        return struct.unpack(FMT[t], d)[0] if d and len(d) == SIZE[t] else None

    def write_value(self, addr, value, t='i32'):
        return self.wr(addr, struct.pack(FMT[t], value))

    def q(self, addr):
        d = self.rd(addr, 8)
        return int.from_bytes(d, 'little') if d else None

    def i32(self, addr):
        d = self.rd(addr, 4)
        return int.from_bytes(d, 'little', signed=True) if d else None

    def cstr(self, addr, n=128):
        """null-terminated ASCII, validated -- returns None if it is not plausible text"""
        if not addr or addr < 0x10000:
            return None
        d = self.rd(addr, n)
        if not d:
            return None
        z = d.find(b'\0')
        s = d[:z if z >= 0 else n]
        if not s or len(s) >= n:
            return None
        try:
            t = s.decode('ascii')
        except Exception:
            return None
        return t if all(32 <= ord(c) < 127 for c in t) else None

    def wstr(self, addr):
        """IL2CPP / .NET System.String -> python str"""
        ln = self.i32(addr + 0x10)
        if ln is None or not (0 <= ln < 65536):
            return None
        d = self.rd(addr + 0x14, ln * 2)
        try:
            return d.decode('utf-16-le') if d else None
        except Exception:
            return None

    # ---------- regions ----------
    def regions(self, writable_only=False):
        allow = PAGE_RW if writable_only else PAGE_OK
        out = []
        addr = 0
        mbi = MBI()
        while addr < 0x7FFFFFFFFFFF:
            if k32.VirtualQueryEx(self.h, ctypes.c_void_p(addr),
                                  ctypes.byref(mbi), ctypes.sizeof(mbi)) == 0:
                break
            if mbi.RegionSize == 0:
                break
            if (mbi.State == MEM_COMMIT and (mbi.Protect & 0xFF) in allow
                    and not (mbi.Protect & PAGE_GUARD)):
                out.append((mbi.BaseAddress, mbi.RegionSize))
            addr = mbi.BaseAddress + mbi.RegionSize
        return out

    # ---------- scanning ----------
    def scan_bytes(self, needle, regions=None, align=0, first_only=False,
                   accept=None, limit=0):
        """memchr-backed pattern search. `accept(addr)` is an optional validator."""
        if regions is None:
            regions = self.regions(writable_only=True)
        hits = []
        nl = len(needle)
        for base, size in regions:
            off = 0
            while off < size:
                n = min(CHUNK, size - off)
                data = self.rd(base + off, min(n + nl - 1, size - off))
                if data:
                    st = 0
                    while True:
                        i = data.find(needle, st)
                        if i < 0 or i >= n:
                            break
                        st = i + 1
                        a = base + off + i
                        if align and (a % align):
                            continue
                        if accept is not None and not accept(a):
                            continue
                        hits.append(a)
                        if first_only:
                            return hits
                        if limit and len(hits) >= limit:
                            return hits
                off += n
        return hits

    def scan_exact(self, value, t='i32', writable_only=True):
        """first scan -- remembers the hits for scan_next()"""
        needle = struct.pack(FMT[t], value)
        self.last_hits = self.scan_bytes(needle, self.regions(writable_only),
                                         align=min(SIZE[t], 8))
        self.last_type = t
        return self.last_hits

    def scan_next(self, value):
        """re-check the previous hits, keep those now equal to `value`"""
        t = self.last_type or 'i32'
        keep = [a for a in self.last_hits if self.read_value(a, t) == value]
        self.last_hits = keep
        return keep

    def scan_reset(self):
        self.last_hits = []
        self.last_type = None

    def scan_range(self, lo, hi, t='i64', writable_only=True):
        """values within [lo, hi] -- use when the exact figure drifts"""
        sz = SIZE[t]
        out = []
        for base, size in self.regions(writable_only):
            off = 0
            while off < size:
                n = min(CHUNK, size - off)
                data = self.rd(base + off, n)
                if data:
                    m = len(data) - (len(data) % sz)
                    for i in range(0, m, sz):
                        v = struct.unpack_from(FMT[t], data, i)[0]
                        if lo <= v <= hi:
                            out.append(base + off + i)
                off += n
        return out

    # ---------- process control ----------
    def suspend(self):
        return ntdll.NtSuspendProcess(self.hs) == 0

    def resume(self):
        return ntdll.NtResumeProcess(self.hs) == 0

    def freeze(self, addr, value, t='i32', interval=0.05):
        """keep re-writing a value in a background thread -> returns a stop() callable"""
        stop = threading.Event()

        def loop():
            data = struct.pack(FMT[t], value)
            while not stop.is_set():
                self.wr(addr, data)
                time.sleep(interval)
        threading.Thread(target=loop, daemon=True).start()
        return stop.set


TYPE_CODE = {0x02: 'bool', 0x03: 'char', 0x04: 'i1', 0x05: 'u1', 0x06: 'i2', 0x07: 'u2',
             0x08: 'i4', 0x09: 'u4', 0x0A: 'i8', 0x0B: 'u8', 0x0C: 'r4', 0x0D: 'r8',
             0x0E: 'string', 0x11: 'valuetype', 0x12: 'class', 0x14: 'array',
             0x15: 'genericinst', 0x1C: 'object', 0x1D: 'szarray'}


class Il2Cpp(object):
    """IL2CPP metadata reader.

    Il2CppClass:  +0x00 image  +0x10 name  +0x18 namespace  +0x58 parent
                  +0x80 fields  +0xB8 static_fields
    FieldInfo (stride 0x20):  +0x00 name  +0x08 type  +0x10 parent  +0x18 offset
    Il2CppType:  +0x08 attrs(u16, &0x10 = static)  +0x0A type code(u8)
    MethodInfo:  +0x00 native code  +0x10 name  +0x18 klass
    """

    def __init__(self, mem):
        self.m = mem
        self.image = None
        self._ro = None
        self._classes = None

    def _regions(self):
        if self._ro is None:
            self._ro = self.m.regions(writable_only=False)
        return self._ro

    def class_name(self, kl):
        return self.m.cstr(self.m.q(kl + 0x10)) if kl and kl > 0x10000 else None

    def class_full(self, kl):
        nm = self.class_name(kl)
        if not nm:
            return None
        ns = self.m.cstr(self.m.q(kl + 0x18)) or ""
        return (ns + "." + nm).lstrip(".")

    def is_class(self, kl, expect=None):
        """structural validation: name is an identifier AND image name ends with .dll"""
        nm = self.class_name(kl)
        if not nm or (expect is not None and nm != expect):
            return False
        if not all(c.isalnum() or c in '_`<>.[]' for c in nm):
            return False
        img = self.m.q(kl)
        inm = self.m.cstr(self.m.q(img)) if img else None
        return bool(inm and inm.endswith('.dll'))

    def resolve_image(self, known_class_name):
        """anchor on a class name string you know exists -> assembly image pointer"""
        straddrs = self.m.scan_bytes(known_class_name.encode() + b"\0", self._regions())
        for sa in straddrs:
            hit = self.m.scan_bytes(sa.to_bytes(8, 'little'), self._regions(), align=8,
                                    first_only=True,
                                    accept=lambda a: self.is_class(a - 0x10, known_class_name))
            if hit:
                self.image = self.m.q(hit[0] - 0x10)
                return self.image
        return None

    def all_classes(self):
        """every class of the resolved assembly -> {klass: full_name}"""
        if self._classes is not None:
            return self._classes
        if not self.image:
            raise RuntimeError("call resolve_image() first")
        out = {}
        for kl in self.m.scan_bytes(self.image.to_bytes(8, 'little'), self._regions(), align=8):
            nm = self.class_name(kl)
            if nm and all(c.isalnum() or c in '_`<>.[]' for c in nm):
                out[kl] = self.class_full(kl)
        self._classes = out
        return out

    def find_class(self, name):
        for kl, full in self.all_classes().items():
            if full == name or full.rsplit('.', 1)[-1] == name:
                return kl
        return None

    def fields(self, kl, limit=96):
        """-> [(offset, name, is_static, type_code_name)]"""
        fp = self.m.q(kl + 0x80)
        out = []
        if not fp or fp < 0x10000:
            return out
        for i in range(limit):
            fa = fp + i * 0x20
            nm = self.m.cstr(self.m.q(fa))
            ft = self.m.q(fa + 0x08)
            par = self.m.q(fa + 0x10)
            off = self.m.i32(fa + 0x18)
            if not nm or par != kl or off is None or not (0 <= off < 0x4000):
                break
            attrs = self.m.rd(ft + 0x08, 2) if ft else None
            tc = self.m.rd(ft + 0x0A, 1) if ft else None
            attrs = int.from_bytes(attrs, 'little') if attrs else 0
            out.append((off, nm, bool(attrs & 0x10),
                        TYPE_CODE.get(tc[0], hex(tc[0])) if tc else '?'))
        return out

    def statics(self, kl):
        return self.m.q(kl + 0xB8)

    def all_methods(self, module_base, module_size):
        """Reverse-identify MethodInfo structs -> [(code_addr, class_full, method_name)].

        Do NOT trust klass+0x98 as the method table: it is unreliable across
        Unity versions.  Instead require code ptr inside the module AND
        MethodInfo+0x18 pointing at a known class.
        """
        known = set(self.all_classes().keys())
        lo, hi = module_base, module_base + module_size
        out = []
        for base, size in self._regions():
            off = 0
            while off < size:
                n = min(CHUNK, size - off)
                data = self.m.rd(base + off, n)
                if data and len(data) >= 32:
                    m = len(data) - (len(data) % 8)
                    for i in range(0, m - 24, 8):
                        code = int.from_bytes(data[i:i + 8], 'little')
                        if not (lo <= code < hi):
                            continue
                        kl = int.from_bytes(data[i + 24:i + 32], 'little')
                        if kl not in known:
                            continue
                        nm = self.m.cstr(int.from_bytes(data[i + 16:i + 24], 'little'))
                        if nm and (nm[0].isalpha() or nm[0] in '._<'):
                            out.append((code, self._classes[kl], nm))
                off += n
        return sorted(set(out))

    def instances(self, kl):
        """heap objects whose header points at this class"""
        return self.m.scan_bytes(kl.to_bytes(8, 'little'),
                                 self.m.regions(writable_only=True), align=8)


def pe_sections(mem, base):
    """-> [(name, va_offset, vsize, characteristics)]"""
    hdr = mem.rd(base, 0x1000)
    if not hdr or hdr[:2] != b'MZ':
        return []
    e = struct.unpack_from('<I', hdr, 0x3C)[0]
    nsec = struct.unpack_from('<H', hdr, e + 6)[0]
    optsz = struct.unpack_from('<H', hdr, e + 20)[0]
    so = e + 24 + optsz
    out = []
    for i in range(nsec):
        o = so + i * 40
        nm = hdr[o:o + 8].rstrip(b'\0').decode('ascii', 'replace')
        vsz, va = struct.unpack_from('<II', hdr, o + 8)
        ch = struct.unpack_from('<I', hdr, o + 36)[0]
        out.append((nm, va, vsz, ch))
    return out


def xrefs(mem, base, sections, targets):
    """RIP-relative references to any address in `targets`.

    IMPORTANT: IL2CPP game code lives in the `il2cpp` section, not `.text`.
    Pass every section with IMAGE_SCN_MEM_EXECUTE (0x20000000).
    """
    tset = set(targets)
    hits = []
    for nm, va, vsz, ch in sections:
        if not (ch & 0x20000000):
            continue
        off = 0
        while off < vsz:
            n = min(CHUNK, vsz - off)
            data = mem.rd(base + va + off, n + 8)
            if data and len(data) > 8:
                L = len(data) - 8
                for i in range(L):
                    disp = int.from_bytes(data[i:i + 4], 'little', signed=True)
                    tgt = base + va + off + i + 4 + disp
                    if tgt in tset:
                        hits.append((nm, base + va + off + i))
            off += n
    return hits
