#!/usr/bin/env python3
"""Render a crisp, compact preview image (preview-zh.png) directly from the
actual PBF bitmap fonts inside a built .pbl package (paired with PebbleOS base
Latin PBF fonts for authentic mixed Chinese + English rendering).

Requires Pillow (e.g. `uv run --with pillow python3 scripts/render_preview.py ...`).
"""

from __future__ import annotations

import argparse
import struct
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

from build_pbl import PBPACK_DATA_START

SLOTS = [
    (2, "GOTHIC_14", "14 Regular (13px)", "common/base/pbf/GOTHIC_14.pbf"),
    (3, "GOTHIC_14_BOLD", "14 Bold (13px)", "normal/base/pbf/GOTHIC_14_BOLD.pbf"),
    (4, "GOTHIC_18", "18 Regular (16px)", "normal/base/pbf/GOTHIC_18.pbf"),
    (5, "GOTHIC_18_BOLD", "18 Bold (16px)", "common/base/pbf/GOTHIC_18_BOLD.pbf"),
    (19, "ROBOTO_CONDENSED_21", "Roboto Cond 21 (18px)", "normal/base/pbf/ROBOTO_CONDENSED_21.pbf"),
    (6, "GOTHIC_24", "24 Regular (19px)", "common/base/pbf/GOTHIC_24.pbf"),
    (7, "GOTHIC_24_BOLD", "24 Bold (19px)", "normal/base/pbf/GOTHIC_24_BOLD.pbf"),
    (8, "GOTHIC_28", "28 Regular (22px)", "normal/base/pbf/GOTHIC_28.pbf"),
    (9, "GOTHIC_28_BOLD", "28 Bold (22px)", "normal/base/pbf/GOTHIC_28_BOLD.pbf"),
    (10, "GOTHIC_36", "36 Regular (26px)", "normal/base/pbf/GOTHIC_36.pbf"),
    (11, "GOTHIC_36_BOLD", "36 Bold (26px)", "common/base/pbf/GOTHIC_36_BOLD.pbf"),
]


class PbfFont:
    def __init__(self, data: bytes) -> None:
        ver, max_h, num_glyphs, wildcard = struct.unpack_from("<BBHH", data, 0)
        self.ver = ver
        self.max_h = max_h
        self.wildcard = wildcard
        self.glyphs: dict[int, tuple[int, int, int, int, int, bytes]] = {}
        if ver < 2:
            return
        ht_size, cp_bytes = struct.unpack_from("<BB", data, 6)
        info_sz, features = struct.unpack_from("<BB", data, 8) if ver >= 3 else (8, 0)
        ht_entries = [struct.unpack_from("<BBH", data, info_sz + i * 4) for i in range(ht_size)]
        ot_start = info_sz + ht_size * 4
        entry_sz = (2 if (features & 1) else 4) + cp_bytes
        total_ot = sum(c for _, c, _ in ht_entries)
        gt_start = ot_start + total_ot * entry_sz
        is_rle4 = bool(features & 2)

        for _hv, cnt, h_off in ht_entries:
            pos = ot_start + h_off
            for _ in range(cnt):
                cp = struct.unpack_from("<H" if cp_bytes == 2 else "<I", data, pos)[0]
                g_off = struct.unpack_from("<H" if (features & 1) else "<I", data, pos + cp_bytes)[0]
                pos += entry_sz
                if g_off == 0 and cp != wildcard:
                    continue
                g_pos = gt_start + g_off
                w, h, left, top, adv = struct.unpack_from("<BBbbb", data, g_pos)
                if is_rle4:
                    # RLE4: w holds rle_units count, h=0? Actually in PBF v3 RLE4, header is
                    # width, height, left, top, advance + rle4_bytes (nibbles)
                    # Decode nibbles (1 bit colour, 3 bits length 1..8)
                    bits = bytearray()
                    nbits = w * h
                    p = g_pos + 5
                    while len(bits) < nbits and p < len(data):
                        byte = data[p]
                        p += 1
                        for nib in (byte & 0x0F, (byte >> 4) & 0x0F):
                            color = (nib >> 3) & 1
                            run = (nib & 7) + 1
                            bits.extend([color] * run)
                            if len(bits) >= nbits:
                                break
                    raw = bytes(bits[:nbits])
                else:
                    nbits = w * h
                    bm_bytes = (nbits + 7) // 8
                    packed = data[g_pos + 5 : g_pos + 5 + bm_bytes]
                    bits = bytearray(nbits)
                    idx = 0
                    for b in packed:
                        for k in range(8):
                            if idx >= nbits:
                                break
                            bits[idx] = (b >> k) & 1
                            idx += 1
                    raw = bytes(bits)
                self.glyphs[cp] = (w, h, left, top, adv, raw)

    def get(self, cp: int):
        return self.glyphs.get(cp)


