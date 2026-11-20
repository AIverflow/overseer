"""Colour themes (CSS layers, drawn textures, saved choice) and the heat palette of the overseer."""

import colorsys
import math
import os
import re
from xml.sax.saxutils import escape

HOME = os.path.expanduser("~")


HEAT_STEPS = 12  # colour levels between green (0) and red (1)
HEAT_LIGHT_THEMES = {"light", "yacht"}  # light surfaces get darker colours
HEAT_OFF_THEMES = {"cga"}  # the 4-colour theme keeps its 4 colours


def _heat_palette(light, sat):
    out = []
    for i in range(HEAT_STEPS):
        r, g, b = colorsys.hls_to_rgb(120 * (1 - i / (HEAT_STEPS - 1)) / 360, light, sat)
        out.append(f"#{round(r * 255):02x}{round(g * 255):02x}{round(b * 255):02x}")
    return out


HEAT = {"dark": _heat_palette(0.62, 0.80), "light": _heat_palette(0.33, 0.85),
        "yacht": _heat_palette(0.27, 0.95)}  # white panels on a bright deck: darker still


def heat(text, frac, theme="dark"):
    """Pango markup for `text`, coloured by frac (0 = green ... 1 = red, in HEAT_STEPS steps); escaped, plain in
    themes that keep their own colours."""
    text = escape(text)
    if theme in HEAT_OFF_THEMES:
        return text
    pal = HEAT.get(theme) or HEAT["light" if theme in HEAT_LIGHT_THEMES else "dark"]
    color = pal[round(max(0.0, min(1.0, frac)) * (HEAT_STEPS - 1))]
    return f'<span foreground="{color}">{text}</span>'


def heat_percents(text, theme="dark"):
    """`text` as markup where every 'NN%' is coloured by its value."""
    return re.sub(r"\d+%", lambda m: heat(m.group(0), int(m.group(0)[:-1]) / 100, theme), escape(text))


CSS = b"""
* { font-family: "Ubuntu Sans", "Noto Sans", sans-serif; }
window, list { background: #0f1115; }
headerbar {
  min-height: 26px; padding: 2px 6px; border: none; box-shadow: none;
  background: #0f1115; border-bottom: 1px solid #1c1f26;
}
headerbar .title { font-size: 12px; font-weight: 600; color: #e5e7eb; }
headerbar .subtitle { font-size: 10px; color: #6b7280; }
headerbar button { min-height: 20px; min-width: 20px; padding: 0; color: #9ca3af;
                   background: none; border: none; box-shadow: none; }
headerbar button:hover { background: #262a33; }
list row, list row:hover, list row:focus { background: none; padding: 0; outline: none; }
.card {
  margin: 4px 8px; padding: 7px 12px 7px 10px; border-radius: 8px;
  background: #181b21; border-left: 3px solid #4b5563;
}
list row:hover .card { background: #20242c; }
.age { font-family: "Ubuntu Sans Mono", monospace; font-weight: 700; font-size: 12px;
       padding: 2px 8px; border-radius: 9px; }
.name { font-size: 13px; font-weight: 600; color: #f3f4f6; }
.cwd { font-size: 11px; color: #9ca3af; }
.plus { min-height: 18px; min-width: 18px; padding: 0 4px; font-size: 12px; color: #9ca3af;
        background: none; border: none; box-shadow: none; }
.plus:hover { background: #262a33; }
.plus.err { color: #fbbf24; }
.plus.ask { color: #d8b4fe; font-weight: 700; }
.chart { font-family: "Ubuntu Sans Mono", monospace; font-size: 11px; color: #9ca3af; }
.badge { font-family: "Ubuntu Sans Mono", monospace; font-size: 8px; font-weight: 700;
         color: #e5e7eb; background: #0f1115; border: 1px solid #374151;
         border-radius: 3px; padding: 0 2px; }
.model { font-family: "Ubuntu Sans Mono", monospace; font-size: 10px; color: #a5b4fc;
         background: #1e2235; border-radius: 6px; padding: 1px 6px; }
.ok   { border-left-color: #22c55e; } .ok   .age { color: #4ade80; background: rgba(34,197,94,0.14); }
.warn { border-left-color: #f59e0b; } .warn .age { color: #fbbf24; background: rgba(245,158,11,0.16); }
.late { border-left-color: #ef4444; } .late .age { color: #f87171; background: rgba(239,68,68,0.16); }
.wait { border-left-color: #a855f7; } .wait .age { color: #d8b4fe; background: rgba(168,85,247,0.18); }
.busy { border-left-color: #3b82f6; } .busy .age { color: #93c5fd; background: rgba(59,130,246,0.16); }
"""



# ---------------------------------------------------------------- themes
# Each theme is a CSS layer on top of CSS (higher priority, so it must redeclare the level colours).
SCAN = ("repeating-linear-gradient(to bottom, rgba(0,0,0,0.45) 0px, rgba(0,0,0,0.45) 1px, "
        "rgba(0,0,0,0) 1px, rgba(0,0,0,0) 3px)")
