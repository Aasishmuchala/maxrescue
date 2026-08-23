# 3ds Max 2024–2027 scripting + memory behaviour for a headless scene-memory-reduction tool

Scope: drive `3dsmaxbatch.exe` (Max 2026, bundled Python 3.11.9, `pymxs`) to merge a ~170 GB .max into an empty scene in batches and apply only render-identical operations (target ≤ 70 GB, V-Ray CPU).

Confidence tags: **[V]** verified against Autodesk/Chaos/iToo official docs or SDK reference (URL given) · **[F]** forum/vendor-staff statement, not official doc · **[I]** inferred from documented behaviour · **[U]** unknown — must verify on the box.

Render-impact tags: **IDENTICAL** · **POSSIBLY-DIFFERENT** · **CHANGES-RENDER**.

Raw captures live in this directory (`j_*.md`, `itoo_*.txt`, `adn_install.md`).

---

## A. File loading without a full open

### A1. Listing what is in a file without opening it

**`getMAXFileObjectNames`** [V]
```
<array of strings> getMAXFileObjectNames <max_filename_string> [quiet:<boolean>]
    [missingDLLsAction:(#logmsg|#logToFile|#abort|<int>)] [missingDLLsList:<&var array of strings>]
```
- Doc: "Returns an array of strings, one for each of the names of the objects in the given 3ds Max file … a preview list of the objects in another scene file (by name) to set up user selection of the objects to be merged under script control." Returns an empty array for `.DRF`. "The object names used by `mergeMaxFile` are case sensitive."
  https://help.autodesk.com/cloudhelp/2022/ENU/MAXScript-Help/files/MAXScript-Tools-and-Interaction/File-Access/3ds-Max-Scene-Files-Access/GUID-624D3D05-B15D-4A97-9F15-DA35CDB0DDD2.html (same text in the 2025 page: https://help.autodesk.com/cloudhelp/2025/ENU/MAXScript-Help/files/MAXScript-Tools-and-Interaction/File-Access/3ds-Max-Scene-Files-Access/GUID-624D3D05-B15D-4A97-9F15-DA35CDB0DDD2.html)
- How it works under the hood [V, SDK]: `Interface::MergeFromFile(name, mergeAll=TRUE, …, dupAction=MERGE_LIST_NAMES, mrgList=&tab)` — "puts a list of the nodes in the file into mrgList, and simply returns (no merging is done)". https://help.autodesk.com/cloudhelp/2026/ENU/MAXDEV-CPP-API-REF/class_interface.html and https://help.autodesk.com/cloudhelp/2023/ENU/Max-Developer-Help/cpp_ref/group___dup_node_name_actions.html
- Does it load the scene? [I] It goes through the merge loader in "list names" mode; the presence of `missingDLLsAction` implies the file's DLL/class directory is read and plug-in availability is checked, but whether node/object chunks are *instantiated* (i.e., whether RAM spikes to full-file size) is **not documented → [U] measure working-set delta on the box** (see B7 for the measurement calls).
- What it returns [I]: every INode name in the file (hidden and frozen nodes included — the Merge dialog lists them too), including XRef-*object* proxy nodes; the contents of XRef *scenes* referenced by the file are not nodes of that file and will not be listed. Duplicate names appear once per node (array may contain duplicates). **[U]** confirm hidden/duplicate behaviour on the box.
- Handles instead of names [V, 2025+]: `mergeMAXFile … mergeByHandles:<boolean>` — "When true, the `<name_array>` is a list of node handle IDs instead of a list of names." There is no documented `getMAXFileObjectHandles`; how to obtain handles without opening the file is **[U]** (possible route: `getMAXFileObjectNames` + the file's `AnimHandle`s are not exposed; a first pass merge with `mergedNodes:&arr` returns live nodes whose `.inode.handle` are the *new* handles, not the file's).

**Other read-without-open functions**
- `getMaxFileVersionData <filename>` [V] → `#(<fileVersion>, <maxVersionThatSaved>)`, e.g. `#(13000, 16000)` for a 2014 file saved as 2011. https://help.autodesk.com/cloudhelp/2017/ENU/MAXScript-Help/files/GUID-F693FDB5-1B1B-4A4D-9C48-ECF91272FD88.htm
- `getMAXFileAssetMetadata <filename>` / `setMAXFileAssetMetadata <filename> <array of AssetMetadata_StructDef>` [V] → array of structs (`assetId` GUID string, `FileName`, `Type` e.g. `#bitmap`); reads the asset-metadata stream of the file, scene not opened. https://help.autodesk.com/cloudhelp/2017/ENU/MAXScript-Help/files/GUID-2AF2AFE0-1B22-4DA2-B0F9-CA3ADABCB6C6.htm
  (There is no `getMAXFileAssetNames`; the above is the real API.)
- `getFileVersion <filename>` [V] → Windows file/product version string via `GetFileVersionInfo` (for EXE/DLL), not a scene-file function. https://help.autodesk.com/cloudhelp/2017/ENU/MAXScript-Help/files/GUID-BA196B48-8ECA-4E0C-AE2E-F7EFAAF39844.htm
- `isMaxFile <filename>` [V] → true if the file "is a valid MAX file by testing internal structure".
- `clearMAXFilePersonalInfo` (2026) [V] removes User Name / Computer Name file properties from an external file. https://help.autodesk.com/cloudhelp/2026/ENU/MAXScript-Help/files/What-is-New-in-MAXScript/What-s-New-in-MAXScript-in-3ds.html

### A2. `mergeMaxFile`

Full 2025 signature [V]:
```
<boolean> mergeMAXFile <filename_string> [<name_array>] [#prompt]
    [ [#select] #noRedraw ]
    [ #deleteOldDups | #mergeDups | #skipDups | #promptDups | #autoRenameDups ]
    [ #promptMtlDups | #useMergedMtlDups | #useSceneMtlDups | #renameMtlDups ]
    [ #promptReparent | #alwaysReparent | #neverReparent | #mergeChildren ]
    [ quiet:<boolean> ]
    [ missingExtFilesAction:<actions> ] [ missingExtFilesList:<&variable> ]
    [ missingDLLsAction:<actions> ]     [ missingDLLsList:<&variable> ]
    [ missingXRefsAction:<actions> ]    [ missingXRefsList:<&variable> ]
    [ mergedNodes:<&variable> ] [ includeFullGroup:<boolean> ]
    [ mergeByHandles:<boolean> ]
```
`<actions>` = `#logmsg | #logToFile | #abort | <int>`.
Flag semantics [V, 2022 page]: `#prompt` opens the Merge dialog; `#select` selects merged nodes; `#noRedraw` delays redraw; `#deleteOldDups` deletes scene nodes with the same name; `#mergeDups` ignores name conflicts; `#skipDups` skips incoming dups; `#promptDups` dialog; `#autoRenameDups` renames incoming (2011+); `#promptMtlDups` is the **default** — always pass one of `#useMergedMtlDups | #useSceneMtlDups | #renameMtlDups` in batch; `#promptReparent` default — pass `#alwaysReparent | #neverReparent` in batch; `mergedNodes:&arr` receives the merged node array; `includeFullGroup:` (2020+, default false) pulls in the whole group when a member is named; `<name_array>` omitted = merge everything. Returns false for `.DRF` or on failure. Also `getLastMergedNodes()` (2017+). 
SDK equivalent [V]: `Interface::MergeFromFile(const MCHAR* name, BOOL mergeAll=FALSE, BOOL selMerged=FALSE, BOOL refresh=TRUE, int dupAction=MERGE_DUPS_PROMPT, NameTab* mrgList=NULL, int dupMtlAction=MERGE_DUP_MTL_PROMPT, int reparentAction=MERGE_REPARENT_PROMPT, BOOL includeFullGroup=FALSE, MaxSDK::Array<MaxRefEntryData>* dataList=nullptr)` — "When you specify a pointer to a NameTab … and don't set dupAction to MERGE_LIST_NAMES, then this method will merge the nodes whose names are listed in the mrgList." `MERGE_DUPS_RENAME = 1000`.

