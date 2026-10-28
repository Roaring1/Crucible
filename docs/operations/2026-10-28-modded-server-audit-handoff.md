# Modded 1.21.1 server audit — handoff (2026-10-28)

## Status and confidence

This is a checkpoint for continuing the server audit when the NobaraSupervisor MCP connection is healthy again. It records the last confirmed state, not a claim about the host's current state after the connection failure. No host-side changes were made after Supervisor tools began returning \`UNAVAILABLE: McpServerError: Connection failed\`.

**Do not wipe/reset the world again as a first response.** A new factor-8 world was already generated and reached the Minecraft ready marker. Preserve it for user testing; the older worlds and archives are retained for rollback/comparison.

## Environment

- Crucible version observed: 0.8.0.
- Minecraft: 1.21.1.
- Loader: NeoForge 21.1.265.
- Target instance: \`username-admin-server\` (server directory under the operator's CrucibleServers directory).
- Approximate installed mod JAR count at the time of inventory: 187.
- Active world folder: \`world\`.
- The host-control connection became unavailable; anything after the timestamps below is unverified.

## Changes confirmed on the host

### Carry On player pickup mitigation

The active instance's Carry On config was changed from \`pickupPlayers = true\` to \`pickupPlayers = false\`. This was a mitigation for a reported player-on-Aeronautics simulated-contraption hang/crash. The server restarted successfully after the change and reached \`Done\`.

A backup was recorded at:
\`$HOME/.local/share/crucible-backups/username-admin-server-carryon-common-before-pickupPlayers-disable-20261028.toml\`

Original config SHA-256 recorded in the session:
\`c94586a9920cee177257b2cd12de51110ddea1030c9a3e70cd53f35d44d707d9\`

### World reset and retained copies

The previous world was archived before generating the fresh world. The original directory was moved aside, not deleted.

- Pre-reset archive: \`$HOME/.local/share/crucible-backups/username-admin-server-world-before-terralith-reset-20261028-0600.tar.gz\`
- SHA-256: \`312ddf86e24c6b58b21999b97244814540400a056a77cf419c1ffed16ac1b929\`
- The session recorded mode 0600 and a successful tar integrity check.
- Retained directory name: \`world.pre-terralith-reset-20261028-0600\`

The factor-4 test world was archived before factor-8 regeneration:

- Archive: \`$HOME/.local/share/crucible-backups/username-admin-server-world-factor4-before-spread8-20261028-0615.tar.gz\`
- SHA-256: \`d1385d0a5780b1aad43a2e4837cad4ee1a186437c05df0ce5c255f344df89f69\`
- Size: 19,818,374 bytes; mode 0600; tar integrity check passed.
- Retained directory name: \`world.pre-spread8-test-20261028-0615\`

The older world and the factor-4 test should remain available for recovery and controlled comparison.

### SparseStructures tuning

Last confirmed file: \`config/sparsestructures.json5\`

Configuration at the last check:

- Global \`spreadFactor\`: 8
- \`idBasedSalt\`: true
- Explicit \`minecraft:mansion\` factor: 8
- Explicit \`dungeons_arise:major_structures\` factor: 12
- Explicit \`ati_structures:aboveground_large\` factor: 12

Config SHA-256 recorded: \`0e023de70b51869abd4bcc011b1a0ea1aaec04c45f44f5204429d9482a25e27d\`

Backup of the previous factor-4 config:
\`$HOME/.local/share/crucible-backups/username-admin-server-sparsestructures-before-spread8-20261028.json5\`

Backup SHA-256:
\`92383a4de8a1f185f9e0464d9a5b0d43e614ed7c0c74a09ea8be15eb29d0de03\`

The explicit overrides matter: a per-structure override can supersede the global factor. Do not assume that changing only the global value affects every structure family.

The active factor-8 world was generated after the change and reached \`Done (4.982s)!\` at approximately 06:13:42 local time on 2026-10-28. A subsequent \`/tick query\` at approximately 06:14:53 reported normal tick performance (~0.8 ms average). No player was online at the last player checks. These are historical observations; recheck before relying on current status.

### Worldgen observations

Terralith was confirmed active: locate queries from the origin returned examples including:

- \`terralith:blooming_valley\` at approximately \`[32, 80, 0]\`
- \`terralith:alpine_grove\` at approximately \`[-2144, 144, 2304]\`
- \`terralith:caldera\` at approximately \`[-1952, 144, 1920]\`

The factor-4 test had looked too dense/vanilla-like to the operator: their initial check saw two structures within roughly 18 chunks. Factor 8 was chosen for the new test world. A few queried variants were very far from the origin (abandoned temple around 5,136 blocks, mage tower around 15,240, large plains village around 45,003, and one ATi ancient-temple variant around 251,153). These are individual named-variant locate results, not evidence of total structure density or a complete distribution analysis.

**Do not run many distant locate searches as a substitute for normal playtesting.** Several locate queries took ~10–13 seconds and caused temporary \`Can't keep up\` warnings. Tick rate recovered afterward, but repeated exhaustive searches add noise and lag.

### Structure inventory

A \`/dumpstructuresets\` output was saved on the host at:
\`sparsestructures/structure_sets_dump_26_10_28_06_10.txt\` (408 lines).

Sets observed include \`dungeons_arise:major_structures\`, \`ati_structures:aboveground_large\`, Terralith-associated sets, CTOV, and vanilla structures.

## High-priority unresolved defects

### 1. ATi Structures version/hash and missing template pool

Installed JAR path observed:
\`mods/ATi Structures Vanilla V1.4.7.jar\`

During startup, the update checker logged an internal status resembling “Current: 1.4.5, Target: 1.4.7”, despite the filename saying 1.4.7. This could be stale/mislabelled content or stale embedded metadata. **The installed JAR hash was not verified before Supervisor disconnected. Do not replace it based only on the filename or updater line.**

Official Modrinth release page:
https://modrinth.com/datapack/ati-structures-vanilla-edition/version/ivErXrfo

Published SHA-512 from the public PrismNix manifest:
\`4n6LESUh42huKv655i88MRrdkkFCr632C9oCYAWhZPC9tsG5MteYAv//k48muUIqKjvrm+JMjnaQ1TcQqUuVMw==\`

The installed JAR was inspected and contained \`terrain_large.json\` and \`terrain_small.json\`, but no \`data/ati_structures/worldgen/template_pool/terrain.json\`. During structure lookup/generation, the log repeatedly reported:
\`Couldn't find template pool reference: ati_structures:terrain\`

Observed sample coordinates included \`[13592,73,-13384]\`, \`[-107400,78,147912]\`, \`[-13320,111,161352]\`, \`[-174616,61,72]\`, and \`[-174568,103,-241768]\`.

A third-party modpack contains a pool file named \`ati_structures:terrain\` but uses custom processor identifiers from that modpack. **Do not copy it as-is**; those processor references are not guaranteed to exist on this server. Before creating a datapack overlay:
1. Verify the actual installed JAR's SHA-512 against the official artifact.
2. Inspect the installed JAR's full relevant template-pool and structure-template contents.
3. Prefer replacing a verified stale JAR with the official artifact, preserving the old JAR and recording hashes.
4. If the official JAR itself lacks the pool, report/confirm upstream behavior first. Any fallback pool/datapack patch must use resources and processor IDs proven to exist and must be tested on a disposable copy before rollout.

### 2. First-start BCLib level.dat message

Fresh-world first generation logged:
\`Failed fixing Level-Data in level.dat (./world/./level.dat)\`

On the factor-4 world, the message did not recur after a second restart and the server reached ready normally. The factor-8 world received the same first-generation message, but a second-start verification had not happened before Supervisor disconnected. Once host access returns, perform a controlled second restart and inspect the log for repeat errors and the presence/integrity of \`world/level.dat\`. Do not remove mods or reset the world just because of this first-create message alone.

### 3. Recipe Integration bad conditional identifiers

Installed Recipe Integration version: 0.4.0. Repeated load errors report unknown condition codec \`neoforge:mod_laoded\` (misspelling of \`mod_loaded\`) and missing \`farm_and_charm:mincer\` recipe serializer. The upstream repository contains many affected Farm & Charm mincer recipes.

Example upstream repository:
https://github.com/Mogwump/recipe-integration-mod

A sample current-main file uses \`neoforge:mod_laoded\` for Farm & Charm / TFMG condition checks. A likely repair is to correct only the affected recipe resources through a well-defined override or upstream fix, preserving the intended optional-mod checks. Do not perform a broad text replacement across unrelated mods or delete Recipe Integration without checking feature impact. Test recipe parsing and optional-mod behavior after the change.

### 4. Mekanism Elements painting recipe mismatch

Runtime reported invalid fields for:
\`mekanismelements:painting/powder_high_quality_concrete_aqua\`

The upstream NeoForge 1.21.1 resource at the time of research had the valid fields \`chemical_input\`, \`item_input\`, \`output\`, and \`per_tick_usage\`. This points to the installed resource being stale or overridden, but its source was not yet established.

Upstream reference:
https://github.com/fixdol/Mekanism-Elements/blob/Neo/1.21.1/src/generated/resources/data/mekanismelements/recipe/painting/powder_high_quality_concrete_aqua.json

Before patching, inspect the actual JAR resource and every loaded datapack/KubeJS override for the same recipe ID.

### 5. Empty/bad recipe JSON that differs from upstream

Other observed runtime errors included:

- \`tfmg:crafting/materials/steel_axe\`: recipe parse says required key \`type\` is missing from an empty object.
- Mekanism \`control_circuit/infused_advanced\` and \`infused_elite\`: required \`type\` absent from an empty object.

The upstream TFMG steel axe recipe and the upstream Mekanism infusing recipes appeared valid when checked. This suggests a stale/malformed installed artifact or a competing resource/override; it is not enough to assume upstream source is what the runtime loaded.

Upstream references:
- https://github.com/DrMango14/Create-The_Factory_Must_Grow/blob/1.21.1/src/generated/resources/data/tfmg/recipe/crafting/materials/steel_axe.json
- Mekanism upstream repository: https://github.com/mekanism/Mekanism

First determine the actual runtime source (JAR, built-in data, datapack, or KubeJS), then fix the smallest verified source and test reload/startup.

## Other log noise to triage after the blockers

- A factor-4 test log snapshot contained 439 ERROR/FATAL lines; ~412 were repeated Railways optional-integration loot-table errors for items from absent BYG, TFC, Biomes O' Plenty, Twilight Forest, and other mods. These appear to be optional compatibility data errors and were nonfatal in the tested startup. Do not install a large list of extra mods just to silence optional loot-table errors.
- \`sablephysicscompat-1.3.0.jar\` tag parse error involving \`sable:bouncy\`.
- Invalid Recipe Integration path: \`immersiveengineering/sawmill/aoa3/haunted_purpling_log copy.json\`.
- HammerLib registers \`org.zeith.hammerlib.core.test.HammerLibRecipeExtension\` as an attachment and emits an error.
- Moonlight warns that Forgified Fabric API/Connector on NeoForge can affect compatibility.
- Five dedicated-server/client-only class reference errors mention \`RenderSystem\` and \`net.minecraft.client.main.Main\`; map each error to the responsible mod/mixin before acting.
- Quark warns that \`stoneling\` and \`toretoise\` (CREATURE category) are added under MONSTER categories for many Terralith biomes. Startup continues, but this may alter spawn behavior.
- ModernFix reported ~28.9 seconds to load during the fresh factor-8 generation; server did reach ready.
- C2ME reported some optimizations disabled due to \`ChunkDataEvent.Save\` listeners from Mekanism/Architectury.

These findings need prioritization based on user-visible impact, not a goal of getting a perfectly silent log at any cost.

## Exact next actions after Supervisor reconnects

1. Start with read-only status checks: session state, player list, \`latest.log\` tail, active config, world folder/level.dat, running process, and disk/backup availability. Confirm whether the current factor-8 server is still online before restarting.
2. Preserve the active factor-8 world and both older copies. Do not wipe again without an actual world corruption diagnosis or explicit user request.
3. Verify the installed ATi JAR SHA-512 against the official artifact. If mismatched, stop server, backup/hash the old JAR, replace only that JAR, run appropriate validation, and restart. If correct, investigate the missing pool carefully rather than applying a third-party pack's processor IDs.
4. Restart once under controlled conditions to determine whether the BCLib message is first-creation-only.
5. Find the exact loaded source of the Recipe Integration, Mekanism Elements, TFMG, and Mekanism recipe JSON errors. Compare JAR resources with any override.
6. Patch the narrowest defective resource, retain backup/diff/hash, and test both server startup and recipes. Avoid unnecessary client-pack downloads if the fix is server-side.
7. Have the operator explore the factor-8 terrain and report whether the density/variety feels right. Only then consider another density change.
8. After changes, update this handoff with exact command results, hashes, backup paths relative to the instance, startup marker, and any remaining warnings.

## Supervisor incident

The NobaraSupervisor MCP client disconnected and subsequent read-only and execution tools all returned \`UNAVAILABLE: McpServerError: Connection failed\`. No host-side changes were made after that failure. Related private incident tracking: https://github.com/Roaring1/NobaraSupervisor/issues/11

Do not conflate:
- the incorrect port-443 root route to port 8787 found in the Supervisor repo's earlier read-only investigation;
- the working-at-the-time \`:10000\` route to port 8790;
- current ChatGPT/plugin creation's generic “A service needed to create the app is temporarily unavailable. Try again shortly.” message;
- the later MCP connector-wide \`UNAVAILABLE\` errors.

They may be related, but no evidence yet establishes a single root cause. Restore reliable host-side access before continuing any server mutations.