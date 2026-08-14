#!/usr/bin/env python3
"""excalidraw (.excalidraw JSON) -> SVG -> PNG.

Renderer tối giản đủ cho diagram kỹ thuật của repo: rectangle (bo góc), text
(đa dòng, canh trái/giữa), arrow/line (polyline + đầu mũi tên). roughness=0.
Dùng: python3 excalidraw2png.py <file.excalidraw> [out.png]
"""
import json, sys, math, html
from pathlib import Path

MONO = "'JetBrainsMono Nerd Font Mono', 'JetBrainsMono Nerd Font', Menlo, monospace"


def esc(s):
    return html.escape(s, quote=True)


def render_svg(doc):
    els = [e for e in doc["elements"] if not e.get("isDeleted")]
    maxx = maxy = 0
    for e in els:
        maxx = max(maxx, e["x"] + abs(e.get("width", 0)))
        maxy = max(maxy, e["y"] + abs(e.get("height", 0)))
    W, H = int(maxx + 40), int(maxy + 40)
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
           f'viewBox="0 0 {W} {H}"><rect width="{W}" height="{H}" fill="#ffffff"/>']

    for e in els:
        t = e["type"]
        op = e.get("opacity", 100) / 100.0
        stroke = e.get("strokeColor", "#000")
        sw = e.get("strokeWidth", 1)
        if t in ("rectangle", "ellipse", "diamond"):
            x, y, w, h = e["x"], e["y"], e["width"], e["height"]
            fill = e.get("backgroundColor", "transparent")
            if fill == "transparent":
                fill = "none"
            dash = ' stroke-dasharray="8 6"' if e.get("strokeStyle") == "dashed" else (
                ' stroke-dasharray="2 5"' if e.get("strokeStyle") == "dotted" else "")
            if t == "rectangle":
                rx = 12 if e.get("roundness") else 0
                out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" '
                           f'fill="{fill}" fill-opacity="{op}" stroke="{stroke}" '
                           f'stroke-width="{sw}" stroke-opacity="{op}"{dash}/>')
            elif t == "ellipse":
                out.append(f'<ellipse cx="{x+w/2}" cy="{y+h/2}" rx="{w/2}" ry="{h/2}" '
                           f'fill="{fill}" fill-opacity="{op}" stroke="{stroke}" '
                           f'stroke-width="{sw}" stroke-opacity="{op}"{dash}/>')
            else:  # diamond
                pts = f"{x+w/2},{y} {x+w},{y+h/2} {x+w/2},{y+h} {x},{y+h/2}"
                out.append(f'<polygon points="{pts}" fill="{fill}" fill-opacity="{op}" '
                           f'stroke="{stroke}" stroke-width="{sw}" stroke-opacity="{op}"{dash}/>')
        elif t in ("arrow", "line"):
            pts = e.get("points", [[0, 0]])
            ax, ay = e["x"], e["y"]
            abspts = [(ax + p[0], ay + p[1]) for p in pts]
            dash = ' stroke-dasharray="8 6"' if e.get("strokeStyle") == "dashed" else ""
            poly = " ".join(f"{px},{py}" for px, py in abspts)
            out.append(f'<polyline points="{poly}" fill="none" stroke="{stroke}" '
                       f'stroke-width="{sw}" stroke-opacity="{op}"{dash}/>')
            if t == "arrow" and e.get("endArrowhead", "arrow") and len(abspts) >= 2:
                (x1, y1), (x2, y2) = abspts[-2], abspts[-1]
                ang = math.atan2(y2 - y1, x2 - x1)
                L = 14
                for da in (math.radians(150), math.radians(-150)):
                    hx = x2 + L * math.cos(ang + da)
                    hy = y2 + L * math.sin(ang + da)
                    out.append(f'<line x1="{x2}" y1="{y2}" x2="{hx:.1f}" y2="{hy:.1f}" '
                               f'stroke="{stroke}" stroke-width="{sw}" stroke-opacity="{op}"/>')
        elif t == "text":
            x, y = e["x"], e["y"]
            fs = e.get("fontSize", 16)
            lh = e.get("lineHeight", 1.25) * fs
            align = e.get("textAlign", "left")
            anchor = {"left": "start", "center": "middle", "right": "end"}[align]
            tx = x + (e["width"] / 2 if align == "center" else (e["width"] if align == "right" else 0))
            lines = e.get("text", "").split("\n")
            baseline = y + fs * 0.82
            for i, ln in enumerate(lines):
                out.append(f'<text x="{tx:.1f}" y="{baseline + i*lh:.1f}" '
                           f'font-family="{MONO}" font-size="{fs}" fill="{stroke}" '
                           f'fill-opacity="{op}" text-anchor="{anchor}" '
                           f'xml:space="preserve">{esc(ln)}</text>')
    out.append("</svg>")
    return "\n".join(out)


def main():
    if len(sys.argv) < 2:
        print("usage: excalidraw2png.py <file.excalidraw> [out.png]", file=sys.stderr)
        sys.exit(1)
    src = Path(sys.argv[1])
    doc = json.loads(src.read_text())
    svg = render_svg(doc)
    svg_path = src.with_suffix(".svg")
    svg_path.write_text(svg)
    out_png = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_suffix(".png")
    import cairosvg
    cairosvg.svg2png(bytestring=svg.encode(), write_to=str(out_png), scale=2.0)
    print(f"wrote {svg_path.name} + {out_png.name}")


if __name__ == "__main__":
    main()
