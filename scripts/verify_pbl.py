#!/usr/bin/env python3
"""Comprehensive offline verifier for PebbleOS .pbl language packs.

Checks:
  1. pbpack header, table bounds, trailing bytes, STM32 CRC-32 (pack + every slot)
  2. GNU MO format, strictly sorted UTF-8 keys, PebbleOS PJW hash lookup (100% hit)
  3. Required PebbleOS metadata headers (Project-Id-Version, Language, Name)
  4. Translation hygiene: no empty msgstr, matching printf format specifiers
  5. PBF v3 font headers, hash/offset/glyph table bounds
  6. Glyph coverage: all non-ASCII non-emoji chars in MO exist in PBF (or base font)
  7. Optional PebbleOS source coverage check (--pebbleos-dir)
"""

from __future__ import annotations

import argparse
import os
import re
import struct
import sys
from pathlib import Path

from build_pbl import PBPACK_DATA_START, PBPACK_TABLE_ENTRIES, stm32_crc32

SLOT_NAMES = {
    1: "STRINGS (.mo)",
    2: "GOTHIC_14",
    3: "GOTHIC_14_BOLD",
    4: "GOTHIC_18",
    5: "GOTHIC_18_BOLD",
    6: "GOTHIC_24",
    7: "GOTHIC_24_BOLD",
    8: "GOTHIC_28",
    9: "GOTHIC_28_BOLD",
    10: "GOTHIC_36",
    11: "GOTHIC_36_BOLD",
    19: "ROBOTO_CONDENSED_21",
}

FMT_RE = re.compile(
    r"%(?:\d+\$)?[-+ #0]*(?:\d+|\*)?(?:\.(?:\d+|\*))?(?:hh|h|ll|l|j|z|t|L)?[diuoxXfFeEgGaAcspn%]"
)

# Characters intentionally provided by PebbleOS base Gothic font or system emoji font
BASE_FONT_CHARS = {0x2013}  # en-dash '–' (present in PebbleOS base GOTHIC_*)


def is_emoji_cp(cp: int) -> bool:
    return (
        0x2600 <= cp <= 0x27BF
        or 0x1F300 <= cp <= 0x1FAFF
        or cp in (0xFE0F, 0x200D)
    )


def pjw_hash(b: bytes) -> int:
    hval = 0
    for c in b:
        hval = ((hval << 4) + c) & 0xFFFFFFFF
        g = hval & 0xF0000000
        if g:
            hval ^= g >> 24
            hval ^= g
    return hval & 0xFFFFFFFF


def parse_mo(mo: bytes) -> tuple[dict[str, str], dict[str, int]]:
    magic, rev, nstrings, orig_off, trans_off, hash_size, hash_off = struct.unpack_from("<7I", mo, 0)
    if magic != 0x950412DE:
        raise AssertionError(f"Invalid MO magic: 0x{magic:08x}")
    if hash_size == 0:
        raise AssertionError("MO file missing PJW hash table (hash_size == 0)")

    origs_raw: list[bytes] = []
    entries: dict[str, str] = {}
    for i in range(nstrings):
        olen, ooff = struct.unpack_from("<II", mo, orig_off + i * 8)
        tlen, toff = struct.unpack_from("<II", mo, trans_off + i * 8)
        ob = mo[ooff : ooff + olen]
        tb = mo[toff : toff + tlen]
        origs_raw.append(ob)
        entries[ob.decode("utf-8")] = tb.decode("utf-8")

    # GNU MO requires strictly sorted originals
    if not all(origs_raw[i] < origs_raw[i + 1] for i in range(len(origs_raw) - 1)):
        raise AssertionError("MO original strings are not strictly sorted")

    # Simulate PebbleOS PJW hash table lookup
    ht = struct.unpack_from(f"<{hash_size}I", mo, hash_off)
    hits = 0
    max_probes = 0
    for key in origs_raw:
        h = pjw_hash(key)
        pos = h % hash_size
        incr = 1 + (h % (hash_size - 2))
        probes = 0
        while True:
            probes += 1
            entry = ht[pos]
            if entry == 0:
                break
            if origs_raw[entry - 1] == key:
                hits += 1
                break
            pos = (pos + incr) % hash_size
            if probes > hash_size:
                break
        max_probes = max(max_probes, probes)

    if hits != nstrings:
        raise AssertionError(f"PJW hash lookup missed {nstrings - hits}/{nstrings} entries")

    return entries, {
        "rev": rev,
        "nstrings": nstrings,
        "hash_size": hash_size,
        "hits": hits,
        "max_probes": max_probes,
    }


