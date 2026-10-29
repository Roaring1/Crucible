# Fresh-world preflight — 1.21.1 modded server

**Updated:** 2026-10-28, after deployment and full Crucible-manager startup test  
**Status:** Production is intentionally stopped and ready for the next Crucible **Start** click. The tested repair datapack and two JAR hotfixes are deployed; the next launch is configured to create `world_fresh_20261028`. Existing worlds remain preserved.  
**Target instance:** `username-admin-server`  
**Host path:** `/home/roaring/CrucibleServers/username-admin-server`

## Latest deployment update

The definitive isolated test now uses the exact deployed server JAR set and the repair datapack. It started from a brand-new level folder with no `level.dat` through Crucible's actual `TmuxManager.start()` path using `-Xms8G -Xmx16G`, reached `Done (3.887s)!`, and created `level.dat` plus four region files. It logged WorldWeaver's `wover:normal` preset with 95 Terralith biomes and automatically enabled `file/fresh-world-launch-fixes (world)` among 20 data packs. A second manager restart reached `Done (3.561s)!`.

**Final tested startup outcome:** zero ERROR-level log lines; zero recipe parse errors; zero loot-table parse errors; zero BCLib level-data errors; zero Sable malformed-resource errors; zero invalid Recipe Integration path errors; zero dedicated-server client-class probes. Moonlight's entire Fabric API/Connector compatibility advisory is still printed, now at WARN rather than ERROR. No message was hidden.

## Deployed server/client repairs

- Repair datapack: `world_fresh_20261028/datapacks/fresh-world-launch-fixes/`; ZIP SHA-256 `e76ddbb936b502eb664388da8bd75372580e451d029c37e49f3c702f61e75e57`; 429 JSON data resources; archive/JSON integrity checks pass.
- `bclib-21.0.28.jar`: SHA-256 `abf6e81ecaa1aafe945bd351e49fa9627c4ba351aeeba81ec50393be99fff80b`. A small guard returns from `DataFixerAPI.fixLevel` only when `worldDir/level.dat` is missing, avoiding a false first-generation error while preserving normal migration for an existing level. This was tested on a truly new world and a subsequent restart; BCLib errors were zero both times.
- `moonlight-1.21.1-3.7.2-neoforge.jar`: SHA-256 `7c7e8ff34976c2a47850cf6090d4c643d7aa38a3e49a11e415fdcbad65aa9e80`. Only its explicit Fabric API/Connector advisory call changed from ERROR to WARN; the full warning remains visible.
- `sablephysicscompat-1.3.0.jar`: SHA-256 `a01586a6e74405b6690202fdc9b072028b057c42442aa9d82bdc7bb4df5c0578`.
- `recipe_integration-0.4.0.jar`: SHA-256 `f6cfe76956df1af5bacfc10fadebbaba9f85e88c380e3fc97b11348d6cded8b5`.
- `struts-1.3.1.jar`: SHA-256 `1a85201a919bd69c9036849a3175432ab0b1054717a51e661382139202e770db`.
- `HammerLib-1.21-21.0.17.jar`: SHA-256 `c4b48f2f6a13110e1804a8b05c3c5f2858d2646410931673b0bb1ab47f384563`; only the log severity for the successful recipe-extension attachment was lowered to INFO. The extension remains registered and HammerLib still injects 43 recipes.
- `ConnectorExtras-1.12.1+1.21.1.jar`: server-only SHA-256 `5427041ec1dda48994ab9fdc5ec20f76819e825111d4e15888919be7c2c62a83`. The EMI Bridge entrypoint mixin is server-only and targets `net.minecraft.server.Main`; the Prism client retains its original ConnectorExtras JAR so its client entrypoint still works.

All six matching server/client JARs match by hash; ConnectorExtras is intentionally the sole server/client hash difference.

## Production status and rollback

Production is **stopped**. Crucible's authoritative `ServerInstance.readiness()` passes all seven checks: folder, Java, EULA, launcher, mods, properties, and settings. Its registered Start path points to `/home/roaring/CrucibleServers/username-admin-server/start.sh`, passes `-Xms8G -Xmx16G`, and reports world root `/home/roaring/CrucibleServers/username-admin-server/world_fresh_20261028`. Port 25566 has no listener.

