# Local SVG icon provenance

Prepared: 2026-10-29

This repository vendors a deliberately small set of three Lucide SVGs for actions where the icon improves scanning. Their labels stay visible; these assets are not used as decoration on every control. No AI-generated imagery or runtime network image requests are used.

## Upstream pin

- Project: Lucide Icons, https://github.com/lucide-icons/lucide
- Upstream commit: 81875c8c42d2320fa5123b37fee3554f4ca2bddc
- Commit date returned by GitHub: 2026-10-29
- Retrieved from repository contents at that exact commit on 2026-10-29.
- Upstream license path: LICENSE, Git blob 718bb3f0e44153809972abed31839375804bf652
- The complete upstream license text is packaged at crucible/assets/icons/LICENSES/LUCIDE-LICENSE.txt, preserving the ISC and Feather/MIT notices as supplied upstream. The selected folder-open, archive, and refresh-cw names do not appear in the license's Feather-derived icon list, so the ISC terms apply to these selected SVGs. The full file is preserved to avoid dropping upstream notice material.

## Selected assets

| Local file | Upstream path | Upstream Git blob | SHA-256 of unmodified upstream SVG | SHA-256 of packaged SVG | Local modification |
|---|---|---|---|---|---|
| crucible/assets/icons/folder-open.svg | icons/folder-open.svg | b01f60fcb51848a9b3a7380d4566c4de7534745c | ee57452b40e71df310214091dd9da1941a397c83197b8ff8d7ae66dd4242578b | 57386b38edc9b0fccc3f0f76ac831127b19fbfb7aac748cb06f998a5ce2324f9 | Changed only stroke currentColor to Crucible amber stroke #c8903a; path geometry and stroke metrics are unchanged. |
| crucible/assets/icons/archive.svg | icons/archive.svg | 4c5ae8e33b565fa5edc5ea5fba50a860c4b9abcf | 78c732e34e232db63af06a2330b4448045cfdf49b48b59f2d31922df54c7bb17 | bee241c8489350313749ae1267d3ac3f3193449e5edfac6d381ffe12aa68804f | Changed only stroke currentColor to theme dark ink #111113 so it contrasts on the amber-filled Backup Now primary button; path geometry and stroke metrics are unchanged. |
| crucible/assets/icons/refresh-cw.svg | icons/refresh-cw.svg | c27b3a9677e35fb2353167b01553d3d4cc4e3091 | 2e10dd403c85a24f163d59fc6151aa21147fe9402e1305dfc8979208caee8944 | c8d9643589ef234f2111b59f61ff2e8745fcb5c28b6622bb26e42e0538af41e4 | Changed only stroke currentColor to Crucible amber stroke #c8903a; path geometry and stroke metrics are unchanged. |

## Usage convention

- Open Folder: folder-open SVG beside the visible text label.
- Backup Now: archive SVG beside the visible text label.
- Reload server properties: refresh-cw SVG beside the visible Reload label.
- New icon names must be added to the explicit allow-list in crucible/ui/icons.py, be sourced from a pinned upstream revision, and be accompanied by source blob/SHA-256 and license notes here.
- Icons are local files inside the Python package; they never fetch external content at runtime.
