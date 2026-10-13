"""Render the repo-native SVG icon into an ICO used only as a build input."""
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

root = Path(__file__).resolve().parents[1]
output = root / "build" / "app.ico"
output.parent.mkdir(parents=True, exist_ok=True)
image = QImage(256, 256, QImage.Format.Format_ARGB32)
image.fill(Qt.GlobalColor.transparent)
painter = QPainter(image)
renderer = QSvgRenderer(str(root / "desktop/assets/app.svg"))
renderer.render(painter)
painter.end()
if not image.save(str(output), "ICO"):
    raise RuntimeError("App icon rendering failed")
