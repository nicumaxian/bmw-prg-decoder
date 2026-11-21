#!/usr/bin/env python3
"""Parse an EDIABAS BEST/2 .prg file and emit a Markdown book + JSON/CSV dumps.

Usage: python3 -I parse_prg.py <file.prg>   (prints JSON)
       from parse_prg import parse_prg; data = parse_prg(path)

Layout (verified against EdiabasLib, all strings/code XOR 0xF7 unless noted):
  0x18 u32  job array size          0x7C u32  uses (include) list
  0x84 u32  tables                  0x88 u32  jobs
  0x90 u32  description text        0x94 u32  version info
"""
import json
import struct
import sys

KEY = 0xF7


def dec(b):
    return bytes(c ^ KEY for c in b)


# ---- registers / opcodes (from EdiabasLib) -------------------------------
def reg_name(code):
    if code <= 0x0F:
        return "B%X" % code
    if 0x10 <= code <= 0x17:
        return "I%X" % (code - 0x10)
    if 0x18 <= code <= 0x1B:
        return "L%X" % (code - 0x18)
    if 0x1C <= code <= 0x23:
        return "S%X" % (code - 0x1C)
    if 0x24 <= code <= 0x2B:
        return "F%X" % (code - 0x24)
    if 0x2C <= code <= 0x33:
        return "S%X" % (code - 0x2C + 8)
    if 0x80 <= code <= 0x8F:
        return "A%X" % (code - 0x80)
    if 0x90 <= code <= 0x97:
        return "I%X" % (code - 0x90 + 8)
    if 0x98 <= code <= 0x9B:
        return "L%X" % (code - 0x98 + 4)
    raise ValueError("bad register 0x%02X" % code)


OPS = """move clear comp subb adds mult divs and or xor not jump jtsr ret jc jae jz jnz jv jnv jmi jpl
clrc setc asr lsl lsr asl nop eoj push pop scmp scat scut slen spaste serase xconnect xhangup xsetpar
xawlen xsend xsendf xrequf xstopf xkeyb xstate xboot xreset xtype xvers ergb ergw ergd ergi ergr ergs
a2flt fadd fsub fmul fdiv ergy enewset etag xreps gettmr settmr sett clrt jt jnt addc subc break clrv
eerr popf pushf atsp swap setspc srevrs stoken parb parw parl pars fclose jg jge jl jle ja jbe fopen
fread freadln fseek fseekln ftell ftellln a2fix fix2flt parr test wait date time xbatt tosp xdownl
xgetport xignit xloopt xprog xraw xsetport xsireset xstoptr fix2hex fix2dez tabset tabseek tabget
strcat pary parn ergc ergl tabline xsendr xrecv xinfo flt2a setflt cfgig cfgsg cfgis a2y xparraw
hex2y strcmp strlen y2bcd y2hex shmset shmget ergsysi flt2fix iupdate irange iincpos tabseeku flt2y4
flt2y8 y42flt y82flt plink pcall fcomp plinkv ppush ppop ppushflt ppopflt ppushy ppopy pjtsr tabsetex
ufix2dez generr ticks waitex xopen xclose xcloseex xswitch xsendex xrecvex ssize tabcols tabrows""".split()
NEAR = {0x0B, 0x0C, 0x0E, 0x0F, 0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x41, 0x47, 0x48,
        0x5A, 0x5B, 0x5C, 0x5D, 0x5E, 0x5F}


def fmt_str(b):
    if not b:
        return "{ }"
    if all(c in (9, 10, 13) or 32 <= c < 127 for c in b[:-1]) and b[-1] == 0:
        return '"' + b[:-1].decode("latin1").replace("\n", "\\n") + '"'
    return "{" + ",".join("$%X.B" % c for c in b) + "}"


class Cur:
    def __init__(self, data, pos):
        self.d, self.p = data, pos

    def take(self, n):
        if self.p + n > len(self.d):
            raise EOFError
        b = dec(self.d[self.p:self.p + n])
        self.p += n
        return b


def read_operand(cur, mode):
    """returns (text, imm32_value_or_None)"""
    if mode == 0:
        return "", None
    if mode in (1, 2, 3, 4):
        return reg_name(cur.take(1)[0]), None
    if mode == 5:
        return "#$%X.B" % cur.take(1)[0], None
    if mode == 6:
        v = struct.unpack("<H", cur.take(2))[0]
        return "#$%X.I" % v, None
    if mode == 7:
        v = struct.unpack("<I", cur.take(4))[0]
        return "#$%X.L" % v, v
    if mode == 8:
        n = struct.unpack("<h", cur.take(2))[0]
        return fmt_str(cur.take(n)), None
    if mode == 9:
        b = cur.take(3)
        return "%s[#$%X]" % (reg_name(b[0]), struct.unpack("<H", b[1:])[0]), None
    if mode == 10:
        b = cur.take(2)
        return "%s[%s]" % (reg_name(b[0]), reg_name(b[1])), None
    if mode == 11:
        b = cur.take(4)
        return "%s[%s,#$%X]" % (reg_name(b[0]), reg_name(b[1]), struct.unpack("<H", b[2:])[0]), None
    if mode == 12:
        b = cur.take(5)
        return "%s[#$%X]#$%X" % (reg_name(b[0]), *struct.unpack("<HH", b[1:])), None
    if mode == 13:
        b = cur.take(4)
        return "%s[#$%X]%s" % (reg_name(b[0]), struct.unpack("<H", b[1:3])[0], reg_name(b[3])), None
    if mode == 14:
        b = cur.take(4)
        return "%s[%s]#$%X" % (reg_name(b[0]), reg_name(b[1]), struct.unpack("<H", b[2:])[0]), None
    if mode == 15:
        b = cur.take(3)
        return "%s[%s]%s" % (reg_name(b[0]), reg_name(b[1]), reg_name(b[2])), None
    raise ValueError("bad addr mode %d" % mode)


