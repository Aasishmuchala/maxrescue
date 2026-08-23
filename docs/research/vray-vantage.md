# V-Ray 7 (3ds Max 2026) + Chaos Vantage 3.3.1 — memory behaviour research

Target: take an archviz scene from ~170 GB RAM / 29 GB VRAM open → ≤70 GB RAM open (so V-Ray CPU can render inside a 128 GB box) and ≤32 GB VRAM, using only render-identical operations.

Research date: 2026-08-23. Docs were read from the Confluence mirror `docs-chaos.atlassian.net/wiki/spaces/{VMAX,LAV}/pages/<id>` (same page IDs as `documentation.chaos.com/space/{VMAX,LAV}/<id>`; the old `docs.chaos.com/display/...` URLs 301 to the JS app). Raw page dumps are in this directory (`adf_*.txt`, `vmax_*.txt`, `lav_*.txt`, `f_*.txt`).

Confidence key: **[V]** verified in official docs / Chaos staff post · **[I]** inferred from verified facts · **[U]** unknown, must be verified on the box.
Render-impact key: **IDENTICAL** · **POSSIBLY DIFFERENT** · **CHANGES RENDER**.

---

## 0. Executive summary (what actually moves the needle)

| Lever | Open-scene RAM | VRAM (Nitrous) | VRAM (Vantage) | V-Ray CPU render RAM | Render impact |
|---|---|---|---|---|---|
| Convert heavy Editable Mesh/Poly → VRayProxy (.vrmesh), one object per file, display = bounding box | Large win: geometry leaves the Max scene; only preview (bbox = ~0) stays [V] | Large win (bbox uploads nothing; preview faces upload only the preview) [I] | **None** — Vantage loads proxies fully; "no difference between mesh and proxy" [V] | Proxies become *dynamic* geometry: loaded on demand through the dynamic raycaster pool; identical instances share memory [V] | IDENTICAL if exported per-object from a clean (no negative scale, modifiers collapsed) mesh and material stays on the node; **POSSIBLY DIFFERENT** for mirrored/negative-scale nodes, multi-object merges, modifiers left on proxy, explicit normals (see §1.5) |
| Bitmap → VRayBitmap (+ .tx) | Moderate win only if Max's Bitmap keeps full-res images loaded for the viewport; VRayBitmap loads lazily at render time [V] | Win via Nitrous texture-size limit (independent of loader) [V] | Small/none: Vantage mip-maps all textures itself and only uploads needed levels when *Enable dynamic textures* is on [V] | Win: tiled .tx loads only needed tiles, shared with the dynamic-memory cache (or `VRAY_TEXTURE_CACHE`) [V] | **POSSIBLY DIFFERENT** — filtering kernel changes (Max pyramidal/summed-area → V-Ray isotropic/elliptical/sharp-isotropic), interpolation & gamma rules differ (§3.4). Not bit-identical by design; must be visually validated. |
| Nitrous `SetTextureSizeLimit` / Display Performance limits | Small RAM win (viewport copies) [I] | Direct VRAM win [V] | None [V] | None [V] | IDENTICAL (viewport-only) [V] |
| Proxy preview cache / deferred proxy previews (V-Ray 7.2+) | Faster load, less viewport RAM [V]; numbers [U] | Some [I] | None | None | IDENTICAL |
| Vantage: Enable dynamic textures, Texture LOD bias, disable bump/normal maps, lower displacement tessellation, fewer render elements, Safe GPU allocation off | n/a | n/a | The only documented VRAM levers [V] | n/a | Dynamic textures/LOD bias: changes Vantage image (mip level) but not V-Ray CPU; displacement/bump toggles CHANGE Vantage render |
| V-Ray CPU render: bucket sampler, BF GI instead of LC, disable Memory frame buffer + raw .vrimg output, Embree "Conserve memory", `system_raycaster_memLimit`, `VRAY_TEXTURE_CACHE`, `VRAY_TEXTURES_LOAD_16BIT_AS_8BIT` | n/a | n/a | n/a | Each documented as a RAM reducer [V] | Bucket vs progressive: sampling differs; LC→BF: CHANGES RENDER; memory FB off + raw file: IDENTICAL pixels; Conserve memory: IDENTICAL geometry (storage only) [I]; mem limits: IDENTICAL (affects only residency) [V]; 16→8-bit: CHANGES RENDER slightly |

Budget justification (§2.6): V-Ray's memory tracker counts *only V-Ray's* allocations; 3ds Max's own scene copy (meshes, textures for viewport, undo) sits on top of it. Chaos staff: a scene at 20 GB idle hit 49 GB during render while the tracker said 15 GB peak (forum 1157982) — i.e. render RAM ≈ (Max-open RAM) + (V-Ray frame peak), not N × scene RAM. The 70 GB open target therefore leaves ≈ 50–55 GB for V-Ray's own peak (static Embree BVH + dynamic pool + textures + VFB + LC) on a 128 GB box — reasonable if the heavy geometry is in proxies (dynamic, capped) and textures are tiled (cached, capped).

---

## 1. V-Ray Proxy (VRayProxy / .vrmesh)

Sources: VRayProxy page `VMAX/113575423` · Working with vrmesh Files `VMAX/113575435` · MAXScript `VMAX/113575613` · Proxy Preview Cache `VMAX/113587893` · Environment Variables `VMAX/113586786`.

### 1.1 What a .vrmesh stores and how it is rendered [V]
- "This file contains all geometric information for a mesh such as vertices and face topology as well as texture channels, face material IDs, smoothing groups, and normals. In short, everything that is needed to render the mesh is included in the file." (`VMAX/113575423`)
- "It can also include point cloud data. **Materials are not saved in the file.** Instead, the geometry is rendered with the material applied to the VRayProxy object." (`VMAX/113575435`)
- "VRayProxy imports a geometry from an external mesh at render time only. The geometry is not present in the scene, and does not use up any resources, except for the viewport preview."
- "the mesh is preprocessed and subdivided into chunks for easier access. Also, V-Ray automatically regards proxies that load the same .vrmesh files like instanced duplicates in the scene to save memory when those proxies have the same settings."
- "When a modifier is added, the proxy loses its memory-saving properties and is regarded as a regular mesh in the scene." → never leave modifiers on proxies. "The Show whole mesh display type should be used for the VRayProxy if modifiers are applied."
- Explicit normals: "In cases where the original mesh has explicit information about vertex normals, this information is also saved in the .vrmesh file when the mesh is exported." (Import section; `Use explicit normals` on re-import.)
- Chunking on export can duplicate vertices across chunks ("V-Ray might split the original mesh into smaller chunks that can be loaded independently of one another. This can cause some vertices to be duplicated in more than one chunk.") — relevant to Random-by-element texturing on re-import, not to render of the proxy itself.