Production `server.properties` selects `level-name=world_fresh_20261028`, leaves the seed blank/random, retains `level-type=wover:normal` and `generate-structures=true`. The target folder currently has the data pack but no `level.dat` yet. The next production Start creates the world. The old `world` and both dated prior worlds remain untouched; the old `world/level.dat` SHA-256 is still `091d4e46ff6980c931887ba66fd959712e57e211c83d6c929d0c7d086bf29482`.

Verified original JAR/settings archives are stored in:
- `/home/roaring/.local/share/crucible-backups/username-admin-server-fresh-world-deployment-20261028-171846/`
- `/home/roaring/.local/share/crucible-backups/username-admin-server-compatibility-round2-20261028-172940/`
- `/home/roaring/.local/share/crucible-backups/username-admin-server-hammerlib-log-fix-20261028-173507/`
- `/home/roaring/.local/share/crucible-backups/username-admin-server-bclib-firstgen-fix-20261028-174136/`
- `/home/roaring/.local/share/crucible-backups/username-admin-server-moonlight-warning-level-20261028-174408/`
- Full existing-world archive: `/home/roaring/.local/share/crucible-backups/username-admin-server-world-factor8-before-fresh-20261028-161756.tar.gz`.

All 10 current original-JAR/settings tar archives passed integrity checks. Detailed checksums and final first/second-start counts are in the primary `deployment-manifest.json` and `test-results.md` under the first directory above. No production startup has occurred.

## Executive decision

The world-generation stack is retained. The dedicated server has 187 mod JARs; the Prism client had 234, and all 186 originally shared JARs were byte-identical. SparseStructures is the only server-only JAR and remains configured for sparse structure placement.

Do **not** launch into the old `world` folder. The production `level-name` has already been changed to `world_fresh_20261028`. This preserves `world` and both dated prior-world copies without deleting or renaming them. The production server is left stopped for the user to click Start when ready.
## Current server state

- Minecraft 1.21.1, NeoForge 21.1.265.
- The latest full test, launched through Crucible's own `TmuxManager.start()`, reached `Done (3.589s)!` with the registered `-Xms8G -Xmx16G` heap settings. The manager start returned success; the matching graceful-stop API stopped the test cleanly.
- Production remains stopped. There is no Minecraft Java server process, no production tmux session, and port 25566 is free.
- Crucible's read-only `ServerInstance.readiness()` returned all seven checks OK: folder, Java, EULA, launcher, mods, properties, and settings. It resolves `start.sh` and the saved `-Xms8G -Xmx16G` arguments.
- The configured production level name is now `world_fresh_20261028`; seed is blank/random; structures and the Nether are enabled; level type stays `wover:normal`. Existing difficulty, survival mode, 16 view distance, and 10 simulation distance were retained.
- The target folder exists only to hold the datapack; it does not yet have a `level.dat`. It will become the new world on the next Start.
- The current `world/level.dat` SHA-256 remains `091d4e46ff6980c931887ba66fd959712e57e211c83d6c929d0c7d086bf29482`.
- Crucible passes the registered JVM arguments via `CRUCIBLE_JAVA_ARGS` to its generated `start.sh`; both the script and Crucible's actual manager start path were tested in the disposable copy.
- NobaraSupervisor is connected: version 1.5.0, OAuth enabled. No production startup was triggered during this deployment.
## World-generation and mod parity audit

### Present on the dedicated server

The inspected mod folder includes the intended world-generation and structure providers, including:

- Terralith 2.6.2.
- CTOV 3.6.3.
- ATi Structures Vanilla Edition 1.4.7.
- Dungeons Arise 2.1.68 and Dungeons Arise Seven Seas 1.0.4.
- YUNG's API, Better Desert Temples, Better Dungeons, Better End Island, Better Nether Fortresses, Better Ocean Monuments, Better Strongholds, and YUNG's Extras.
- Even Better Nether, Deeper and Darker, Ecologics, WorldWeaver 21.0.30, BCLib 21.0.28, and SparseStructures 3.0.

The installed WorldWeaver JAR registers mod ID `wover`; the latest startup log contains `preset = wover:normal`. Terralith was confirmed active in previous gameplay checks, with `terralith:blooming_valley`, `terralith:alpine_grove`, and `terralith:caldera` found by locate queries. The server and client copies of `config/terralith.json`, `config/wover/main.json`, `config/wover/cached.json`, `config/bclib/main.json`, and `defaultconfigs/biolith/general.json` match.

