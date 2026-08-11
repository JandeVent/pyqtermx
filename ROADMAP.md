# pyqtermx — Implementation Roadmap

Build a terminal emulator compatible with **ECMA-48**, **VT102**, and **xterm** (de facto modern standard), one testable milestone at a time.

## Architecture

A terminal emulator is not "a window that shows text". It is a pipeline with 4 layers:

```
PTY (shell) → ① byte parser  → ② screen model (grid)  → ③ renderer (Qt)
                  (state machine)   (cells, cursor, modes)   (glyphs → pixels)
                        ←────────────────────────────────────  input path (keys → escape sequences)
```

## Standards: layers of the onion

| Standard | Role |
|---|---|
| **ECMA-48** | The *grammar*: how sequences are formed (CSI = `ESC [` or `0x9B`, params `0x30–0x3F`, intermediates `0x20–0x2F`, final `0x40–0x7E`) and the abstract repertoire (CUU, CUP, ED, SGR…) |
| **VT102** | A *specific device*: ECMA-48 subset + DEC private extensions (`?`-prefixed modes, DECSTBM, DECSC/DECRC, DEC line-drawing charset) |
| **xterm** | The *de facto standard*: everything above + OSC title/clipboard/hyperlinks, mouse tracking, truecolor, bracketed paste, DA/DSR queries |

**Strategy:** ECMA-48 core → DEC `?` private sequences → xterm extensions.

## Phases

Each phase is independently testable — never move on with a failing phase. Phases group sequence families by what they unlock; each family still gets its own unit tests.

Old step numbering maps to the phases as: 1–2 → Phase 1, 3–5 → Phase 2, 6–7 → Phase 3, 10–11 + scrollback → Phase 4, 8–9 + 12 → Phase 5.

### Phase 1 — Core pipeline ✅ done
Parser + dumb screen + the print path. Complete: 252 tests green, xterm fixture corpus passing.

- **Byte parser:** a byte-stream state machine: `GROUND, ESCAPE, CSI_ENTRY, CSI_PARAM, CSI_INTERMEDIATE, OSC_STRING, CHARSET, DCS_ENTRY…` — the full 15-state VT500 table, with DCS/APC/SOS/PM parse-and-ignore so the stream never desyncs (ADR-0002).
  - **Never line-based** — input can be split mid-sequence arbitrarily.
  - Code points in, bytes decoded upstream (`codecs.getincrementaldecoder`, ADR-0001); C1 controls like `0x9B` = CSI arrive directly.
  - Reference: xterm.js `src/common/parser/` (modern state table), pyte `streams.py` (simplest version).