# Drawn textures: SVG files written to ART_DIR at start (write_art), drawn by GTK's SVG loader.
# Theme CSS names them as url("@ART@/name.svg").
ART_DIR = f"{HOME}/.cache/overseer/art"


def _gear(cx, cy, r, teeth, rot):
    """SVG of one gear: toothed rim with a round opening, 5 spokes, hub."""
    step = 2 * math.pi / teeth
    pts = []
    for i in range(teeth):
        a = math.radians(rot) + i * step
        pts += [(r - 9, a), (r, a + step * 0.18), (r, a + step * 0.42), (r - 9, a + step * 0.6)]
    rim = "M" + " L".join(f"{cx + d * math.cos(a):.1f},{cy + d * math.sin(a):.1f}" for d, a in pts) + " Z"
    h = r - 18  # the opening inside the rim
    rim += f" M{cx + h},{cy} a{h},{h} 0 1,0 {-2 * h},0 a{h},{h} 0 1,0 {2 * h},0 Z"
    spokes = "".join(f'<rect x="{cx - 3}" y="{cy - h}" width="6" height="{2 * h}" '
                     f'transform="rotate({rot + k * 36} {cx} {cy})"/>' for k in range(5))
    return f'<path fill-rule="evenodd" d="{rim}"/>{spokes}<circle cx="{cx}" cy="{cy}" r="{r * 0.2:.0f}"/>'