SparseStructures remains intentionally configured with global `spreadFactor: 8`, `idBasedSalt: true`, Mansion factor 8, Dungeons Arise major structures factor 12, and ATi aboveground-large factor 12. Preserve these values for the first fresh-world test; the operator chose factor 8 after factor 4 looked too dense. Do not tune structure frequency again until the operator has explored the new terrain.

### Keep the generated cache server-specific

The client and server both have Supplementaries' generated data-pack cache, but the hashes/content differ (client hash begins `e0fff7fe...`, server hash begins `adeb37ce...`; 39 vs 57 files). The server's pack contains additional generated recipe resources and `remove` tag fields. **Do not copy the client cache over the server cache.** Keep the server-generated cache paired with the server's mod/config set and inspect the fresh-start log if this pack fails to load.

## Recovery points

Existing world locations remain intact:

- Active/preexisting folder: `world` (~27 MB).
- Retained pre-spread-8 test: `world.pre-spread8-test-20261028-0615` (~31 MB).
- Retained pre-Terralith-reset world: `world.pre-terralith-reset-20261028-0600` (~35 MB).

Additional archives include:

- `/home/roaring/.local/share/crucible-backups/username-admin-server-world-before-terralith-reset-20261028-0600.tar.gz` — SHA-256 `312ddf86e24c6b58b21999b97244814540400a056a77cf419c1ffed16ac1b929`.
- `/home/roaring/.local/share/crucible-backups/username-admin-server-world-factor4-before-spread8-20261028-0615.tar.gz` — SHA-256 `d1385d0a5780b1aad43a2e4837cad4ee1a186437c05df0ce5c255f344df89f69`.
- **New before-fresh-world archive:** `/home/roaring/.local/share/crucible-backups/username-admin-server-world-factor8-before-fresh-20261028-161756.tar.gz` — SHA-256 `f9191b54c77e0a87438df9cbc65bb9e819546ff5f074aada6af1cdf46dc3653c`. Mode 0600; tar integrity test passed; the source world was not modified during archive creation.

The server filesystem had about 551 GB available when checked.

## Deployed launch-fix datapack

The tested pack is installed only under the new world, not in the retained worlds:

- Source directory: `prepared-datapacks/fresh-world-launch-fixes-20261028/`
- ZIP: `prepared-datapacks/fresh-world-launch-fixes-20261028.zip`
- ZIP SHA-256: `e76ddbb936b502eb664388da8bd75372580e451d029c37e49f3c702f61e75e57`
- Data-pack format: 48 (Minecraft 1.21.1).
- Deployed path: `world_fresh_20261028/datapacks/fresh-world-launch-fixes/`
- Integrity: ZIP passes integrity test; all 429 data JSON resources parse.
- Confirmed active in test: `/datapack list enabled` reported `file/fresh-world-launch-fixes (world)` among 20 enabled packs.
- Final isolated launch through Crucible's manager reached `Done (3.589s)!`; recipe parse errors 0, loot-table parse errors 0, Sable resource errors 0, invalid resource-path errors 0, BCLib level-data errors 0.

The datapack contains 14 restored compatibility recipes, 108 corrected Recipe Integration condition files, schema/optional-mod recipe repairs, 267 Railways optional-track loot conditions, seven invalid loot-table suppressions, and traceable manifest/reason/hash metadata. The two JAR hotfixes below are deployed separately because those resource defects cannot all be corrected from the world datapack alone.
### Fix A: 14 empty compatibility resources shadowing valid recipes

A read-only scan of all 187 installed JARs found 14 resource paths where `mekanism_tfmg_compat-0.4-1.21.1.jar` supplies an empty JSON object `{}`, while another installed JAR supplies a valid recipe at exactly the same path. This directly explains errors such as missing recipe key `type` for TFMG steel tools, Mekanism infused control circuits, Mekanism lead/steel conversions, and Create's Mekanism lead recipes.

The pack overrides those exact paths with the valid payloads from the corresponding base Create, Mekanism, or TFMG JAR. It does not remove the compatibility mod or modify any JAR.

Compatibility JAR SHA-256: `352302fa9a80628ae70b3292e7788273ea86ab98ae304c9079c1b4b608bc3f5e`.

### Fix B: Recipe Integration conditional codec typo