- **Dumb screen + printable characters:** grid of cells (char, fg, bg, bold, underline, reverse, blink), cursor, CR/LF/BS/TAB/BEL. `render()` prints the grid as text to verify.
  - **Deferred wrap from day one** (pending-wrap flag on the cursor).
  - **Wide and combining chars** — wide glyphs fill two cells (explicit continuation cell), combining marks attach to the cell behind the cursor.
  - **256-color cell model** (fg/bg palette index) — SGR `0`/`30–37`/`40–47`/`38;5` land here so the milestone is real; the rest of SGR is Phase 2.
  - **Resize reflow** (ADR-0003): re-wrap every line at the new width instead of clipping.
  - **OSC parsing** — `_osc_string_rules` collects any payload (printable, Unicode, DEL) and terminates on BEL (DEC/xterm tradition, the de-facto standard), ST (`ESC \` / `0x9C` — the ECMA-48 form), or the two-byte ST quirk. Dispatch is Phase 5.
- **Verify:** feed sequences byte-by-byte and in chunks — identical result. The xterm fixture corpus passes (see Testing strategy).

### Phase 2 — Text-mode CSI (was Steps 3–5)
Everything a text-mode program (`ls`, `less`, `man`) emits. One coherent chunk — these families share the dispatcher seam and the screen primitives.

- Cursor moves: CUU/CUD/CUF/CUB (A–D), CUP/HVP, plus the line moves CNL/CPL
- Erase: ED (`J`), EL (`K`), ECH
- SGR (`m`) completion: 1/4/7/blink, 21/22/24/25/27 resets, 39/49 defaults, brights 90–97 / 100–107
- SM/RM + DECSET/DECRST mode registry (DECAWM, IRM, DECOM, NLM…)
- DECSTBM (scroll region — `less` needs it), SU/SD, IL/DL, ICH/DCH
- DECSC/DECRC (`ESC 7/8`), HTS/TBC
- G0/G1 designation + DEC line-drawing (`ESC ( 0` — needed for `man`, `ls` boxes)
- **Wrapped-row flag** per row (ADR-0003): marks a line that ended in a pending wrap, so reflow-on-widen stops merging distinct full-width rows. Scrollback (Phase 4) needs it anyway.

Region + origin-mode interaction is where emulators get subtle.
**Milestone:** `ls | less` and `man` render boxes correctly; fixture t0080-HT un-skips (cursor motion lands here).

### Phase 3 — Full-screen apps & color (was Steps 6–7) ✅ done
Complete: 441 tests green, milestone fixture t0081-vim-session passing.

- Alternate screen (`?47`/`?1047`/`?1049`) with xterm.js semantics: per-screen state (grid, cursor position, scroll region, tab stops, DECSC slot), erase-fill entry, clear-on-exit, cursor carry (ADR-0004)
- DECALN (`ESC # 8`): screen alignment test — the active grid becomes `E` in the cursor's full rendition, wrapped flags cleared
- Reverse video (`?5`, DECSCNM) via the `effective_rendition(x, y)` seam — XOR stacking with SGR reverse
- Truecolor (`38;2;r;g;b` / `48;2`): RGB ints in the cell model (`≥ 0x1000000`), truncated-ignored, clamp-to-255 deviation
- Bold-as-bright deferred to the renderer (contract pinned in the spec — no seam this phase)

**Milestone: a scripted vim-style session renders headlessly, deterministically** (a hand-built 80×25 fixture in the conformance corpus).

### Phase 4 — PTY + scrollback + GUI (was Steps 10–11) ✅ done
The point where the pipeline becomes a terminal. Scrollback moves here from the old Step 4 — it is invisible until a GUI exists, and it needs the wrapped-row flag from Phase 2. Design locked in the grilling session (ADR-0005, ADR-0006).

- **Slice A (headless)**: `pyqtermx/ptyspawn.py` — fork + setsid, Qt-free `Pty` interface (optional `cwd` spawns the child in a working directory); a reader thread as the **single writer** (command queue for send/resize/scroll/close, snapshot signals — ADR-0005); scrollback on the screen: history rows above the grid, one-stream reflow, xterm retention (full-screen scroll only, bounded 1000, alt excluded), ED3, viewport API (ADR-0006). Tested with fake child programs in pytest.
- **Slice B (PyQt6)**: custom QPainter `TerminalWidget` (cell metrics, wide/combining chars, fractional-width grid alignment), dirty-line snapshot repaint, bold-as-bright (ADR-0004 §9); cursor that blinks at 500 ms while focused, inverts the cell it sits on, and becomes a hollow outline when the widget loses focus; `encode_key` input path (DECCKM `?1`, bracketed paste `?2004`, modifier encoding, PgUp/PgDn viewport policy); debounced resize → reflow → TIOCSWINSZ; QScrollBar; single-window app shell, `$SHELL` + `TERM=xterm-256color`, SIGTERM + waitpid on close (guarded by a `tcgetpgrp` foreground-job check); `set_font`/`set_palette` theming.

Reference: xterm.js ships no renderer sources.
**Milestone:** a real shell you can type into.

### Phase 5 — Dialogue & conformance (was Steps 8–9, 12)
Programs *ask* the terminal things; the terminal replies. Needs the PTY from Phase 4 to be observable end-to-end — which is why it comes after it.

- Queries: DA1 (`\x1b[c` → reply `\x1b[?1;2c`), DSR cursor position (`\x1b[6n` → `\x1b[row;colR`), DECRPM. Terminfo-driven apps hang or break without these.
- OSC dispatch — all work lands in `emulator.osc_dispatch()`: split the payload on `;` and dispatch on the first field.

| OSC | Purpose | Priority |
|---|---|---|
| `0` | window title + icon title (`0;title`) | ★★★★★ |
| `2` | window title only (`2;title`) | ★★★★★ |
| `8` | hyperlinks (`8;;URI` … text … `8;;`) | ★★★★★ |
| `52` | clipboard (`52;c;base64`) — SSH must-have | ★★★★★ |
| `7` | cwd sync (`7;file://host/path`) | ★★★★ |
| `4` / `10` / `11` | palette / fg / bg color queries — ✅; the set forms (palette mutation) remain | ★★★★ |
| `12` / `112` | cursor color — set/query ✅ (the renderer paints the block in it); `112` resets to the default inverted block | ★★★★ |
| `133` | shell integration (prompt/command markers) | ★★★★ |
| `633` | VS Code shell integration | ★★★ |
| `9` / `9;9` | notification / WSL cwd | ★★★ |

Notes: OSC 8 `params` may be empty — `8;;URI` is the common form; OSC 52 is pure base64 (no escaping needed); the terminator used (BEL vs ST) is invisible to the dispatcher. The color queries (`4;?`, `4;i;?`, `10;?`, `11;?`) reply BEL-terminated with the xterm 16-bit `rgb:RRRR/GGGG/BBBB` form, sourced from `pyqtermx/palette.py` — the single source of truth shared with the GUI renderer, so a themed terminal reports its themed colors. Set forms (`4;i;spec`, `10;spec`, `11;spec`) parse-and-ignore until palette mutation lands. OSC 12 (`12;#rrggbb` / `12;rgb:RRRR/GGGG/BBBB`) applies the app's cursor color, `12;?` reports it back, and `112` resets it.
**Milestone:** `printf '\033]0;hi\007'` sets the window title.

- Interactive input: mouse (1000/1003/1006 SGR), bracketed paste (2004), focus reporting — then **run `vttest`** (the canonical conformance suite).

## Key design decisions (make early)

1. **UTF-8 decoding before parsing** — decode bytes → code points incrementally (`codecs.getincrementaldecoder`), then feed the parser code points. C1 controls (`0x9B` = CSI) can appear directly. (ADR-0001 — decided)
2. **Resize behavior: reflow vs. clip** — xterm.js reflows scrollback; VT102 clipped. Reflow is what users expect today. (ADR-0003 — decided)
3. **Don't copy pyte's code** (LGPL, dated — no truecolor) — but its *architecture* (`streams.py` / `escape.py` / `screens.py`) is the clearest skeleton in Python.
4. **Day-to-day reference:** xterm's `ctlseqs.txt` (control sequences doc); ECMA-48 for grammar; vt100.net for DEC history.

## Reference material in this repo

| Reference | Use for |
|---|---|
| `references/pyte/` | Minimal engine architecture: `streams.py` (parser), `escape.py` (sequence names), `screens.py` (screen model + actions) |
| `references/xterm.js/` | Modern parser (submodule — the vendor tree ships no renderer sources); `test/fixtures/escape_sequence_files/` for tests |
| `ECMA-48.md` | The grammar and repertoire spec |

## Testing strategy

- **Unit tests per sequence family**.
- **xterm fixtures as the oracle** — `references/xterm.js/test/fixtures/escape_sequence_files/` (`.in`/`.text` pairs captured from real xterm, 80×25) feed through the full pipeline and diff against `render()`. The earlier pyte-as-oracle idea is dropped: pyte wraps immediately at the right edge, which conflicts with our deferred-wrap behavior, so it is not a valid oracle.
  - Skipped fixtures unlock with phases: t0080-HT (cursor motion) at Phase 2; t0004-LF (needs terminal echo ON — reproducible only through a pty) at Phase 4.
- **Real programs** at each milestone: `ls`, `less`, `man` (Phase 2), `vim`, `htop`, `tmux` (Phase 3), a shell (Phase 4).
- **`vttest`** at the end (Phase 5).
