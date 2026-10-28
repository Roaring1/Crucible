# Fresh-world preflight — 1.21.1 modded server

**Updated:** 2026-10-28, after the NobaraSupervisor MCP connection recovered  
**Status:** Server intentionally left stopped. Fresh-world startup plan and a data-pack repair candidate are staged; neither has been activated or runtime-tested.  
**Target instance:** \`username-admin-server\`  
**Host path:** \`/home/roaring/CrucibleServers/username-admin-server\`

## Executive decision

The pack appears to be intact enough to attempt a controlled fresh-world startup without removing the generation stack. The live instance has 187 mod JARs; the original Prism client has 234, and all 186 JARs present in both locations are byte-for-byte identical. The 47 client-only JARs are predominantly UI, animation, rendering, and client performance mods. The only server-only JAR is SparseStructures, which is required for the deliberately sparse structure placement.

Do **not** restart into the existing world folder. At the eventual launch point, switch the configured level name to the unique target \`world_fresh_20261028\`. This preserves \`world\` and both dated prior-world copies without deleting or renaming them. Do not perform that setting change or start the server until the launch window is explicitly chosen.

## Current server state

- Minecraft 1.21.1, NeoForge 21.1.265.
- Last observed startup reached \`Done (3.209s)!\` at 2026-10-28 14:50:29 -07:00.
- Log shows \`Stopping the server\` at 14:54:05. This is consistent with a controlled/manual stop and is not evidence of a crash by itself.
- No Minecraft Java server process was found during this read-only session. NobaraSupervisor itself is connected and reports service version 1.5.0, OAuth enabled, session user \`roaring\` UID 1000.
- Current level name is \`world\`; seed is blank/random; structures and the Nether are enabled. Level type is \`wover:normal\`; the startup log echoed that preset. Keep this world type unless a later controlled test shows a problem.
- Current settings include hard difficulty, survival, 16 view distance and 10 simulation distance.
- The standard \`start.sh\` uses a 2G/4G heap fallback unless overridden. Crucible's registered launch configuration records \`-Xms8G -Xmx16G\`; use the registered Crucible launch path at startup and verify the actual Java command rather than accidentally launching with the fallback heap.

## World-generation and mod parity audit

### Present on the dedicated server

The inspected mod folder includes the intended world-generation and structure providers, including:

- Terralith 2.6.2.
- CTOV 3.6.3.
- ATi Structures Vanilla Edition 1.4.7.
- Dungeons Arise 2.1.68 and Dungeons Arise Seven Seas 1.0.4.
- YUNG's API, Better Desert Temples, Better Dungeons, Better End Island, Better Nether Fortresses, Better Ocean Monuments, Better Strongholds, and YUNG's Extras.
- Even Better Nether, Deeper and Darker, Ecologics, WorldWeaver 21.0.30, BCLib 21.0.28, and SparseStructures 3.0.

The installed WorldWeaver JAR registers mod ID \`wover\`; the latest startup log contains \`preset = wover:normal\`. Terralith was confirmed active in previous gameplay checks, with \`terralith:blooming_valley\`, \`terralith:alpine_grove\`, and \`terralith:caldera\` found by locate queries. The server and client copies of \`config/terralith.json\`, \`config/wover/main.json\`, \`config/wover/cached.json\`, \`config/bclib/main.json\`, and \`defaultconfigs/biolith/general.json\` match.

SparseStructures remains intentionally configured with global \`spreadFactor: 8\`, \`idBasedSalt: true\`, Mansion factor 8, Dungeons Arise major structures factor 12, and ATi aboveground-large factor 12. Preserve these values for the first fresh-world test; the operator chose factor 8 after factor 4 looked too dense. Do not tune structure frequency again until the operator has explored the new terrain.

### Keep the generated cache server-specific

The client and server both have Supplementaries' generated data-pack cache, but the hashes/content differ (client hash begins \`e0fff7fe...\`, server hash begins \`adeb37ce...\`; 39 vs 57 files). The server's pack contains additional generated recipe resources and \`remove\` tag fields. **Do not copy the client cache over the server cache.** Keep the server-generated cache paired with the server's mod/config set and inspect the fresh-start log if this pack fails to load.

## Recovery points

Existing world locations remain intact:

- Active/preexisting folder: \`world\` (~27 MB).
- Retained pre-spread-8 test: \`world.pre-spread8-test-20261028-0615\` (~31 MB).
- Retained pre-Terralith-reset world: \`world.pre-terralith-reset-20261028-0600\` (~35 MB).

Additional archives include:

- \`/home/roaring/.local/share/crucible-backups/username-admin-server-world-before-terralith-reset-20261028-0600.tar.gz\` — SHA-256 \`312ddf86e24c6b58b21999b97244814540400a056a77cf419c1ffed16ac1b929\`.
- \`/home/roaring/.local/share/crucible-backups/username-admin-server-world-factor4-before-spread8-20261028-0615.tar.gz\` — SHA-256 \`d1385d0a5780b1aad43a2e4837cad4ee1a186437c05df0ce5c255f344df89f69\`.
- **New before-fresh-world archive:** \`/home/roaring/.local/share/crucible-backups/username-admin-server-world-factor8-before-fresh-20261028-161756.tar.gz\` — SHA-256 \`f9191b54c77e0a87438df9cbc65bb9e819546ff5f074aada6af1cdf46dc3653c\`. Mode 0600; tar integrity test passed; the source world was not modified during archive creation.

The server filesystem had about 551 GB available when checked.

## Staged recipe repair candidate

A data pack has been prepared **outside all world datapack directories**. Nothing in it is automatically loaded now.

- Directory: \`prepared-datapacks/fresh-world-launch-fixes-20261028/\`
- ZIP: \`prepared-datapacks/fresh-world-launch-fixes-20261028.zip\`
- ZIP SHA-256: \`48932679d664881526e24804dc6f3a6f2167ba4d8aa15103c3c7b302da25ece5\`
- Data pack format: 48 (Minecraft 1.21.1).
- Integrity: ZIP integrity passed; all 122 staged recipe JSON files parse.
- Status: **staged only, not active, not runtime-tested**. See the pack's README and manifest for per-resource paths and source hashes.

### Fix A: 14 empty compatibility resources shadowing valid recipes

A read-only scan of all 187 installed JARs found 14 resource paths where \`mekanism_tfmg_compat-0.4-1.21.1.jar\` supplies an empty JSON object \`{}\`, while another installed JAR supplies a valid recipe at exactly the same path. This directly explains errors such as missing recipe key \`type\` for TFMG steel tools, Mekanism infused control circuits, Mekanism lead/steel conversions, and Create's Mekanism lead recipes.

The pack overrides those exact paths with the valid payloads from the corresponding base Create, Mekanism, or TFMG JAR. It does not remove the compatibility mod or modify any JAR.

Compatibility JAR SHA-256: \`352302fa9a80628ae70b3292e7788273ea86ab98ae304c9079c1b4b608bc3f5e\`.

### Fix B: Recipe Integration conditional codec typo

The installed \`recipe_integration-0.4.0.jar\` contains 216 occurrences of the invalid type \`neoforge:mod_laoded\` across 108 recipe JSON files. The latest startup log reported 108 affected recipe errors with this exact unknown condition codec. The staged pack copies those 108 resource files into the same namespace/path and changes only that token to \`neoforge:mod_loaded\`, preserving the other recipe content and optional-mod checks.

Recipe Integration JAR SHA-256: \`4e41f8dcf06ba21e0e494ccee30ece2c3c8e7094cf863d4fb3a50495ef740e35\`.

**Do not call these fixes proven until a real server startup confirms that the pack wins the resource merge and the corresponding parser errors disappear.** The artifact is a narrow repair candidate, not an upstream binary replacement.

## Other currently observed data issues

The last \`latest.log\` contains 439 ERROR lines: 155 recipe parse errors and 274 loot-table parse errors. Most loot-table errors involve Railways optional-integration files referencing content from mods that are not installed. These were nonfatal in the last successful startup; do not add a large set of optional mods only to silence log noise.

The staged pack targets 122 of the 155 distinct recipe failures. The remaining failures need their own treatment, including:

- Optional Recipe Integration recipes that use an unknown \`clayworks:config\` condition codec.
- Create Factory recipes referencing the absent fluid \`create_confectionery:caramel\`.
- Fisherman's Haven integration recipes referencing absent Aquaculture items.
- A Create Extended Wrenches recipe with an unknown serializer.
- A Recipe Integration Stellaris milling recipe with invalid ingredient/fluid fields and a path with a space (\`haunted_purpling_log copy.json\`) that Minecraft ignores.
- SablePhysicsCompat tag data that does not parse cleanly.

### Verify the exact Mekanism Elements project before patching/upgrading

The installed file is \`mekanismelements-3.0.18-neoforge-1.21.1.jar\`, mod ID \`mekanismelements\`, author \`Infernos\`; SHA-256 \`0ed59d328f584880289cf835e57ec4cd9f9e28ee63d31cd67013a7ae635b5f56\`. It has several recipe parse failures (missing \`per_tick_usage\`, an unknown \`mekanismelements:zirconium\` chemical, references to nonexistent dye items \`minecraft:aqua_dye\`/\`minecraft:dark_red_dye\`, and an invalid oxidizing input schema).

There are similarly named but separate Mekanism Elements projects. The \`fixdol/Mekanism-Elements\` repository is **not** the source matching this installed JAR and must not be used as a drop-in recipe source. The publisher page for the installed author's project lists a 1.21.1 release 3.0.20, but its published changelog describes Adsorption Separator changes and does not establish that these recipe errors are fixed. No upgrade or Mekanism Elements override has been staged yet.

Publisher page for the installed project: https://www.curseforge.com/minecraft/mc-mods/mekanism-elements-neoforge/files/8614281

## ATi Structures reassessment

The installed ATi JAR SHA-512 is:

\`e27e8b112521e3686e2afeb9e62f3c311add924142afadf60bda026005a164f0bdb6c1b932d79802ffff938f26b9422a2a3beb9be24c8e7690d53710a94b9533\`

This exactly matches the published SHA-512 from the public PrismNix manifest:

\`4n6LESUh42huKv655i88MRrdkkFCr632C9oCYAWhZPC9tsG5MteYAv//k48muUIqKjvrm+JMjnaQ1TcQqUuVMw==\`

The installed JAR contains \`terrain_large.json\` and \`terrain_small.json\`, but not \`data/ati_structures/worldgen/template_pool/terrain.json\`. A search across current installed JAR JSON resources found no current external reference to \`ati_structures:terrain\` beyond \`terrain_large\`/\`terrain_small\` identifiers in those pools, and the latest \`latest.log\` did not reproduce the earlier reported missing-pool error. Do **not** patch or replace this verified release on current evidence; test ATi structures in the fresh world and recheck logs first.

Official release page: https://modrinth.com/datapack/ati-structures-vanilla-edition/version/ivErXrfo

## Fresh-world launch and smoke-test sequence

Do this only at the later startup window. This preflight does not start the server or modify the current server properties.

1. Recheck Supervisor status, process list, disk space, and hashes of the existing world/config/backup. Confirm there is still no Minecraft process.
2. Keep \`world\`, both dated world folders, and all archives. Set \`level-name=world_fresh_20261028\` only for the fresh-world launch; do not delete or rename \`world\`. Leave the blank seed so the fresh world is random.
3. Decide whether to activate the staged repair pack. If approved, copy it under the new world's \`datapacks/fresh-world-launch-fixes\` before the first world-generation start; do not copy it into the existing \`world\`.
4. Launch through Crucible's registered instance command, then verify the actual Java heap arguments (expected 8G min/16G max for this instance). Avoid accidentally using the bare \`start.sh\` fallback heap.
5. Wait for the \`Done\` marker. Check that the server loaded the intended \`wover:normal\` preset, structures are enabled, Terralith and the worldgen mod stack load, and the optional staged datapack is listed if activated.
6. Review fresh startup errors and compare counts for the 14 restored recipe IDs and 108 corrected Recipe Integration IDs. Confirm the known recipe parse failures are gone before players join. Don't treat unrelated optional loot-table messages as a world-generation failure.
7. Verify fresh-world generation with a small, targeted set of checks: \`/datapack list enabled\`, a Terralith biome locate such as \`/locate biome terralith:blooming_valley\`, and a representative ATi structure such as \`/locate structure ati_structures:lighthouse\`. Avoid many far-distance locate searches; previous exhaustive searches caused temporary \`Can't keep up\` warnings.
8. Confirm a new \`world_fresh_20261028/level.dat\` and region data exist; verify the old \`world/level.dat\` hash remains \`091d4e46ff6980c931887ba66fd959712e57e211c83d6c929d0c7d086bf29482\`.
9. Only after the startup checks pass should the operator join and playtest biome variety, structure density, loot, and the most-used machine/recipe paths.

## Evidence links

- Current Crucible audit issue: https://github.com/Roaring1/Crucible/issues/32
- Recipe Integration source project: https://github.com/Mogwump/recipe-integration-mod
- ATi Structures official release: https://modrinth.com/datapack/ati-structures-vanilla-edition/version/ivErXrfo
- Installed Mekanism Elements publisher page: https://www.curseforge.com/minecraft/mc-mods/mekanism-elements-neoforge/files/8614281
- Supervisor connectivity incident: https://github.com/Roaring1/NobaraSupervisor/issues/11

## Change safety statement

During this resumed session, no mods, server properties, world files, or active configuration files were changed. The only host-side writes were (1) a new verified rollback archive of the existing world and (2) the inactive prepared datapack and its ZIP/manifest/README under \`prepared-datapacks\`. The Minecraft server remains stopped.