Batch-safe call pattern (MAXScript) [I from the above]:
```
arr = #()
ok = mergeMAXFile bigFile #("NodeA","NodeB") #noRedraw #mergeDups #useSceneMtlDups #neverReparent \
       quiet:true missingExtFilesAction:#logmsg missingDLLsAction:#logmsg missingXRefsAction:#logmsg \
       mergedNodes:&arr
```
Caveats [I]: `#neverReparent` breaks hierarchies when parents arrive in a later batch — merge parent+children in the same batch (walk hierarchy from a first "all names" pass, or use `#mergeChildren`/`includeFullGroup`); `#useSceneMtlDups` makes later batches reuse materials already merged (keeps instancing of materials, avoids N copies) — but only if the duplicate-name test matches; `#renameMtlDups` is the safe-but-bloating alternative.

**Does merge deserialise the whole file?**
- Official docs: silent. Autodesk support article "Crash occurs when opening or merging individual objects and scene files in 3ds Max" recommends, for files that crash on open, to "open a new, empty scene and use File > Merge to selectively import objects from the corrupt file, as 3ds Max may be able to read some chunks even if others are damaged" and, if that fails, FBX export/import. [F, Autodesk support KB] https://www.autodesk.com/support/technical/article/caas/sfdcarticles/sfdcarticles/Crash-occurs-when-opening-or-merging-individual-objects-and-scene-files-in-3ds-Max.html — so **batch merge is an Autodesk-endorsed recovery technique for files that won't open**, and it is chunk-selective at least for *damaged* chunks.
- Counter-evidence that merge is *not* memory-selective for global scene data [F]: cgarchitect 2011 thread — merging a single box from a "corrupted" file took a 9 MB / 667 MB-RAM scene to 225 MB / 3221 MB RAM; cleaning Motion-Mixer tracks (`theMixer.removeMaxMixer`) dropped it to 1287 MB. https://forums.cgarchitect.com/topic/62325-3ds-max-memory-error-some-max-files-using-all-available-memory/ — i.e. note tracks / mixer / animation layers / material libraries ride along with any merge regardless of node selection.
- Autodesk alumni (Jon A. Bell, Senior Tech Support) on slow merges: causes are "thousands of objects, many with uncollapsed modifier stacks" and "spurious/hidden animation and note tracks" in the *source* file [F] https://forums.autodesk.com/t5/3ds-max-forum/slow-merge-3ds-max/td-p/9406313 — consistent with the merge loader walking the full object graph of the source.
- Third-party claim that Merge "only loads the geometry and basic properties" and therefore loads faster than Open [F, unverified vendor blog]: https://www.sehatdiri.com/2026/07/fix-3ds-max-slow-scene-open.html
- **Verdict [I]:** peak RAM of a selective merge is undocumented and may approach the full-open peak (the loader has to resolve the reference graph of the selected nodes — shared materials, maps, controllers — and global data comes along). Steady-state RAM after `gc()` will only contain the selected nodes' graphs. **[U] Measure on the box**: merge 1 small node from the 170 GB file and log peak working set (`sysinfo.getMAXMemoryInfo()[2]`). If the peak ≈ full open, batching only helps steady-state, not peak, and the tool needs a different ingestion path (see unknowns).

### A3. `loadMaxFile`, `3dsmaxbatch.exe`, `3dsmax.exe`

