# UI Design and Interaction Workstream

**Status:** Preparatory plan and source-level inspection; implementation not started.  
**Prepared:** 2026-10-29  
**Initial reference commit:** `34ca28fd8f989fe3eb98b90c81012d141684f4c9`  
**Workstream:** Chat 3 — whole-app UX, local SVG icons, loading skeletons, restrained transitions.

This document records the implementation boundary and verification plan. It is not a claim that the full application has been rendered, visually reviewed, or tested at all listed sizes. The existing [UX audit](ux-audit.md) covers key first-run, create, import, and navigation decisions; this work extends the review across the whole application and its visual system.

## Integration gate and scope

The provider-identity work in PR [#34](https://github.com/Roaring1/Crucible/pull/34) and Add Mod icon lifecycle work in PR [#35](https://github.com/Roaring1/Crucible/pull/35) were open at preparation time. Both touch the Add Mod browser or closely related tests and preview tooling.

- Do not edit `crucible/ui/add_mod_dialog.py`, `scripts/capture_ui_previews.py`, or their tests while PR #35 remains active.
- Do not edit provider identity, download, or related behavior from PR #34.
- Begin implementation against a fresh `main` only after both dependencies are integrated, recording the exact base commit. If either PR changes its scope, re-check changed filenames before proceeding.
- Keep visual changes in one focused branch/PR. Do not bump the release version. Keep all visual assets local; no AI-generated images, generated raster illustrations, or external runtime image requests for decoration.
- Test with synthetic server instances, temporary registries, and fake APIs. Never modify real server/world data, read or print a live CurseForge secret, or include secrets in screenshots, fixtures, logs, or reports.

## Source-level baseline (not a visual acceptance result)

Files inspected on the initial base:

- `crucible/ui/theme.py`: existing palette is warm dark/amber and centralized in one stylesheet. A global `outline: none` rule is present, while line edits define their own focus border. Focus behavior across all other controls needs rendered keyboard testing; do not infer that every control is inaccessible from the stylesheet alone.
- `crucible/ui/sidebar.py` and `crucible/ui/instance_panel.py`: some actions use Unicode glyphs next to text labels. The aim is consistency and legibility, not replacing every glyph or every native control with a custom icon.
- `crucible/assets/`: the inspected main tree contained the app PNG but no dedicated SVG icon family.
- `scripts/capture_ui_previews.py`: the existing helper renders six states offscreen using temporary data. Its current test primarily verifies expected image files/PNG headers/size; it does not by itself establish visual quality or prove real pointer/keyboard journeys.
- `docs/development/ux-audit.md`: provides a useful, existing record of first-run, import, and selected-server workflow reasoning. Update it only when new evidence justifies changing those conclusions; do not silently rewrite its historical scope.

## Design guardrails

1. Preserve Crucible's warm dark/amber identity. Prefer better hierarchy, spacing, focus visibility, text, and state communication over a wholesale redesign.
2. Use plain language and clear primary/secondary/destructive action hierarchy. Keep labels for actions whose meaning is not universal. Avoid icons, cards, badges, gradients, and animation that merely add noise.
3. Establish small shared tokens for color, spacing, typography, corner radius, icon geometry/sizes, focus treatment, loading states, and motion before applying changes across screens.
4. Keep SVG paths and attribution/license information in the repository. Favor a coherent, human-maintained icon set such as Lucide, or simple hand-authored paths that can be reviewed. Do not draw external images at runtime.
5. Skeletons are structure hints, not fake content or progress indicators. They must resolve on success, empty state, error, cancellation, and close. Avoid permanent shimmer or stale placeholders.
6. Transitions should be brief and explain a state change. Never make important information or actions depend on animation. Respect reduced-motion preferences when the platform exposes them, and provide a non-animated fallback.
7. Preserve keyboard navigation, native control affordances, resizing, text legibility, and existing safe server-management workflows.

## Candidate icon inventory (validate before adoption)

The initial source read found repeated control meanings that currently use a mix of Unicode/emoji glyphs and plain text. This is a **candidate semantic inventory**, not a mandate to put an icon on every control. Final inclusion and sizing depend on baseline screenshots and keyboard/accessibility checks.

| Meaning / current example | Candidate local SVG | Rule |
|---|---|---|
| Add server / add item | `plus` | Reserve the prominent treatment for the primary Add server action; ordinary add controls stay secondary. |
| Start / stop / restart | `play`, `square`, `rotate-cw` | Keep the text labels and distinct primary/danger/secondary button treatments. Never rely on icon color alone. |
| Console | `terminal` | Use consistently for navigation or opening a console; do not use it for unrelated actions. |
| Mods / package | `package` | Use only where it helps identify the Mods area, not as decoration on each table row. |
| World / dimensions | `globe` | A candidate for the World section; avoid repeating the same decorative glyph in every world-related label. |
| Backups / archive | `archive` | Reuse for backup/archive concepts; distinguish destructive deletion with label and danger treatment. |
| Configuration | `sliders-horizontal` | Keep distinct from general-purpose more/options menus. |
| Players | `users` | Candidate for the Players section; player actions still need explicit text. |
| Notes / information | `notebook-pen`, `info` | Use only if the icon improves navigation scanning at the actual tab size. |
| Copy / open folder / refresh / save | `copy`, `folder-open`, `refresh-cw`, `save` | Icon-only compact buttons require accessible names and useful tooltips; preserve text labels in normal layouts. |
| Download / upload / external link | `download`, `upload`, `external-link` | Keep the action direction and destination clear in text. Sharing can use a dedicated `share-2` icon only if user testing/rendered review finds it clearer. |
| Delete / warning / success / failure | `trash-2`, `triangle-alert`, `check`, `x` | Pair status symbols with readable words where ambiguity matters; never make color the sole signal. |

Do not automatically add status icons to every row or iconize native widgets that already communicate their role. One meaning should use one icon consistently. Keep the brand PNG as-is unless the separate branding task calls for a change.

Implementation should start with a small, reviewed subset of frequent actions and navigation. The accepted set, SVG attribution, and packaging/test decisions must be documented in the implementation PR.

## Review coverage

| Area | User task / states | Evidence required |
|---|---|---|
| First run and sidebar | No servers, add menu, large server list, selection, reorder, context menu | Screenshots, geometry checks, keyboard navigation |
| Server workspace | Starting, running, stopping, stopped, missing/unknown status; primary tabs and More menu | Screenshots plus state/enablement assertions |
| Setup and import | Create, folder/archive/Prism import, advanced settings, validation, cancel, failure | Screenshots plus no-side-effect cancel and validation assertions |
| Console | Long logs, command input, search/suggestions, no logs, scroll/resize | Screenshot and keyboard/focus tests |
| Mods | Table selection, installed-mod state, provider search loading/results/empty/error, dependencies and detail view | Keep PR #34/#35 changes intact; test integrated behavior once merged |
| World and backups | Empty state, available backups/slots, confirmation, progress/error | Temporary fixture screenshots and confirmation/cancel assertions |
| Configuration and properties | Long names/values, validation, advanced controls, save/cancel | Screenshots, validation and keyboard checks |
| Players and sharing/export | Empty/online lists, review gate, warnings, progress, success/failure | Synthetic data only; no real uploads |
| Settings and global dialogs | Default values, warnings, errors, destructive confirmation, close while work is pending | Keyboard-only and lifecycle assertions |
| Window and display | Minimum/large size, narrow width, 100%/125%/150% scaling where available | Screenshot contact sheet and clipping/visibility report |

Each finding should state severity, screen/state, exact widget or interaction, evidence, proposed fix, and a repeatable check. A source-level hypothesis must stay labeled as a hypothesis until reproduced.

## Implementation sequence after the integration gate

1. Fetch current `main`; record its commit and inspect the merged PRs plus current design files.
2. Capture and inspect the integrated baseline screenshots. Do not treat the existence of PNGs as a visual pass.
3. Add a small token layer and bundled SVG assets, package-data coverage, source/license notes, and focused tests.
4. Apply tokens and icons to a limited group of shared controls/screens first; inspect the screenshots before extending the pattern.
5. Introduce loading/empty/error/success states where they meaningfully reduce uncertainty. Start with static skeletons; animate only where helpful and safe.
6. Add restrained state transitions and reduced-motion fallback without delaying input or affecting server operations.
7. Run the unit suite, real Qt interaction checks, keyboard-only checks, focus visibility/contrast review, and scaling/clipping review. Capture before/after states and document remaining gaps.
8. Update changelog and design docs; open a focused PR. Do not auto-deploy or change the release version.

## Completion criteria

- All new visual icons are local SVG files with documented provenance and license; no AI raster art or third-party runtime decoration requests.
- Repeated meanings use the same icon and visual treatment. Icons do not replace labels where meaning could be ambiguous.
- Primary, secondary, disabled, destructive, focused, loading, empty, failed, and successful states are visually distinguishable where applicable.
- Skeletons always resolve or are removed on close/cancel; stale asynchronous results do not revive closed UI.
- Keyboard focus is visible and predictable across interactive controls; controls remain usable at supported window sizes and tested scaling factors.
- Screenshots have been opened and reviewed. Interaction assertions accompany important screenshots. Test results and untested environments are reported honestly.
- Existing workflows, server-safety boundaries, provider integration, and cancel behavior remain intact.

WCAG 2.2 is a useful reference for focus visibility and pointer-target sizing, but its web-specific success criteria should not be described as proof of native Qt-app conformance. Evaluate the actual desktop platform and controls, using WCAG guidance as a review yardstick where appropriate.

## References

- Lucide icon guide: https://lucide.dev/guide/ and https://github.com/lucide-icons/lucide/blob/main/docs/how-to/icon-guide.md
- Nielsen Norman Group, “The Custodial Era of UX: Cleaning Up After AI” (2026-08-28): https://www.nngroup.com/articles/ai-ux-debt/
- W3C, Web Content Accessibility Guidelines (WCAG) 2.2: https://www.w3.org/TR/wcag/
- Existing Crucible workflow audit: [ux-audit.md](ux-audit.md)
- Existing screenshot/test instructions: [ci-and-ui-evidence.md](ci-and-ui-evidence.md)