ART = {
    # dark walnut: long dark grain plus fine light fibres (rows, title bar)
    "walnut.svg": """<svg xmlns="http://www.w3.org/2000/svg" width="900" height="160">
<filter id="g" x="0" y="0" width="100%" height="100%">
<feTurbulence type="fractalNoise" baseFrequency="0.0035 0.11" numOctaves="4" seed="7"/>
<feColorMatrix type="matrix" values="0 0 0 0 0.10  0 0 0 0 0.05  0 0 0 0 0.02  0 0 0 -1.6 1.15"/></filter>
<filter id="f" x="0" y="0" width="100%" height="100%">
<feTurbulence type="fractalNoise" baseFrequency="0.002 0.6" numOctaves="2" seed="2"/>
<feColorMatrix type="matrix" values="0 0 0 0 0.50  0 0 0 0 0.30  0 0 0 0 0.16  0 0 0 0.9 -0.45"/></filter>
<rect width="900" height="160" fill="#3a2010"/>
<rect width="900" height="160" filter="url(#g)"/><rect width="900" height="160" filter="url(#f)"/></svg>""",
    # brushed dark steel: fine horizontal streaks (rows)
    "steel.svg": """<svg xmlns="http://www.w3.org/2000/svg" width="900" height="160">
<filter id="b" x="0" y="0" width="100%" height="100%">
<feTurbulence type="fractalNoise" baseFrequency="0.0015 0.9" numOctaves="2" seed="11"/>
<feColorMatrix type="matrix" values="0 0 0 0 0.5  0 0 0 0 0.52  0 0 0 0 0.54  0 0 0 0.45 -0.2"/></filter>
<filter id="d" x="0" y="0" width="100%" height="100%">
<feTurbulence type="fractalNoise" baseFrequency="0.004 0.05" numOctaves="2" seed="4"/>
<feColorMatrix type="matrix" values="0 0 0 0 0  0 0 0 0 0  0 0 0 0 0  0 0 0 -0.9 0.6"/></filter>
<rect width="900" height="160" fill="#18191b"/>
<rect width="900" height="160" filter="url(#d)"/><rect width="900" height="160" filter="url(#b)"/></svg>""",
    # blackened iron plates: faint mottling, a seam with rivets on the right and bottom edge of each 240 px plate
    "iron.svg": """<svg xmlns="http://www.w3.org/2000/svg" width="240" height="240">
<filter id="n" x="0" y="0" width="100%" height="100%">
<feTurbulence type="fractalNoise" baseFrequency="0.012" numOctaves="3" seed="3" stitchTiles="stitch"/>
<feColorMatrix type="matrix" values="0 0 0 0 0.13  0 0 0 0 0.12  0 0 0 0 0.11  0 0 0 -0.7 0.5"/></filter>
<filter id="s" x="0" y="0" width="100%" height="100%">
<feTurbulence type="fractalNoise" baseFrequency="0.9" numOctaves="1" seed="5" stitchTiles="stitch"/>
<feColorMatrix type="matrix" values="0 0 0 0 0.3  0 0 0 0 0.28  0 0 0 0 0.25  0 0 0 0.6 -0.25"/></filter>
<radialGradient id="r" cx="38%" cy="32%" r="70%"><stop offset="0" stop-color="#5a524a"/>
<stop offset="1" stop-color="#141210"/></radialGradient>
<rect width="240" height="240" fill="#100f0e"/>
<rect width="240" height="240" filter="url(#n)"/><rect width="240" height="240" filter="url(#s)"/>
<path d="M238.5,0 V238.5 H0" fill="none" stroke="#050404" stroke-width="2"/>
<path d="M236.5,0 V236.5 H0" fill="none" stroke="#2a2622" stroke-width="1"/>
""" + "".join(f'<circle cx="{c}" cy="231" r="2.2" fill="url(#r)"/><circle cx="231" cy="{c}" r="2.2" fill="url(#r)"/>'
              for c in range(15, 231, 30)) + "</svg>",
    # domed brass rivet centred in a 12 px square (one per row corner)
    "bolt.svg": """<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12">
<radialGradient id="r" cx="38%" cy="32%" r="70%"><stop offset="0" stop-color="#f6dfa8"/>
<stop offset=".45" stop-color="#a87a3c"/><stop offset="1" stop-color="#3a2410"/></radialGradient>
<circle cx="6" cy="6.7" r="3" fill="#000" opacity=".6"/><circle cx="6" cy="6" r="2.7" fill="url(#r)"/></svg>""",
    # copper pipe under the title bar, a bolted brass coupling every 140 px
    "pipe.svg": """<svg xmlns="http://www.w3.org/2000/svg" width="140" height="11">
<linearGradient id="c" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#3b1d0c"/>
<stop offset=".3" stop-color="#b56a3a"/><stop offset=".42" stop-color="#eaa77a"/><stop offset=".62" stop-color="#9a5729"/>
<stop offset="1" stop-color="#2e160a"/></linearGradient>
<linearGradient id="b" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#4a3112"/>
<stop offset=".35" stop-color="#e4c07a"/><stop offset=".6" stop-color="#a77a36"/><stop offset="1" stop-color="#3a250c"/></linearGradient>
<rect y="2" width="140" height="8" fill="url(#c)"/>
<rect x="113" y="1.5" width="3" height="8.5" fill="url(#b)" stroke="#2a1a08" stroke-width=".6"/>
<rect x="132" y="1.5" width="3" height="8.5" fill="url(#b)" stroke="#2a1a08" stroke-width=".6"/>
<rect x="116" y="0.5" width="16" height="10.5" rx="1" fill="url(#b)" stroke="#2a1a08" stroke-width=".8"/>
<circle cx="120" cy="5.7" r="1.4" fill="#5a3c14"/><circle cx="128" cy="5.7" r="1.4" fill="#5a3c14"/>
<circle cx="119.7" cy="5.3" r=".6" fill="#f6dfa8"/><circle cx="127.7" cy="5.3" r=".6" fill="#f6dfa8"/></svg>""",
    # teak deck: 14 px planks of slightly different tones with grain, black caulking, staggered butt joints
    "teak.svg": ('<svg xmlns="http://www.w3.org/2000/svg" width="720" height="168">'
                 '<filter id="g" x="0" y="0" width="100%" height="100%">'
                 '<feTurbulence type="fractalNoise" baseFrequency="0.006 0.45" numOctaves="4" seed="9"/>'
                 '<feColorMatrix type="matrix" values="0 0 0 0 0.30  0 0 0 0 0.12  0 0 0 0 0.03  0 0 0 -2.2 1.35"/>'
                 '</filter><filter id="h" x="0" y="0" width="100%" height="100%">'
                 '<feTurbulence type="fractalNoise" baseFrequency="0.003 0.9" numOctaves="2" seed="4"/>'
                 '<feColorMatrix type="matrix" values="0 0 0 0 1  0 0 0 0 0.8  0 0 0 0 0.55  0 0 0 1.1 -0.6"/>'
                 '</filter><rect width="720" height="168" fill="#1a1109"/>'
                 + "".join(f'<rect x="0" y="{i * 14 + 0.6}" width="720" height="13" fill="{c}"/>'
                           for i, c in enumerate(["#b5652b", "#a95b24", "#bd6c30", "#a35621", "#b06129", "#c07134",
                                                  "#a85a23", "#b9682d", "#ac5e26", "#bb6a2f", "#a45822", "#b3632a"]))
                 + '<rect width="720" height="168" filter="url(#g)"/><rect width="720" height="168" filter="url(#h)"/>'
                 + "".join(f'<rect x="{x}" y="{i * 14}" width="1.2" height="14" fill="#1a1109"/>'
                           for i, x in enumerate([90, 470, 250, 610, 150, 530, 330, 40, 400, 680, 200, 560]))
                 + '</svg>'),
    # twisted rope with signal flags hanging from it, under the yacht title bar (tile repeats every 140 px)
    "flags.svg": ('<svg xmlns="http://www.w3.org/2000/svg" width="140" height="19">'
                  '<linearGradient id="s" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fff4d6"/>'
                  '<stop offset=".5" stop-color="#d9bf86"/><stop offset="1" stop-color="#7a6236"/></linearGradient>'
                  '<rect y="1" width="140" height="5" fill="#8a7040"/>'
                  + "".join(f'<path d="M{x},6 L{x + 3},1 L{x + 5.5},1 L{x + 2.5},6 Z" fill="url(#s)"/>'
                            for x in range(-4, 142, 4))
                  + "".join(f'<g transform="translate({10 + 20 * i},7)"><line x1="5" y1="-1" x2="5" y2="1" '
                            f'stroke="#5a4628" stroke-width=".8"/>{f}'
                            f'<rect x=".25" y="1.25" width="9.5" height="9.5" fill="none" stroke="#000" '
                            f'stroke-opacity=".35" stroke-width=".5"/></g>'
                            for i, f in enumerate([
                                # O: red over yellow, diagonal
                                '<path d="M0,1 H10 V11 Z" fill="#d22b2b"/><path d="M0,1 V11 H10 Z" fill="#f5c400"/>',
                                # C: blue, white, red, white, blue stripes
                                ('<rect y="1" width="10" height="10" fill="#1d3f8f"/><rect y="3" width="10" '
                                 'height="6" fill="#fff"/><rect y="5" width="10" height="2" fill="#d22b2b"/>'),
                                # K: yellow and blue halves
                                ('<rect y="1" width="5" height="10" fill="#f5c400"/>'
                                 '<rect x="5" y="1" width="5" height="10" fill="#1d3f8f"/>'),
                                # V: red saltire on white
                                ('<rect y="1" width="10" height="10" fill="#fff"/>'
                                 '<path d="M0,1 L10,11 M10,1 L0,11" stroke="#d22b2b" stroke-width="2"/>'),
                                # P: blue with a white square
                                ('<rect y="1" width="10" height="10" fill="#1d3f8f"/>'
                                 '<rect x="3" y="4" width="4" height="4" fill="#fff"/>'),
                                # D: yellow, blue, yellow
                                ('<rect y="1" width="10" height="10" fill="#f5c400"/>'
                                 '<rect y="3.5" width="10" height="5" fill="#1d3f8f"/>'),
                                # E: blue over red
                                ('<rect y="1" width="10" height="5" fill="#1d3f8f"/>'
                                 '<rect y="6" width="10" height="5" fill="#d22b2b"/>')]))
                  + '</svg>'),
    # sailcloth: faint woven threads plus fibre noise, seamless (white yacht rows)
    "sailcloth.svg": ('<svg xmlns="http://www.w3.org/2000/svg" width="120" height="120">'
                      '<filter id="n" x="0" y="0" width="100%" height="100%">'
                      '<feTurbulence type="fractalNoise" baseFrequency="0.8 0.25" numOctaves="2" seed="6" '
                      'stitchTiles="stitch"/><feColorMatrix type="matrix" values="0 0 0 0 0.35  0 0 0 0 0.42  '
                      '0 0 0 0 0.5  0 0 0 0.5 -0.22"/></filter>'
                      '<rect width="120" height="120" filter="url(#n)"/>'
                      + "".join(f'<rect y="{y}" width="120" height="1" fill="#5a6c80" fill-opacity=".07"/>'
                                f'<rect x="{y}" width="1" height="120" fill="#5a6c80" fill-opacity=".05"/>'
                                for y in range(0, 120, 3))
                      + '</svg>'),
    # life ring and a coiled rope for the bottom right corner of the deck
    "ring.svg": ('<svg xmlns="http://www.w3.org/2000/svg" width="270" height="150">'
                 '<g opacity=".95"><path fill="none" stroke="#000" stroke-opacity=".35" stroke-width="5" d="M'
                 + " L".join(f"{72 + 2 + (5 + 1.15 * t) * math.cos(t):.1f},{92 + 3 + (5 + 1.15 * t) * math.sin(t):.1f}"
                             for t in [k * 0.2 for k in range(260)])
                 + '"/><path fill="none" stroke="#d9c08a" stroke-width="4.2" d="M'
                 + " L".join(f"{72 + (5 + 1.15 * t) * math.cos(t):.1f},{92 + (5 + 1.15 * t) * math.sin(t):.1f}"
                             for t in [k * 0.2 for k in range(260)])
                 + '"/><path fill="none" stroke="#8a7040" stroke-width="4.2" stroke-dasharray="1.2 2.4" d="M'
                 + " L".join(f"{72 + (5 + 1.15 * t) * math.cos(t):.1f},{92 + (5 + 1.15 * t) * math.sin(t):.1f}"
                             for t in [k * 0.2 for k in range(260)])
                 + '"/></g>'
                 '<circle cx="203" cy="91" r="42" fill="#000" opacity=".35"/>'
                 '<circle cx="200" cy="87" r="31" fill="none" stroke="#fbfbf8" stroke-width="20"/>'
                 + "".join(f'<circle cx="200" cy="87" r="31" fill="none" stroke="#d8281e" stroke-width="20" '
                           f'stroke-dasharray="24.3 170.5" stroke-dashoffset="{-12 - 48.7 * k:.1f}"/>'
                           for k in range(4))  # 4 red bands, an eighth of the ring each
                 + '<circle cx="200" cy="87" r="38" fill="none" stroke="#e8d6a8" stroke-width="1.6" '
                 'stroke-dasharray="14 10"/>'
                 '<circle cx="200" cy="87" r="31" fill="none" stroke="#fff" stroke-opacity=".35" stroke-width="3" '
                 'stroke-dasharray="40 155" stroke-dashoffset="100"/></svg>'),
    # two meshed gears, faint, for the bottom right corner of the window
    "gears.svg": ('<svg xmlns="http://www.w3.org/2000/svg" width="300" height="220">'
                  '<g fill="#c9995a" fill-opacity="0.06" stroke="#000" stroke-opacity="0.3" stroke-width="1">'
                  f'{_gear(225, 175, 95, 18, 0)}{_gear(95, 205, 60, 11, 9)}</g></svg>'),
}


