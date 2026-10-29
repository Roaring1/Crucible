# Modded 1.21.1 server audit — handoff (2026-10-28)

## Latest resumed-session update — 2026-10-28

The authoritative current state, final startup evidence, exact hashes, and rollback instructions are in [the fresh-world preflight](https://github.com/Roaring1/Crucible/blob/main/docs/operations/2026-10-28-fresh-world-preflight.md).

### Final status

- Production is **stopped**, port 25566 is free, and no production Minecraft JVM/tmux session exists. Crucible is ready for the operator's next Start click; `level-name=world_fresh_20261028` is configured.
- The definitive final first-generation test reached `Done (3.887s)!` from a level folder without `level.dat`; created `level.dat` plus four region files; loaded `file/fresh-world-launch-fixes (world)` among 20 enabled data packs; and logged `wover:normal` with 95 Terralith biomes.
- A second start reached `Done (3.561s)!`. Both starts have zero ERROR-level lines, zero recipe parse errors, zero loot-table parse errors, zero BCLib errors, zero Sable resource errors, zero invalid recipe-path errors, and zero dedicated-server class probes.
- Moonlight's complete Fabric API/Connector compatibility advisory is still visible at WARN; it is not suppressed. HammerLib's successful recipe-extension registration is INFO; the extension still registers and injects 43 recipes.

### Final deployed compatibility state

- Repair datapack under `world_fresh_20261028/datapacks/fresh-world-launch-fixes/`, SHA-256 `e76ddbb936b502eb664388da8bd75372580e451d029c37e49f3c702f61e75e57` (429 valid JSON resources).
- BCLib first-generation guard: `bclib-21.0.28.jar` SHA-256 `abf6e81ecaa1aafe945bd351e49fa9627c4ba351aeeba81ec50393be99fff80b`; it only skips the pre-patch if `worldDir/level.dat` doesn't exist. Fresh generation and second restart passed with zero BCLib errors.
- Moonlight advisory severity fix: `moonlight-1.21.1-3.7.2-neoforge.jar` SHA-256 `7c7e8ff34976c2a47850cf6090d4c643d7aa38a3e49a11e415fdcbad65aa9e80`; only `Logger.error` -> `Logger.warn` changed for the explicit mixed-loader advisory, with the entire warning text preserved.
- SablePhysicsCompat, Recipe Integration, Struts, and HammerLib fixes are deployed identically to server and Prism client. ConnectorExtras' EMI Bridge side fix is deployed to the dedicated server only; the client retains the original ConnectorExtras JAR.
- Rollback dirs: primary `/home/roaring/.local/share/crucible-backups/username-admin-server-fresh-world-deployment-20261028-171846/`; round two `/home/roaring/.local/share/crucible-backups/username-admin-server-compatibility-round2-20261028-172940/`; HammerLib `/home/roaring/.local/share/crucible-backups/username-admin-server-hammerlib-log-fix-20261028-173507/`; BCLib `/home/roaring/.local/share/crucible-backups/username-admin-server-bclib-firstgen-fix-20261028-174136/`; Moonlight `/home/roaring/.local/share/crucible-backups/username-admin-server-moonlight-warning-level-20261028-174408/`.
- Original production `world/level.dat` SHA-256 remains `091d4e46ff6980c931887ba66fd959712e57e211c83d6c929d0c7d086bf29482`. The production fresh world has no `level.dat` yet because production was intentionally not started.

### Immediate next action

No pre-start changes remain. When ready, click **Start** in Crucible for `username admin server`; do not change the level name back to `world`. Verify `Done`, enabled repair pack, new `world_fresh_20261028/level.dat` + region files, zero recipe/loot errors, and unchanged old-world hash, then playtest biomes, structures, loot, and normal modded recipe/machine flows.

## Status and confidence

This section began as a historical checkpoint from the earlier Supervisor outage. The connection has since recovered and the deployment actions above supersede the old checkpoint statements. The production server is still intentionally stopped; only the disposable test instance was started, and it has been stopped cleanly.

The final `TmuxManager.start()`/`stop()` smoke test, data-pack activation, recipe/loot/Sable validation, and worldgen locate checks have passed in the disposable copy. Production configuration and the matching Prism client have been prepared with rollback archives. Production has not yet generated or loaded the new world.

## Environment

- Crucible version observed: 0.8.0.
- Minecraft: 1.21.1.
- Loader: NeoForge 21.1.265.
- Target instance: `username-admin-server` (server directory under the operator's CrucibleServers directory).
- Approximate installed mod JAR count at the time of inventory: 187.
- Active world folder: `world`.
- The host-control connection became unavailable; anything after the timestamps below is unverified.

## Changes confirmed on the host

### Carry On player pickup mitigation

The active instance's Carry On config was changed from `pickupPlayers = true` to `pickupPlayers = false`. This was a mitigation for a reported player-on-Aeronautics simulated-contraption hang/crash. The server restarted successfully after the change and reached `Done`.

A backup was recorded at:
`$HOME/.local/share/crucible-backups/username-admin-server-carryon-common-before-pickupPlayers-disable-20261028.toml`

Original config SHA-256 recorded in the session:
`c94586a9920cee177257b2cd12de51110ddea1030c9a3e70cd53f35d44d707d9`

### World reset and retained copies

The previous world was archived before generating the fresh world. The original directory was moved aside, not deleted.

- Pre-reset archive: `$HOME/.local/share/crucible-backups/username-admin-server-world-before-terralith-reset-20261028-0600.tar.gz`
- SHA-256: `312ddf86e24c6b58b21999b97244814540400a056a77cf419c1ffed16ac1b929`
- The session recorded mode 0600 and a successful tar integrity check.
- Retained directory name: `world.pre-terralith-reset-20261028-0600`

The factor-4 test world was archived before factor-8 regeneration:

- Archive: `$HOME/.local/share/crucible-backups/username-admin-server-world-factor4-before-spread8-20261028-0615.tar.gz`
- SHA-256: `d1385d0a5780b1aad43a2e4837cad4ee1a186437c05df0ce5c255f344df89f69`
- Size: 19,818,374 bytes; mode 0600; tar integrity check passed.
- Retained directory name: `world.pre-spread8-test-20261028-0615`

The older world and the factor-4 test should remain available for recovery and controlled comparison.

### SparseStructures tuning

Last confirmed file: `config/sparsestructures.json5`

Configuration at the last check:

- Global `spreadFactor`: 8
- `idBasedSalt`: true
- Explicit `minecraft:mansion` factor: 8
- Explicit `dungeons_arise:major_structures` factor: 12
- Explicit `ati_structures:aboveground_large` factor: 12

Config SHA-256 recorded: `0e023de70b51869abd4bcc011b1a0ea1aaec04c45f44f5204429d9482a25e27d`

Backup of the previous factor-4 config:
`$HOME/.local/share/crucible-backups/username-admin-server-sparsestructures-before-spread8-20261028.json5`

Backup SHA-256:
`92383a4de8a1f185f9e0464d9a5b0d43e614ed7c0c74a09ea8be15eb29d0de03`

The explicit overrides matter: a per-structure override can supersede the global factor. Do not assume that changing only the global value affects every structure family.

The active factor-8 world was generated after the change and reached `Done (4.982s)!` at approximately 06:13:42 local time on 2026-10-28. A subsequent `/tick query` at approximately 06:14:53 reported normal tick performance (~0.8 ms average). No player was online at the last player checks. These are historical observations; recheck before relying on current status.

### Worldgen observations

Terralith was confirmed active: locate queries from the origin returned examples including:

- `terralith:blooming_valley` at approximately `[32, 80, 0]`
- `terralith:alpine_grove` at approximately `[-2144, 144, 2304]`
- `terralith:caldera` at approximately `[-1952, 144, 1920]`

The factor-4 test had looked too dense/vanilla-like to the operator: their initial check saw two structures within roughly 18 chunks. Factor 8 was chosen for the new test world. A few queried variants were very far from the origin (abandoned temple around 5,136 blocks, mage tower around 15,240, large plains village around 45,003, and one ATi ancient-temple variant around 251,153). These are individual named-variant locate results, not evidence of total structure density or a complete distribution analysis.

**Do not run many distant locate searches as a substitute for normal playtesting.** Several locate queries took ~10–13 seconds and caused temporary `Can't keep up` warnings. Tick rate recovered afterward, but repeated exhaustive searches add noise and lag.

### Structure inventory

A `/dumpstructuresets` output was saved on the host at:
`sparsestructures/structure_sets_dump_26_10_28_06_10.txt` (408 lines).

Sets observed include `dungeons_arise:major_structures`, `ati_structures:aboveground_large`, Terralith-associated sets, CTOV, and vanilla structures.

## Historical defects and current outcomes

### 1. ATi Structures version/hash and missing template pool

Installed JAR path observed:
`mods/ATi Structures Vanilla V1.4.7.jar`

During startup, the update checker logged an internal status resembling “Current: 1.4.5, Target: 1.4.7”, despite the filename saying 1.4.7. This could be stale/mislabelled content or stale embedded metadata. **Current outcome:** the installed ATi Structures 1.4.7 JAR SHA-512 was verified against the published artifact and matches exactly. The latest test log did not reproduce the missing-pool error and no active installed-JAR reference to `ati_structures:terrain` was found. Do not replace or patch this verified release unless the fresh production world reproduces the issue.

Official Modrinth release page:
https://modrinth.com/datapack/ati-structures-vanilla-edition/version/ivErXrfo

Published SHA-512 from the public PrismNix manifest:
`4n6LESUh42huKv655i88MRrdkkFCr632C9oCYAWhZPC9tsG5MteYAv//k48muUIqKjvrm+JMjnaQ1TcQqUuVMw==`

The installed JAR was inspected and contained `terrain_large.json` and `terrain_small.json`, but no `data/ati_structures/worldgen/template_pool/terrain.json`. Older logs repeatedly reported `Couldn't find template pool reference: ati_structures:terrain`; the current isolated startup no longer reproduced that message. The registered `ati_structures:lighthouse` structure is locatable in the test world.

Observed sample coordinates included `[13592,73,-13384]`, `[-107400,78,147912]`, `[-13320,111,161352]`, `[-174616,61,72]`, and `[-174568,103,-241768]`.

A third-party modpack contains a pool file named `ati_structures:terrain` but uses custom processor identifiers from that modpack. **Do not copy it as-is**; those processor references are not guaranteed to exist on this server. Before creating a datapack overlay:
1. Verify the actual installed JAR's SHA-512 against the official artifact.
2. Inspect the installed JAR's full relevant template-pool and structure-template contents.
3. Prefer replacing a verified stale JAR with the official artifact, preserving the old JAR and recording hashes.
4. If the official JAR itself lacks the pool, report/confirm upstream behavior first. Any fallback pool/datapack patch must use resources and processor IDs proven to exist and must be tested on a disposable copy before rollout.

### 2. First-start BCLib level.dat message

The older first-generation logs contained `Failed fixing Level-Data in level.dat (./world/./level.dat)`. It did not recur in the later isolated startup tests, including the final test using the patched Sable JAR and final datapack. Do not remove mods or reset a world because of the historical first-create message alone.

### 3. Recipe Integration bad conditional identifiers — repaired in staged/deployed pack

Installed Recipe Integration version: 0.4.0. Its resources contained `neoforge:mod_laoded` (misspelling of `mod_loaded`) in 108 recipes. The final world datapack fixes that token in place (216 replacements), removes unregistered optional condition codecs from optional recipes while retaining standard mod-loaded guards, and adds the absent Stellaris guard. The local prepared JAR hotfix also removes a single invalid filename containing a space, which Minecraft previously ignored anyway.

Example upstream repository:
https://github.com/Mogwump/recipe-integration-mod

A sample current-main file uses `neoforge:mod_laoded` for Farm & Charm / TFMG condition checks. The final isolated startup confirms zero recipe parse errors. The upstream source remains worth reporting; do not broad-replace unrelated mods or remove Recipe Integration.

### 4. Mekanism Elements painting recipe mismatch

Runtime reported invalid fields for:
`mekanismelements:painting/powder_high_quality_concrete_aqua`

The upstream NeoForge 1.21.1 resource at the time of research had the valid fields `chemical_input`, `item_input`, `output`, and `per_tick_usage`. This points to the installed resource being stale or overridden, but its source was not yet established.

Upstream reference:
https://github.com/fixdol/Mekanism-Elements/blob/Neo/1.21.1/src/generated/resources/data/mekanismelements/recipe/painting/powder_high_quality_concrete_aqua.json

Current outcome: the final datapack repairs the two painting schemas and methane oxidizing schema based on the installed Mekanism serializer; four resources that require absent registry IDs/serializers are explicitly disabled with `neoforge:false` rather than inventing unregistered IDs. This was validated in the isolated startup with zero recipe parse errors. An upstream fix is still needed to restore those disabled recipes.

### 5. Empty/bad recipe JSON that differs from upstream

Other observed runtime errors included:

- `tfmg:crafting/materials/steel_axe`: recipe parse says required key `type` is missing from an empty object.
- Mekanism `control_circuit/infused_advanced` and `infused_elite`: required `type` absent from an empty object.

The upstream TFMG steel axe recipe and the upstream Mekanism infusing recipes appeared valid when checked. This suggests a stale/malformed installed artifact or a competing resource/override; it is not enough to assume upstream source is what the runtime loaded.

Upstream references:
- https://github.com/DrMango14/Create-The_Factory_Must_Grow/blob/1.21.1/src/generated/resources/data/tfmg/recipe/crafting/materials/steel_axe.json
- Mekanism upstream repository: https://github.com/mekanism/Mekanism

Current outcome: a complete installed-JAR scan located the empty placeholders in `mekanism_tfmg_compat-0.4-1.21.1.jar`; 14 valid duplicates are restored by the datapack from the base Create/Mekanism/TFMG resources. The final isolated test has zero recipe parse errors.

## Other log noise to triage after the blockers

- A factor-4 test log snapshot contained 439 ERROR/FATAL lines; ~412 were repeated Railways optional-integration loot-table errors for items from absent BYG, TFC, Biomes O' Plenty, Twilight Forest, and other mods. These appear to be optional compatibility data errors and were nonfatal in the tested startup. Do not install a large list of extra mods just to silence optional loot-table errors.
- **Resolved in local hotfix:** `sablephysicscompat-1.3.0.jar` had an invalid trailing comma in `sable:bouncy` and an array where the loader expects one physics-property object per resource. A two-entry local JAR repair preserves the bouncy tag values and the supported Quark Gravisand float property; no Sable parser/property errors remain in the final test.
- **Resolved in local hotfix:** the invalid whitespace-containing Recipe Integration path `immersiveengineering/sawmill/aoa3/haunted_purpling_log copy.json` was removed from the patched JAR; no invalid-path error remains.
- HammerLib registers `org.zeith.hammerlib.core.test.HammerLibRecipeExtension` as an attachment and emits an error.
- Moonlight warns that Forgified Fabric API/Connector on NeoForge can affect compatibility.
- Five dedicated-server/client-only class reference errors mention `RenderSystem` and `net.minecraft.client.main.Main`; map each error to the responsible mod/mixin before acting.
- Quark warns that `stoneling` and `toretoise` (CREATURE category) are added under MONSTER categories for many Terralith biomes. Startup continues, but this may alter spawn behavior.
- ModernFix reported ~28.9 seconds to load during the fresh factor-8 generation; server did reach ready.
- C2ME reported some optimizations disabled due to `ChunkDataEvent.Save` listeners from Mekanism/Architectury.

These findings need prioritization based on user-visible impact, not a goal of getting a perfectly silent log at any cost.

## Exact next actions after deployment

1. Wait for the operator's chosen launch time; do not start production automatically.
2. On the user's Crucible Start click, verify the registered JVM arguments are `-Xms8G -Xmx16G` and wait for `Done`.
3. Confirm `file/fresh-world-launch-fixes (world)` is enabled, the new `world_fresh_20261028/level.dat`/region files are created, and the retained `world/level.dat` hash stays `091d4e46ff6980c931887ba66fd959712e57e211c83d6c929d0c7d086bf29482`.
4. Confirm recipe/loot parser errors remain zero; then playtest a small selection of worldgen structures and common modded recipe/machine workflows.
5. Triage the seven remaining nonfatal ERROR-level startup messages only after the first live-world smoke test, without removing Connector or client-compatible mods blindly.
6. Continue recording exact log results and hashes in the fresh-world preflight document.

## Supervisor incident

The NobaraSupervisor MCP client disconnected and subsequent read-only and execution tools all returned `UNAVAILABLE: McpServerError: Connection failed`. No host-side changes were made after that failure. Related private incident tracking: https://github.com/Roaring1/NobaraSupervisor/issues/11

Do not conflate:
- the incorrect port-443 root route to port 8787 found in the Supervisor repo's earlier read-only investigation;
- the working-at-the-time `:10000` route to port 8790;
- current ChatGPT/plugin creation's generic “A service needed to create the app is temporarily unavailable. Try again shortly.” message;
- the later MCP connector-wide `UNAVAILABLE` errors.

They may be related, but no evidence yet establishes a single root cause. Restore reliable host-side access before continuing any server mutations.