### 1.2 `vrayMeshExport` — documented V-Ray 7 signature [V] (`VMAX/113575613`)
```
vrayMeshExport
  [meshFile: "<mesh file>"]
  [autoCreateProxies: true|false]        -- default: false (meshes just exported)
  [exportMultiple: true|false]           -- default: true (one file per node)
  [animation: on|off]
  [animationRange: #scene|#explicit]
  [animationStart: <integer>] [animationEnd: <integer>]
  [animationRedrawViews: true|false]
  [minPreviewFaces: <integer>]
  [maxPreviewFaces: <integer>]           -- default 10000 triangles
  [previewFacesPercentage: <integer>]
  [previewMesh: <preview node>]          -- only valid when exportMultiple:false
  [nodes: <object/array of objects/objectset>]
  [proxyName: "<string>"]                -- only valid when autoCreateProxies:true
  [createMultiMtl: true|false]
  [condenseMultiMtl: true|false]
  [facesPerVoxel: <integer>]
  [oneVoxelPerMesh: true|false]          -- "Optimize for instancing"
  [exportPointClouds: true|false]
  [pointSize: <float>]                   -- "Lowest level point size"
  [previewType: <string>]                -- "clustering" | "combined" | "face_sampling"
```
- "Exports a .vrmesh file without showing a dialog. Returns an array of newly created VRayProxy object(s), if any." Returns `ok`/`undefined` when not creating proxies (`VMAX/113575435`).
- `doVRayMeshExport()` opens the dialog; `vrayMeshImport [proxy:] [explicitNormals:] [weldVertices:]` is the inverse.
- Parameters asked about that **do not exist in the 3ds Max API**: `exportMultiple` ✓ exists; `autoCreateProxies` ✓; `animOn`/`animType` → `animation:` / `animationRange:`; `previewFaces` → `maxPreviewFaces`/`previewFacesPercentage`; `pointCloudOn`/`pointCloudLevel` → `exportPointClouds`/`pointSize`; `includeMaterials`, `vertexColorsOn`, `smoothingGroups`, `compressed` → **not present** (those are V-Ray for Maya `vrayCreateProxy` flags). Vertex colours / smoothing groups are always written (they are "texture channels"/"smoothing groups" in the file). [V]
- Old-style positional form `vrayMeshExport meshfile:"..." autoCreateProxies:true exportMultiple:false createMultiMtl:true` is used in Chaos forum scripts (forum 70709) and in the support article "Using MAXScript with V-Ray" (`vraymeshexport meshfile:(path)`); keyword names are case-insensitive in MAXScript.
- Dialog semantics (`VMAX/113575435`):
  - "Export each selected object in a separate file – … The transformation of an object is not included in its mesh file, and the corresponding proxy must have the same transformation as the original object … the object's pivot point to be preserved."
  - "Export all selected objects in a single file – … also stores the transformations of the selected objects. When importing the file with a proxy object, it must be centered at the origin … all meshes from the file render with that material. You must use sub-object materials and different material IDs if you want the meshes to have different materials."
  - "Automatically create proxies – … The proxies will have the correct transformations and materials derived from the original objects. The original objects will be deleted."
  - "Create Multi/Sub-Object Mtl – available when multiple objects with different materials are exported. When enabled, V-Ray assigns a Multi/Sub-Object material to the created proxy and the sub-materials are assigned to the appropriate faces automatically." "Condense Multi Mtl – … only unique materials are exported."
  - "Export point clouds – adds a point cloud representation of the mesh separated into different levels of detail … for saving memory when rendering distant objects." (render-changing LOD — see 1.4)
  - Preview type: "Vertex Clustering (fast)", "Refined Clustering (quality)", "Face skipping (very fast)".

