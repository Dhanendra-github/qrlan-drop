"""Rasterize the code-native logo for Tk and Windows; SVG remains the master."""
import re
import sys
from pathlib import Path
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from branding import MARK_PATHS, MARK_SVG, MINT


def path_points(path):
    tokens = iter(re.findall(r'[MLQZ]|-?\d+(?:\.\d+)?', path))
    points = []
    for token in tokens:
        if token in ('M', 'L'):
            points.append((float(next(tokens)), float(next(tokens))))
        elif token == 'Q':
            x0, y0 = points[-1]
            x1, y1, x2, y2 = [float(next(tokens)) for _ in range(4)]
            for step in range(1, 17):
                t = step / 16
                points.append(((1-t)**2*x0+2*(1-t)*t*x1+t*t*x2,
                               (1-t)**2*y0+2*(1-t)*t*y1+t*t*y2))
    return points


def render(size, badge=False):
    scale = 4
    image = Image.new('RGBA', (size*scale, size*scale))
    draw = ImageDraw.Draw(image)
    if badge:
        draw.rounded_rectangle((0, 0, size*scale-1, size*scale-1), radius=size*scale*.22, fill='#111923')
    extent = size*scale*(.70 if badge else .94)
    factor = extent/35
    offset_x = (size*scale-32*factor)/2
    offset_y = (size*scale-35*factor)/2
    for path in MARK_PATHS:
        draw.polygon([(offset_x+x*factor,offset_y+y*factor) for x,y in path_points(path)],fill=MINT)
    return image.resize((size,size),Image.Resampling.LANCZOS)


if __name__ == '__main__':
    assets = ROOT / 'assets'
    assets.mkdir(exist_ok=True)
    (assets/'brand-mark.svg').write_text(MARK_SVG,encoding='utf-8')
    render(512).save(assets/'brand-mark.png')
    icon = render(256,True)
    icon.save(assets/'app-icon.png')
    icon.save(assets/'app.ico',sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])
