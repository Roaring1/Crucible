"""Small, deterministic color accents derived from existing server artwork."""
from __future__ import annotations

import math

from PyQt6.QtCore import QRect
from PyQt6.QtGui import QColor, QIcon, QPainter, QPen

from . import theme


def icon_accent(icon: QIcon) -> QColor:
    """Find a restrained hue from a tiny icon sample, adjusted for Crucible's theme."""
    pixmap = icon.pixmap(24, 24)
    if pixmap.isNull():
        return QColor(theme.ACCENT)
    image = pixmap.toImage()
    hue_x = hue_y = 0.0
    saturation_total = value_total = weight_total = 0.0
    for y in range(image.height()):
        for x in range(image.width()):
            color = image.pixelColor(x, y)
            if color.alpha() < 96:
                continue
            hue, saturation, value, _alpha = color.getHsv()
            if hue < 0 or saturation < 38 or value < 28:
                continue
            weight = max(0.15, saturation / 255.0)
            angle = math.radians(hue)
            hue_x += math.cos(angle) * weight
            hue_y += math.sin(angle) * weight
            saturation_total += saturation * weight
            value_total += value * weight
            weight_total += weight
    if weight_total <= 0.0:
        return QColor(theme.ACCENT)
    hue = round((math.degrees(math.atan2(hue_y, hue_x)) + 360.0) % 360.0)
    saturation = int(max(72, min(116, saturation_total / weight_total)))
    value = int(max(142, min(196, value_total / weight_total)))
    accent = QColor()
    accent.setHsv(hue, saturation, value)
    return accent


def icon_with_accent(icon: QIcon) -> QIcon:
    """Overlay one quiet accent dot; retain the source server artwork unchanged."""
    pixmap = icon.pixmap(28, 28)
    if pixmap.isNull():
        return icon
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor(theme.CRUST), 1.25))
    painter.setBrush(icon_accent(icon))
    painter.drawEllipse(QRect(19, 19, 8, 8))
    painter.end()
    return QIcon(pixmap)