**`loadMaxFile`** [V]
```
<boolean> loadMaxFile <filename_string> [useFileUnits:<boolean>] [quiet:<boolean>] [allowPrompts:<boolean>]
    [missingExtFilesAction:(#logmsg|#logToFile|#abort|<int>)] [missingExtFilesList:<&var>]
    [missingDLLsAction:(...)] [missingDLLsList:<&var>]
    [missingXRefsAction:(...)] [missingXRefsList:<&var>]
    [skipXRefs:<boolean>]      -- 2025 doc: "disable scene and object XRefs during scene load"
```
`useFileUnits` default false (keeps system units), `allowPrompts` default false, "will replace the current scene content without warning and will not issue the save-changes prompt". Sources: 2022 + 2025 pages above.
Companions [V]: `saveMaxFile <filename> [saveAsVersion:<int>] [clearNeedSaveFlag:<bool>] [useNewFile:<bool>] [quiet:<bool>]` (overwrites silently; `useNewFile` defaults false when `saveAsVersion` given); `saveNodes <node_collection> <filename> [saveAsVersion:<int>] [quiet:<bool>]`; `resetMaxFile [#noPrompt]`; `quitMAX [#noPrompt] quiet:<boolean> exitCode:<int>` (https://help.autodesk.com/cloudhelp/2022/ENU/MAXScript-Help/files/MAXScript-Tools-and-Interaction/Miscellaneous-Functions/GUID-92E4150E-0AD4-430C-A852-783A45276D23.html); `checkForSave()`; `holdMaxFile()` / `fetchMaxFile [quiet:] [missingDLLsAction:] …`; `archiveMAXFile <filename> [quiet:] [saveAsVersion:] [missingExtFilesAction:] …` (2019.1+).

**`3dsmaxbatch.exe`** — 2026 doc [V] https://help.autodesk.com/cloudhelp/2026/ENU/3DSMax-Batch/files/GUID-48A78515-C24B-4E46-AC5F-884FBCF40D59.htm
```
3dsmaxbatch.exe <script_file> [options]
  -help
  -v <0..5>            batch-process log verbosity (0 fatal … 2 warnings [default] … 5 debug); "affects messages generated by 3ds Max Batch, not 3ds Max itself"
  -dateFormat <str> / -timeFormat <str>   (.NET format strings for console log)
  -i <3dsmax.ini>      config file (default per-user 3dsmax.ini)
  -p <Plugin.UserSettings.ini>
  -listenerlog <file>  MAXScript Listener log ("No listener log is created by default")
  -log <file>          system log (script errors/exceptions; default Max.log)
  -dm on|off           Dialog Monitor "watches for dialogs from plug-ins, and closes them" (default off)
  -mxsString key:"value"   (repeatable) → maxOps.mxsCmdLineArgs dictionary
  -mxsValue  key:"<mxs value>" (repeatable)
  -sceneFile <file>    loaded before the script runs
  -safescene ON|OFF    Safe Scene Script Execution override
```
- Script type auto-detected: "all MAXScript extensions, and .py, .pyc, and .pyw Python files"; Python reads args via `pymxs.runtime.maxOps.mxsCmdLineArgs`.
- **No** `-secure`, `-mi`, `-mip`, `-ig`, `-q`, `-silent` switches exist for 3dsmaxbatch (they are 3dsmax.exe switches); no documented timeout or memory switch.
- Exit codes [V]: `0` success; `-100` missing/invalid args; `-110` cannot start 3ds Max process; `-120` cannot establish/maintain communication with 3ds Max; `-130` 3ds Max reported an error during execution; `-1` handled exception; `-2` unhandled exception; `-3` crash processing WM_CLOSE; `-4` crash processing WM_DESTROY; `-5` crash before WinMain() completion; `-6` **Out of memory**; `-7` **Could not create graphics device**; `-8` License error; `-9` Could not create application. Custom: `quitMAX quiet:true exitCode:<int>` ("Verbosity level must be set to 'debug' (5) for the exit code to print to the console").
- Process model [F, Autodesk product owner Attila Szabo]: "3dsmaxbatch.exe launches 3dsmax.exe in a special non-interactive mode as a child process" — -130 means the child reported an error. https://forums.autodesk.com/t5/3ds-max-programming-forum/3dsmax-batch-always-exits-with-code-130/td-p/9461582 (same thread: Python `logging.StreamHandler` printing to stdout produced -130 in 2020/2021 — route logs to a file).
- UI/memory [V]: "In Batch mode, 3ds Max does not load the desktop application's user interface. It starts faster and uses less memory." https://help.autodesk.com/cloudhelp/2019/ENU/3DSMax-Batch/files/GUID-0968FF0A-5ADD-454D-B8F6-1983E76A4AF9.htm  Detect with `maxops.isInNonInteractiveMode()` [V].
- Nitrous/GPU in batch: **[I]** exit code -7 "Could not create graphics device" proves a graphics device is still created in batch mode; no switch disables it. Viewports are not drawn (no UI), so Nitrous mesh/texture buffers for scene objects are most likely *not* built — **[U]** confirm with `NitrousGraphicsManager.GetSystemMemoryUsed()` and `nvidia-smi` while a batch job holds a big scene. Forcing the software (WARP) driver: 3dsmax.exe `-h` opens the driver picker and `-v` "Loads a display driver, overriding the setting in 3dsmax.ini"; for batch, pass a custom ini via `-i` with `[WindowState] GFXType=Nitrous` / `GFXDevice=…` keys [F, search-level only — key names unverified] https://help.autodesk.com/cloudhelp/2025/ENU/3DSMax-Customizing/files/GUID-3D6B4C8E-8C0D-4A9C-BFB0-2463803268CE.htm ("The Nitrous Software driver doesn't require hardware support … supported by WARP").
- Licensing [F, Autodesk Support employee BenBisares, 2021]: "only when the UI of 3ds Max is opened will it check for a license. When it's being used just to render on a render farm (where you don't see the UI) it shouldn't be checking for a license … I'm not aware of any calls or functions that when done from the 3ds Max batch would trigger a license check." No official doc statement; exit code -8 exists, so a license *can* be required. https://forums.autodesk.com/t5/3ds-max-forum/max-batch-licence/td-p/10334463 **[U]** check license-server logs during a batch run (V-Ray will take its own render license only if you render).

**`3dsmax.exe` switches** (2025 doc) [V] https://help.autodesk.com/cloudhelp/2025/ENU/3DSMax-Basics/files/GUID-1A97CFEC-60A3-4221-B9C3-5C808E2AED35.htm
`-a <bmp>` splash, `-about`, `-d`, `-df/-dfc/-dfd` (design/classic mode), `-dm` dialog monitor, `-g`, `-h` graphics driver chooser, `-i otherfile.ini`, `-l` load last file, `-lang=`, `-log <file>`, `-ma` maximized, `-mi` minimized, `-ms` minimized+no splash, `-mxs "cmd; cmd"` (runs after the scene file on the command line is opened), `-n` disable network mode, `-p plugin.ini`, `-q` no splash, `-safescene ON|OFF`, `-silent` ("without the splash screen and any dialogs that might block script execution"), `-u <utility>` (e.g. `-U MAXScript script.ms`), `-v` display-driver override, `-y <license path>`, `-z <file>` version. `-mip` appears in old Autodesk examples but is not in the 2025 list; Autodesk staff recommend `-mi` [F] https://forums.autodesk.com/t5/3ds-max-programming-forum/3dsmax-command-line-switches-mip/td-p/6683406. Running scripts: https://help.autodesk.com/cloudhelp/2021/ENU/3DSMax-Basics/files/GUID-BCB04DEC-7967-4091-B980-638CFDFE47EC.htm. Prefer 3dsmaxbatch: it has exit codes, logs, no UI.

### A4. XRefs as an offload container

[V] https://help.autodesk.com/cloudhelp/2016/ENU/MAXScript-Help/files/GUID-090B28AB-5710-45BB-B324-8B6FD131A3C8.htm
```
xrefs.addNewXRefFile <filename_string> [#noLoad] [root:<XRefScene>]   -> XRefScene
xrefs.getXRefFile <index> [root:]      xrefs.getXRefFileCount [root:]
xrefs.deleteAllXRefs [root:]           xrefs.updateChangedXRefs [#noRedraw] [root:]
xrefs.findUnresolvedXRefs [root:]      xrefs.attemptUnresolvedXRefs [root:]
xrefs.addNewXRefObject <filename string> <object name> dupMtlNameAction:{#prompt|#useXRefed|#useScene|#autoRename} -> XRefObject
XRefScene props: filename, tree (RO root node), parent, autoUpdate, boxDisp, hidden, disabled,
                 ignoreLights, ignoreCameras, ignoreShapes, ignoreHelpers, ignoreAnimation
XRefScene methods: delete, merge, updateXRef, flagChanged
```
(`objXRefMgr` is the preferred interface for XRef *objects* in Max 8+ [V].)
- Disabled XRef scenes are not in RAM [V]: XRef Scenes dialog — "When an XRef is disabled, it's listed in gray in the list, and it's not loaded into memory." https://help.autodesk.com/cloudhelp/2022/ENU/3DSMax-Manage-Scenes/files/GUID-AD995B82-D069-4C96-A3DF-D9FE8C8A284A.htm  Same for XRef Objects ("Disabled external reference files and objects are not loaded into memory") [V, 2012 XRef Objects dialog page].
- `#noLoad` adds the record without loading; `loadMaxFile … skipXRefs:true` (2025+) opens the master without loading xrefs.
- **Render impact**: a disabled XRef does not render → **CHANGES-RENDER**; an enabled XRef renders identically but is fully in RAM at render time. So XRef is *not* a render-time memory lever; it is useful only to keep the *master* file openable/editable (interactive RAM) and as a partitioning scheme. [I]

---

## B. Memory levers (classified for V-Ray CPU render-identity)

### B5. Bitmaps: proxy system, pager, Nitrous texture limits

**Bitmap Proxies** — MAXScript interface `BitmapProxyMgr` (note: not "BitmapProxyManager") [V] https://help.autodesk.com/cloudhelp/2023/ENU/MAXScript-Help/files/3ds-Max-Objects-and-Interfaces/Interfaces/Core-Interfaces/Core-Interfaces-Documentation/B/GUID-B0E699D2-AC68-47A7-822C-C2D0AAE07901.html
```
BitmapProxyMgr.globalProxyEnable      : boolean          -- "Enable Proxy System"
BitmapProxyMgr.globalProxyRenderMode  : enum  #renderMode_UseProxies | #renderMode_UseFullRes_KeepInMemory | #renderMode_UseFullRes_FlushFromMemory
BitmapProxyMgr.globalProxySizeFactor  : enum  #full | #half | #third | #quarter | #eighth   -- "Downscale map to…"
BitmapProxyMgr.globalProxySizeMin     : integer (px)     -- "Use proxy only if the original map's largest dimension is greater than N"
BitmapProxyMgr.GetProxySizeFactor <bitmapFilename> / SetProxySizeFactor <bitmapFilename> <enum>
BitmapProxyMgr.GetProxyUseGlobal <bitmapFilename> / SetProxyUseGlobal <bitmapFilename> <bool>
BitmapProxyMgr.GetProxyReady <bitmapFilename>
BitmapProxyMgr.RefreshAll [#updateStale|#refreshAll|#generateAll]   (alias GenerateAll)
BitmapProxyMgr.ShowConfigDialog / ShowPrecacheDialog [<string array>]
```
Dialog text [V] https://help.autodesk.com/cloudhelp/2022/ENU/3DSMax-Manage-Scenes/files/GUID-BCC5D81D-3355-49CF-ADB3-0748C4631301.htm: Render Mode = "Render with Proxies (High Performance, Low Memory)" / "Render with Full Resolution Images and Keep them In Memory (High Performance, High Memory)" / "Render with Full Resolution Images and Free them from Memory (Low Performance, Low Memory)"; "Page Large Images to Disk — When on, 3ds Max uses a page file on disk when it handles large bitmap textures or large renderings"; proxies are "intended for use primarily in the viewports".
- Render impact: `#renderMode_UseProxies` → **CHANGES-RENDER** (downscaled textures). `#renderMode_UseFullRes_FlushFromMemory` → **IDENTICAL** (full-res at render, freed afterwards; cost = reload time). `globalProxyEnable` with FullRes render mode → **IDENTICAL** and cuts *interactive* RAM (viewport/material-editor loads proxies).
- Applies to 3ds Max `Bitmap` texmaps loaded through the BitmapManager. `VRayBitmap`/`VRayHDRI` use V-Ray's own loader: Chaos staff guidance for a RAM budget is "convert them to tiled EXRs, load them with a VRayHDRI loader, and set your texture cache size accordingly" (Lele, Chaos RnD, 2017) and Vlado (2012) on Max's proxies with V-Ray: "It will certainly save RAM, but the rendering won't be much faster." [F] https://forums.chaos.com/forum/v-ray-for-3ds-max-forums/v-ray-for-3ds-max-general/74253-bitmap-pager-still-a-no-no and …/50631-bitmap-pager. A user reports that 16 k textures loaded via VRayHDRI still slow *scene loading* and viewports ("3dsmax viewports recognize these textures as full 16k") [F] https://forums.chaos.com/t/texture-proxy-system/90841. **[U]** whether `BitmapProxyMgr` has any effect on `VRayBitmap` in V-Ray 7 — test; expectation: none.
- VRayBitmap render-identical memory levers [V] https://documentation.chaos.com/space/VMAX/113575830: tiled OpenEXR / tiled TIFF (`.tx`) "allow only portions of the textures to be loaded at various resolutions" (use `img2tiledexr` / maketx and the "Bitmap to VRayBitmap converter"); "Use full resolution for viewport" viewport checkbox (viewport-only); filtering type changes the image (CHANGES-RENDER). Converting `Bitmap`→`VRayBitmap` is **POSSIBLY-DIFFERENT** (filtering/gamma differences) — not render-identical by default.

**Bitmap Pager** — `IBitmapPager` [V] https://help.autodesk.com/cloudhelp/2017/ENU/MAXScript-Help/files/GUID-F557DED9-D275-4032-AD05-E646A75AACFA.htm ("UI has been completely removed from Preferences; controllable only through MAXScript")
```
IBitmapPager.enabled                 : boolean  default true
IBitmapPager.pageFilePath            : filename
IBitmapPager.memoryPadding_percent   : float 0..1  default 0.2
IBitmapPager.memoryLimit_percent     : float 0..1  default 0.1
IBitmapPager.memoryLimitAutoMode     : boolean  default true ("heuristic")
IBitmapPager.memoryLimit_megabytes / memoryUsedForPager_megabytes / memoryTotalForPager_megabytes /
IBitmapPager.memoryPadding_megabytes / memoryAvailablePadded_megabytes   : integer, read-only
```
No `pageThreshold` property exists in the doc. Render impact **IDENTICAL** (paging, not resampling) — but V-Ray deactivates the pager for the duration of a render (Lele: "BP was active, and … being deactivated for the time it took to render") [F], so it is an interactive lever only. SDK side: `Interface::FreeSceneBitmaps()` [V].

**Nitrous viewport textures/geometry** — `NitrousGraphicsManager` [V] https://help.autodesk.com/cloudhelp/2017/ENU/MAXScript-Help/files/GUID-34892DB6-E840-4F52-9175-30332799B7B1.htm
```
NitrousGraphicsManager.SetTextureSizeLimit <int px> <bool enabled>          -> bool
NitrousGraphicsManager.SetBackgroundTextureSizeLimit <int px> <bool enabled> -> bool
NitrousGraphicsManager.SetProceduralTextureSizeLimit <int px>   -- "Limit the size of these bitmaps to avoid running out of graphics card memory"
NitrousGraphicsManager.ForceDisableMipMapGeneration <bool>      -- "can save significant amounts of graphics card memory"
NitrousGraphicsManager.GetSystemMemoryUsed()                     -> Integer64 bytes "Nitrous System memory usage"
NitrousGraphicsManager.HardwareHitTestEnabled : bool (default true)
NitrousGraphicsManager.ShadowmapSizeLimit : int (512)    BackgroundProgressiveRenderingEnabled : bool (false)
NitrousGraphicsManager.IsEnabled() ; GetActiveViewportSetting() ; GetWorldSetting() ; AntialiasingQuality
```
All **IDENTICAL** (viewport only). Where viewport bitmaps live [I]: Nitrous uploads to VRAM and keeps a system-memory working copy (hence `GetSystemMemoryUsed`); the BitmapManager cache (full-res Max `Bitmap`s) is separate and is what `freeSceneBitmaps()` releases. Per-material toggles [V]: `showTextureMap <material> [<texmap>] <boolean>` (material must be in hardware-display flyout mode) and `<material>.showInViewport`; https://help.autodesk.com/cloudhelp/2015/ENU/MAXScript-Help/files/GUID-BDAD17C6-AC43-4297-8AD3-69F0920ABC59.htm — **IDENTICAL**. Node-level `boxMode`, `xray`, `backFaceCull` — **IDENTICAL**.

### B6. Modifier stack memory and collapsing

- Caching [V]: "3ds Max caches the results of evaluating most-recently used modifiers … To conserve memory use, the list of most-recently used modifiers has a fixed length … By default, the list length is 1" (ini `MRUModSize`). https://help.autodesk.com/cloudhelp/2023/ENU/3DSMax-Basics/files/GUID-80209C3A-C2E4-4541-8738-D1E5ECE16E9C.htm  SDK: each INode keeps a world-space cache "stored as an ObjectState with an associated validity interval"; channels (geom/topo/texmap/select/display) cached separately. https://help.autodesk.com/cloudhelp/2017/ENU/Max-SDK/files/GUID-4B8FAE08-DD17-4D87-9195-0F9C95BA9FC8.htm
- So per node at rest ≈ base object + modifier local data (`ModContext::localData`: Edit Poly / Edit Mesh / Skin / Unwrap keep full per-node copies) + one world-state cache + (interactive only) Nitrous buffers. A user-level rule of thumb from the forums: "each modifier applied … makes the object 3X heavier" [F, unverified] https://superrendersfarm.com/article/optimize-performance-large-3ds-max-scenes. **[I]** collapsing a stack whose result equals its base (pure Edit Poly edits, UVW Map, Normal etc.) removes the duplicate copies → real RAM saving and **IDENTICAL** *provided nothing in the stack is animated, render-dependent or render-time-only*.
- Collapse APIs [V]: `collapseStack <node>` (mapped) — "Collapses the modifiers out of a stack, leaving a resultant editable base object corresponding to the class of the node(s) on top of their stack"; `convertToMesh <node>` — "will always replace the base object with an editable mesh version, even if there are no modifiers present" (geometry and shapes only; `undefined` on failure); `convertTo <node> <class>` (e.g. `Editable_Poly`, `TriMeshGeometry`), `convertToSplineShape`, `canConvertTo <node> <class>`; `maxOps.CollapseNode <node> <bool noWarning>`; `maxOps.CollapseNodeTo <node> <modIndex> <bool noWarning>` ("counted from top of the stack"). https://help.autodesk.com/cloudhelp/2025/ENU/MAXScript-Help/files/3ds-Max-Objects-and-Interfaces/Node-MAXWrapper/Node-Common-Properties-Operators/Node-Common-Methods/GUID-0B7D0519-6059-45CE-BB1D-C8673C12B506.html , …/GUID-D7747871-6C5D-46FD-97A5-5A6A67585E1A.html , maxOps: https://help.autodesk.com/cloudhelp/2026/ENU/MAXScript-Help/files/3ds-Max-Objects-and-Interfaces/Interfaces/Core-Interfaces/Core-Interfaces-Documentation/M/GUID-48C5E2F2-DE34-4EA3-A84C-4DBD463DBF90.html
- Keeping instances intact [F, accepted solution]: `maxops.collapsenodeto node node.modifiers.count off` collapses while preserving instancing (the Utilities-panel Collapse loses it). https://forums.autodesk.com/t5/3ds-max-programming-forum/collapse-modifierstack-and-keep-instances/td-p/10150393 — collapse one representative per `InstanceMgr.GetInstances` group, never `convertToMesh` each instance separately [I].
- Render-dependent modifiers (do **not** collapse, or collapse only after forcing the render state):
  - TurboSmooth [V] https://help.autodesk.com/cloudhelp/2017/ENU/MAXScript-Help/files/GUID-05FAC300-EED9-4999-8B9F-4EE4E87E19A4.htm: `.iterations` (default 1, animatable; "When useRenderIterations is false, this value will be used by the production renderer, too"), `.renderIterations` (default 0, animatable), `.useRenderIterations` (false), `.isolineDisplay`, `.explicitNormals`, `.sepBySmGroups`, `.sepByMats`, `.smoothResult`, `.update` (0 Always / 1 When Rendering / 2 Manually). MeshSmooth has the same `useRenderIterations` [V]. Collapsing at viewport iterations when `useRenderIterations` is true → **CHANGES-RENDER**; collapsing at render iterations is **IDENTICAL** but *increases* RAM (the subdivided mesh is now stored) — so TurboSmooth/MeshSmooth/OpenSubdiv nodes should be left uncollapsed; `update=1` only defers viewport evaluation. Same logic for Displace/Optimize/ProOptimizer with render-only switches, V-Ray displacement modifiers, Hair&Fur, Cloth, etc.
  - Animated stacks: detect via `<node>.isAnimated` and per-sub-anim `.isAnimated` / `.keys.count` (`numSubs`, `getSubAnim`) and controllers on modifier params [I — property names from memory of "SubAnim : Value"/"General Node Properties"; re-verify on the box]; any animated modifier/collapse is **CHANGES-RENDER** for animation, identical for a single still frame only if collapsed at that frame.
- Editable Mesh vs Editable Poly: no official memory numbers. Structural facts [V]: Mesh = triangles (`Mesh` class: verts, `Face` = 3 indices + smoothing group + flags), Poly = `MNMesh` polygons with edge/vertex adjacency [I: MNMesh carries per-vertex edge lists, per-edge records, per-face variable index arrays → roughly 2–3× a TriMesh]. Evidence [F]: converting a 160 k-poly scene from Editable Poly to Editable Mesh shrank the file 25 MB → 15 MB (cgarchitect 2014) https://forums.cgarchitect.com/topic/72040-mesh-editable-poly-file-size/ ; Autodesk forum 2011: a 7 M-poly CAD import consumed 8.5 GB and "legacy Edit Mesh was outperforming Edit Poly" https://forums.autodesk.com/t5/3ds-max-forum/3ds-max-memory-management-edit-mesh-and-edit-poly/td-p/4227975 (login-walled; quotes via search snippet).
- What `convertToMesh` / `convertTo … TriMeshGeometry` changes [V, Edit Normals doc] https://help.autodesk.com/cloudhelp/2020/ENU/3DSMax-Modifiers/files/GUID-75F9A187-305A-49D3-8C66-2C28B43E4BBD.htm: collapsing/"convert to Editable Mesh" *embeds* edited (explicit) normals in the mesh; converting a mesh-based object to Editable Poly *loses* them; poly-based explicit normals survive a collapse to poly. Topology-changing modifiers (Edit Poly, MeshSmooth, Tessellate, Slice, Mirror, Symmetry, Face Extrude, Vertex Weld) and all compound objects strip edited normals. Smoothing groups and map channels are carried by both `Mesh` and `MNMesh` and survive poly↔mesh conversion [I]; quads become triangle pairs (hidden edges) — identical for V-Ray CPU which triangulates anyway **unless** the object has explicit normals (Edit Normals, imported FBX/Datasmith normals, TurboSmooth `explicitNormals`) or V-Ray features that consume poly edges (e.g. VRayEdgesTex "world units"/wire rendering, quad-based subdivision in V-Ray displacement/OpenSubdiv) → **POSSIBLY-DIFFERENT**; treat Editable Poly → Editable Mesh as render-identical only for nodes with no explicit normals and no edge/quad-dependent shading. **[U]** verify with a pixel diff on a test subset.
- Edit Poly *modifier* vs Editable Poly *base*: the modifier keeps its own MNMesh copy in local data, so a stack "Editable Poly + Edit Poly" holds ≥2 meshes [I]; collapsing is **IDENTICAL** when the Edit Poly has no animation and `Show End Result` state is irrelevant.

### B7. Hidden objects, renderability, and measuring memory

- Render of hidden objects [V]: `rendHidden` — "Get/set the renderer's render hidden objects flag. TRUE = on; FALSE = off" (default follows Render Setup › Options › Render Hidden Geometry, off by default). https://help.autodesk.com/cloudhelp/2026/ENU/MAXScript-Help/files/MAXScript-Tools-and-Interaction/Interacting-with-the-3ds-Max/Render-Scene-Dialog/GUID-30AF1E53-5A69-402D-84D6-4D6ECCDD6D20.html (note: "Changing the Render Setup dialog settings via MAXScript should be done with the actual render setup dialog in a closed state"). **[U]** confirm V-Ray 7 honours `rendHidden=false` for V-Ray lights/proxies/Forest objects (it follows Max's common setting; verify).
- Node properties [V] https://help.autodesk.com/cloudhelp/2016/ENU/MAXScript-Help/files/GUID-00AB0CFA-3190-4A28-A185-4774B684F6D8.htm: `.renderable` (true), `.primaryVisibility` ("Visible to Camera"), `.secondaryVisibility` ("Visible to Reflection/Refraction"), `.isHidden` (node OR layer hidden; setting false clears both), `.isHiddenInVpt` (read-only, hidden for any reason), `.isNodeHidden` (node flag only), `.boxMode`, `.backFaceCull`, `.xray`, `.visibility` (animatable), `.castShadows`, `.receiveShadows`, `.renderOccluded`, `.displayByLayer`, `.renderByLayer`.
- Therefore, with `rendHidden == false`: nodes with `isHidden == true` **or** `renderable == false` contribute nothing to the image → deleting them (after checking they are not referenced as Forest/RailClone/tyFlow/V-Ray-proxy sources, instance sources, scatter surfaces, path-constraint targets, light include/exclude lists, or Xref/container members) is **IDENTICAL**. Nodes with `primaryVisibility == false` still affect reflections/shadows/GI → keep. [I]
- Per-object size [V signatures]: `getPolygonCount <node>` → `#(faces, verts)`; `GetTriMeshFaceCount <node>`; `meshop.getNumMaps <mesh>`, `meshop.getMapSupport <mesh> <ch>`, `meshop.getNumMapVerts <mesh> <ch>`, `getNumTVerts`, `polyop.getNumMaps`, `getNumVerts/getNumFaces`. https://help.autodesk.com/cloudhelp/2016/ENU/MAXScript-Help/files/GUID-A97DF4E3-04A7-4A91-AE7C-3C75A8BAA399.htm  No per-object byte counter exists; estimate [I]: TriMesh ≈ 12 B/vert + ~20 B/face + per active map channel (12 B/mapvert + 12 B/face) + 12 B/vert explicit normals; MNMesh ≈ 2–3× — calibrate by `sysinfo.getMAXMemoryInfo()` deltas across merge batches.
- Process-level [V] https://help.autodesk.com/cloudhelp/2021/ENU/3DSMax-MAXScript/files/GUID-CAC36F27-CB51-4C9F-B265-167F636C9A4D.htm:
  - `sysinfo.getSystemMemoryInfo()` → 7 elements: % in use, total physical, free physical, total pagefile, free pagefile, total user address space, free user address space.
  - `sysinfo.getMAXMemoryInfo()` → 9 elements: PageFaultCount, PeakWorkingSetSize, WorkingSetSize, QuotaPeakPagedPoolUsage, QuotaPagedPoolUsage, QuotaPeakNonPagedPoolUsage, QuotaNonPagedPoolUsage, PagefileUsage, PeakPagefileUsage (= Win32 `PROCESS_MEMORY_COUNTERS`). Use `[2]` (peak WS) and `[3]` (WS) after `gc()`.
  - `sysinfo.getMAXHeapUsed()` (2021+), `sysinfo.getMAXHandleCount()/getMAXUserObjectCount()/getMAXGDIObjectCount()` (2019.3+), `sysinfo.cpucount`.
  - `heapSize` (Integer64, settable) / `heapFree` — MAXScript's own heap only [V] https://help.autodesk.com/cloudhelp/2018/ENU/MAXScript-Help/files/GUID-D5E7028F-51A9-4B64-B71E-C8C694136089.htm
  - `NitrousGraphicsManager.GetSystemMemoryUsed()` [V].
- Python in Max 2026 [V]: Python **3.11.9** (2025 = 3.11.4, 2025.1 = 3.11.8, 2024 = 3.10.8). https://help.autodesk.com/cloudhelp/2026/ENU/MAXDEV-Python/files/MAXDEV_Python_what_s_new_in_3ds_max_python_api_html.html  Installing packages [V] https://help.autodesk.com/cloudhelp/2026/ENU/MAXDEV-Python/files/MAXDEV_Python_python_extension_libraries_html.html: from `[3ds Max Install]\Python`: `.\python.exe -m ensurepip --upgrade --user` then `.\python.exe -m pip install --user psutil` ("Make sure you are executing the 3ds Max python.exe … `--user` installs in your user directory rather than the 3ds Max Python directory" → `%APPDATA%\Python\Python311\site-packages`). psutil ships cp311 wheels, so no compiler needed. Fallback without pip: `ctypes.windll.psapi.GetProcessMemoryInfo` (same counters as `getMAXMemoryInfo`) or `rt.sysinfo.getMAXMemoryInfo()`. GPU: `nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader` via `subprocess`; per-process: `nvidia-smi --query-compute-apps=pid,used_memory --format=csv`.

### B8. Instances

[V] https://help.autodesk.com/cloudhelp/2015/ENU/MAXScript-Help/files/GUID-EB69A035-7FF4-4A0A-8A82-409C4145B9DF.htm
```
<DWORD> InstanceMgr.GetInstances <node>source <&node array>instances     -- count + array (includes source)
<bool>  InstanceMgr.MakeObjectsUnique <&node array>nodes <enum>{#prompt|#individual|#group}
<bool>  InstanceMgr.CanMakeObjectsUnique <&node array>
<bool>  InstanceMgr.MakeModifiersUnique <&nodes> <&maxObject array>modifiers <enum>
<bool>  InstanceMgr.MakeControllersUnique <&nodes> <&controllers> <enum>
<DWORD> InstanceMgr.SetMtlOnInstances <node> <material>
InstanceMgr.autoMtlPropagation : bool
```
- Instances share one base object and modifiers (same `node.baseObject` / `getHandleByAnim`), so mesh memory is shared [V by definition]; `instanceReplace <dest> <src>` turns copies into instances (keeps transform/material/name) [V] — **IDENTICAL** only if the meshes are bit-identical (compare `getPolygonCount`, hash of verts/faces/maps). Nitrous buffer sharing across instances **[U]** (irrelevant in batch if no viewport is built).

### B9. Scatter / plug-in display modes

- **Forest Pack** (iToo docs, Display rollout) [V] https://docs.itoosoft.com/forestpack/forest-plugin/display — Viewport group: Mesh / Adaptive (switches when faces exceed N) / Proxy (Planes, Pyramid, Box, Thin Box, Plant, Arrow) / Points Cloud (≤ 250 000 points per object, GPU-cached; `forestpack.ini` keys `cloudPointsByObject`, `cloudHitTestMaxPoints`, `cloudSubSelDensity`, `cloudSubUnselDensity`); "Max Items" (viewport draw limit); Manual Update; Freeze All; "Disable Object — When enabled the object is disabled in the viewports **and the render**". Render group: Mesh/Adaptive/Proxy (render geometry!), Simplification Level, Render Mode Automatic/Meshes, Max Items / Max Faces ("Max Faces is ignored in Corona, V-Ray, Arnold … in Automatic mode"), Hide Custom Objects before rendering. Classification: **viewport group = IDENTICAL**; anything in the Render group, Disable Object, Simplification Level = **CHANGES-RENDER**. MAXScript property names for these params are **not documented** on the iToo site; the documented discovery route is `showProperties $forest` / `for p in (getPropNames $) …` and `showInterfaces $forest` (exposes the `trees` interface, e.g. `$.trees.create p:<point3> width: height: geomid:`) [V/F] https://forum.itoosoft.com/forest-pro-(*)/maxscript-questions/ — **[U] dump `getPropNames` on the box and map the Display/Render params**.
- **RailClone** [V] https://docs.itoosoft.com/railclone/display-settings — Viewport: Mesh / Boxes / Adaptive / Points-Cloud / Quick Mesh (instanced viewport display; "improved memory management"); registry `HKCU\SOFTWARE\Itoo Software\RailClone Pro` `cloudPointsByObject`, `cloudHitTestMaxPoints`. Render: Mesh / Boxes / Use Instancing Engine (**CHANGES-RENDER** if toggled). Proxy Cache (Embedded / External `.rcproxy`) bakes the result — "Note that this does not affect the render" for its viewport limitation; `$.setProxyMode <0|1|2> <externalFile>` (0 Disabled, 1 Embedded, 2 External) — **IDENTICAL** with the caveat "Results may vary depending on the render engine that was active when the cache was baked" → bake under V-Ray and re-verify. Limits (Segments 50 000 / Faces 5 M) **CHANGES-RENDER** if lowered.
- **tyFlow** [V] https://docs.tyflow.com/tyflow_particles/operators/display/ and https://docs.tyflow.com/tyflow_maxscript/ — viewport drawing is the per-event **Display operator** (types Pixels, Small/Large dots, Ticks, Sprites, Bounding Boxes, Geometry; "Percent %" of particles drawn; Mapping Overrides → "Ignore (instancing: YES)" avoids "a million different meshes" being built for the viewport). These are viewport-only → **IDENTICAL**. MAXScript: `tf.addEvent()`, `[event].addOperator "Display" -1`, `[operator].setEnabled`, parameters accessed as properties (e.g. `frc.gravityStrength`); `[obj].reset_simulation()` clears the cache (**CHANGES-RENDER** unless re-simulated); `tyFlow_found_in_scene()`, `tyFlow_print_scene_load_time`. Display-operator property names are **[U]** (read via `getPropNames` on the operator).
- **VRayProxy** [F, Chaos docs + ScriptSpot]: `.display` viewport mode (0 bounding box, 1 preview from file (edges), 2 point, 3 preview from file (faces); docs list Bounding box / From file (edges) / From file (faces) / Point / Whole mesh) — viewport only → **IDENTICAL**; lowest-RAM interactive setting is bounding box. https://documentation.chaos.com/space/VMAX/113575423 **[U]** confirm exact enum indices in V-Ray 7.
- **Phoenix FD / GrowFX**: not researched to source level — Phoenix "Preview" rollout (GPU preview, voxel/particle preview) is viewport-only; GrowFX has a viewport "mesh/wire" mode. **[U]**.

### B10. Save-level cleanups

- Official Autodesk "Performance Issues" page lists the three memory-freeing calls [V] https://help.autodesk.com/cloudhelp/2026/ENU/3DSMax-Reference/files/GUID-5704C695-6270-4273-B178-88DA8E26689D.htm: `gc()` (garbage collection; `gc light:true` keeps MAXWrapper values/undo; `gc delayed:true` full GC at next undo flush — https://help.autodesk.com/cloudhelp/2023/ENU/MAXScript-Help/files/MAXScript-Language-Reference/Variables-Assignment-and-Scope/Memory-Allocation-and-Garbage/GUID-CA7ABB4B-6CB6-47B7-903D-84D2F45D993D.html), `freeSceneBitmaps()` ("Frees up all the memory used by the image file bitmap caches" — https://help.autodesk.com/cloudhelp/2015/ENU/MAXScript-Help/files/GUID-53F8D6CE-A5A6-4353-BE48-4298BBE107DE.htm), `clearUndoBuffer()` ("Empties the Undo stack" — https://help.autodesk.com/cloudhelp/2015/ENU/MAXScript-Help/files/GUID-C8517C4A-A75F-4D4E-A058-BE3EE61A2A11.htm; wrap batch edits in `undo off (...)` because "it is quite easy to fill up the Undo stack and consume substantial amounts of memory"). All **IDENTICAL**. SDK: `Interface::FlushUndoBuffer()`, `Interface::FreeSceneBitmaps()` [V].
- `disableSceneRedraw()` / `enableSceneRedraw()` and `with redraw off` — viewport only, moot in batch [V].
- Compress on save [V/F]: `setINISetting (getMAXIniFile()) "Performance" "WriteCompressed" "1"` (Preferences › Files › Compress on Save); affects **file size only**, not RAM; may need admin rights / takes effect for subsequent saves. https://forums.autodesk.com/t5/3ds-max-programming-forum/switching-quot-compress-on-save-quot-preference-via-maxscript/td-p/4280236 , https://help.autodesk.com/view/MAXDEV/2023/ENU/?guid=GUID-CF408D64-D4E2-4C39-90DB-62E525D7B45A
- `saveMaxFile` options above; `saveAsVersion:` down-saves (2026 → 2025 etc.) — file-format only.
- Spurious global data that merges drag along and that inflates RAM [F]: note tracks (`hasNoteTracks`/`deleteNoteTrack` family), animation layers, Motion Mixer tracks (`for i = 1 to theMixer.numMaxMixers() do theMixer.removeMaxMixer 1 false 1`), scene states, empty layers; Autodesk ships "Scene Cleaner" scripts for this. https://forums.cgarchitect.com/topic/62325-3ds-max-memory-error-some-max-files-using-all-available-memory/ , https://forums.autodesk.com/t5/3ds-max-forum/slow-merge-3ds-max/td-p/9406313 — **IDENTICAL** (non-rendering data) except animation layers/mixer if they drive rendered animation.
- Unused materials: `sceneMaterials` only lists materials assigned in the scene; material-editor slots (`meditMaterials`) and the scene material library hold references — clear with `for i = 1 to 24 do meditMaterials[i] = Standard()` / `clearMaterialLibrary()` [I] — **IDENTICAL**.
- Rendered Frame Window: `rendShowVFB` [V] (rendSaveFile/rendOutputFilename) — disabling the VFB saves a frame buffer's worth of RAM (cgtricks: "200 to 300 meg") — **IDENTICAL** when writing to file.

### B11. RAM-per-polygon data points (all non-official)

- "Wireframe viewport … 100MB for every 1 million polygons" saving claim (Max 2008-era) [F] https://cgtricks.com/save-memory-when-working-with-big-scenes-3dsmax/
- 7 M polygons (CAD via Maya) → 8.5 GB incl. textures on load, Max 2012 [F] https://forums.autodesk.com/t5/3ds-max-forum/3ds-max-memory-management-edit-mesh-and-edit-poly/td-p/4227975
- Editable Poly → Editable Mesh: 25 MB → 15 MB file at 160 k polys; 454 k polys as mesh = 21 MB [F] https://forums.cgarchitect.com/topic/72040-mesh-editable-poly-file-size/
- Nitrous VRAM/system memory per vertex: **no published figure** [U]; only `NitrousGraphicsManager.GetSystemMemoryUsed()` for empirical measurement. V-Ray proxy preview memory: bounding-box mode ≈ 0; "preview from file" loads the preview mesh stored in the .vrmesh (user-set face count at export) [I].

---

## C. Render-impact matrix (V-Ray CPU, still frames)

| Lever | API | Impact | Notes |
|---|---|---|---|
| Delete hidden / non-renderable nodes (`rendHidden=false`) | `.isHidden`, `.renderable`, `delete` | IDENTICAL | guard against referenced sources (scatter, instances, constraints, include/exclude lists) |
| Collapse non-animated, non-render-dependent stacks | `maxOps.CollapseNodeTo n n.modifiers.count off` | IDENTICAL | keep instances; skip TurboSmooth/MeshSmooth/Displace/V-Ray displacement/Hair/Cloth |
| Collapse TurboSmooth at render iterations | set `.iterations = .renderIterations` then collapse | IDENTICAL but RAM ↑ | do not use as a memory lever |
| Editable Poly → Editable Mesh | `convertToMesh` / `convertTo n TriMeshGeometry` | POSSIBLY-DIFFERENT | identical unless explicit normals / edge-dependent shading; pixel-diff a sample |
| Make duplicate meshes instances | `instanceReplace` | IDENTICAL if bit-identical meshes | hash-compare first |
| Bitmap proxies, render mode UseProxies | `BitmapProxyMgr.globalProxyRenderMode = #renderMode_UseProxies` | CHANGES-RENDER | |
| Bitmap proxies, FullRes_FlushFromMemory | `#renderMode_UseFullRes_FlushFromMemory` | IDENTICAL | Max `Bitmap` texmaps only; V-Ray reloads full res at render |
| Bitmap pager | `IBitmapPager.enabled` | IDENTICAL | V-Ray disables it during render → interactive only |
| Tiled EXR/.tx via VRayBitmap | converter script | POSSIBLY-DIFFERENT | Chaos-recommended RAM lever but not bit-identical (filtering) |
| Nitrous texture limits, mipmap off, showTextureMap off, boxMode | `NitrousGraphicsManager.*`, `showTextureMap` | IDENTICAL | viewport only; probably moot in batch |
| Forest/RailClone viewport modes, point cloud, Max Items (viewport) | plugin props ([U] names) / `$.setProxyMode` | IDENTICAL | |
| Forest/RailClone Render group, Disable Object, limits | | CHANGES-RENDER | |
| tyFlow Display operator type/percent | operator props | IDENTICAL | `reset_simulation` is not |
| VRayProxy `.display` | | IDENTICAL | |
| XRef scene disabled / `#noLoad` | `xrefs.*` | CHANGES-RENDER (does not render) | enabled = full RAM at render |
| `gc()`, `freeSceneBitmaps()`, `clearUndoBuffer()`, `undo off` | | IDENTICAL | |
| Note tracks / mixer / anim layers cleanup | scene cleaner scripts | IDENTICAL (stills) | |
| Compress on Save, `saveAsVersion` | `setINISetting … "Performance" "WriteCompressed"` | IDENTICAL (file size only) | |
| `rendShowVFB=false` | | IDENTICAL | |

---

## D. Unknowns to verify on the box (ordered by how much they change the design)

1. **Peak RAM of a selective `mergeMAXFile`** from the 170 GB file (merge one tiny node; log `sysinfo.getMAXMemoryInfo()[2]` peak WS). If peak ≈ full-open peak, batching only controls steady-state; consider alternative ingestion (XRef-object load of subsets, or `saveNodes` from a machine that can open it, or FBX/USD round-trip — not render-identical).
2. **`getMAXFileObjectNames` cost** on the big file: time + peak working set; whether it lists hidden nodes and how duplicate names are returned; whether `mergeByHandles:true` can be fed from anything other than a prior merge.
3. **Does 3dsmaxbatch build Nitrous buffers / touch VRAM** for a loaded scene (`NitrousGraphicsManager.GetSystemMemoryUsed()`, `nvidia-smi`), and does `-i custom.ini` with a Nitrous-Software (`GFXType`) setting change anything. Exit -7 on a headless/RDP box is a risk to test.
4. **License**: whether a 3dsmaxbatch run checks out a 3ds Max seat (license-server log / Autodesk Access); V-Ray render license behaviour if you render test frames.
5. **V-Ray 7 vs Max Bitmap Proxies**: confirm `VRayBitmap` ignores `BitmapProxyMgr`, and that `#renderMode_UseFullRes_FlushFromMemory` produces a bit-identical render for Max `Bitmap` texmaps.
6. **Render-identity of Poly→Mesh** on a sample (pixel diff), especially nodes with explicit normals, Edit Normals, VRayEdgesTex, displacement; confirm `convertToMesh` preserves map channels 1..n and smoothing groups.
7. **Forest Pack / tyFlow / VRayProxy MAXScript property names** for display modes (`getPropNames`, `showInterfaces`); confirm VRayProxy `.display` enum.
8. **`rendHidden`** is respected by V-Ray 7 for all node classes (V-Ray lights, VRayProxy, Forest, Phoenix).
9. **Memory counters**: calibrate bytes/face for Editable Mesh vs Editable Poly vs Edit Poly stacks on this scene (delta of `getMAXMemoryInfo()[3]` after `gc()` around representative merges) to drive the ≤ 70 GB budget planner.
10. **`isAnimated`/key detection** property names on nodes, modifiers and sub-anims in 2026 (re-verify `<node>.isAnimated`, `subAnim.isAnimated`, `.keys.count`).
11. **Duplicate node names in the source**: what `mergeMAXFile file #("Name")` does when several nodes share "Name" (all? first?) — affects batch planning; `mergeByHandles` as the fix.
12. **Python `--user` site-packages** are visible to the 3dsmaxbatch-spawned interpreter (it should be, same `python.exe`), and psutil imports inside Max; otherwise fall back to `ctypes`/`sysinfo`.
