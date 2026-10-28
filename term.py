"""Paints an iTerm2 screen as ANSI so a browser terminal (xterm.js) can mirror it; input goes back as typed text."""
FLAGS = (("bold", "1"), ("faint", "2"), ("italic", "3"), ("underline", "4"), ("blink", "5"), ("inverse", "7"), ("invisible", "8"), ("strikethrough", "9"))


def color(c, base: int) -> str:
    """SGR for an iTerm cell color: base is 30 (foreground) or 40 (background); defaults and system colors give ''."""
    if c is None:
        return ""
    if c.is_rgb:
        return f"{base + 8};2;{c.rgb.red};{c.rgb.green};{c.rgb.blue}"
    if c.is_standard:
        return f"{base + 8};5;{c.standard}"
    return ""


def sgr(style) -> str:
    if style is None:
        return ""
    params = [code for name, code in FLAGS if getattr(style, name)]
    params += [p for p in (color(style.fg_color, 30), color(style.bg_color, 40)) if p]
    return ";".join(params)


def render_row(line, columns: int) -> str:
    cells = []
    for x in range(columns):
        try:
            ch = line.string_at(x)
        except IndexError:  # uninitialised cell
            ch = " "
        if ch:  # a wide character's second cell is empty
            cells.append((sgr(line.style_at(x)), ch.replace("\x00", " ")))
    while cells and cells[-1] == ("", " "):
        cells.pop()  # the erase-to-end-of-line in paint() covers the blank tail
    out, current = [], ""
    for style, ch in cells:
        if style != current:
            out.append(f"\x1b[0;{style}m" if style else "\x1b[0m")
            current = style
        out.append(ch)
    return "".join(out)


def paint(rows: list[str], previous: list[str], cursor: tuple[int, int]) -> str:
    """ANSI that turns a terminal showing `previous` into `rows`, rewriting only the rows that changed."""
    out = [f"\x1b[{y + 1};1H{row}\x1b[0m\x1b[K" for y, row in enumerate(rows) if y >= len(previous) or previous[y] != row]
    out.append(f"\x1b[{cursor[1] + 1};{cursor[0] + 1}H")
    return "".join(out)


if __name__ == "__main__":
    class Color:
        def __init__(self, rgb=None, standard=None):
            self.is_rgb, self.is_standard = rgb is not None, standard is not None
            self.rgb = type("RGB", (), dict(zip(("red", "green", "blue"), rgb or (0, 0, 0))))
            self.standard = standard

    class Style:
        def __init__(self, fg=None, bg=None, **flags):
            self.fg_color, self.bg_color = fg, bg
            for name, _ in FLAGS:
                setattr(self, name, flags.get(name, False))

    class Line:
        def __init__(self, cells):
            self.cells = cells

        def string_at(self, x):
            return self.cells[x][0]

        def style_at(self, x):
            return self.cells[x][1] if x < len(self.cells) else None  # like the real API

    plain = [("a", None), ("b", Style()), (" ", None), (" ", None)]
    assert render_row(Line(plain), 4) == "ab"  # default style needs no escape, blank tail is trimmed
    red_bold = Style(fg=Color(standard=1), bold=True)
    row = render_row(Line([("x", red_bold), ("y", red_bold), ("z", Style(bg=Color(rgb=(1, 2, 3))))]), 3)
    assert row == "\x1b[0;1;38;5;1mxy\x1b[0;48;2;1;2;3mz", repr(row)
    wide = render_row(Line([("世", None), ("", None), ("!", None)]), 3)
    assert wide == "世!", repr(wide)
    assert render_row(Line([("\x00", None)]), 3) == ""  # blank tail, including cells past the line's end
    assert sgr(Style(inverse=True, underline=True)) == "4;7"
    assert paint(["a", "b"], [], (3, 1)) == "\x1b[1;1Ha\x1b[0m\x1b[K\x1b[2;1Hb\x1b[0m\x1b[K\x1b[2;4H"
    assert paint(["a", "c"], ["a", "b"], (0, 0)) == "\x1b[2;1Hc\x1b[0m\x1b[K\x1b[1;1H"
    print("ok")