def parse_pbf(chunk: bytes) -> dict:
    ver, max_h, num_glyphs, wildcard, ht_size, cp_bytes, info_sz, features = struct.unpack_from(
        "<BBHHBBBB", chunk, 0
    )
    if ver != 3:
        raise AssertionError(f"Unexpected PBF version: {ver}")
    ht_entries = [struct.unpack_from("<BBH", chunk, info_sz + i * 4) for i in range(ht_size)]
    ot_start = info_sz + ht_size * 4
    entry_sz = (2 if (features & 1) else 4) + cp_bytes
    total_ot = sum(c for _, c, _ in ht_entries)
    gt_start = ot_start + total_ot * entry_sz

    codepoints: set[int] = set()
    bad_offsets = 0
    for _hv, cnt, h_off in ht_entries:
        pos = ot_start + h_off
        for _ in range(cnt):
            cp = struct.unpack_from("<H" if cp_bytes == 2 else "<I", chunk, pos)[0]
            g_off = struct.unpack_from("<H" if (features & 1) else "<I", chunk, pos + cp_bytes)[0]
            pos += entry_sz
            codepoints.add(cp)
            if gt_start + g_off + 5 > len(chunk):
                bad_offsets += 1
    if bad_offsets:
        raise AssertionError(f"PBF has {bad_offsets} out-of-bounds glyph offsets")
    return {
        "max_h": max_h,
        "num_glyphs": num_glyphs,
        "features": features,
        "codepoints": codepoints,
    }


def check_pebbleos_coverage(pebbleos_dir: Path, mo_entries: dict[str, str]) -> list[str]:
    str_lit = r'"(?:[^"\\]|\\.)*"'
    adj_str = rf"({str_lit}(?:\s*{str_lit})*)"
    pat_ctx = re.compile(rf"\bpbl_i18n_ctx_(?:get|noop|get_with_buffer)\s*\(\s*{adj_str}\s*,\s*{adj_str}")
    pat_simple = re.compile(rf"\bpbl_i18n_(?:get|noop|get_with_buffer)\s*\(\s*{adj_str}")

    def unescape_c_bytes(raw_lit: str) -> str:
        # Handle C string escapes including \xNN UTF-8 byte sequences
        out = bytearray()
        i = 0
        while i < len(raw_lit):
            if raw_lit[i] == "\\" and i + 1 < len(raw_lit):
                nxt = raw_lit[i + 1]
                mp = {"n": b"\n", "r": b"\r", "t": b"\t", '"': b'"', "\\": b"\\", "'": b"'"}
                if nxt in mp:
                    out.extend(mp[nxt])
                    i += 2
                    continue
                if nxt == "x" and i + 3 < len(raw_lit):
                    out.append(int(raw_lit[i + 2 : i + 4], 16))
                    i += 4
                    continue
            out.extend(raw_lit[i].encode("utf-8"))
            i += 1
        return out.decode("utf-8")

    def join_adj(raw: str) -> str:
        return "".join(unescape_c_bytes(p) for p in re.findall(r'"((?:[^"\\]|\\.)*)"', raw))

    keys: set[str] = set()
    for root, _, files in os.walk(pebbleos_dir / "pebble"):
        for fn in files:
            if not fn.endswith((".c", ".h")):
                continue
            txt = Path(root, fn).read_text(encoding="utf-8", errors="ignore")
            for m in pat_ctx.finditer(txt):
                ctx, msg = join_adj(m.group(1)), join_adj(m.group(2))
                if msg:
                    keys.add(f"{ctx}\x04{msg}")
            for m in pat_simple.finditer(txt):
                msg = join_adj(m.group(1))
                if msg:
                    keys.add(msg)

    return sorted(k for k in keys if k not in mo_entries)