def draw_pbf_line(
    img: Image.Image,
    x0: int,
    y0: int,
    text: str,
    base_font: PbfFont | None,
    ext_font: PbfFont,
    scale: int = 2,
    fg: tuple[int, int, int] = (20, 24, 31),
) -> int:
    """Render mixed text exactly like PebbleOS: check base font first, fall back to ext font."""
    px = img.load()
    cursor = 0
    for ch in text:
        cp = ord(ch)
        glyph = base_font.get(cp) if base_font is not None else None
        if glyph is None:
            glyph = ext_font.get(cp)
        if glyph is None:
            glyph = ext_font.get(ext_font.wildcard)
        if glyph is None:
            continue
        w, h, left, top, adv, bits = glyph
        for gy in range(h):
            row_off = gy * w
            for gx in range(w):
                if bits[row_off + gx]:
                    dx = x0 + (cursor + left + gx) * scale
                    dy = y0 + (top + gy) * scale
                    for sy in range(scale):
                        for sx in range(scale):
                            if 0 <= dx + sx < img.width and 0 <= dy + sy < img.height:
                                px[dx + sx, dy + sy] = fg
        cursor += adv
    return cursor * scale


def main() -> int:
    parser = argparse.ArgumentParser(description="Render compact bitmap preview from .pbl")
    parser.add_argument("--pbl", type=Path, required=True, help="Path to built .pbl")
    parser.add_argument("--pebbleos-dir", type=Path, default=None, help="Path to PebbleOS repo (for base Latin PBFs)")
    parser.add_argument("--out", type=Path, default=Path("preview-zh.png"), help="Output PNG path")
    parser.add_argument("--version", type=str, default="v11", help="Version label shown in header")
    args = parser.parse_args()

    data = args.pbl.read_bytes()
    num_files = struct.unpack_from("<I", data, 0)[0]
    pbl_fonts: dict[int, PbfFont] = {}
    for i in range(num_files):
        fid, off, length, _ = struct.unpack_from("<IIII", data, 12 + i * 16)
        if fid >= 2 and length > 0:
            pbl_fonts[fid] = PbfFont(data[PBPACK_DATA_START + off : PBPACK_DATA_START + off + length])

    base_fonts: dict[int, PbfFont | None] = {}
    for fid, _name, _label, rel_pbf in SLOTS:
        if args.pebbleos_dir is not None:
            p = args.pebbleos_dir / "resources" / rel_pbf
            base_fonts[fid] = PbfFont(p.read_bytes()) if p.exists() else None
        else:
            base_fonts[fid] = None

    sample_a = "设置 / 抬手背光 / 显示时长 5秒  12:34  80%"
    sample_b = "微信：明早九点开会，别迟到！苏州 晴 25° 93 BPM"

    scale = 2
    pad_x = 36
    label_w = 240
    row_gap = 18
    header_h = 84
    footer_h = 36

    # Measure required width & height
    row_heights = []
    max_text_w = 0
    for fid, _name, _label, _rel in SLOTS:
        ext = pbl_fonts[fid]
        base = base_fonts[fid]
        # Compute visual bottom from '中' glyph when no base PBF is present (e.g. Slot 19)
        if base:
            line_px = base.max_h
        else:
            zh_g = ext.get(ord("中"))
            line_px = (zh_g[3] + zh_g[1]) if zh_g else ext.max_h
        line_h = line_px * scale
        sample = sample_a if fid not in (10, 11) else "苏州 晴 25° 93 BPM 抬手背光"
        # Estimate advance width
        adv_sum = 0
        for ch in sample:
            g = (base.get(ord(ch)) if base else None) or ext.get(ord(ch)) or ext.get(ext.wildcard)
            if g:
                adv_sum += g[4]
        max_text_w = max(max_text_w, adv_sum * scale)
        row_heights.append((fid, _label, line_h, sample))

    # Add a second compact showcase card at the bottom showing 18/24 Bold mixed notifications
    width = max(1120, pad_x * 2 + label_w + max_text_w + 40)
    total_rows_h = sum(max(36, rh + 16) + row_gap for _, _, rh, _ in row_heights)
    card_h = 172
    height = header_h + total_rows_h + card_h + footer_h

    bg = (248, 249, 251)
    card_bg = (255, 255, 255)
    border = (224, 229, 236)
    baseline_col = (214, 224, 238)
    label_col = (95, 108, 128)
    title_col = (20, 24, 31)
    sub_col = (100, 112, 130)

    img = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(img)

    # Load system TTF for clean metadata labels
    try:
        title_font = ImageFont.truetype("/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc", 24, index=2)
        sub_font = ImageFont.truetype("/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc", 15, index=2)
        lbl_font = ImageFont.truetype("/usr/share/fonts/noto/NotoSans-Medium.ttf", 14)
    except OSError:
        title_font = sub_font = lbl_font = ImageFont.load_default()

    # Header
    draw.text((pad_x, 22), f"Pebble 简体中文语言包（思源黑体 Noto Sans SC · {args.version}）", fill=title_col, font=title_font)
    draw.text(
        (pad_x, 54),
        "11 个完整字体槽（常规 + 真粗体，2× 点阵像素无损渲染）· 基线与 PebbleOS 官方英文字体精确对齐",
        fill=sub_col,
        font=sub_font,
    )

    # Font table card
    card_top = header_h
    card_bot = header_h + total_rows_h + 6
    draw.rounded_rectangle([pad_x, card_top, width - pad_x, card_bot], radius=12, fill=card_bg, outline=border, width=1)

    y = card_top + 16
    for idx, (fid, label, line_h, sample) in enumerate(row_heights):
        ext = pbl_fonts[fid]
        base = base_fonts[fid]
        row_h = max(36, line_h + 16)
        if idx > 0:
            draw.line([(pad_x + 18, y - row_gap // 2), (width - pad_x - 18, y - row_gap // 2)], fill=(240, 243, 247), width=1)

        # Label on left
        lbl_y = y + (row_h - 18) // 2
        draw.text((pad_x + 20, lbl_y), f"Slot {fid:02d}  {label}", fill=label_col, font=lbl_font)

        # Baseline guide + rendered PBF text
        text_x = pad_x + label_w
        text_y = y + (row_h - line_h) // 2
        # Baseline at bottom of base_font.max_h
        base_y = text_y + line_h
        rendered_w = draw_pbf_line(img, text_x, text_y, sample, base, ext, scale=scale)
        draw.line([(text_x, base_y + 1), (text_x + rendered_w, base_y + 1)], fill=baseline_col, width=1)

        y += row_h + row_gap

    # Bottom showcase card: Chinese notification & Traditional variant glyphs
    sc_top = card_bot + 18
    sc_bot = sc_top + card_h - 18
    draw.rounded_rectangle([pad_x, sc_top, width - pad_x, sc_bot], radius=12, fill=card_bg, outline=border, width=1)
    draw.text((pad_x + 20, sc_top + 16), "通知与繁体变体实效预览（Slot 04 / 05 · Gothic 18 Regular / Bold）", fill=label_col, font=sub_font)
    draw_pbf_line(img, pad_x + 20, sc_top + 50, sample_b, base_fonts[5], pbl_fonts[5], scale=scale)
    draw_pbf_line(
        img,
        pad_x + 20,
        sc_top + 96,
        "繁體聽寫相容：氣溫、甚麼、時間、電話、開關 / 睡眠 6h 22min / 區間時長",
        base_fonts[4],
        pbl_fonts[4],
        scale=scale,
        fg=(55, 65, 81),
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    img.save(args.out, optimize=True)
    print(f"Rendered {args.out}: {img.size[0]}x{img.size[1]} ({args.out.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