### 1.3 VRayProxy object parameters (UI names) [V] (`VMAX/113575423`)
- **Mesh file**, **Preview override** (separate .vrmesh/.abc used only for viewport display "when the Display mode is set to preview from file (edges), preview from file (faces), or show whole mesh"), **Expand # to frame number**.
- **Display / Preview type**: `bounding box` ("Represents the mesh as a box"), `preview from file (edges)`, `preview from file (faces)`, `point` ("Represents the mesh as a point"), `show whole mesh` ("Previews the entire mesh"). Note: "The proxy preview cache depends on the chosen Display type."
- **Preview level** (Mudbox subdivision levels), **LOD Scale** ("maximum number of pixels an edge can span over before it is subdivided … control the loaded vrmesh level for rendering" — render-affecting for multi-level meshes), **Scale**, **Flip axis** ("switches between the Y and Z-axis of the proxy being used as a vertical axis … when the proxy was not exported from 3ds Max").
- **Point cloud**: `Use if present`, `Level multiplier` ("0.0 means that no point cloud level is loaded and the original mesh is rendered instead"; 1.0 = by distance).
- **Compatibility**: `Force first map channel`, `First map channel` (vrmesh/abc from other apps store UVs in channel 0 = Vertex Color Channel).
- **Cosmos Asset rollout** (Cosmos assets are VRayProxy objects; "Note that the Cosmos geometry is imported as VRayProxy" `VMAX/113587032`).
- **Alembic**: `Recompute bounding box`, `Instancing` ("speeds up and optimizes the memory during rendering of alembic files which contain instances") — .abc only.
- MAXScript property names: `fileName` [V — support article `4411319570577`], `display` with `0 = bounding box` [V — ScriptSpot thread, Lele's script], remaining enum values [U]. Web snippets claim `1 = preview from file, 2 = point, 3 = preview faces` but V-Ray 7.x notes include a fix "VRayProxy preview types edges and faces are showing swapped results" (`VMAX/113580676`), so **enumerate with `showproperties VRayProxy` on the box**. Other names (`scale`, `flip_axis`, point-cloud, LOD) [U].
- Caveat from V-Ray 6 notes: "Explicit normals are changed to Unspecified for modified Proxy nodes" was a fixed bug; "Cosmos models crash 3ds Max if a value of zero is used in the VRay Proxy scale" fixed in 7.

### 1.4 RAM cost of a proxy inside 3ds Max, per display mode
- Docs: the only in-scene cost is "the viewport preview" [V]. Bounding box / point → essentially a node + transform [I]. Preview from file → the stored preview mesh (default ≤10,000 tris, `maxPreviewFaces`) [V]. Show whole mesh → the full mesh is read into Max (and required if modifiers are applied) [V].
- **Proxy preview cache** (`Render Setup > Settings > Proxy preview cache`, `VMAX/113587893`): "When enabled, viewport preview information for all VRayProxy objects in the scene is cached for every frame … Reset cache – Clears all cached information." [V]. MAXScript name [U].
- **V-Ray 7.2 (7.20.00, Aug 2025)**: "Deferred loading of the VRayProxy viewport previews"; V-Ray 7 notes: "Improve loading time for V-Ray Proxy using a preview override cache" [V]. Exact behaviour/option name [U] — verify whether previews are loaded lazily on first draw (affects measured open-scene RAM).
- No official bytes-per-proxy numbers exist [U].

### 1.5 Render-identity of export→proxy and known pitfalls
| Item | Finding | Confidence | Impact |
|---|---|---|---|
| Smoothing groups / normals / UVs / per-face material IDs | All stored in .vrmesh; "everything that is needed to render the mesh" | [V] | IDENTICAL (for a clean mesh) |
| Material | Not stored; proxy renders with node material; auto-create keeps original material | [V] | IDENTICAL when per-object export keeps the node's material |
| Multi-object merge into one file | Material IDs are **not** re-ordered/condensed unless `createMultiMtl`; historically only one material applied (forum 51734, Vlado: "it will not create new sub/object materials nor reorder the material IDs for you") → use per-object export (`exportMultiple:true`) | [V] | POSSIBLY DIFFERENT if merged |
| Mirrored / negative-scale nodes | Forum 51734 (Chaos: "the meshes are mirrored, probably have their normals in local space reversed. You would have to xform it before exporting"); Maya thread 995498 same issue | [V] (old) | POSSIBLY DIFFERENT — flipped faces/normals. Mitigation: export in object space per object and keep node transform, or Reset XForm + flip before export; must be validated |
| Modifiers on source | Exporter bakes the evaluated mesh at export time; leaving modifiers on the **proxy** makes it a regular mesh again | [V] | IDENTICAL geometry if modifiers are collapsed/baked at the export frame; animated modifiers need animated export |
| Displacement | VRayDisplacementMod on a proxy works only via "Use object mtl" + distinct material IDs + mapping (docs workflow); displaced objects are always dynamic geometry | [V] | POSSIBLY DIFFERENT if displacement was applied by a modifier in object stack: reconstruct on the proxy and compare |
| Vertex colours | Stored as a texture channel (channel 0 = vertex color channel) | [V] | IDENTICAL |
| Instances | Same file + same settings → treated as instances by V-Ray at render ("to save memory") | [V] | IDENTICAL |
| Point cloud export + `Use if present` | LOD replaces distant geometry with disks | [V] | CHANGES RENDER — leave `Level multiplier = 0` / do not enable |
| Explicit normals | Stored; V-Ray 6 fixed "Explicit normals are changed to Unspecified for modified Proxy nodes" | [V] | POSSIBLY DIFFERENT on 6.x; verify on 7.x |
| Viewport normals env var `VRAY_MESHPROXY_VIEWPORT_NORMALS_SIMPLE_SMOOTH` | Viewport only ("differs from the render appearance") | [V] | IDENTICAL |
| Shadow maps | "Standard shadow maps do not include information about the proxy objects" — use VRayShadow | [V] | CHANGES RENDER only for legacy shadow-map lights |
| Motion blur on moving proxies | 7.x fix "Incorrect normals for dynamic moving mesh geometry when rendering with motion blur" | [V] | POSSIBLY DIFFERENT on older builds |

---

## 2. V-Ray CPU render memory (V-Ray 7)

Sources: System rollout `VMAX/113587447` (current) and its 2021 archived version (Wayback, `docs.chaos.com/display/VMAX/System`), Environment Variables `VMAX/113586786`, Frame Buffer Settings `VMAX/113575651`, Light Cache Settings `VMAX/113575669`, support article 4412978786833, forum 1141266 (Lele, Chaos), 1157982 (Chaos staff).

### 2.1 Static vs dynamic geometry; dynamic memory limit
- 2021 docs (archived) — definitions [V]: "Default geometry – … This parameter determines the type of geometry for standard 3ds Max mesh objects. Note that some objects (displacement-mapped objects, VRayProxy, and VRayFur objects, for example) always generate dynamic geometry, regardless of this setting. Static – All geometry is precompiled into an acceleration structure at the beginning of the rendering … The static raycasters are not limited in any way and will consume as much memory as necessary. Dynamic – Geometry is loaded and unloaded on the fly … controlled by the Dynamic memory limit parameter. Auto – … V-Ray makes the decision on which type to use based on the face count for an object and the number of its instances in the scene."
- "Dynamic memory limit – The total RAM limit for the dynamic raycasters, which store dynamic geometry, such as displacement and VRayProxy objects. Set this to 0 to remove any limit (V-Ray takes as much memory as needed). The memory pool is shared between the different rendering threads. Therefore, if geometry needs to be unloaded and loaded too often, the threads must wait for each other and the rendering performance suffers." [V]
- MAXScript: `renderers.current.system_raycaster_memLimit` (int, MB; `0` = unlimited; negative = "keep that much physical RAM free" — Vlado, forum 51530, V-Ray 2.3, "experimental") [V name; negative semantics in 7.x = U]. A 2023 snippet dump shows `vr.system_raycaster_memLimit = 0`, `vr.system_embree_on = true`, `vr.system_embree_hair = false` [V]. Default geometry property name [U] (probably `system_raycaster_defaultGeometry`; confirm with `showproperties`). Default value of the limit in V-Ray 7 [U] — historically 400 MB (V-Ray 1.5) / 4000 MB; the 2023 dump shows 0.
- **Important**: the **current** (2026) System page no longer documents *Dynamic memory limit* or *Default geometry* — only Embree "Conserve memory" remains in the Advanced list. Chaos (Lele, Mar 2022, forum 1141266) still described them in "System tab → advanced mode, right below memory limit". Whether the V-Ray 7 UI hides them or only the docs dropped them is [U]; the MAXScript property should still exist (verify).
- Lele (Chaos), forum 1141266 [V]: "The dynamic memory pool is *exclusively* for dynamic geometry by default: proxies, or rendertime displacement. You can change this behavior by setting the 'Default Geometry' to 'Dynamic' … that will make *all* the geo dynamic, and obeying the memory limit. a) Not all setups may obey this (f.e., fPro may do its own thing.) b) Rendering will be a *lot* slower than usual by virtue of the unloading from RAM … However, you won't swap to disk."
- Bucket sequence: "the default Triangulation sequence is best if you use a lot of dynamic geometry (displacement-mapped objects, VRayProxy or VRayFur objects) since it walks through the image in a very consistent manner so that geometry that was generated for previous buckets can be used for the next buckets." [V]
- Frame-stamp keyword `%primitives`: "With the dynamic raycaster, only geometry that is actually needed is generated and accounted for." — usable as a cheap counter in automated tests. [V]

### 2.2 Embree
- "By default, V-Ray uses Intel Embree raycaster." "Conserve memory – Embree will use a more compact method for storing triangles, which might be slightly slower but reduces memory usage." [V] (`VMAX/113587447`). MAXScript `system_embree_on` [V]; conserve-memory property name [U].
- V-Ray 7 notes: "Improve memory usage for static geometry"; "Slow memory release (single threaded) and incremental render time added with a huge Forestpro mesh instance count on multicore machines" (fixed) [V].
- Out-of-memory symptom to grep in the log: `Building Embree Voxel Tree failed: [EmbreeTree<…>::build] 1: Creating new scene failed` / `… Setting vertex buffer failed` / `… Committing scene failed` (support 4412978786833) [V].

### 2.3 Textures at render time
- VRayBitmap: "loads the texture the first time it is needed during the actual rendering, rather than at the start of a frame. Textures that are not needed (for example, because their materials are not needed for the particular camera angle) aren't loaded at all." [V] (`VMAX/113575830`)
- `VRAY_TEXTURE_CACHE` — "Specifies the size, in megabytes, of a separate texture cache to be used for tiled OpenEXR files. If this is not present, or the value is 0, the same cache is shared between dynamic geometry and tiled textures. One of the advantages of a separate texture cache is that it is persistent across frames." [V] → in 3ds Max there is **no UI "Bitmap/texture memory" spinner**; the tiled-texture budget is the dynamic memory limit unless this env var is set. (`bitmap_memoryLimit` does not exist in Max.)
- `VRAY_TEXTURES_LOAD_16BIT_AS_8BIT=1` — loads 16-bit PNG/TIFF as 8-bit in memory [V] (CHANGES RENDER: precision).
- Non-tiled formats are loaded whole: Chaos (forum 1203867, 2024): "The formats that contain mipmapping are .tx and .tif. Formats like regular TIF, PNG, and JPG could be loaded only with full resolution." "We apply mip-mapping internally in V-Ray to every texture regardless of their format after they were loaded. This helps for a better sampling. However, this does not save memory." [V]
- Lele (forum 1141266): "The correct approach to fixing running out of ram because of textures is to use Tiled .TX textures and set the texture memory limit to a coherent value." and "we *strongly* advise to keep bitmap filtering active at all times" [V]. Another user: disabling filtering cut 80 GB→28 GB (not recommended; changes render).
- V-Ray 7 notes: "Improve loading performance and memory use of png/TIFF/OpenEXR bitmaps"; 7.3: "Speedup bitmap loading by deferring color corrections to the GPU" (GPU only) [V].

### 2.4 Frame buffer, Light Cache, displacement
- "Due to technical reasons, the original 3ds Max frame buffer is still created at render time even if rendering to the V-Ray Frame Buffer." Recommended: Common tab width/height = 100, disable Rendered Frame Window, untick *Get resolution from MAX* and set size in the VFB rollout [V] (`VMAX/113575651`). **Memory frame buffer** off + **V-Ray raw image file** (.vrimg/.exr): "It does not store any data in the RAM" [V]. Support article lists "(3ds Max) Disable memory frame buffer" as a RAM lever [V]. IDENTICAL pixels (output path only) [I].
- Render elements: "Having many render elements and rendering huge resolutions will affect the memory utilization." [V]
- Light cache: "Don't delete – When enabled (the default), the light cache remains in memory after the rendering. Disable this option to automatically delete the light cache (and thus save memory)" [V]. Support article: "Although Light Cache comes with powerful features such as Adaptive Lights, disabling it would help memory utilization" (Brute Force instead) [V] — CHANGES RENDER.
- Bucket vs Progressive: "Progressive mode … needs to load all assets into the memory. The Bucket mode … needs to load only the information required to render the current bucket." "A very small [bucket] size will cause less geometry to be loaded" [V].
- Displacement/fur/subdivision are dynamic geometry and "tend to consume a lot of memory" [V]; no per-object numbers in docs [U].

### 2.5 Memory reporting you can parse
- **Memory tracking** (System rollout, Advanced): "When enabled, it generates a detailed HTML memory usage report for textures and objects. … Output directory – Determines the location of the memory tracking files. Show latest stats – Displays the last memory usage report generated." [V]. HTML column format [U]; MAXScript property names [U]. Chaos staff (forum 1157982): "the V-Ray memory tracker shows what **only** V-Ray uses as memory. This does not include what 3ds Max utilizes (3ds Max also loads textures, etc)". "Process RAM usage … It's not printed in the memory tracking - you can see it in the top part of the Stats menu in the VFB." [V]
- **VFB Stats panel** + MAXScript: `vfbControl #stats` "Prints all available stats", `vfbControl #stats "list"` prints categories [V] (`VMAX/113587631`) → scriptable after a test render.
- **V-Ray Profiler** (System rollout; `Mode: System` "Reports the time spent on system processes such as compiling geometries, building the Light Cache, etc."; output directory) [V] — time, not bytes.
- **Log**: `Log file` (default `%TEMP%\VRayLog.txt`, override `VRAY_SYSTEM_LOG_FILE`, parameter `system_vrayLog_file`; `system_vrayLog_ccToDebugger`) [V]. Standalone verbose output historically prints `[NNNN MB resman] [NNN MB texman]` per progress line where resman = "memory usage from geometry generated at render time like V-Ray proxies, displacement, subdivision" and texman = tiled-texture cache (Vlado, forum 71312) [V, 2016]; whether V-Ray 7 for Max logs "Total memory usage"/per-category lines [U] — check VRayLog.txt at verbose level 4.
- Raw EXR/VRImg carry extra attributes "about raycast and primitive count stats" (7.x fix) [V].
- Frame stamp `%primitives`, `%ram`, `%vmem` [V].

### 2.6 Budget rule of thumb (for the 128 GB target)
- No official "render RAM = N × scene RAM" guidance exists [V: none found]. Evidence: idle 20 GB → 49 GB rendering with a 15 GB V-Ray frame peak (forum 1157982) ⇒ render RSS ≈ Max-open RSS + V-Ray peak (+ VFB + Max frame buffer). Chaos recommends exporting to .vrscene and rendering in V-Ray Standalone to drop the host's share [V].
- Working formula for the tool: `RAM_render ≈ RAM_open(Max) + static_BVH(non-proxy meshes) + min(dynamic_pool, system_raycaster_memLimit) + texture_cache + VFB(all channels × W×H×16 B) + LC`. With proxies capped by the dynamic limit and textures tiled/capped, the uncapped term is the static Embree tree for whatever stays as Editable Mesh — so the ≤70 GB open target only works if the remaining static geometry is small. [I]

---

## 3. VRayBitmap vs 3ds Max Bitmap

Sources: VRayBitmap `VMAX/113575830`; converter tool `VMAX/113575627`; Scene Converter `VMAX/113575465`; forum 1082393 (Lele, Chaos), 1203867, 1173083; What's New / 7.x notes.

### 3.1 Memory model [V]
- VRayBitmap: "VRayBitmap loads the texture the first time it is needed during the actual rendering"; tiled OpenEXR/TIFF (.tx/.tex) "allow only portions of the textures to be loaded at various resolutions. This allows V-Ray to load only the parts of the textures that are needed". "tiled 8-bit TIFF textures are smaller on the disk and take up less RAM rendering." Cache budget = dynamic memory pool or `VRAY_TEXTURE_CACHE` (§2.3). Blur/mip: "you can choose the mipmap resolutions for the tiled textures by using the Blur parameter … Smaller values than 1 are not recommended, since they will force higher resolutions to be loaded" (support 4412978786833).
- Lele (Chaos) on VRayBitmap vs Bitmap (forum 1082393): "Bitmap loading is done differently: they are loaded as required by the traced rays, rather than at render start … VrayBitmap loaders support tiled, mip-mapped .tx files … load on demand will happen on a tile (f.e. 64x64px) of the right resolution for the given camera view. This also has a specifiable maximum RAM usage value, and if that is reached the old data will be unloaded in favour of the new … VRayBitmap loaders offer a number of sharp filter methods: Elliptical, and Sharp Isotropic are both much sharper than the max's own area filtering … Speed is much better across the board".
- 3ds Max `Bitmap` texmap: loaded by the Max bitmap manager (and pager); Max SDK notes pyramidal filtering needs ~133 % and summed-area ~400 % of the bitmap size in RAM (Autodesk SDK "Bitmap Filter Types"). [V for the SDK statement; whether Max keeps them resident while the scene is open = I (the 49-vs-15 GB discrepancy above is consistent with Max holding texture copies)].
- Viewport: VRayBitmap has "Use full resolution for viewport – When enabled, ignores the requested viewport resolution and uses the bitmap resolution instead … 3ds Max does not support resolutions larger than 16384x16384" [V] → leave OFF; Nitrous texture-size limit applies. V-Ray 6.1 added "Deferred loading of the VRayBitmap viewport previews" (had regressions; fixed 6.10.02) [V].

### 3.2 Converter [V]
- V-Ray 7 (Max 2022+): "the V-Ray Bitmap to VRayBitmap converter tool is located in the new combined converter tool at V-Ray menu > Converters > V-Ray scene converter." Options: "Convert Materials' Bitmap Loaders to V-Ray Bitmap Loaders", "Convert Nodes' and Modifiers' Bitmap Loaders to V-Ray Bitmap Loaders", "Reset Filtering Blur to 1.0", "Convert all V-Ray Bitmap loaders' textures to .TX format" (invokes OpenImageIO `maketx`; "MakeTX.exe found in …"), "Convert selected nodes only".
- Legacy converter script: "automatically invokes OpenImageIO's maketx converter … Afterwards, all Bitmap maps within the scene are replaced with VRayBitmap maps." MAXScript entry point: "standard V-Ray functions (contained in vrutils.ms) to swap the bitmap loaders to V-Ray ones" [V]; exact function name [U] (inspect `vrutils.ms` in the V-Ray install).
- Material Library Processor note: "Reset Blur to 1.0 – … [VRayBitmap loaders] default to a much sharper filtering mode than Max Bitmap Loaders and won't need to lower the blur as it used to be." [V]
- 7.2 note: "V-Ray scene converter should convert Coronabitmap to VRayHDRI nodes using sharp isotropic filtering" [V].

### 3.3 Is the conversion render-identical? — **No, by design (POSSIBLY DIFFERENT)**
- Filtering kernels differ: Max Bitmap = Pyramidal / Summed Area / None (+Blur) vs VRayBitmap = Isotropic ("Pyramidal MIP map"), Elliptical ("anisotropic MIP map"), Sharp Isotropic ("closer to the results with disabled filtering") [V]. Scene Converter sets Blur→1.0 and sharp isotropic for Corona bitmaps; for Max bitmaps the chosen default [U].
- Interpolation: "Default – Interpolation type is chosen automatically depending on the bitmap format to match the behavior of the standard 3ds Max Bitmap texture. For HDR and EXR images, the interpolation is Bilinear, and for all other formats - Bicubic." [V]
- Gamma / colour space: "Type – None; Inverse gamma; sRGB; From 3ds Max; Auto – … If a bitmap file name contains the [sRGB/linear] suffix … otherwise no correction" and "RGB primaries … Raw – suitable for normal maps" [V]. Regressions existed ("SRGB suffix in the VRayBitmap's name is converted to lowercase srgb and the image renders differently" fixed in 7; "Output selections of Bitmap are not transferred when converting" fixed in 5.2). A forum user reported the .tx conversion "messed up the ForestPack shaders, rendering them much much lighter" (forum 1230198, 2025) — unverified cause.
- Therefore classify Bitmap→VRayBitmap as **POSSIBLY DIFFERENT** and gate it behind an image diff with a tolerance (not bit-exact), or keep Bitmap loaders and attack viewport RAM via Nitrous limits instead.

---

## 4. Chaos Vantage 3.3.x VRAM

Sources: Render Settings Tab `LAV/125273847`; Edit Menu/Preferences `LAV/124621189`; File Menu (Scene/Texture/Mesh/Render Stats) `LAV/125111399`; Known Limitations `LAV/125305619`; System Requirements `LAV/124753566`; Live Link from 3ds Max `LAV/125143081`; release notes 1.8.0 `LAV/124390957`, 3.0.0 `LAV/124491538`, 3.0.2 `LAV/158072839`, 3.1.0 `LAV/376111163`, 3.3.0 `LAV/908558346`; Chaos staff forum posts (nikola.bozhinov / vladimir.nedev / npg) 1211486, 1235942, 1154016, 1094363, 1147974, 118857.

### 4.1 What is resident in VRAM [V]
- **All geometry, always.** Chaos (forum 1211486, Jul 2024): "Vantage doesn't do dynamic load/unload of V-Ray Proxies. That kind of memory saving is more suitable for bucket rendering … there is no difference between mesh and proxy." Forum 1154016: "No, we don't do that, Vantage aims to be a real-time not an offline renderer." → converting to proxies does **nothing** for Vantage VRAM (and nothing for Vantage CPU RAM). Point-cloud proxy LOD is not used: "Vantage supports only triangular meshes, no hair or particles" (2020; fur/hair later added as Vantage objects; point clouds [U]).
- **Instancing**: same .vrmesh / same mesh → one copy of geometry; "Maximum number of 16 777 216 instances is supported" [V]. 3.0: "Enhanced the Lavina renderer to better handle very large numbers of unique meshes" [V]. No bytes/triangle published; one Chaos data point: 32 M unique triangles ≈ 8.08 GB on a 2080 Ti at 720p (forum 1094363 era, vladimir.nedev) ≈ 250 B/tri incl. BVH [V single datapoint].
- **Textures**: "The textures are also mipmapped and only the necessary level is loaded on the GPU (unless 'dynamic textures' is disabled)" and "Such textures are part of a shading graph and the bitmap exists only once in memory" (nikola, Jul 2025, forum 1235942) [V]. Max texture resolution 16384² [V]. **CPU RAM**: "Vantage needs to keep textures and geometry in CPU memory so they can be quickly uploaded to the GPU memory when needed … even more important [with dynamic textures]" (forum 1154016) → System Requirements: "It is recommended to have RAM twice the size of GPU memory" [V].
- **Out-of-core**: none implemented. With *Safe GPU allocation* off, "Windows manages the excess memory by leaving it in CPU memory (the GPU accesses it from there, which is much slower)" / "it's a bit similar to 'Out of core', but it's handled by DirectX and the driver, not our code … it could crash at some point" [V]. Multi-GPU does **not** pool memory: "Each card holds a separate copy of the scene" [V]; NVLink "does not double the amount of memory each GPU can use" [V].

### 4.2 The documented VRAM levers (Vantage 3.3 docs) [V]
- Render Settings → Textures: **Enable dynamic textures** – "textures are downscaled based on where they are viewed from. This reduces GPU memory consumption (scene dependent). Texture size is updated if necessary on every scene movement." **Texture LOD bias** – "+1 reduces the MIP level to half the texture width … −1 results in double MIP level". (Introduced 1.8.0 as "Option for dynamic textures".) There is **no** fixed "texture resolution limit" or texture-compression setting in 3.3 docs; compression was only mentioned as a possible future (2022).
- Materials: "Bump map … Normal map – When disabled and Dynamic textures is enabled, reduces GPU memory consumption (scene dependent)."
- Preferences → Render Defaults → Displacement: "Tessellation level – None/Low/Medium/High … High tessellation level requires more GPU memory"; "Per-triangle tessellation level – … potentially saving memory"; "Emulate 2D displacement – … using more memory".
- Preferences → General: **Safe GPU allocation** ("will try not to use more GPU memory than the available physical memory"); "Save 16-bit PNG files … Uses additional video memory"; "Combined denoiser type uses more memory".
- Render elements: fewer = less VRAM (staff list). High-quality render dialog shows "how much additional memory you need for the high quality render" [V].
- "Compact loaded geometry" (an older Menus page described it as a post-processing step to save GPU memory) is **not** in the 3.3 Edit Menu/Render Settings docs → probably removed/renamed in 3.x [U — check Preferences on the box].

### 4.3 Measuring Vantage memory [V]
- File → **Scene Stats**, **Texture Stats** ("amount of CPU and GPU memory consumed by each texture, all textures' resolution, the time taken to load each texture … Show percent … Show path"), **Mesh Stats** ("percentage of total memory each mesh uses"), **Render Stats**. Status bar shows device(s) ("2x 4090" = two copies). Debug log (`debug_log.txt`, `-debugLogFile`) prints lines like `Freed 0.00 bytes of unused textures and uploaded 33.44 Mbytes for 1 newly used texture(s) to GPU memory in 17 ms` [V].
- CLI: `vantage_console.exe -s scene.vrscene -conf x.vantage … -dl/-d <idx>`; offline render args (width/height/samples/noise/denoiser/LC) [V] — no documented memory-report flag [U].

### 4.4 Live Link behaviour [V]
- Live Link sends the scene in "V-Ray format" over port 20701 (DR) / **20703 (DR2; required with V-Ray 7.3+)**; "the data is duplicated in a way. This is inevitable … RAM usage will be much lower if you can work directly with a vrscene file" (Chaos, 2022) → Live Link costs host RAM ≈ another copy of geometry+textures in Vantage's process.
- 3ds Max Live Link settings: **Enable Object Render State** – "uses Render states/counts/iterations/modes instead of their viewport representation" (scatter/Anima counts) — i.e. by default some objects are sent with their *viewport* counts. Proxies: Vantage receives the .vrmesh reference and loads it fully; proxy display mode in Max is irrelevant to Vantage VRAM [I from 4.1]. Object creation is disabled during Live Link; "3.3: Property values edited in Vantage … are now preserved when the same property is updated by a Live Link update".
- V-Ray 7.4: "Vantage in 3ds Max viewport" (Active Shade = V-Ray GPU 7 update 4 DR2) — runs Vantage as the viewport renderer; same residency rules [V].
- Fixes relevant to memory: 1.8.0 "GPU memory for a VRayProxy not being freed after the proxy is deleted"; 2.5.2 bug: duplicate virtual display adapter made Vantage upload the scene twice (use `vantage.exe -d 0`); 7.3: "Excessive GPU Memory consumption caused by Decal in a specific scene" [V].
- **Does reducing 3ds Max viewport weight change Vantage VRAM? No** [V/I]: Vantage VRAM depends on the exported V-Ray scene (full meshes/proxies, textures at source resolution, mip-selected), not on Nitrous state. Only the Max and Vantage processes competing for the same GPU matters ("Other GPU heavy applications … like 3dsMax … can also eat up the memory available to Vantage"; run Vantage on a second GPU or add a small display GPU) [V].

### 4.5 Changes 2.x/3.0 → 3.3 that matter [V]
- 1.8.0 (2022): dynamic textures; proxy VRAM leak fix. 2.5.x: corrected required-memory estimate in the render dialog; multi-device selection bug. 3.0 (Oct 2025): USD/MaterialX, Gaussian splats from .vrscene and Live Link, volumes, "better handle very large numbers of unique meshes", large-HDRI Blackwell fix. 3.1 (Dec 2025): Hydra delegate, fluid sim. 3.3.0 (Jun 2026): wet effects, Veras, Anima scenes, Gaussian-splat relighting, material node editor, DLSS frame generation (2–4×), Live Link property preservation, command-line rendering. **No new VRAM-management feature (no OOC, no texture cap, no compression) was added between 3.0 and 3.3.**

---

## 5. 3ds Max 2026 Nitrous viewport VRAM

Sources: Display Performance panel (3ds Max 2026 help, GUID-502A94D2…); NitrousGraphicsManager MAXScript interface; V-Ray docs.

- Texture limits [V]: Display Performance panel: "Procedural Maps … Default = 1024 x 1024", "Texture Maps … Default = 512 x 512", "Viewport Background/Environment … Default = 1024 x 1024" (2026 page; older docs quote 1024/2048/4096 — confirm on the box). "These controls override the bitmap proxy settings."
- MAXScript [V]: `NitrousGraphicsManager.SetTextureSizeLimit <int>sizeLimit <bool>enabled`, `SetBackgroundTextureSizeLimit`, `SetProceduralTextureSizeLimit <int>` ("to avoid running out of graphics card memory"). Doc note: "When loading scenes with large amounts of texture data, Nitrous might start paging … enabling the limit can restore the viewport performance by eliminating the memory paging." INI: `[Nitrous] ViewportTextureSizeLimitEnabled=1`, `ViewportTextureSizeLimit=512`; restart required (community). All viewport-only → IDENTICAL render.
- Geometry: Nitrous uploads a GPU mesh per displayed object (vertices/normals/UVs/indices; instances share buffers) — no Autodesk numbers published [U]. VRayProxy in **bounding box / point** mode draws a box/point only ("does not use up any resources, except for the viewport preview") → effectively no mesh upload [I]; "preview from file" uploads the ≤`maxPreviewFaces` preview; "show whole mesh" uploads the full mesh.
- Other VRAM consumers: viewport AA/MSAA settings and "Improve Quality Progressively" (community reports 4 GB→0.6 GB by disabling AA) [anecdotal]; Max itself holds a few GB of VRAM that competes with Vantage/V-Ray GPU (Chaos support) [V].
- Numbers per object/texture: none documented [U] — measure with `nvidia-smi --query-gpu=memory.used` before/after toggling display modes.

---

## 6. V-Ray 7 specifics that affect memory

- **No "V-Ray scene optimizer" exists.** V-Ray 7's combined **Scene Converter** window = V-Ray Scene Converter + Material Library Processor + .TX Bitmap Converter (`VMAX/113575465`) [V]. The old "Bitmap to VRayBitmap converter" menu entry was removed in 7 ("Remove the Bitmap to VRayBitmap converter V-Ray menu entry where the redesigned scene converter is available") [V].
- **Chaos Cosmos** assets import as VRayProxy (+ Cosmos Asset rollout for variants; lights as VRayProxy→VRayLight hierarchy; 7.3: MaxScript command to import Cosmos assets by name; 7.2: "Import lights from Cosmos as instances", "Faster viewport loading for large scenes with delayed VRayProxy objects") [V]. Cosmos proxies obey all of §1 and §4.1.
- **Gaussian splats** (`VRayGaussiansGeom`, `VMAX/113580674`): viewport "Point size", "Viewport primitives %", "Opacity filter"; 7.2 clipping masks; 7.3 relighting; Vantage 3.0+ renders splats from Live Link [V]. Memory figures [U].
- **Memory tracking / Profiler / Stats**: see §2.5. 7.x also added "Way to check last Frame Buffer and Render Region size via MaxScript", `vrayExportVRScene … exportRegion:` [V].
- **V-Ray 7.3 DR2 + Vantage**: port 20703; "Vantage config file/changes are reset when starting Live-Link animation rendering" fixed; "DR2 builds <> Vantage live-link crashes after loading config scene and panning in max viewport" fixed [V].
- **Deferred proxy previews (7.2)** and "preview override cache" (7.x) reduce open-scene cost of proxies [V]; magnitude [U].
- **GPU-only** (not relevant to CPU budget but often confused): V-Ray GPU Out-of-core textures ("Use System Memory for Textures", 6.2+), Compressed textures (6.1+), on-demand mip-mapping mode removed from GPU UI in 7 [V].

---

## 7. Render-identity verification

### 7.1 Determinism controls in V-Ray 7 [V]
- Image Sampler page: "Lock noise pattern - When enabled, the sampling pattern is the same from frame to frame in an animation … **Note that re-rendering the same frame produces the same result in both cases.**" MAXScript `renderers.current.dmc_lockNoisePattern = true` (forum 1175048) and `renderers.current.dmc_randomSeed` (Chaos staff, forum 1194282: "You can additionally modify the random seed using the renderers.current.dmc_randomSeed property") [V]. With the seed fixed, unlocked mode uses the frame number as seed → same frame ⇒ same pattern.
- Bucket sampler recommended for determinism and memory; progressive is time-limited (stop conditions vary with load) → use **Bucket**, fixed `Max subdivs`/noise threshold, no time limit.
- **Light cache is the main nondeterminism source** (multi-threaded path tracing; `Number of passes` tied to threads). Neutralise by: (a) render reference with LC **Mode: Save to file** (`Don't delete` on, or `Auto save` + `Switch to saved cache`), then render both A and B with **Mode: From file** pointing at the same `.vrlmap` — "You can safely use light caches computed in one of these modes for the other" [V]; or (b) use Brute Force for the identity test. Adaptive Lights use the LC → also pinned by (a).
- Other sources to pin: `VRAY_NUM_THREADS` (thread count affects bucket order only; with locked pattern pixels should match, but keep it constant), denoiser off (OptiX/OIDN not bit-stable across drivers) or compare `RGB color` not `effectsResult`, VFB colour-correction layers off or compare raw channels, no `Test resolution`, same `Render mask`/region, same `Min shading rate`, same time/frame, motion blur off unless both scenes animate identically, `Check for missing files` on (asset drift), Max OCIO settings identical, DR off (different hosts ⇒ different bucket results in some builds; "render servers" asset cache). Per-object `dmc`/"Use local subdivs" unchanged.
- Texture cache residency (`system_raycaster_memLimit`, `VRAY_TEXTURE_CACHE`) affects **only** which tiles are resident, not sample values [V] — safe to change between A and B.

### 7.2 Getting pixels out (MAXScript) [V]
```maxscript
vr = renderers.current
vr.dmc_lockNoisePattern = true
vr.dmc_randomSeed = 12345                -- same in A and B
-- LC pinned: vr.lightcache_mode = <FromFile>, vr.lightcache_loadFileName = @"X:\ref.vrlmap"  (exact property names: showproperties vr)
vr.output_saveRawFile = true             -- raw multichannel output, no memory FB
vr.output_rawFileName = @"X:\cmp\A.exr"  -- tiled EXR with all REs ("EXR 32-bit output": vr.output_rawExrUseHalf=false)
max quick render                          -- or render camera:$Cam outputSize:[w,h] vfb:true
-- alternative: from the VFB after the render
vfbControl #savemultiimage @"X:\cmp\A_vfb.exr"      -- multichannel EXR/vrimg with all channels
vfbControl #setchannel 0 ; vfbControl #saveimage @"X:\cmp\A_rgb.exr"
n = vrayVFBGetNumChannels()
for i = 1 to n do format "% % %\n" i (vrayVFBGetChannelName i) (vrayVFBGetChannelType i)
bm = vrayVFBGetChannelBitmap 1           -- in-process pixel access (3ds Max bitmap), no disk round-trip
vfbControl #stats                        -- print render stats (memory/timing) to the listener
```
All names above are on `VMAX/113587631`/`113575613` except `dmc_lockNoisePattern`/`dmc_randomSeed` (Chaos forum staff) and `lightcache_*` (U: confirm with `showproperties`). Note: "the region coordinates are always relative to the resolution specified in Output Size"; keep region off.
- Save **32-bit** EXR (`EXR 32-bit output`), no colour corrections baked (`Save VFB color corrections to RGB channel` off), no denoiser.

### 7.3 Comparing [V — OpenImageIO docs]
```bash
# bit-exact gate (default fail threshold 1e-6, any pixel)
idiff A.exr B.exr
# tolerant gate: fail if >0.1% of pixels differ by >1/1024, or any pixel by >1/256; write a scaled diff image
idiff -fail 0.001 -failpercent 0.1 -hardfail 0.004 -abs -scale 64 -o diff_A_B.exr A.exr B.exr
idiff -a A.exr B.exr                 # compare all subimages/channels (multichannel EXR)
idiff -p A.exr B.exr                 # perceptual (Yee) metric
# oiiotool equivalent, scriptable with stats
oiiotool A.exr B.exr --fail 0.001 --failpercent 0.1 --hardfail 0.004 --diff
oiiotool A.exr B.exr --sub --abs --stats         # max/mean error per channel
```
`idiff` exit codes: 0 match, 1 warning, 2 failure, 3 different dimensions, 4 file error (`openimageio.readthedocs.io/en/latest/idiff.html`). V-Ray ships no standalone compare tool (`vdenoise` is the denoiser; `vrimg2exr` converts .vrimg); there is no `vrayCompare`. The VFB **History → Compare A/B** and `vfbControl #historyseta/#historysetb` exist for eyeballing [V].
- Pass criteria proposal: proxy/Nitrous/FB-mode levers → `idiff` default (bit-exact) must pass with LC-from-file and locked seed; if not bit-exact, investigate (mirrored node, explicit normals). Bitmap→VRayBitmap → expect small filtering deltas; gate with `-fail 0.004 -failpercent 0.5 -hardfail 0.05 -p` and a human look.

---

## 8. Unknowns to verify on the box (3ds Max 2026 + V-Ray 7.x + Vantage 3.3.1)

1. `showproperties (renderers.current)` — confirm existence/defaults of `system_raycaster_memLimit`, default-geometry property (`system_raycaster_defaultGeometry`?), Embree conserve-memory property, memory-tracking on/off + output-dir properties, `dmc_lockNoisePattern`, `dmc_randomSeed`, `lightcache_mode`/`lightcache_loadFileName`/`lightcache_autoSave*`, `system_vrayLog_file`/`_level`. Check whether *Dynamic memory limit* / *Default geometry* still appear in the Advanced System rollout UI in V-Ray 7 (docs no longer list them).
2. `showproperties VRayProxy` — `display` enum order (0 = bbox confirmed), names for preview type, scale, flip axis, point-cloud use/multiplier, LOD scale; and whether `display` changes are honoured after the 7.3 "edges/faces swapped" fix.
3. Measure open-scene RAM/VRAM of one proxy per display mode (bbox / preview / whole mesh) and with Proxy preview cache on/off and the 7.2 deferred-preview behaviour — no numbers exist.
4. Memory-tracking HTML report: file name pattern, columns, totals; confirm `vfbControl #stats "list"` category names so the budget checker can parse "Process RAM usage" vs "frame total".
5. VRayLog.txt at verbose level 4: does V-Ray 7 still print `resman`/`texman` or tree-memory lines; where the Embree OOM strings appear.
6. Default value of `system_raycaster_memLimit` in V-Ray 7 and behaviour of negative values; whether `VRAY_TEXTURE_CACHE` is honoured inside 3ds Max (env var table says yes).
7. Bitmap→VRayBitmap: the filter/interpolation/colour-space defaults the Scene Converter writes for Max `Bitmap` (isotropic vs sharp isotropic; Auto gamma), and a diff on a representative material set.
8. Negative-scale/mirrored nodes through `vrayMeshExport` per-object: confirm normals/face winding survive (old forum says no for merged export).
9. Whether `vrutils.ms` exposes a callable Bitmap→VRayBitmap function for scripting.
10. Vantage 3.3.1: Preferences contents (is "Compact loaded geometry" still present?), Mesh Stats per-mesh GPU bytes → derive bytes/triangle for this scene; confirm proxies show identical GPU bytes in bbox vs whole-mesh Max display modes; confirm Live Link RAM delta (≈ second copy) on the 128 GB box.
11. Nitrous: actual 2026 default texture limits (512 vs 2048) and VRAM delta from `SetTextureSizeLimit` + AA settings, measured with `nvidia-smi`.
12. Determinism: render the same scene twice with LC from file + locked seed + bucket on the actual CPU count and confirm `idiff` bit-exact; then once with `VRAY_NUM_THREADS` changed to see whether thread count leaks into pixels.

---

## 9. Source index

Official docs (Chaos):
- VRayProxy — https://documentation.chaos.com/space/VMAX/113575423/VRayProxy
- Working with vrmesh Files — https://documentation.chaos.com/space/VMAX/113575435/Working+with+vrmesh+Files
- MAXScript (vrayMeshExport, vrayMeshImport, vrayExportVRScene) — https://documentation.chaos.com/space/VMAX/113575613
- System rollout (current) — https://documentation.chaos.com/space/VMAX/113587447 ; 2021 version with Dynamic memory limit / Default geometry text — https://web.archive.org/web/2021/https://docs.chaos.com/display/VMAX/System
- Proxy Preview Cache — https://documentation.chaos.com/space/VMAX/113587893
- VRayBitmap — https://documentation.chaos.com/space/VMAX/113575830
- V-Ray Bitmap to VRayBitmap converter — https://documentation.chaos.com/space/VMAX/113575627
- V-Ray Scene Converter — https://documentation.chaos.com/space/VMAX/113575465
- Controlling the VFB Programmatically — https://documentation.chaos.com/space/VMAX/113587631
- Frame Buffer Settings — https://documentation.chaos.com/space/VMAX/113575651 ; V-Ray Frame Buffer — https://documentation.chaos.com/space/VMAX/113578174
- Image Sampler (Lock noise pattern) — https://documentation.chaos.com/space/VMAX/113587190 ; Light Cache Settings — https://documentation.chaos.com/space/VMAX/113575669
- Environment Variables — https://documentation.chaos.com/space/VMAX/113586786
- V-Ray 7 release notes — https://documentation.chaos.com/space/VMAX/113580676 ; 7.20.00 — https://documentation.chaos.com/space/VMAX/113587390 ; 7.30.02 — https://documentation.chaos.com/space/VMAX/707559558 ; What's New in V-Ray 7 — https://documentation.chaos.com/space/VMAX/113586464 ; V-Ray 6 notes — https://documentation.chaos.com/space/VMAX/113577358
- Chaos Cosmos Browser — https://documentation.chaos.com/space/VMAX/113587032 ; VRayGaussiansGeom — https://documentation.chaos.com/space/VMAX/113580674 ; V-Ray Profiler — https://documentation.chaos.com/space/VMAX/113580772 ; Chaos Vantage Integration — https://documentation.chaos.com/space/VMAX/859635795
- Vantage: Render Settings Tab — https://documentation.chaos.com/space/LAV/125273847 ; Edit Menu / Preferences — https://documentation.chaos.com/space/LAV/124621189 ; File Menu (Scene/Texture/Mesh/Render Stats) — https://documentation.chaos.com/space/LAV/125111399 ; Known Limitations — https://documentation.chaos.com/space/LAV/125305619 ; System Requirements — https://documentation.chaos.com/space/LAV/124753566 ; Live Link from 3ds Max — https://documentation.chaos.com/space/LAV/125143081 ; Opening Scene Files — https://documentation.chaos.com/space/LAV/124490020 ; Command Line Options — https://documentation.chaos.com/space/LAV/124520960 ; release notes 3.3.0 — https://documentation.chaos.com/space/LAV/908558346 , 3.1.0 — https://documentation.chaos.com/space/LAV/376111163 , 3.0.2 — https://documentation.chaos.com/space/LAV/158072839 , 3.0.0 — https://documentation.chaos.com/space/LAV/124491538 , 1.8.0 — https://documentation.chaos.com/space/LAV/124390957
- Support: Optimizing memory (RAM) for CPU rendering — https://support.chaos.com/hc/en-us/articles/4412978786833 ; Optimizing VRAM for GPU rendering — https://support.chaos.com/hc/en-us/articles/4412959408017 ; Using MAXScript with V-Ray — https://support.chaos.com/hc/en-us/articles/4411319570577

Chaos forums (staff answers):
- Does V-Ray proxies save VRAM? (Vantage, 2024) — https://forums.chaos.com/forum/chaos-vantage/chaos-vantage-general/1211486-does-v-ray-proxies-saves-vram
- VRAM Bitmap usage question (Vantage, 2025) — https://forums.chaos.com/forum/chaos-vantage/chaos-vantage-general/1235942-vram-bitmap-usage-question
- Double the consumption of physical memory? (Vantage Live Link RAM, 2022) — https://forums.chaos.com/forum/chaos-vantage/chaos-vantage-issues/1154016-double-the-consumption-of-physical-memory
- GPU crash with Vantage / Safe GPU, NVLink, local host (2020) — https://forums.chaos.com/forum/chaos-vantage/chaos-vantage-issues/1094363-gpu-crash-with-vantage-and-livelink-connection-question
- About the video memory consumption of scene loading — https://forums.chaos.com/forum/chaos-vantage/chaos-vantage-issues/1147974-about-the-video-memory-consumption-of-scene-loading
- Recommended GPU setup? (multi-GPU copies) — https://forums.chaos.com/t/recommended-gpu-setup/118857 ; GPU memory issue (2.5.2 duplicate device) — https://forums.chaos.com/t/gpu-memory-issue/119654 ; Point cloud proxy support — https://forums.chaos.com/t/point-cloud-proxy-object-support/101970
- Dynamic memory limit never seems to help (Lele, 2022) — https://forums.chaos.com/forum/v-ray-for-3ds-max-forums/v-ray-for-3ds-max-problems/1141266-dynamic-memory-limit-never-seems-to-help
- VRay RAM usage and Memory Tracker log (2022) — https://forums.chaos.com/forum/v-ray-for-3ds-max-forums/v-ray-for-3ds-max-general/1157982-vray-ram-usage-and-memory-tracker-log
- Vray 5 (and 6), VrayBitmap and mipmap (2024) — https://forums.chaos.com/forum/v-ray-for-3ds-max-forums/v-ray-for-3ds-max-general/1203867-vray-5-and-6-vraybitmap-and-mipmap
- Bitmap vs. VRayHDRI performance (Lele, 2020) — https://forums.chaos.com/forum/v-ray-for-3ds-max-forums/v-ray-for-3ds-max-general/1082393-bitmap-vs-vrayhdri-vraybitmap-performance
- flipped faces on proxy export (2012) — https://forums.chaos.com/forum/v-ray-for-3ds-max-forums/v-ray-for-3ds-max-problems/51734-flipped-faces-on-proxy-export ; Vray mesh export doesn't export materials — https://forums.chaos.com/forum/v-ray-for-3ds-max-forums/v-ray-for-3ds-max-problems/70709-vray-mesh-export-doesn-t-export-materials
- set Dynamic memory limit at percentage (negative values, Vlado) — https://forums.chaos.com/forum/v-ray-for-3ds-max-forums/v-ray-for-3ds-max-wishlist/51530-set-dynamic-memory-limit-at-percentage
- Randomise noise / dmc_randomSeed — https://forums.chaos.com/forum/chaos-common/chaos-common-public/1194282-randomise-noise-on-repeat-frames-lock-noise-pattern ; Lock noise pattern keeps unchecking (dmc_lockNoisePattern) — https://forums.chaos.com/forum/v-ray-for-3ds-max-forums/v-ray-for-3ds-max-problems/1175048-lock-noise-pattern-keeps-unchecking
- resman/texman log fields (Vlado, 2016) — https://forums.chaos.com/forum/v-ray-for-maya-forums/v-ray-for-maya-general/71312-vray-render-stat-texture-and-geometry-memory-usage
- VRayBitmap viewport previews 6.1 deferred loading — https://forums.chaos.com/forum/v-ray-for-3ds-max-forums/v-ray-for-3ds-max-problems/1173083-vraybitmap-viewport-texture-previews-6-1 ; Scene converter .tx ForestPack report — https://forums.chaos.com/forum/v-ray-for-3ds-max-forums/v-ray-for-3ds-max-general/1230198-vray-scene-converter

Other:
- 3ds Max 2026 Display Performance panel — https://help.autodesk.com/cloudhelp/2026/ENU/3DSMax-Customizing/files/GUID-502A94D2-0037-407B-95BC-6CD90067740B.htm ; NitrousGraphicsManager MAXScript — https://help.autodesk.com/cloudhelp/2017/ENU/MAXScript-Help/files/GUID-34892DB6-E840-4F52-9175-30332799B7B1.htm
- OpenImageIO idiff — https://openimageio.readthedocs.io/en/latest/idiff.html ; oiiotool — https://openimageio.readthedocs.io/en/latest/oiiotool.html
- MAXScript property dump showing `system_raycaster_memLimit`/`system_embree_on` — https://gist.github.com/jeremybep/5af88420687ab260a6590aa60f0e85f1 ; `display = 0` for VRayProxy — https://www.scriptspot.com/forums/3ds-max/scripts-wanted/select-all-vray-proxy-switch-preview-mode-and-so-one