def write_art():
    """Write the theme textures to ART_DIR (only files whose content changed)."""
    os.makedirs(ART_DIR, exist_ok=True)
    for name, svg in ART.items():
        path = f"{ART_DIR}/{name}"
        try:
            with open(path) as f:
                if f.read() == svg:
                    continue
        except OSError:
            pass
        with open(path, "w") as f:
            f.write(svg)


THEMES = {
    "dark": "",
    # clean minimal light mode: white and soft grey, thin lines, muted state colours
    "light": """
window, list { background: #f7f7f8; }
headerbar { background: #f7f7f8; border-bottom: 1px solid #e4e4e7; }
headerbar .title { color: #18181b; } headerbar .subtitle { color: #3f3f46; opacity: 1; }
headerbar button { color: #52525b; } headerbar button:hover { background: #ececef; }
.card { background: #ffffff; border: 1px solid #e4e4e7; border-left: 3px solid #a1a1aa; border-radius: 8px;
        box-shadow: 0 1px 2px rgba(0,0,0,0.04); }
list row:hover .card { background: #fafafa; border-color: #d4d4d8; }
.name { color: #18181b; } .cwd, .chart { color: #3f3f46; }
.plus { color: #52525b; } .plus:hover { background: #ececef; }
.plus.err { color: #b45309; } .plus.ask { color: #7e22ce; }
.badge { color: #3f3f46; background: #ffffff; border: 1px solid #a1a1aa; }
.model { color: #4f46e5; background: #eef2ff; }
.ok   { border-left-color: #16a34a; } .ok   .age { color: #15803d; background: rgba(22,163,74,0.10); }
.warn { border-left-color: #d97706; } .warn .age { color: #b45309; background: rgba(217,119,6,0.12); }
.late { border-left-color: #dc2626; } .late .age { color: #b91c1c; background: rgba(220,38,38,0.10); }
.wait { border-left-color: #9333ea; } .wait .age { color: #7e22ce; background: rgba(147,51,234,0.10); }
.busy { border-left-color: #2563eb; } .busy .age { color: #1d4ed8; background: rgba(37,99,235,0.10); }
""",
    # plain black terminal: monospace, no borders, no rounded corners, no fills; state shown by text colour only
    "terminal": """
* { font-family: "Ubuntu Sans Mono", monospace; }
window, list, list row { background: #000000; }
headerbar { background: #000000; border: none; box-shadow: none; }
headerbar .title { color: #d0d0d0; } headerbar .subtitle { color: #707070; }
headerbar button { color: #707070; } headerbar button:hover { background: #1a1a1a; }
.card { margin: 0; padding: 3px 10px; border: none; border-radius: 0; background: #000000; }
list row:hover .card { background: #111111; }
.name { color: #ffffff; font-weight: 700; }
.cwd, .chart { color: #8a8a8a; }
.plus { color: #8a8a8a; } .plus:hover { background: #1a1a1a; } .plus.err { color: #ffd23a; }
.plus.ask { color: #d787ff; }
.badge { color: #8a8a8a; background: #000000; border: none; border-radius: 0; padding: 0; }
.model { color: #5fafff; background: none; border-radius: 0; padding: 0 6px; }
.age { background: none; border-radius: 0; padding: 0 6px; }
.ok   .age { color: #5fff5f; background: none; }
.warn .age { color: #ffd75f; background: none; }
.late .age { color: #ff5f5f; background: none; }
.wait .age { color: #d787ff; background: none; }
.busy .age { color: #5fafff; background: none; }
.ok, .warn, .late, .wait, .busy { border-left-color: transparent; }
""",
    # green phosphor terminal: scanlines, glow, and a short colour-split glitch every few seconds
    "crt": """
* { font-family: "Ubuntu Sans Mono", monospace; }
window, list { background-color: #010603; background-image: @SCAN@; }
headerbar { background: #010603; border-bottom: 1px solid #0f5a26; }
headerbar .title { color: #39ff6e; text-shadow: 0 0 5px rgba(57,255,110,0.7); }
headerbar .subtitle { color: #1fa04a; }
headerbar button { color: #1fa04a; } headerbar button:hover { background: #052b12; }
.card { border-radius: 0; background-color: #031209; background-image: @SCAN@;
        border: 1px solid #0f5a26; border-left: 3px solid #1fa04a; }
list row:hover .card { background-color: #06240f; }
.name { color: #39ff6e; font-weight: 700; text-shadow: 0 0 5px rgba(57,255,110,0.6); }
.cwd { color: #22b455; }
.chart { color: #39ff6e; text-shadow: 0 0 4px rgba(57,255,110,0.6); }
.plus { color: #22b455; } .plus:hover { background: #052b12; } .plus.err { color: #ffd23a; }
.badge { color: #39ff6e; background: #010603; border: 1px solid #1fa04a; border-radius: 0; }
.model { color: #010603; background: #22b455; border-radius: 0; }
.age { border-radius: 0; }
.ok   { border-left-color: #39ff6e; } .ok   .age { color: #39ff6e; background: rgba(57,255,110,0.15); }
.warn { border-left-color: #d8ff3a; } .warn .age { color: #d8ff3a; background: rgba(216,255,58,0.15); }
.late { border-left-color: #ff5a3a; } .late .age { color: #ff7a5a; background: rgba(255,90,58,0.15); }
.wait { border-left-color: #3affd8; } .wait .age { color: #3affd8; background: rgba(58,255,216,0.15); }
.busy { border-left-color: #1fa04a; } .busy .age { color: #22b455; background: rgba(31,160,74,0.15); }
window.glitch list { margin-left: 7px; }
window.glitch .name, window.glitch .chart, window.glitch .cwd, window.glitch .subtitle {
  text-shadow: 3px 0 rgba(255,0,170,0.85), -3px 0 rgba(0,255,255,0.85); }
window.glitch .card { background-color: #0a3a1c; }
""",
    # brushed steel rows on riveted iron, walnut title bar with a copper pipe; brass only on trims, rivets and plates (textures: ART)
    "steampunk": """
headerbar .title, .name { font-family: "C059", "DejaVu Serif", serif; }
window { background-color: #121110; background-image: url("@ART@/gears.svg"), url("@ART@/iron.svg");
         background-position: right bottom, left top; background-repeat: no-repeat, repeat; }
list { background-color: transparent; background-image: none; }
headerbar { padding-bottom: 12px; border: none; box-shadow: 0 2px 4px rgba(0,0,0,0.6); background-color: #1c0f07;
  background-image: url("@ART@/pipe.svg"), linear-gradient(to bottom, rgba(0,0,0,0.25), rgba(0,0,0,0.55)),
                    url("@ART@/walnut.svg");
  background-position: left bottom, left top, left top; background-repeat: repeat-x, no-repeat, repeat;
  background-size: auto, 100% 100%, auto; }
headerbar .title { color: #e2bf7c; font-weight: 700; text-shadow: 0 -1px 0 rgba(0,0,0,0.9); }
headerbar .subtitle { color: #c9ad7d; font-weight: 700; }
headerbar button { color: #d4b06a; } headerbar button:hover { background: rgba(226,191,124,0.15); }
.card {
  margin: 2px 8px; padding: 6px 16px 6px 14px; border-radius: 3px;
  border: 1px solid #050505; border-left: 4px solid #6a6f73; background-color: #18191b;
  background-image: url("@ART@/bolt.svg"), url("@ART@/bolt.svg"), url("@ART@/bolt.svg"), url("@ART@/bolt.svg"),
    linear-gradient(to bottom, rgba(255,255,255,0.06), rgba(255,255,255,0.01) 40%, rgba(0,0,0,0.25)),
    url("@ART@/steel.svg");
  background-position: left top, right top, left bottom, right bottom, left top, left top;
  background-repeat: no-repeat, no-repeat, no-repeat, no-repeat, no-repeat, repeat;
  background-size: 12px 12px, 12px 12px, 12px 12px, 12px 12px, 100% 100%, auto;
  box-shadow: inset 0 0 0 1px rgba(206,160,90,0.42), 0 2px 4px rgba(0,0,0,0.8); }
list row:hover .card {
  background-image: url("@ART@/bolt.svg"), url("@ART@/bolt.svg"), url("@ART@/bolt.svg"), url("@ART@/bolt.svg"),
    linear-gradient(to bottom, rgba(255,255,255,0.11), rgba(255,255,255,0.04) 40%, rgba(0,0,0,0.15)),
    url("@ART@/steel.svg");
  box-shadow: inset 0 0 0 1px rgba(226,186,110,0.65), 0 2px 4px rgba(0,0,0,0.8); }
.name { font-size: 14px; color: #fff6e0; font-weight: 700; text-shadow: 0 1px 2px #000000; }
.cwd { font-size: 12px; color: #eadcc0; text-shadow: 0 1px 1px #000000; }
.chart { color: #f0c46a; }
.plus { color: #c9a15a; } .plus:hover { background: rgba(226,191,124,0.15); }
.plus.err { color: #f2c230; } .plus.ask { color: #c9a6dc; }
.badge { color: #e2bf7c; background: #140b05; border: 1px solid #8a6430; border-radius: 2px; }
.model { color: #24150a; font-weight: 700; border: 1px solid #4a2f12; border-radius: 2px; padding: 0 6px;
         background-image: linear-gradient(to bottom, #e9cd8f, #bf9450 55%, #8f6a32);
         text-shadow: 0 1px 0 rgba(255,240,200,0.45); box-shadow: 0 1px 2px rgba(0,0,0,0.6); }
.age { border-radius: 11px; border: 2px solid #a87a3c; background-color: #0b0d0c; padding: 3px 9px 1px 9px;
       background-image: linear-gradient(to bottom, rgba(255,255,255,0.13), rgba(255,255,255,0.02) 48%,
                                         rgba(0,0,0,0) 52%), linear-gradient(to bottom, #070908, #1a201d);
       box-shadow: 0 0 0 1px #1e1006, inset 0 1px 3px rgba(0,0,0,0.9); }
.ok   { border-left-color: #5f9a7e; }
.ok   .age { color: #9fe0bd; background-color: #0b0d0c; text-shadow: 0 0 5px rgba(120,220,170,0.6); }
.warn { border-left-color: #d6a23a; }
.warn .age { color: #ffd27a; background-color: #0b0d0c; text-shadow: 0 0 5px rgba(255,190,80,0.6); }
.late { border-left-color: #a8392f; }
.late .age { color: #ff8a78; background-color: #0b0d0c; text-shadow: 0 0 5px rgba(255,100,80,0.6); }
.wait { border-left-color: #8a65a8; }
.wait .age { color: #d9b8f0; background-color: #0b0d0c; text-shadow: 0 0 5px rgba(200,160,240,0.6); }
.busy { border-left-color: #5d7e98; }
.busy .age { color: #a9cbe6; background-color: #0b0d0c; text-shadow: 0 0 5px rgba(150,200,240,0.55); }
""",
    # CGA, 4 colours: black, cyan, magenta, white (alerts get yellow and red)
    "cga": """
* { font-family: "Ubuntu Sans Mono", monospace; }
window, list { background: #000000; }
headerbar { background: #ff55ff; border-bottom: 2px solid #55ffff; }
headerbar .title { color: #000000; font-weight: 700; }
headerbar .subtitle { color: #000000; }
headerbar button { color: #000000; } headerbar button:hover { background: #ffffff; color: #000000; }
.card { background: #000000; border: 2px solid #55ffff; border-left: 6px solid #55ffff;
        border-radius: 0; box-shadow: none; }
list row:hover .card { border-color: #ff55ff; }
.name { color: #ffffff; font-weight: 700; }
.cwd { color: #55ffff; }
.chart { color: #ff55ff; }
.plus { color: #55ffff; } .plus:hover { background: #ff55ff; color: #000000; }
.plus.err { color: #ffff55; } .plus.ask { color: #ff55ff; }
.badge { color: #ffffff; background: #000000; border: 1px solid #55ffff; border-radius: 0; }
.model { color: #000000; background: #ff55ff; border-radius: 0; }
.age { border-radius: 0; font-weight: 700; color: #000000; }
.ok   { border-left-color: #55ffff; } .ok   .age { color: #000000; background: #55ffff; }
.warn { border-left-color: #ffff55; } .warn .age { color: #000000; background: #ffff55; }
.late { border-left-color: #ff5555; } .late .age { color: #000000; background: #ff5555; }
.wait { border-left-color: #ff55ff; } .wait .age { color: #000000; background: #ff55ff; }
.busy { border-left-color: #ffffff; } .busy .age { color: #000000; background: #ffffff; }
""",
    # yacht: glossy teak deck (life ring, coiled rope), white gelcoat rows with hull stripes,
    # varnished teak rail with rope and signal flags
    "yacht": """
window { background-color: #9a5424;
         background-image: linear-gradient(160deg, rgba(255,240,215,0.22), rgba(255,240,215,0) 35%,
                                           rgba(255,240,215,0.10) 55%, rgba(255,240,215,0) 70%),
                           url("@ART@/ring.svg"), url("@ART@/teak.svg");
         background-position: left top, right bottom, left top;
         background-repeat: no-repeat, no-repeat, repeat; background-size: 100% 100%, auto, auto; }
list { background-color: transparent; background-image: none; }
headerbar { border: none; padding-bottom: 21px; background-color: #a65a24;
  background-image: url("@ART@/flags.svg"),
                    linear-gradient(to bottom, rgba(255,236,205,0.55), rgba(255,236,205,0.12) 6%, rgba(255,236,205,0) 14%,
                                    rgba(0,0,0,0) 60%, rgba(40,14,0,0.35)),
                    linear-gradient(to bottom, #8f4a1a, #6a3310);
  background-position: left bottom, left top, left top;
  background-repeat: repeat-x, no-repeat, no-repeat; background-size: auto, 100% 100%, 100% 100%;
  box-shadow: 0 2px 5px rgba(40,18,4,0.6); }
headerbar .title { font-size: 13px; font-weight: 700; color: #fffaf0; text-shadow: 0 1px 2px #1e0a00; }
headerbar .subtitle { font-size: 11px; font-weight: 700; color: #fff3df; text-shadow: 0 1px 2px #1e0a00; }
headerbar button { color: #fffaf0; } headerbar button:hover { background: rgba(255,255,255,0.2); }
.card { margin: 3px 8px; padding-bottom: 10px; border-radius: 6px; border: 1px solid #b9c3cc;
        border-left: 4px solid #8a96a3; background-color: #f7f9fb;
        background-image: linear-gradient(to bottom, #12325a, #12325a 2px, #ffffff 2px, #ffffff 3px,
                                          #c8302a 3px, #c8302a 5px),
                          url("@ART@/sailcloth.svg"),
                          linear-gradient(to bottom, #ffffff, #f7f9fb 45%, #eceff3 52%, #f4f6f8);
        background-position: left bottom, left top, left top; background-repeat: no-repeat, repeat, no-repeat;
        background-size: 100% 5px, auto, 100% 100%;
        box-shadow: inset 0 1px 0 #ffffff, inset 0 0 6px rgba(120,150,185,0.35),
                    0 0 7px rgba(255,238,205,0.55), 0 2px 6px rgba(50,22,4,0.55); }
list row:hover .card { background-image: linear-gradient(to bottom, #12325a, #12325a 2px, #ffffff 2px, #ffffff 3px,
                                                         #c8302a 3px, #c8302a 5px),
                                         url("@ART@/sailcloth.svg"),
                                         linear-gradient(to bottom, #ffffff, #ffffff 45%, #f0f4f7 52%, #fbfcfd);
        box-shadow: inset 0 1px 0 #ffffff, inset 0 0 6px rgba(120,150,185,0.35),
                    0 0 12px rgba(255,240,210,0.95), 0 2px 6px rgba(50,22,4,0.55); }
.name { font-size: 14px; color: #0b1a2c; font-weight: 700; }
.cwd { font-size: 12px; color: #2c3a4a; }
.chart { color: #a4561c; }
.plus { color: #2c3a4a; } .plus:hover { background: rgba(11,26,44,0.08); }
.plus.err { color: #b4410e; } .plus.ask { color: #6a2fa8; }
.badge { color: #ffffff; background: #12325a; border: 1px solid #ffffff; border-radius: 3px; }
.model { color: #ffffff; font-weight: 700; border-radius: 4px; background-color: #12325a;
         background-image: linear-gradient(to bottom, rgba(255,255,255,0.3), rgba(255,255,255,0.05) 50%,
                                           rgba(0,0,0,0) 51%, rgba(0,0,0,0.15));
         box-shadow: 0 1px 2px rgba(0,0,0,0.3); }
.age { color: #ffffff; border-radius: 10px; text-shadow: 0 1px 1px rgba(0,0,0,0.45);
       background-image: linear-gradient(to bottom, rgba(255,255,255,0.4), rgba(255,255,255,0.08) 50%,
                                         rgba(0,0,0,0) 51%, rgba(0,0,0,0.12));
       box-shadow: inset 0 -1px 0 rgba(0,0,0,0.25), 0 1px 2px rgba(0,0,0,0.3); }
.ok   { border-left-color: #1d7f4f; } .ok   .age { color: #ffffff; background-color: #1d7f4f; }
.warn { border-left-color: #d08a12; } .warn .age { color: #ffffff; background-color: #c98010; }
.late { border-left-color: #b8322a; } .late .age { color: #ffffff; background-color: #b8322a; }
.wait { border-left-color: #6a3fa8; } .wait .age { color: #ffffff; background-color: #6a3fa8; }
.busy { border-left-color: #1f5fa8; } .busy .age { color: #ffffff; background-color: #1f5fa8; }
""",
}
THEMES = {k: v.replace("@SCAN@", SCAN)
                .replace("@ART@", f"file://{ART_DIR}") for k, v in THEMES.items()}
THEME_FILE = f"{HOME}/.cache/overseer/theme"


def load_theme():
    try:
        with open(THEME_FILE) as f:
            name = f.read().strip()
    except OSError:
        return "dark"
    return name if name in THEMES else "dark"


def save_theme(name):
    os.makedirs(os.path.dirname(THEME_FILE), exist_ok=True)
    with open(THEME_FILE + ".tmp", "w") as f:
        f.write(name)
    os.replace(THEME_FILE + ".tmp", THEME_FILE)
