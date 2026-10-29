# UI Design and Interaction Workstream

**Status:** Evidence-only visual audit completed on pinned main snapshot; UI implementation not started while prerequisite PRs #34 and #35 remain open.  
**Prepared:** 2026-10-29  
**Initial reference commit:** `34ca28fd8f989fe3eb98b90c81012d141684f4c9`  
**Workstream:** Chat 3 — whole-app UX, local SVG icons, loading skeletons, restrained transitions.

This document records the implementation boundary, evidence-only visual audit, and verification plan. The screenshots listed below were actually opened and reviewed; the audit is a bounded offscreen Qt review, not a claim that every application state, physical display, screen reader, or assistive-technology combination has been tested. The existing [UX audit](ux-audit.md) covers key first-run, create, import, and navigation decisions; this work extends the review across the whole application and its visual system.

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

#### 5. Medium — the destructive world-wipe prompt is too wide and its instructions are hard to scan

- **Screen/state and exact widget:** \`WorldTab._confirm_and_wipe()\` uses \`QInputDialog.getText()\` for the permanent WIPE confirmation.
- **Evidence:** \`extra-sf1.0/world-wipe-input-sf1.0.png\` shows a 1122×148 dialog dominated by a long single-line explanation and a narrow text field. The call was captured from the real widget path with a synthetic server and then cancelled; the synthetic marker remained present.
- **Classification:** visually reproduced; the destructive branch was not executed.
- **Proposed change:** use a bounded custom confirmation dialog with wrapped warning text, a visually separate “Type WIPE to confirm” instruction and input, and explicit Cancel/confirm buttons. Keep the irreversible consequence and absence of automatic backup unmistakable.
- **Repeatable test:** capture at 900×600, 1200×760, and 1500×900, including 150% scale; ensure warning text wraps without clipping; cancel and verify the test world marker and files remain unchanged; enter a wrong phrase and verify refusal; use only a disposable fixture for any test of a valid phrase.

#### 6. Low–Medium — empty backup tables give no next-step guidance

- **Screen/state and exact widgets:** Backups tab table and World tab's “Saved worlds (backups)” section.
- **Evidence:** \`extra-sf1.0/backups-standard-sf1.0.png\` shows \`Backups: 0 (0 KB)\` above an empty filename table; \`extra-sf1.0/world-standard-sf1.0.png\` shows the saved-worlds table empty under a descriptive heading. Both screens retain their action buttons, but the table areas are mostly blank.
- **Classification:** visually observed; this is a comprehension/usability opportunity rather than a functional failure.
- **Proposed change:** add brief inline empty states: “No backups yet. Select Backup Now to create one.” and “No saved world slots yet. Create a backup to make one available.” Keep these messages near the relevant table and avoid a large decorative illustration.
- **Repeatable test:** render empty and populated fixture states side by side; assert the empty message disappears once a row exists, and that the Backup Now / world-backup actions remain reachable and functional with temporary data.

#### 7. Low — selected-server Setup content extends below the minimum-size viewport

- **Screen/state and exact widget:** Setup tab checklist and memory settings at 900×600.
- **Evidence:** \`extra-sf1.0/setup-min-sf1.0.png\` (also 125%/150% review sheets) shows that the Setup page continues below the visible area and relies on a scrollbar.
- **Classification:** visual observation, **not yet classified as a defect**; a scrollbar is present, so the screenshot alone does not prove content is unreachable.
- **Proposed change:** keep the checklist high in the hierarchy, make the scroll affordance easier to spot if keyboard interaction shows it is needed, and avoid shrinking text to fit everything at once.
- **Repeatable test:** navigate Setup using keyboard only at 900×600, reach the last memory/legacy controls via Tab and scrollbar keys, and assert focus never lands on hidden/offscreen actionable controls without scrolling them into view.

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

## Implementation sequence after the integration gate

1. Fetch current `main`; record its commit and inspect the merged PRs plus current design files.
2. Capture and inspect the integrated baseline screenshots. Do not treat the existence of PNGs as a visual pass.
3. Add a small token layer and bundled SVG assets, package-data coverage, source/license notes, and focused tests.
4. Apply tokens and icons to a limited group of shared controls/screens first; inspect the screenshots before extending the pattern.
5. Introduce loading/empty/error/success states where they meaningfully reduce uncertainty. Start with static skeletons; animate only where helpful and safe.
6. Add restrained state transitions and reduced-motion fallback without delaying input or affecting server operations.
7. Run the unit suite, real Qt interaction checks, keyboard-only checks, focus visibility/contrast review, and scaling/clipping review. Capture before/after states and document remaining gaps.
8. Update changelog and design docs; open a focused PR. Do not auto-deploy or change the release version.

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