The installed `recipe_integration-0.4.0.jar` contains 216 occurrences of the invalid type `neoforge:mod_laoded` across 108 recipe JSON files. The latest startup log reported 108 affected recipe errors with this exact unknown condition codec. The staged pack copies those 108 resource files into the same namespace/path and changes only that token to `neoforge:mod_loaded`, preserving the other recipe content and optional-mod checks.

Recipe Integration JAR SHA-256: `4e41f8dcf06ba21e0e494ccee30ece2c3c8e7094cf863d4fb3a50495ef740e35`.

**Do not call these fixes proven until a real server startup confirms that the pack wins the resource merge and the corresponding parser errors disappear.** The artifact is a narrow repair candidate, not an upstream binary replacement.

## Remaining warnings after final fixes

The final isolated first-start and second-start both reached `Done` with zero ERROR-level lines. Recipe and loot-table parser errors, BCLib first-generation errors, Sable malformed resources, the bad Recipe Integration path, and dedicated-server class probes are all zero.

Moonlight's full compatibility advisory about Fabric API being detected through Connector is retained at WARN. It documents the mixed-loader risk rather than hiding it. The other known warnings include Quark entity-category mismatch warnings on some Terralith biomes, voice-chat offline-encryption warning during non-player startup, and Biolith notices about temporary test dimensions created by Sable. They did not prevent readiness.

HammerLib's prior ERROR entry was a misclassified log call after successful recipe-extension registration; it is now INFO, and the extension still registers and injects its recipes. Struts and ConnectorExtras server-only mixin side fixes eliminated all client-class probes without disabling those mods.

The BCLib first-generation guard specifically checks whether `worldDir/level.dat` exists. If the file is absent, it skips the pre-patch step that otherwise tries to read a nonexistent file. It does not change the existing-world migration path. A genuinely fresh test level then created `level.dat` and region files, reached `Done`, and successfully restarted with zero BCLib errors.

### Verify the exact Mekanism Elements project before patching/upgrading

The installed file is `mekanismelements-3.0.18-neoforge-1.21.1.jar`, mod ID `mekanismelements`, author `Infernos`; SHA-256 `0ed59d328f584880289cf835e57ec4cd9f9e28ee63d31cd67013a7ae635b5f56`. It has several recipe parse failures (missing `per_tick_usage`, an unknown `mekanismelements:zirconium` chemical, references to nonexistent dye items `minecraft:aqua_dye`/`minecraft:dark_red_dye`, and an invalid oxidizing input schema).

There are similarly named but separate Mekanism Elements projects. The `fixdol/Mekanism-Elements` repository is **not** the source matching this installed JAR and must not be used as a drop-in recipe source. The publisher page for the installed author's project lists a 1.21.1 release 3.0.20, but its published changelog describes Adsorption Separator changes and does not establish that these recipe errors are fixed. The fresh-world datapack now disables four recipes whose registry IDs or serializers are absent from this installed build: `mekanismelements:centrifuging/xenon`, the two invalid aqua/dark-red clump recipes, and the invalid Extended Wrenches dye recipe. The painting and methane schema failures are repaired. Missing chemical/item registrations are not invented; those unsupported recipes are explicitly disabled with `neoforge:false`.

Publisher page for the installed project: https://www.curseforge.com/minecraft/mc-mods/mekanism-elements-neoforge/files/8614281

## ATi Structures reassessment

The installed ATi JAR SHA-512 is:

`e27e8b112521e3686e2afeb9e62f3c311add924142afadf60bda026005a164f0bdb6c1b932d79802ffff938f26b9422a2a3beb9be24c8e7690d53710a94b9533`

This exactly matches the published SHA-512 from the public PrismNix manifest:

`4n6LESUh42huKv655i88MRrdkkFCr632C9oCYAWhZPC9tsG5MteYAv//k48muUIqKjvrm+JMjnaQ1TcQqUuVMw==`

The installed JAR contains `terrain_large.json` and `terrain_small.json`, but not `data/ati_structures/worldgen/template_pool/terrain.json`. A search across current installed JAR JSON resources found no current external reference to `ati_structures:terrain` beyond `terrain_large`/`terrain_small` identifiers in those pools, and the latest `latest.log` did not reproduce the earlier reported missing-pool error. Do **not** patch or replace this verified release on current evidence; test ATi structures in the fresh world and recheck logs first.

