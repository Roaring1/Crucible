# UI Design and Interaction Workstream

**Status:** PRs #37–#41 are implemented in focused open PRs. A disposable combined tree applied their exact heads with no changed-file overlap; integrated validation is green (631 passed, 74 subtests; all five PRs have green Python 3.11/3.13 CI; wheel verified). One small-window issue remains: Setup at 640×420 has reproducible horizontal overflow and right-edge clipping. PRs #34/#35 remain integration gates for broader Add Mod/provider work.  
**Prepared:** 2026-10-29  
**Initial reference commit:** `34ca28fd8f989fe3eb98b90c81012d141684f4c9`  
**Workstream:** Chat 3 — whole-app UX, local SVG icons, loading skeletons, restrained transitions.

This document records the implementation boundary, evidence-only visual audit, and verification plan. The screenshots listed below were actually opened and reviewed; the audit is a bounded offscreen Qt review, not a claim that every application state, physical display, screen reader, or assistive-technology combination has been tested. The existing [UX audit](ux-audit.md) covers key first-run, create, import, and navigation decisions; this work extends the review across the whole application and its visual system.

## Integration gate and scope

The provider-identity work in PR [#34](https://github.com/Roaring1/Crucible/pull/34) and Add Mod icon lifecycle work in PR [#35](https://github.com/Roaring1/Crucible/pull/35) were open at preparation time. Both touch the Add Mod browser or closely related tests and preview tooling.

- Do not edit `crucible/ui/add_mod_dialog.py`, `scripts/capture_ui_previews.py`, or their tests while PR #35 remains active.
- Do not edit provider identity, download, or related behavior from PR #34.
- Broader design work touching Add Mod/provider code remains gated on both dependencies integrating. The independent keyboard-focus/Create Server DPI fixes are implemented separately in [PR #37](https://github.com/Roaring1/Crucible/pull/37), based on exact main SHA `34ca28fd8f989fe3eb98b90c81012d141684f4c9`; the active PR changed-file sets were rechecked and do not overlap. If either dependency changes scope, re-check changed filenames before more UI work.
- Keep visual changes in one focused branch/PR. Do not bump the release version. Keep all visual assets local; no AI-generated images, generated raster illustrations, or external runtime image requests for decoration.
- Test with synthetic server instances, temporary registries, and fake APIs. Never modify real server/world data, read or print a live CurseForge secret, or include secrets in screenshots, fixtures, logs, or reports.

## Source-level baseline (not a visual acceptance result)

Files inspected on the initial base:

- `crucible/ui/theme.py`: existing palette is warm dark/amber and centralized in one stylesheet. A global `outline: none` rule is present, while line edits define their own focus border. Focus behavior across all other controls needs rendered keyboard testing; do not infer that every control is inaccessible from the stylesheet alone.
- `crucible/ui/sidebar.py` and `crucible/ui/instance_panel.py`: some actions use Unicode glyphs next to text labels. The aim is consistency and legibility, not replacing every glyph or every native control with a custom icon.
- `crucible/assets/`: the inspected main tree contained the app PNG but no dedicated SVG icon family.
- `scripts/capture_ui_previews.py`: the existing helper renders six states offscreen using temporary data. Its current test primarily verifies expected image files/PNG headers/size; it does not by itself establish visual quality or prove real pointer/keyboard journeys.
- `docs/development/ux-audit.md`: provides a useful, existing record of first-run, import, and selected-server workflow reasoning. Update it only when new evidence justifies changing those conclusions; do not silently rewrite its historical scope.

## Focused source observations to verify in the rendered app

These are reproducible code/palette observations, not a claim of a completed visual or assistive-technology audit:

- **Primary navigation mixes symbol styles.** Setup, World, and Backups use emoji in their labels, while Console and Mods are text-only. Less-frequent tabs hidden behind More also mix emoji and text. Evaluate a consistent approach at actual tab widths; uniform text-only navigation may be better than adding icons everywhere.
- **Context-menu palette bypasses the theme.** The sidebar context menu embeds colors and radius values directly rather than reusing `theme.py` tokens. It visibly risks drifting from the rest of the interface as the palette evolves.
- **Sidebar status is visually represented by a dot.** The status word is in the row tooltip, while the selected-server header shows a text label. Verify keyboard and assistive-technology discoverability; consider an accessible description and whether a clearer non-color-only cue is needed without cluttering every row.
- **Stopped-state dot may be too dim.** Simple WCAG-style sRGB calculations from the current palette give `SURFACE2 (#545460)` against `BASE (#1c1c1e)` about **2.28:1**. If that dot alone communicates state, it falls below the 3:1 non-text contrast reference. Confirm its actual rendering and strengthen the marker or expose status through a clearer textual/accessibility channel.
- **Main text contrast is strong in the base palette.** `TEXT (#dddad0)` against `BASE (#1c1c1e)` calculates to about **12.16:1**; `SUBTEXT (#96929e)` against `SURFACE0 (#2c2c30)` calculates to about **4.57:1**. This is not a complete contrast audit: inline widget styles, disabled states, selected/hover states, status colors, and actual rendered backgrounds still need inspection.
- **Focus is an explicit test target.** The shared stylesheet contains a global `outline: none` rule and styles focus borders on line edits. Test every interactive control with keyboard navigation before deciding whether any native focus treatment is suppressed or missing.

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

## Evidence-only visual audit — 2026-10-29

### Method, source boundary, and evidence location

- **Exact source snapshot:** \`34ca28fd8f989fe3eb98b90c81012d141684f4c9\` (GitHub \`main\` at capture time). The local checkout's \`main\` ref did not match remote \`main\`, so the audit source was obtained with \`git archive\` at the explicit SHA rather than by trusting the local branch.
- **Output root on the Nobara machine:** \`/tmp/crucible-ui-audit-20261029.bQJK3g/\`. The original PNGs, compressed review copies, JSON metadata, and SHA-256 manifest are local temporary evidence, not committed source files. The selected-file manifest is \`SHA256SUMS-evidence.txt\`.
- **Standard preview states:** ran the existing \`scripts/capture_ui_previews.py\` at Qt scale factors 1.0, 1.25, and 1.5, giving six previews per scale (main empty, add-server menu, main selected, create server, import files, install modpack). The script used temporary registry data; the modpack/version lookups are explicitly disabled by the preview harness.
- **Additional screens:** a separate temporary harness captured Setup, Console, World, Backups, Config, Players, Notes, and Info at 900×600, 1200×760, and a 1500×900 selected-server window. It also captured reset confirmation, wipe input prompt, and invalid-port warning dialogs. Test instance/config/world files were synthetic and isolated under \`/tmp\`; methods that could start/stop a real service were stubbed, and modal operations were cancelled automatically.
- **Focus probe:** a temporary Qt test recorded focus-widget progression from repeated Tab presses, captured screenshots for selected focus steps, and compared their pixels. Focus ownership moved between widgets, but screenshots for the steps covering SidebarList, Add server, Copy IP, Start, Restart, QTabBar, More, and Accept EULA were byte-identical. A second 900×600 Setup pass recorded 50 Tab presses, reached the scroll area's maximum scroll value, and reached bottom-page controls such as “Check for updates” and “Update from a link or file”; metadata is in `keyboard-setup/setup-tab-order.json`. This establishes that the Setup content is keyboard-reachable in this synthetic case, but does not establish screen-reader behavior or visible focus indication.
- **Scaling:** the same Create dialog was rendered under \`QT_SCALE_FACTOR=1.0\`, \`1.25\`, and \`1.5\`. This exposes an actual clipping issue in offscreen captures, but must be confirmed on the user's normal desktop because offscreen scaling is not a substitute for physical high-DPI rendering.
- **Safety check:** the temporary world marker remained present after the cancel-only dialog runs. No actual server registry, world, server process, API key, mod download, or user server data was used.
- **Capture harness limitation:** the multi-instance sidebar capture process segfaulted during Qt shutdown *after* writing the screenshots. Treat those sidebar variants as supporting images only; this capture did not count as a clean process-level pass.

Evidence file examples (all below the audit root unless a path is shown explicitly):
- First-run: \`previews/main-empty.png\`
- Create form / DPI comparison: \`previews/create-server.png\`, \`previews-sf1.25/create-server.png\`, \`previews-sf1.5/create-server.png\`, \`create-fields-scaling.jpg\`
- Import and selected workspace: \`previews/import-files.png\`, \`previews/main-selected.png\`
- Console/World/Backups/Config/Players/Notes/Info at standard dimensions: \`extra-sf1.0/console-standard-sf1.0.png\`, \`extra-sf1.0/world-standard-sf1.0.png\`, \`extra-sf1.0/backups-standard-sf1.0.png\`, \`extra-sf1.0/config-standard-sf1.0.png\`, \`extra-sf1.0/players-standard-sf1.0.png\`, \`extra-sf1.0/notes-standard-sf1.0.png\`, \`extra-sf1.0/info-standard-sf1.0.png\`
- Additional dialogs: \`extra-sf1.0/world-reset-confirm-sf1.0.png\`, \`extra-sf1.0/world-wipe-input-sf1.0.png\`, \`extra-sf1.0/invalid-port-warning-sf1.0.png\`
- Keyboard focus: \`keyboard/focus-chain.json\`, \`keyboard/focus-00.png\`, \`keyboard/focus-01.png\`, \`keyboard/focus-03.png\`, \`keyboard/focus-05.png\`, \`keyboard/focus-07.png\`, \`keyboard/focus-11.png\`
- Multi-instance sidebar: \`sidebar-variants/contact-sidebar.jpg\` (process crashed on shutdown after saving the images).

### Ranked findings

#### 1. High — visible keyboard focus is missing in the captured common controls

- **Screen/state and exact controls:** selected-server Setup with focus moving through \`SidebarList\`, \`SidebarAddButton\`, Copy IP, Start, Restart, \`QTabBar\`, \`MoreTabsButton\`, and Setup buttons.
- **Evidence:** \`keyboard/focus-chain.json\` records a different focused widget after Tab; \`keyboard/focus-00.png\`, \`focus-01.png\`, \`focus-03.png\`, \`focus-05.png\`, \`focus-07.png\`, and \`focus-11.png\` have identical SHA-256 hashes. A pixel comparison also found no changed pixels between focus step 0 and steps 1–11. Source inspection finds a global \`outline: none\` in \`crucible/ui/theme.py\`.
- **Classification:** visually reproduced in offscreen Qt. This does not yet prove behavior on every desktop style and does not constitute a screen-reader test.
- **Proposed change:** add a deliberate shared, high-contrast \`:focus\` treatment to buttons, tool buttons, the sidebar's selected/focused item, tab bar/tab, inputs, and menu actions as appropriate. Do not make focus indicators depend on hover or animation; preserve clear selected and disabled states.
- **Repeatable test:** run the temporary focus-chain probe (or promote it into a safe Qt test), use Tab/Shift+Tab to focus each control, capture its focus state, and assert visible pixel changes in a bounded indicator region. Repeat with ordinary desktop Qt rendering and manually inspect with the platform's screen reader before closing the finding.

#### 2. High — Create-server text fields clip their text at 125% and 150% Qt scale factors

- **Screen/state and exact controls:** Create a server dialog, \`NewServerDialog._name_edit\` and \`NewServerDialog._path_edit\`.
- **Evidence:** \`previews/create-server.png\` at scale 1.0 looks normal; the same existing preview tool at scale 1.25 and 1.5 shows the server name/path text vertically clipped inside the line edits. \`create-fields-scaling.jpg\` shows matched crops from the three outputs. Source is \`crucible/ui/new_server_dialog.py\`; the fields are ordinary \`QLineEdit\`s without an explicit sizing policy, under a stylesheet that sets font size and padding.
- **Classification:** visually reproduced in Qt offscreen scaled captures; verify against the real desktop compositor before treating offscreen pixels as final platform evidence.
- **Proposed change:** adjust the line-edit minimum/size policy and the related form layout/font metrics together rather than reducing text size. Inspect Browse, combo-box, and button baselines at the same time.
- **Repeatable test:** capture the dialog at \`QT_SCALE_FACTOR=1.0\`, \`1.25\`, and \`1.5\`; assert text ink remains within each field's content rectangle and compare actual rendered screenshots. Keep the test-only data local and network-free.

#### 3. Medium — the selected secondary-tab label truncates in the More button

- **Screen/state and exact control:** selected Config/Info secondary pages; \`InstancePanel._more_tabs_button\` (\`objectName=MoreTabsButton\`).
- **Evidence:** \`extra-sf1.0/config-standard-sf1.0.png\` and \`extra-sf1.5/config-standard-sf1.5.png\` show the secondary selection label shortened to \`More: Confi... settings\`; the Info label is likewise visually crowded. This occurs because \`_update_more_tabs_button()\` inserts the selected page's longer label into a compact corner widget.
- **Classification:** visually reproduced.
- **Proposed change:** keep the compact visible control label stable (for example, \`More ▾\`) and communicate the selected secondary page through the page itself and the menu's checked action; set a useful accessible name/tooltip without relying on clipped text. If a selected label is retained, choose measured short labels and enforce a sensible minimum width.
- **Repeatable test:** choose every More-menu page at 900×600 and 1200×760, then at scale factors 1.0/1.25/1.5; assert the button caption is not elided or clipped and the correct menu action remains checked.

#### 4. Medium — offline/stopped status marker is visually too dim, and the sidebar uses color dots as the inline status cue

- **Screen/state and exact widgets:** selected-server header \`_dot\` and \`_status_label\`; sidebar \`InstanceItem.drawRow()\` status dot.
- **Evidence:** \`previews/main-selected.png\`, \`extra-sf1.0/setup-min-sf1.0.png\`, and \`sidebar-variants/contact-sidebar.jpg\`. The stopped header label uses \`theme.SURFACE2\`; \`STATUS_COLORS["stopped"]\` also feeds the dot. Palette arithmetic gives \`SURFACE2 #545460\` against \`BASE #1c1c1e\` about 2.28:1. In the sidebar, each row paints a colored dot; the status word exists in the row tooltip, but it is not persistently visible inline.
- **Classification:** visual dimness and dot-only inline representation are observed; the numeric contrast is a color calculation from the current palette. Screen-reader announcements and tooltip accessibility have **not** been verified.
- **Proposed change:** make the stopped status label legible using a stronger theme text color, and do not rely on dot color as the sole persistent cue. Prefer a compact text/accessibility description or a higher-contrast marker that complements (rather than duplicates noisily) the selected-server header. Preserve a clear distinction for running, missing, unknown, starting, and stopping states.
- **Repeatable test:** render synthetic stopped/running/missing/unknown states, calculate contrast for actual text and status graphics, inspect selected/disabled states, and verify accessible names/descriptions using a screen reader. WCAG 2.2 contrast values are a useful reference yardstick for the native Qt app, not a claim of conformance: normal text 4.5:1 and graphical UI state 3:1 ([W3C WCAG 2.2](https://www.w3.org/TR/wcag/)).

#### 5. Implemented in PR #38 — bounded, safe World wipe confirmation

- **Screen/state and exact widget:** WorldTab._confirm_and_wipe() now uses _WipeWorldConfirmDialog instead of QInputDialog.getText().
- **Before evidence:** extra-sf1.0/world-wipe-input-sf1.0.png shows the prior 1122×148 one-line prompt with hard-to-scan text.
- **Implemented behavior:** the new dialog is capped at 560px and adapts to the parent width. Its warning says that no backup will be created and that the deletion is permanent; the world-folder label, warning panel, exact-token instruction/input, and buttons are separate. Cancel is default and initial focus. The destructive button remains disabled unless the text is exactly uppercase WIPE; WorldTab checks the accepted result and token again before dispatch. Escape/close cancel. Return in the field is validated before it can reach the default button.
- **Repeatable Qt tests:** tests/test_world_wipe_confirmation_qt.py covers Cancel, lowercase/wrong text and Return, exact token, Escape, close, focus and Tab navigation, standard/small layout, and WorldTab integration. The wipe dispatch boundary is mocked, and the synthetic world marker is checked after every integration case.
- **Final screenshot evidence:** /tmp/crucible-world-wipe-evidence-20261029/final/world-wipe-before-after-contact.jpg compares the original prompt with standard (1200×760 parent; 560×349 dialog) and small (520×420 parent; 472×349 dialog) captures. The complete warning is visible in both. The manifest is /tmp/crucible-world-wipe-evidence-20261029/final/SHA256SUMS.txt.
- **Validation:** the full suite passes with 617 tests and 74 subtests; the targeted dialog suite passes 10 interaction/visual cases. Captures use offscreen Qt and synthetic folders; desktop-compositor and screen-reader verification are still outside the evidence collected here.

#### 6. Low–Medium — empty backup tables give no next-step guidance

- **Screen/state and exact widgets:** Backups tab table and World tab's “Saved worlds (backups)” section.
- **Evidence:** \`extra-sf1.0/backups-standard-sf1.0.png\` shows \`Backups: 0 (0 KB)\` above an empty filename table; \`extra-sf1.0/world-standard-sf1.0.png\` shows the saved-worlds table empty under a descriptive heading. Both screens retain their action buttons, but the table areas are mostly blank.
- **Classification:** visually observed; this is a comprehension/usability opportunity rather than a functional failure.
- **Proposed change:** add brief inline empty states: “No backups yet. Select Backup Now to create one.” and “No saved world slots yet. Create a backup to make one available.” Keep these messages near the relevant table and avoid a large decorative illustration.
- **Repeatable test:** render empty and populated fixture states side by side; assert the empty message disappears once a row exists, and that the Backup Now / world-backup actions remain reachable and functional with temporary data.

#### 7. Partially verified — vertical keyboard scroll works at 900×600; horizontal overflow at 640×420 remains

- **Screen/state and exact widgets:** Setup tab at 900×600 and 640×420; server readiness action buttons, “Save memory settings”, and “Apply suggested”.
- **Vertical-focus regression guard:** PR #41 adds tests/test_setup_scroll_keyboard_qt.py. The 900×600 synthetic case records 14 content controls in the Tab traversal, zero focused controls outside the viewport, scroll maximum 348px, and a visible bottom “Update from a link or file…” action. PageUp scrolls back.
- **New combined integration finding:** at 640×420 the content width is 684px while the scroll viewport is 633px; the horizontal scrollbar maximum is 51px and the horizontal value remains 0 in the captured state. “Accept EULA” and “Install server now” extend to x=649, while “Save memory settings” and “Apply suggested” extend to x=663. Their right edges exceed the 633px viewport, so the right-side controls are clipped at the initial horizontal position.
- **Classification:** vertical keyboard reachability works in both synthetic standard and small captures, but **horizontal clipping at 640×420 is reproduced in the combined offscreen Qt tree**. PR #41 covers the 900×600 vertical-focus behavior only; its passing test does not cover this 640px-wide overflow.
- **Proposed fix after PRs #37–#41 are reviewed/integrated:** make the quick-action, readiness-button, and memory-control rows wrap or stack at narrow widths; do not reduce text size. Do not open a new feature branch as part of the current integration audit.
- **Evidence:** /tmp/crucible-setup-scroll-evidence-20261029/setup-scroll-bottom.png, plus the combined captures /tmp/crucible-pr37-41-integration-audit-20261029/visual-audit/setup/setup-bottom-standard.png and setup-bottom-small.png. Combined viewport and button geometry was measured during the disposable audit.
- **Limits:** offscreen Qt is not proof for every desktop compositor or screen reader; verify the responsive fix and keyboard path on the real desktop before closing this finding.

#### 8. Low / source-level — sidebar context menu colors are hard-coded outside the theme

- **Screen/state and exact widget:** \`Sidebar._on_context_menu()\` in \`crucible/ui/sidebar.py\`.
- **Evidence:** source sets its own purple-toned colors (\`#1e1e2e\`, \`#cdd6f4\`, \`#45475a\`, \`#313244\`) while the shared application palette is warm dark/amber in \`crucible/ui/theme.py\`. This menu was not separately captured in the evidence run; the finding is source-level, not a visually reproduced defect.
- **Proposed change:** route menu colors, border, focus/selection, and spacing through shared tokens or a shared menu style. Keep the menu readable with long server names and preserve native keyboard behavior.
- **Repeatable test:** capture the menu for a short and long server name, inspect normal/selected/disabled/focused menu rows and check that theme values come from shared tokens.

### Other observations that did not warrant a defect report

- **First run:** \`previews/main-empty.png\` makes “No servers yet” prominent, explains the next step, and gives Add server a clear primary treatment. The empty state is deliberately sparse and looks coherent.
- **Add-server menu:** \`previews/add-server-menu.png\` clearly separates create, modpack install, and import tasks.
- **Import:** \`previews/import-files.png\` and the 150% capture show the source choices, destination, optional Change action, collapsed registration, and Cancel/Add server actions in a readable hierarchy.
- **Console:** \`extra-sf1.0/console-standard-sf1.0.png\` gives the log viewport most of the space and groups command entry/utilities at the bottom; the empty/offline message makes the absence of logs understandable.
- **Players and Notes:** Players explicitly says “No players online”; Notes has a brief placeholder. These are better empty-state examples than the blank backup tables.
- **World reset confirmation and invalid-port warning:** \`extra-sf1.0/world-reset-confirm-sf1.0.png\` and \`extra-sf1.0/invalid-port-warning-sf1.0.png\` are readable at the captured size. Their actions were not confirmed/continued during the audit.
- **Icon restraint:** the current mix of emoji and text in navigation is inconsistent, but the answer is not to decorate every action. Use a small set of repeatable SVGs where they materially improve scanning, while retaining text for command/destructive/ambiguous actions.

### Audit limits

- All captures were from an offscreen Qt renderer, not a full physical desktop/display-driver matrix.
- The focus-chain check measured widget focus and pixels; it did not test screen-reader output, color-vision emulation, or moderated usability with participants.
- The synthetic server's mods directory was deliberately absent in these fixtures (some startup/log messages note that); the review is not a claim about a fully installed server state.
- The temporary scripts and screenshots are not part of PR #36. The document records findings and exact local evidence paths; it does not claim that visual or accessibility fixes have already been implemented.
- The six standard preview captures do not include the Mod tab's Add Mod UI because that is covered by active PR #35. Do not modify the active Add Mod/provider files until those changes integrate.

## Implementation evidence — keyboard focus and Create Server DPI (2026-10-29)

### Implementation branch and scope

The independent fixes are in [PR #37 — Improve keyboard focus and Create Server DPI sizing](https://github.com/Roaring1/Crucible/pull/37).

- Branch: \`fix/ui-focus-dpi-20261029\`
- Base: \`34ca28fd8f989fe3eb98b90c81012d141684f4c9\`
- Implementation commit: \`eece818439b752ac274f87f16b421bdfc84c2d09\`
- Modified paths are limited to \`crucible/ui/theme.py\`, \`crucible/ui/new_server_dialog.py\`, \`crucible/ui/instance_panel.py\`, \`crucible/ui/sidebar.py\`, and the new \`tests/test_ui_focus_dpi.py\`.
- The current changed-file lists for PRs #34 and #35 were inspected before branching. No provider/download files, \`crucible/ui/add_mod_dialog.py\`, \`scripts/capture_ui_previews.py\`, or their tests were changed.
- No release-version bump, deployment, SVG addition, AI-generated image, or runtime remote-image request was made.

### Fix 1 — visible keyboard focus

The shared Qt stylesheet now applies an explicit focus state independently of hover to buttons, tool buttons, combo boxes, line edits, multi-line text inputs, checkboxes/radio buttons, spinboxes, tab bars, list/tree/table views, selected list items, and selected menu items. Amber-filled primary actions use a dark ring so the indicator remains visible on the amber fill. The More-tabs corner button, sidebar context menu, Create Server advanced-options toggle, and EULA checkbox also have treatment for their local stylesheets, which otherwise override parts of the shared theme.

The regression test renders and pixel-checks 12 shared control types plus the two dialog-local controls at Qt scale factors 1.0, 1.25, and 1.5. In all three final runs, no focus target failed its focus-color assertion. It also asserts the test disabled button remains disabled and the selected tab stays selected. Focused screenshots were opened and inspected, including the selected tab (with the focus check deliberately ignoring the pre-existing amber top stripe), list border, menu item, primary button, advanced toggle, and EULA row.

### Fix 2 — Create Server fields at high DPI

The Name, Server folder, and optional Loader version inputs now get a minimum height derived from their actual font metrics, existing vertical padding, focus-border width, and a small Qt layout/font-polish allowance. The font size is not reduced. The editable Minecraft version combo also gets a font-metric-derived minimum height.

For each of Qt scale factors 1.0, 1.25, and 1.5, the final rendered run measured all three visible line edits at 33 logical pixels high for 18-pixel font metrics; the test's computed minimum was 32 logical pixels. Each field passed the fit assertion. The original baseline captures at 1.25 and 1.5 visibly clip the name/path text; the after captures show both values completely legible. The expanded advanced state selects NeoForge, making the optional Loader version field visible for the same check.

### Test and screenshot evidence

- Final full suite on the implementation commit: **610 passed, 74 subtests passed**.
- Targeted focus/DPI test: **3 scale-factor cases passed**.
- \`compileall\` on the four modified UI modules plus the new test passed; \`git diff --check\` passed.
- Baseline screenshots opened: \`/tmp/crucible-ui-audit-20261029.bQJK3g/previews/create-server.png\`, \`previews-sf1.25/create-server.png\`, and \`previews-sf1.5/create-server.png\`.
- Final screenshots opened: \`/tmp/crucible-ui-audit-20261029.bQJK3g/implementation-after/scale-1.0/create-server-after.png\`, equivalent \`scale-1.25/create-server-after.png\` and \`scale-1.5/create-server-after.png\`, the focused-name and advanced-state captures at all three scales, and focus contact sheets for each scale.
- The full evidence root is \`/tmp/crucible-ui-audit-20261029.bQJK3g/implementation-after/\`. It contains scale-specific screenshots, \`measurements.json\` files, three Create Server scaling contact sheets, focus contact sheets, and \`SHA256SUMS-implementation.txt\` (39 selected evidence hashes at the final capture).
- All evidence was generated using the pinned source worktree, offscreen Qt, temporary registries, and disabled version/network lookup. No live server/world/provider operation was performed.

### Remaining validation limits

This confirms focus-border visibility and line-edit sizing in the offscreen Qt renderer. It is not proof of identical behavior on every real desktop compositor/platform style, at other display scales, or with screen readers. Manual desktop focus/keyboard and assistive-technology verification remain before treating accessibility as complete. The SVG provenance/licensing inventory remains a plan in this workstream; this focused fix intentionally did not add icons or begin ghost UI/transitions.

## Implementation evidence — World wipe confirmation

The separate implementation is in [PR #38 — Make World wipe confirmation bounded and safe](https://github.com/Roaring1/Crucible/pull/38).

- **Branch/base/commit:** fix/world-wipe-confirmation-20261029 / 34ca28fd8f989fe3eb98b90c81012d141684f4c9 / 6269467f103f0fe2ea348347b7cf394332782157.
- **Changed paths:** only crucible/ui/tabs/world_tab.py and new tests/test_world_wipe_confirmation_qt.py. Current open-PR filenames were rechecked immediately before opening #38; it overlaps none of #34–#37.
- **Safety gate:** a separate _start_wipe() is reached only after the dialog returns Accepted and its token is exactly WIPE; the dialog itself checks the same exact case-sensitive token. Cancel is the default and initial focus, Escape/window close reject, Return is intercepted to avoid accidental default-button activation, and Tab/Space supports keyboard-only confirmation. Local focus indicators are checked at the pixel level.
- **Validation:** targeted dialog tests 10 passed; World-related combined tests 38 passed; full suite 617 passed, 74 subtests passed; compileall and git diff --check passed.
- **Visual evidence opened:** original extra-sf1.0/world-wipe-input-sf1.0.png; final standard and small dialog captures; world-wipe-before-after-contact.jpg. Measurements show 560×349 for the standard host and 472×349 for the small host; warning body height increases from 54px to 72px to accommodate wrapping.
- **Artifacts:** the PNGs, measurements.json, contact sheet, and SHA256SUMS.txt are under /tmp/crucible-world-wipe-evidence-20261029/final/ on the supervised Nobara machine. Tests use synthetic server data and mock the dispatch boundary; no actual wipe operation runs.
- **Limits:** screenshot and widget-pixel checks use offscreen Qt. Physical desktop styling and screen-reader behavior remain to be verified manually.

## Implementation evidence — local SVG icon family

The small local icon family is implemented in [PR #39 — Add a small licensed local SVG icon family](https://github.com/Roaring1/Crucible/pull/39).

- Branch/commit/base: feat/local-svg-icon-family-20261029 / d932b5b24ca7228ebe5dcbd37c980106f3f660a9 / 34ca28fd8f989fe3eb98b90c81012d141684f4c9.
- The three local Lucide-derived SVGs are folder-open, archive, and refresh-cw. They are used only next to the existing Open Folder, Backup Now, and Reload labels. Backup Now's icon uses dark ink against the amber-filled primary button; the other two are amber on dark surfaces.
- Exact upstream Lucide commit is 81875c8c42d2320fa5123b37fee3554f4ca2bddc. ICON_SOURCES.md lists upstream blob IDs, source SHA-256, packaged-file SHA-256, the single per-file stroke-color customization, and license notes. The full upstream license is included in the package with its ISC and Feather/MIT notices preserved.
- The icon helper uses a three-name allow-list and only local package files; unrecognized names return an empty icon. The setuptools package-data and the existing release-script assertion now include SVGs and the packaged license.
- Validation: the final full suite passes with 611 tests and 74 subtests. Python 3.11 and 3.13 CI checks pass after the wheel test was changed to use ordinary PEP 517 build isolation. The wheel test confirms all three SVGs, the license, and the helper are inside the built wheel.
- Opened and inspected the actual Backup and Config after screenshots at 1040×560. Evidence on the supervised Nobara machine: /tmp/crucible-svg-icons-evidence-20261029/backup-actions.png, config-actions.png, backup-config-icon-review.jpg, render-measurements.json, and SHA256SUMS.txt.

## Implementation evidence — PlayersTab avatar lifecycle

The first stateful loading-UI slice is implemented in [PR #40 — Make player avatar loading and failure states reliable](https://github.com/Roaring1/Crucible/pull/40).

- **Branch / base / commit:** feat/player-avatar-load-states-20261029 / 34ca28fd8f989fe3eb98b90c81012d141684f4c9 / f3129d05bba04e05875e05269eb2956a9be5f0eb.
- **Changed paths:** only crucible/ui/tabs/players_tab.py and tests/test_player_avatar_states_qt.py. Changed-file lists for every open PR #34–#39 were rechecked directly before opening #40; no overlap.
- **Loading:** a static local 20×20 skeleton is shown with a short tooltip while the avatar is requested. The player name and contextual actions remain present.
- **Success:** image bytes are validated/cached in the worker, and QPixmap creation happens in the GUI thread. A valid stale cache remains available when an offline refresh fails.
- **Error:** the worker always emits its completion signal, even when the cache and network both fail. Pending state clears, a local fallback icon using `theme.RED` appears, and a tooltip says the avatar is unavailable while the player remains online/actionable. A pixel-level Qt assertion confirms the red/pink marker is present in both standard and small captures (44 qualifying pixels each).
- **Empty / reduced motion:** the actual empty row remains “No players online”; no placeholder rows are emitted. This initial implementation has no animation objects and performs immediate state replacement, so it remains motion-free without relying on an unimplemented OS motion preference.
- **Switch / close safety:** generation tokens prevent a late result from an old server or closed widget from changing the current UI. A module-level owner retains the worker and native QThread if the widget is destroyed during a request; cleanup releases it only after the thread stops. The network request remains bounded to 8 seconds rather than being forcibly interrupted inside urllib.
- **Validation:** full suite 613 passed, 74 subtests passed; the dedicated avatar Qt suite passed 6 tests, and the separately rerun `test_log_tail.py` + `test_qt_slot_contracts.py` subset passed 10 tests. compileall and git diff --check passed. GitHub Actions checks on Python 3.11 and 3.13 both pass. The Qt suite includes mocked success, offline failure, stale cache, stale completion after server switch, close/delete with the worker actually blocked in a mocked request, and loading/success/error/empty captures.
- **Visual evidence:** screenshots at standard 1040×560 and small 640×420 sizes were opened and inspected. Real worker success, offline error, and stale-cache fallback captures were opened as well. Evidence files are on the supervised Nobara host in /tmp/crucible-player-avatar-evidence-20261029/: players-avatar-states-standard-contact.jpg, players-avatar-states-small-contact.jpg, players-avatar-worker-and-state-review.jpg, and the final state contact sheet under `/tmp/crucible-player-avatar-evidence-20261029/final/`, with measurements JSON files and SHA256 manifests.
- **Limits:** these are offscreen Qt renderings and deterministic mocked network outcomes. They confirm widget state and thread cleanup but are not physical-desktop or screen-reader verification.

## Implementation evidence — Setup keyboard scroll regression

The focused test-only guard is in [PR #41 — Test Setup keyboard scroll reachability at minimum size](https://github.com/Roaring1/Crucible/pull/41).

- **Branch/base/commit:** test/setup-scroll-reachability-20261029 / 34ca28fd8f989fe3eb98b90c81012d141684f4c9 / 5c555da8ae91aa249bc368a8c5595ec2f5fd9d88.
- **Changed path:** tests/test_setup_scroll_keyboard_qt.py only. Changed-file sets for all open PRs #34–#40 were refreshed before this PR; none overlap.
- **Validation:** focused test 1 passed; full suite 608 passed, 74 subtests passed; compileall and git diff --check passed. GitHub Actions checks on Python 3.11 and Python 3.13 both pass.
- **What it establishes:** on the actual SetupTab widget with synthetic server data, Tab reaches the bottom update action, focused controls remain entirely inside the visible viewport, the scrollbar reaches its maximum, and PageUp scrolls back. No setup action is activated.
- **Screenshot evidence:** /tmp/crucible-setup-scroll-evidence-20261029/setup-scroll-bottom.png; report and SHA256 manifest in the same directory.
- **Scope decision:** because the keyboard/scroll concern was not reproduced as a defect, this PR adds a guard rather than changing production layout. Real desktop and assistive-technology validation remains a separate question.

## Combined integration audit — PRs #37–#41 (2026-10-29)

A disposable worktree was created at the freshly fetched base commit 34ca28fd8f989fe3eb98b90c81012d141684f4c9. The exact PR heads were applied as patches to that tree only; no branch was merged, rewritten, or committed by the integration audit.

| PR | Exact audited head | Purpose | Changed-file overlap with other audited PRs |
|---|---|---|---|
| #37 | eece818439b752ac274f87f16b421bdfc84c2d09 | Shared visible keyboard focus and Create Server high-DPI sizing | None |
| #38 | 6269467f103f0fe2ea348347b7cf394332782157 | Bounded World wipe confirmation and exact WIPE gate | None |
| #39 | d932b5b24ca7228ebe5dcbd37c980106f3f660a9 | Local SVG family, provenance/license, wheel packaging | None |
| #40 | f3129d05bba04e05875e05269eb2956a9be5f0eb | Player avatar loading, error, stale-cache and thread cleanup | None |
| #41 | 5c555da8ae91aa249bc368a8c5595ec2f5fd9d88 | Test-only Setup vertical keyboard-scroll guard | None |

### Combined verification

- **Full test suite:** 631 passed, 74 subtests passed. `compileall` and `git diff --check` passed on the combined tree.
- **Hosted CI:** Python 3.11 and Python 3.13 checks are green on the exact current heads of all five PRs.
- **Wheel/package check:** independently built `crucible-0.8.0-py3-none-any.whl` from the combined tree (373,473 bytes; SHA-256 `00a87b790ebf92f2d77f3ef0e66c89c4ae8e4bcacb0461eebecf61adf9e1c8d2`). It contains the three SVGs, `crucible/ui/icons.py`, and the complete packaged Lucide license file. The packaged license SHA-256 is `b495047bd93a9b06913511076f504daba17d5bbeb3e0650f3bb53a4220329c57`, matching the recorded source copy.
- **Provenance/network/art check:** ICON_SOURCES.md includes the pinned Lucide commit, upstream blob IDs, original and packaged SHA-256 hashes, local stroke-color edits, and license notes. The packaged license retains its ISC and Feather/MIT sections. The icon loader reads only local package files; no decorative icon runtime network request was found. The only new visual assets are the three pinned-source SVGs—no AI-generated or new raster image assets were added. PlayersTab still fetches real player avatars from Minotar, with an 8-second timeout; that existing avatar content request is distinct from decorative icons.
- **Shared focus/Create Server:** the integrated focus driver recorded no missing focus rings. At the standard 100% Qt scale, the Create Server name/path/loader-version fields were each 33px high against 18px font metrics plus 14px for padding/focus border. The full suite also ran the existing 100%, 125%, and 150% scale-factor regression cases.
- **World wipe:** combined dialog captures at standard and small parent sizes show the bounded 560×349 dialog, naturally wrapped warning, separate WIPE instruction/input, Cancel focused and default, and destructive action initially disabled. An integrated WorldTab call-path check for Cancel at 1200×760 and 640×420, and incorrect text at 1200×760, left the call list for the wipe worker empty and kept the synthetic world marker present.
- **SVG controls:** standard and small Backup/Config captures retain “Open Folder”, “Backup Now”, and “Reload” labels. Their icons loaded locally; Reload retains accessible name “Reload server properties”. The archive icon uses dark ink on the amber primary button.
- **Avatar loading/error:** integrated 1040×560 and 640×420 captures show the static skeleton and a contrasting red/pink error marker; both states keep the player label and explanatory tooltip. The error marker had 44 qualifying red/pink pixels in each icon capture, and no animation objects were present. Worker completion, stale result, cache, and close-during-fetch paths were covered by the combined test suite.
- **Setup scrolling:** Tab reached the bottom update action at both 900×600 and 640×420; focused target was visible and PageUp worked. However, the 640×420 capture also revealed the horizontal overflow detailed in finding #7 above.
- **Visual evidence:** the combined contact sheet is /tmp/crucible-pr37-41-integration-audit-20261029/visual-audit/combined-ui-contact-sheet.jpg; per-screen captures, measurements/reports, and hashes are under the same `visual-audit/` directory. Setup overflow geometry is preserved in `visual-audit/setup-small-overflow.json` (SHA-256 `1bc9bb8dfbda3e6387f850dc40694075efef38a7777e75f70af43f518fef6a59`). The wheel manifest is under `package-audit/wheel/package-audit.json`. These files are temporary integration evidence, not source additions.

### Proposed integration order — do not merge automatically

1. **PR #37 first:** it establishes the shared focus treatment and Create Server sizing baseline used by the other UI screens.
2. **PR #38:** integrate the independently tested destructive World wipe gate.
3. **PR #39:** integrate the small licensed SVG family and package-data changes; keep its provenance manifest attached to future asset changes.
4. **PR #40:** integrate the independent PlayersTab worker/state lifecycle.
5. **PR #41:** integrate the test-only 900×600 Setup vertical-focus guard after the production focus/layout baseline is in place.

All five branches are based on the same main commit and their changed filenames do not overlap. Merge one at a time, let hosted CI complete after each merge, and fetch the updated main before proceeding. This is a proposed order only; no integration was performed in this audit.

### Remaining audit findings vs. desktop validation

**Remaining implementation findings after the current set:**
- **Setup narrow width:** horizontal overflow at 640×420 is reproduced in the combined offscreen capture; a responsive row/layout fix remains outstanding.
- **More button:** secondary tab title truncation remains from finding #3; revisit after PR #37 integrates so it does not overlap the current focus/theme change.
- **Stopped status:** strengthen the dim stopped marker and improve non-color-only inline status communication from finding #4 after rechecking the merged sidebar/theme code.
- **Empty backups:** add short next-step guidance to empty Backup and saved-world tables from finding #6 after PR #39 integrates.
- **Sidebar context menu:** source-level hard-coded palette remains from finding #8; confirm the real menu rendering before treating it as a visually reproduced defect.
- **Motion:** no general animation system was added. Define and test a real reduced-motion policy before introducing transitions.

**Real-desktop checks still required:** verify shared focus visibility and Create Server fields at actual 100%/125%/150% display scaling; check the wipe dialog’s focus/Cancel/Escape behavior with the real desktop theme; inspect icon contrast and accessible names with available assistive technology; observe avatar fetch/cache/error/switch/close behavior against real UI lifecycle and a real network; and verify the Setup horizontal overflow and proposed responsive fix in the physical desktop compositor. Offscreen Qt and screenshots do not prove screen-reader behavior or pixel-identical rendering across desktop themes/compositors.

### Next independent UI task

Pause new UI scope until PRs #37–#41 have been reviewed and integrated in the order proposed above. The combined audit uncovered the Setup horizontal overflow at 640×420; keep it explicitly open, but do not start another feature PR during this integration cycle. After integration, address that narrow-width layout first, then revisit the More-tabs selected-label truncation (finding #3), stopped-state contrast/non-color status (finding #4), empty-backup next-step guidance (finding #6), and the sidebar context-menu palette (finding #8). Keep each fix focused, rechecking changed-file overlap against the refreshed main and any still-open PRs. For future animated transitions, first define a real reduced-motion policy and test loading/success/empty/error/cancel/close plus stale-result cleanup. Do not add animation to the avatar placeholders.
## Implementation sequence after the integration gate

1. Integrate the existing focused PRs in this order, one at a time: #37 (shared focus/Create Server DPI), #38 (World wipe confirmation), #39 (local SVG/license/package data), #40 (PlayersTab avatar lifecycle), and #41 (test-only Setup vertical-scroll guard). Wait for hosted CI after each merge and fetch the updated main before the next.
2. Do not add new UI feature branches during that merge sequence. Record the combined-audit Setup horizontal overflow at 640×420 as the next implementation target once the existing branches have integrated.
3. After integration, address the new narrow-width Setup overflow first, then the remaining ranked audit findings in focused branches after rechecking changed filenames and current main.
4. Perform real-desktop focus/scaling/keyboard checks and available assistive-technology review; keep the offscreen Qt limitations explicit in the evidence.
5. Define a reliable reduced-motion policy before adding transitions. Transitions must never hide status, delay interaction, or revive a closed/stale view. Keep the current avatar placeholders static.
6. Continue running the unit suite, real Qt interaction checks, keyboard-only checks, focus/contrast, scaling/clipping review, and wheel/package validation for the affected areas. Do not bump release version or deploy.

## Icon provenance and licensing requirement

For the eventual local SVG subset, do not assume every SVG in an icon repository has identical provenance just because icons share a visual style. Lucide's official project currently identifies the library as ISC-licensed while documenting a subset of icons that originated in Feather under MIT. Before assets are added:

- Pin and record the upstream Lucide version/tag or commit, retrieval date, icon names, original file hashes, and any path edits in \`ICON_SOURCES.md\`.
- Preserve the required upstream license text/attribution for the specific files actually used. Keep the ISC notice and any required MIT/Feather notice distinct in \`LICENSES/\`; do not copy icons without recording source and applicable terms. Verify against the actual upstream license files at adoption time: [Lucide license](https://lucide.dev/license) and [Lucide repository](https://github.com/lucide-icons/lucide).
- Bundle selected SVG assets under \`crucible/assets/icons/\` and update package-data (including \`assets/icons/*.svg\`) with a package-build test that opens each packaged asset.
- Use SVG source, not AI-generated images or raster redraws, and no external runtime image requests for decoration.
- Keep a restrained semantic set; do not give every button an icon. Labels remain adjacent to controls when they explain meaning or consequence.

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