def disassemble(data, start):
    """Return list of (addr, text, jump_target|None); stops after two consecutive eoj."""
    out, cur, prev_eoj = [], Cur(data, start), False
    while True:
        addr = cur.p
        try:
            op, am = cur.take(2)
            if op >= len(OPS):
                raise ValueError("opcode 0x%02X" % op)
            t0, v0 = read_operand(cur, am >> 4)
            t1, _ = read_operand(cur, am & 15)
        except Exception as e:  # keep going gracefully
            out.append((addr, "; DECODE ERROR at 0x%X: %s" % (addr, e), None))
            break
        target = None
        if op in NEAR and (am >> 4) == 7:
            target = (cur.p + v0) & 0xFFFFFFFF
            t0 = "loc_%08X" % target
        text = OPS[op] + (" " + t0 if t0 else "") + (", " + t1 if t1 else "")
        out.append((addr, text, target))
        if op == 0x1D:
            if prev_eoj:
                break
            prev_eoj = True
        else:
            prev_eoj = False
    return out


# ---- file sections ---------------------------------------------------------
def u32(d, o):
    return struct.unpack_from("<I", d, o)[0]


def cstr(b):
    return dec(b).split(b"\x00")[0].decode("latin1")


def parse_descriptions(text):
    """Global key/values + per-job {comment, args[], results[], other{}} from the description block."""
    glob, jobs, job, item = {}, {}, None, None
    for line in text.split("\n"):
        line = line.rstrip("\r")
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        key = key.strip().upper()
        if key == "JOBNAME":
            job = jobs[val.strip()] = {"comment": [], "args": [], "results": [], "other": {}}
            item = None
        elif job is None:
            glob[key] = val
        elif key == "JOBCOMMENT":
            job["comment"].append(val)
        elif key == "ARG":
            item = {"name": val}
            job["args"].append(item)
        elif key == "RESULT":
            item = {"name": val}
            job["results"].append(item)
        elif key.startswith(("ARG", "RESULT")) and item is not None:
            item.setdefault(key.lower(), []).append(val) if key.endswith("COMMENT") else item.__setitem__(key.lower(), val)
        else:
            job["other"].setdefault(key, []).append(val)
    return glob, jobs


def parse_prg(path):
    """Parse an EDIABAS .prg file; return a JSON-serialisable dict of all definitions."""
    d = open(path, "rb").read()
    if not d.startswith(b"@EDIABAS OBJECT"):
        raise SystemExit("not an EDIABAS object file")
    r = {"size": len(d), "header": {hex(o): u32(d, o) for o in range(0x10, 0xA0, 4) if u32(d, o)}}

    # version info (0x94)
    o = u32(d, 0x94)
    vi = dec(d[o:o + 0x6C])
    r["version"] = {
        "bip_version": "%d.%d.%d" % (vi[2], vi[1], vi[0]),
        "revision": "%d.%d" % (struct.unpack_from("<h", vi, 6)[0], struct.unpack_from("<h", vi, 4)[0]),
        "author": vi[8:0x48].split(b"\0")[0].decode("latin1"),
        "origin_file": vi[0x48:0x68].split(b"\0")[0].decode("latin1"),
        "package": struct.unpack_from("<i", vi, 0x68)[0],
    }

    # uses (0x7C)
    o = u32(d, 0x7C)
    r["uses"] = []
    if o != 0xFFFFFFFF:
        n = u32(d, o)
        r["uses"] = [cstr(d[o + 4 + i * 0x100:o + 4 + (i + 1) * 0x100]) for i in range(n)]

    # descriptions (0x90)
    o = u32(d, 0x90)
    n = u32(d, o)
    text = dec(d[o + 4:o + 4 + n]).decode("latin1")
    r["description_raw"] = text
    glob, jobs_desc = parse_descriptions(text)
    r["global_comments"] = glob
    r["job_info"] = jobs_desc

    # jobs (0x88)
    o = u32(d, 0x88)
    n = u32(d, o)
    r["jobs"] = []
    for i in range(n):
        e = dec(d[o + 4 + i * 0x44:o + 4 + (i + 1) * 0x44])
        name = e[:0x40].split(b"\0")[0].decode("latin1")
        addr = struct.unpack_from("<I", e, 0x40)[0]
        code = disassemble(d, addr)
        r["jobs"].append({"name": name, "address": addr,
                          "code": [{"addr": a, "text": t, "target": tg} for a, t, tg in code]})

    # tables (0x84)
    o = u32(d, 0x84)
    n = struct.unpack("<I", dec(d[o:o + 4]))[0]
    r["tables"] = []
    for i in range(n):
        e = dec(d[o + 4 + i * 0x50:o + 4 + (i + 1) * 0x50])
        name = e[:0x40].split(b"\0")[0].decode("latin1")
        coff, ccount, rcount = struct.unpack_from("<I", e, 0x40)[0], *struct.unpack_from("<II", e, 0x48)
        p, rows = coff, []
        for _ in range(rcount + 1):
            row = []
            for _ in range(ccount):
                q = d.index(bytes([KEY]), p)  # NUL (0x00) encrypts to 0xF7
                row.append(dec(d[p:q]).decode("latin1"))
                p = q + 1
            rows.append(row)
        r["tables"].append({"name": name, "offset": coff, "columns": rows[0], "rows": rows[1:]})
    return r


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    json.dump(parse_prg(sys.argv[1]), sys.stdout, indent=1)