Official release page: https://modrinth.com/datapack/ati-structures-vanilla-edition/version/ivErXrfo

## Next Start and first-world validation

The production folder is configured for the new world. **Do not change the level name back to `world`.** When ready, click Start in Crucible for the registered `username admin server` instance. Do not use `run.sh` directly; Crucible's Start path supplies the configured JVM arguments to `start.sh`.

After the first production start, verify the following from the server console/log:
1. The JVM command contains `-Xms8G -Xmx16G` (Crucible's saved setting).
2. The server reaches the `Done` marker.
3. `/datapack list enabled` includes `file/fresh-world-launch-fixes (world)`.
4. The log has zero recipe parser errors, zero loot-table parser errors, and none of the Sable malformed-resource/path errors eliminated during testing.
5. `world_fresh_20261028/level.dat` and region files are created; the old `world/level.dat` hash remains unchanged.
6. Low-volume smoke tests: `/locate biome terralith:blooming_valley` and `/locate structure ati_structures:lighthouse`. In the disposable world both IDs were registered and locatable; distances from spawn were 3,512 and 27,630 blocks respectively. Avoid many long-distance locate searches, which cause temporary tick-delay warnings.

Once the first start passes, have a player join and check terrain variety, sparse structure density, loot, and common modded-machine/recipe paths. The production server is intentionally left stopped for the user's next Start click.

## Rollback points and deployment record

Deployment backups (mode 0600, archive integrity passed):

- Backup directory: `/home/roaring/.local/share/crucible-backups/username-admin-server-fresh-world-deployment-20261028-171846/`
- Original server files (server.properties, both original server JARs, and launcher scripts): `server-files-original.tar.gz`, SHA-256 `703b24d64f9404af2d77fbaceaefac61231b5b412c38a735da239254724a7acc`.
- Original Prism client JARs: `client-mods-original.tar.gz`, SHA-256 `787952d74e8f6decd4c2ccc9f4acf7fcffad066330b8ab7b1bcadde69ead3f59`.
- Full checksums, source/patch hashes, and test outcomes: `deployment-manifest.json`.
- Startup/readiness/parse counts: `test-results.md`.
- Existing-world archive: `/home/roaring/.local/share/crucible-backups/username-admin-server-world-factor8-before-fresh-20261028-161756.tar.gz`, SHA-256 `f9191b54c77e0a87438df9cbc65bb9e819546ff5f074aada6af1cdf46dc3653c`.

The patched JARs were validated in a disposable copy, then deployed identically to the production server and its matching Prism client. To roll back the two JARs and server.properties, restore the original archive under the backup directory above. Leave all original world folders untouched during any rollback.
## Evidence links

- Current Crucible audit issue: https://github.com/Roaring1/Crucible/issues/32
- Recipe Integration source project: https://github.com/Mogwump/recipe-integration-mod
- ATi Structures official release: https://modrinth.com/datapack/ati-structures-vanilla-edition/version/ivErXrfo
- Installed Mekanism Elements publisher page: https://www.curseforge.com/minecraft/mc-mods/mekanism-elements-neoforge/files/8614281
- Supervisor connectivity incident: https://github.com/Roaring1/NobaraSupervisor/issues/11

## Change safety statement

The production server has **not** been started. On disk:

- `server.properties` selects `world_fresh_20261028`; all unrelated properties were preserved.
- The validated repair datapack is installed only under `world_fresh_20261028/datapacks/fresh-world-launch-fixes/`, never in an old world.
- The deployed JAR fixes are present on the server and matching Prism client for BCLib, Moonlight, SablePhysicsCompat, Recipe Integration, Struts, and HammerLib. ConnectorExtras has the tested EMI Bridge side fix on the dedicated server only; the client has the original file.
- Original JARs and server properties are held in checksum-verified rollback archives. The pre-existing world archive also remains available.
- `world/level.dat` hash remains `091d4e46ff6980c931887ba66fd959712e57e211c83d6c929d0c7d086bf29482`; `world_fresh_20261028/level.dat` has not been created in production.

Crucible readiness passes all seven checks. Final disposable `TmuxManager.start()` first-generation and restart tests succeeded with zero ERROR-level messages and zero targeted parser failures. The remaining Moonlight compatibility warning remains visible at WARN. Next step is the operator's Start click, then verify the production first start and playtest.
