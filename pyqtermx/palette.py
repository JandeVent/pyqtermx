# -*- coding: utf-8 -*-
# Copyright (C) 2018-2026 Connet Information Technology Company, Shanghai.
"""The terminal color palette — Qt-free (ADR-0005).

The single source of truth for the colors the emulator can answer OSC
4/10/11 color queries with: the 16 ANSI colors, the cube levels and
grayscale ramp that complete the 256-entry palette, and the default
foreground/background. The GUI renderer imports the same tables, so a
themed terminal reports the themed colors — never a hardcoded default.
"""

from __future__ import annotations

#: xterm's 16 ANSI colors (bright variants in the second half).
PALETTE16: tuple[int, ...] = (
    0x000000, 0xCD0000, 0x00CD00, 0xCDCD00, 0x0000EE, 0xCD00CD, 0x00CDCD, 0xE5E5E5,
    0x7F7F7F, 0xFF0000, 0x00FF00, 0xFFFF00, 0x5C5CFF, 0xFF00FF, 0x00FFFF, 0xFFFFFF,
)

#: The 6×6×6 color cube levels (16–231).
CUBE_LEVELS: tuple[int, ...] = (0, 95, 135, 175, 215, 255)

#: The default foreground/background RGB (the `-1` cell colors).
DEFAULT_FG_RGB: tuple[int, int, int] = (0xE8, 0xE8, 0xE8)
DEFAULT_BG_RGB: tuple[int, int, int] = (0x10, 0x10, 0x10)


def palette_rgb(index: int) -> tuple[int, int, int]:
    """The (r, g, b) of palette index 0–255: the 16 ANSI colors, then
    the 6×6×6 cube (16–231), then the grayscale ramp (232–255). An
    index outside 0–255 returns black (the renderer's fallback for
    invalid codes)."""
    if index < 16:
        value = PALETTE16[index]
        return (value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF
    if index < 232:
        value = index - 16
        return (
            CUBE_LEVELS[value // 36],
            CUBE_LEVELS[(value // 6) % 6],
            CUBE_LEVELS[value % 6],
        )
    if index < 256:
        gray = 8 + 10 * (index - 232)
        return gray, gray, gray
    return 0, 0, 0


def rgb_hex(r: int, g: int, b: int) -> str:
    """`#rrggbb` lowercase hex — the `QColor.name(HexRgb)` form the
    widget forwards, and the input form `set_palette` accepts."""
    return f"#{r:02x}{g:02x}{b:02x}"
