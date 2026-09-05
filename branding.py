"""QRLAN's mint mark and white/blue wordmark, shared by desktop and browser."""
from pathlib import Path

MINT = '#8bd9b6'
BLUE = '#3487ff'
MARK_PATHS = (
    'M 5 3 L 12 3 Q 15 3 15 6.5 Q 15 10 12 10 L 5 10 Q 1.5 10 1.5 6.5 Q 1.5 3 5 3 Z',
    'M 18 3 L 23 3 Q 24 3 24.5 4 L 27 9 L 22 9 Q 21 9 20.5 8 Z',
    'M 5 14 L 17 14 Q 21 14 23 18 L 29 29 Q 30 31 27 31 L 23 31 Q 21.5 31 20.5 29 L 16.5 22 Q 16 21 14 21 L 5 21 Q 1.5 21 1.5 17.5 Q 1.5 14 5 14 Z',
    'M 5 25 L 10 25 Q 13.5 25 13.5 28.5 Q 13.5 32 10 32 L 5 32 Q 1.5 32 1.5 28.5 Q 1.5 25 5 25 Z',
)
MARK_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 35">' + ''.join(
    f'<path fill="{MINT}" d="{path}"/>' for path in MARK_PATHS) + '</svg>'
BRAND_HTML = '<div class="brand" aria-label="QRLAN Drop">' + MARK_SVG + '<span>QRLAN <b>DROP</b></span></div>'


def asset_path(name):
    # PyInstaller extracts bundled assets next to this module.
    return Path(__file__).resolve().parent / 'assets' / name
