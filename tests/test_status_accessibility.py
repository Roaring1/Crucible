"""Status text remains understandable without color and meets WCAG AA contrast."""

from crucible.ui import theme
from crucible.ui.sidebar import InstanceRowWidget


def _linear_channel(value: int) -> float:
    channel = value / 255
    return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4


def _luminance(color: str) -> float:
    red, green, blue = (
        int(color[index : index + 2], 16) for index in (1, 3, 5)
    )
    return (
        0.2126 * _linear_channel(red)
        + 0.7152 * _linear_channel(green)
        + 0.0722 * _linear_channel(blue)
    )


def _contrast_ratio(foreground: str, background: str) -> float:
    lighter, darker = sorted(
        (_luminance(foreground), _luminance(background)),
        reverse=True,
    )
    return (lighter + 0.05) / (darker + 0.05)


def test_every_server_status_has_readable_text_and_accessible_label():
    expected = {
        "running",
        "stopped",
        "starting",
        "stopping",
        "tmux_missing",
        "missing",
        "unmanaged",
        "unknown",
    }
    assert set(InstanceRowWidget.STATUS_LABELS) == expected
    assert expected <= set(theme.STATUS_COLORS)


def test_server_status_text_meets_wcag_aa_contrast_on_app_surfaces():
    # The sidebar and selected-server header render on MANTLE. BASE is also
    # checked because it is used by other workspace surfaces/status contexts.
    for status, foreground in theme.STATUS_COLORS.items():
        for background in (theme.MANTLE, theme.BASE):
            ratio = _contrast_ratio(foreground, background)
            assert ratio >= 4.5, (
                f"{status} status label has only {ratio:.2f}:1 contrast "
                f"({foreground} on {background})"
            )