def main() -> int:
    # Self-test STM32 CRC-32 against official PebbleOS tools/stm32_crc.py vectors
    assert stm32_crc32(b"123 567 901 34") == 0x89F3BAB2
    assert stm32_crc32(b"123456789") == 0xAFF19057
    assert stm32_crc32(b"\xfe\xff\xfe\xff") == 0x0519B130
    assert stm32_crc32(b"\xfe\xff\xfe\xff\x88") == 0x495E02CA

    parser = argparse.ArgumentParser(description="Verify PebbleOS .pbl language pack integrity")
    parser.add_argument("pbl", type=Path, help="Path to .pbl file")
    parser.add_argument("--pebbleos-dir", type=Path, default=None, help="Optional PebbleOS repo root for string coverage")
    args = parser.parse_args()

    data = args.pbl.read_bytes()
    num_files, pack_crc, ts = struct.unpack_from("<III", data, 0)
    print(f"Verifying {args.pbl} ({len(data):,} bytes, {num_files} slots, ts={ts})")

    # 1. Verify slots and CRCs
    max_end = 0
    slots: dict[int, bytes] = {}
    for i in range(num_files):
        fid, off, length, ecrc = struct.unpack_from("<IIII", data, 12 + i * 16)
        assert fid == i + 1, f"Slot index {i} has unexpected fid {fid}"
        chunk = data[PBPACK_DATA_START + off : PBPACK_DATA_START + off + length]
        assert len(chunk) == length, f"Slot {fid} truncated"
        calc_crc = stm32_crc32(chunk) if length > 0 else 0xFFFFFFFF
        assert calc_crc == ecrc, f"Slot {fid} CRC mismatch: got 0x{calc_crc:08x}, expected 0x{ecrc:08x}"
        slots[fid] = chunk
        max_end = max(max_end, off + length)

    trailing = len(data) - (PBPACK_DATA_START + max_end)
    assert trailing == 0, f"Unexpected trailing bytes: {trailing}"

    calc_pack_crc = stm32_crc32(data[PBPACK_DATA_START:])
    assert calc_pack_crc == pack_crc, f"Pack CRC mismatch: 0x{calc_pack_crc:08x} != 0x{pack_crc:08x}"
    print(f"  [OK] pbpack CRCs valid (pack=0x{pack_crc:08x}, trailing=0)")

    # 2. Verify MO catalog
    mo_entries, mo_stats = parse_mo(slots[1])
    header = mo_entries.get("", "")
    for required in ("Project-Id-Version:", "Language: zh_CN", "Name: 简体中文"):
        assert required in header, f"Missing required MO header field: {required}"

    empty_trans = [k for k, v in mo_entries.items() if k != "" and v == ""]
    assert not empty_trans, f"Found {len(empty_trans)} empty translations: {empty_trans[:5]}"

    fmt_errors = []
    for k, v in mo_entries.items():
        if k == "":
            continue
        # Only check printf format parity on strings that don't use strftime %a/%b/%e/%H/%I/%k/%l/%m/%M/%p/%R/%S/%y
        if any(s in k for s in ("%H", "%M", "%S", "%I", "%p", "%R", "%a", "%b", "%B", "%e", "%y", "%k", "%l")):
            continue
        if FMT_RE.findall(k) != FMT_RE.findall(v):
            fmt_errors.append((k, v, FMT_RE.findall(k), FMT_RE.findall(v)))
    assert not fmt_errors, f"Format specifier mismatch: {fmt_errors[:5]}"

    n_valid = len(mo_entries) - 1
    print(
        f"  [OK] MO valid: {n_valid} translations (+1 header = {mo_stats['nstrings']}), "
        f"PJW hash {mo_stats['hits']}/{mo_stats['nstrings']} (max probes={mo_stats['max_probes']}), "
        f"0 empty, 0 format errors"
    )

    # 3. Verify PBF font slots
    font_cps: set[int] | None = None
    non_empty = 0
    for fid in range(2, num_files + 1):
        chunk = slots[fid]
        if not chunk:
            continue
        non_empty += 1
        info = parse_pbf(chunk)
        if font_cps is None:
            font_cps = info["codepoints"]
        else:
            assert info["codepoints"] == font_cps, f"Slot {fid} codepoint set differs from Slot 2"
        label = SLOT_NAMES.get(fid, f"SLOT_{fid}")
        print(f"  [OK] Slot {fid:2d} ({label:19s}): {len(chunk):7,} B, max_h={info['max_h']:2d}, glyphs={info['num_glyphs']}")

    assert font_cps is not None and non_empty > 0, "No non-empty font slots found"

    # 4. Cross-check all MO translation characters against PBF codepoints
    missing_chars: dict[str, list[str]] = {}
    for k, v in mo_entries.items():
        if k == "":
            continue
        for ch in v:
            cp = ord(ch)
            if cp >= 0x80 and cp not in font_cps and cp not in BASE_FONT_CHARS and not is_emoji_cp(cp):
                missing_chars.setdefault(ch, []).append(k)
    assert not missing_chars, f"MO contains characters missing from PBF: {missing_chars}"
    print(f"  [OK] Glyph coverage: 100% of non-emoji CJK/punctuation characters present in all {non_empty} font slots")

    # 5. Optional PebbleOS source coverage check
    if args.pebbleos_dir is not None:
        missing_keys = check_pebbleos_coverage(args.pebbleos_dir, mo_entries)
        if missing_keys:
            print(f"  [WARN] Missing {len(missing_keys)} PebbleOS pbl_i18n_* keys:")
            for mk in missing_keys[:20]:
                print(f"         - {mk!r}")
            return 1
        print("  [OK] PebbleOS pbl_i18n_* source coverage: 100% (0 missing keys)")

    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
