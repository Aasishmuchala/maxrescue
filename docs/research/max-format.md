# 3ds Max `.max` File Format — X-Ray Parser Research Dossier

Research target: a **read-only** Python parser (no Max installed) that reports per node: class (ClassID + DLL + class name), on-disk chunk byte size, modifier-stack depth + modifier classes, mesh/poly/scatter/proxy detection; plus scene-level material/bitmap counts and detection of embedded script payloads (ALC/CRP/etc.) and missing-plugin DLL references.

Confidence legend: **[V]** verified from primary source (SDK header, working parser source, hex dump), **[I]** inferred (consistent across ≥2 sources but not spec-stated), **[U]** unknown / must be discovered empirically.

---

## 0. Executive summary of what is and isn't possible

- A `.max` file is an **OLE2 / Microsoft Compound File** (magic `D0 CF 11 E0 A1 B1 1A E1`). Python `olefile` opens it and lists/reads its streams with **no Max installed**. **[V]**
- Every meaningful stream except the two `\x05…Information` property sets is a **tree of chunks**: `uint16 id`, `int32 size` (size *includes* the 6-byte header; high bit = "this chunk is a container of sub-chunks"); a `size==0` escape means a 14-byte header follows with a `uint64` size. This is the same idea as the `.3ds` format. **[V]**
- The `DllDirectory`, `ClassDirectory3`, and `Scene` streams together let you resolve, for every scene object, its **ClassID, SuperClassID, plugin DLL filename+description, and class display name — without loading any plugin**. This is exactly how Max itself reports "missing DLL" on load, so a missing-plugin X-ray is fully achievable. **[V]**
- **Nobody writes `.max` outside Autodesk in the general case.** Okino states plainly there is no way to read/write `.max` without a resident copy of Max. Two hobby projects (Kaetemi's ryzomcore C++ `pipeline_max`, Neil Marshall's C#) *do* round-trip specific streams by editing chunk bytes in place, but there is no independent full writer. Reading is well-established; writing is not. **[V]**
- **Geometry sizes** are directly measurable: the on-disk chunk sizes of the vertex/face arrays inside `0x08FE` are readable without decoding them, so per-object mesh byte weight is trivial. Actual vertex/face *counts* are also recoverable (see §7), and the file's own `DocumentSummaryInformation` already carries "Vertices: N / Faces: N" and scene totals as plain strings. **[V]**
- **Embedded-script malware (ALC/CRP/ADSL/PhysXPluginMfx/MSCPROP)** lives in ordinary scene constructs — persistent globals, persistent callbacks (`callbacks.addScript … persistent:true`), scripted controllers, and scripted custom-attribute defs (`ScriptedCustAttribDefs` stream) — plus known global-variable **name signatures** you can grep for. Detection by signature is feasible; guaranteeing cleanliness is not. **[V]**

---

## 1. OLE2 container & the stream list

**Format:** Microsoft Compound File Binary (CFBF / OLE2 structured storage). Magic bytes `b'\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1'`. Min file size 1536. Released with 3D Studio MAX 1996. PRONOM `fmt/978`. **[V]** — sources: [Kaetemi Part 1](https://blog.kaetemi.be/2012/08/17/3ds-max-file-format-part-1/), [archiveteam](http://fileformats.archiveteam.org/wiki/MAX_(3ds_Max)), io_scene_max `MAGIC`/`is_maxfile`.

### Streams observed (union across sources) **[V]**
From `gsf list` (Kaetemi), max_dump README, and io_scene_max:

| Stream | Holds | Confidence |
|---|---|---|
| `Scene` | The scene graph: every object/material/controller/node as an indexed chunk. The main payload. | [V] |
| `ClassDirectory3` | Table of `(DllIndex, ClassID, SuperClassID, ClassName)` — one entry per class used. Older files: `ClassDirectory` (no "3"). Scene chunk ids index INTO this table. | [V] |
| `ClassData` | Per-class global data blobs (superclass-id keyed). Non-critical for X-ray. | [V] |
| `DllDirectory` | Ordered list of `(description, dll filename)` for every plugin DLL the scene needs. ClassDirectory3.DllIndex indexes INTO this. | [V] |
| `Config` | UI/config + scripted-plugin parameter *schemas* (`ConfigScript`/`0x2180`). Non-critical. | [V] |
| `VideoPostQueue` | Video Post render queue. Small; good format warm-up sample. | [V] |
| `FileAssetMetaData3` (2014+), `FileAssetMetaData2` (2010–2013) | External asset (bitmap/xref) paths as GUID→path records. Editable by external apps by design. | [V] |
| `ScriptedCustAttribDefs` | Serialized scripted custom-attribute **definitions** (MAXScript source of CA defs). **Primary malware-carrier stream.** | [V] |
| `SaveConfigData` | Save-time config. Non-critical. | [V] |
| `\x05SummaryInformation` | Standard Windows OLE property set (author, title, thumbnail). NOT chunked. | [V] |
| `\x05DocumentSummaryInformation` | Standard OLE property set; in Max holds the human-readable **file-properties** text: version, "Vertices: N", "Faces: N", "Objects/Lights/Cameras" totals, external dependencies, object names, material names, Used Plug-Ins, Render Data. NOT chunked — custom string format. | [V] |
| `SaveConfigData`, `CustomFileStreamDataStorage` (2019+) | user/plugin custom streams (see §8). | [V] |

Example real stream set (max_dump on a 2016 file): `['\x05DocumentSummaryInformation', '\x05SummaryInformation', 'ClassData', 'ClassDirectory3', 'Config', 'DllDirectory', 'FileAssetMetaData3', 'SaveConfigData', 'Scene', 'ScriptedCustAttribDefs', 'VideoPostQueue']`. **[V]**

Note: streams may be **gzip-compressed**. io_scene_max checks the first bytes of a stream: if it starts with `1F 8B` (`root == 0x8B1F`) and the next long is one of `{0xB000000, 0xA040000, 0x8000001E}`, it `zlib.decompress(data, MAX_WBITS|32)`. Newer "compressed save" files wrap `Scene` this way. **[V]** (io_scene_max `ChunkReader.get_chunks`)

### olefile usage **[V]** (v0.47, 2023-12-01; `pip install olefile`)
```python
import olefile
if olefile.isOleFile(path):
    ole = olefile.OleFileIO(path)          # add write_mode=True to enable write_stream
    streams = ole.listdir(streams=True, storages=False)  # list of name-part lists
    exists  = ole.exists('Scene')
    size    = ole.get_size('Scene')
    typ     = ole.get_type('Scene')        # STGTY_STREAM / STGTY_STORAGE / STGTY_ROOT / False
    data    = ole.openstream('Scene').read()
    props   = ole.getproperties('\x05DocumentSummaryInformation')
    ole.close()
```
`\x05`-prefixed names are passed literally. `write_stream` can overwrite a stream of any size (v0.45+) but does NOT resize the compound file structure meaningfully for our read-only tool — irrelevant here. Source: [olefile Howto](https://olefile.readthedocs.io/en/latest/Howto.html), [README](https://github.com/decalage2/olefile).

**IMPORTANT for the parser:** io_scene_max ships its **own** minimal OLE reader (`ImportMaxFile`, `MaxStream`, `MaxFileDirEntry`) rather than depending on `olefile` at runtime — it reimplements FAT/miniFAT/DIFAT sector walking (constants `MAXREGSECT=0xFFFFFFFA`, `DIFSECT=0xFFFFFFFC`, `FATSECT=0xFFFFFFFD`, `ENDOFCHAIN=0xFFFFFFFE`, `FREESECT=0xFFFFFFFF`, `mini_stream_cutoff_size=0x1000`). Using `olefile` directly is simpler and equivalent. **[V]**

---

## 2. The chunk format (inside `Scene`, `ClassDirectory3`, `DllDirectory`, `Config`, `VideoPostQueue`)

**Header, verified three ways** (Kaetemi prose, max_dump `storage_parser.read_header`, io_scene_max `get_next_chunk`, ryzomcore `CStorageChunks::enterChunk`):

```
offset 0: uint16  id            (little-endian)
offset 2: int32   size          (little-endian, signed)
```
- `size` **includes the 6-byte header**. Payload length = `size - 6`.
- **High bit (0x80000000) set ⇒ this chunk is a CONTAINER** of sub-chunks (recurse); clear ⇒ it is a leaf value. Because `size` is read signed, a container reads as **negative** (io_scene_max: `if siz < 0: container`). To get the true length: `chunksize = size & 0x7FFFFFFF`. **[V]**
- **64-bit escape (huge chunks):** if the 4-byte `size == 0`, the real size is the next field, a `uint64`; header becomes 14 bytes; container flag is bit `0x8000000000000000`; `chunksize = size64 & 0x7FFFFFFFFFFFFFFF`. **[V]** — io_scene_max:
```python
typ, siz = struct.unpack("<Hi", data[off:off+6])
chunksize = siz & 0x7FFFFFFF
if siz == 0:
    siz, = struct.unpack("<q", data[off+6:off+14]); header=14
    chunksize = siz & 0x7FFFFFFFFFFFFFFF
container = (siz < 0)
```
- Container payload is parsed by recursively reading child chunks until the parent's byte span is consumed. Leaf payload is raw bytes typed by chunk-id context. **[V]**
- Exceptions that are **NOT** chunked: `\x05SummaryInformation`, `\x05DocumentSummaryInformation` (Windows OLE property sets), and `FileAssetMetaData2/3` (its own GUID+string record format, §9). **[V]**

**Per-object chunk-size for the X-ray:** the outer chunk of each scene object is a single container; its `size & 0x7FFFFFFF` (minus header) is exactly the on-disk byte weight of that object. No decode of contents required to report it. **[V]**

### `DllDirectory` layout **[V]**
Container of entries; ids are meaningful **relative to their parent** (max_dump `DllDecoder`, ryzomcore `dll_directory.cpp`):
- `0x21C0` — optional header value (4 bytes; a build/version int; absent in Max 3, present 2010+). Ignorable.
- `0x2038` — a **DllEntry** container, holding:
  - `0x2039` — UTF-16LE **description** string (e.g. `"Editable Poly Object (Autodesk)"`)
  - `0x2037` — UTF-16LE **DLL filename** (e.g. `"EPoly.dlo"`)

Entry order defines the **DllIndex** used by ClassDirectory3.

### `ClassDirectory3` layout **[V]**
Container of `0x2040` **ClassEntry** containers, each holding:
- `0x2060` — 16-byte **header**, `struct '<IQI'` = **`(int32 DllIndex, uint64 ClassID, uint32 SuperClassID)`**. (io_scene_max `ClassIDChunk.set_data`: `self.set(data,'<IQI',0,16)`; max_dump reads it as `4i` = `dll_index, classIdLow, classIdHigh, superClassId`.)
  - **DllIndex semantics:** `>=0` → index into DllDirectory; **`-1` = built-in** (Scene, Node, ParamBlock2…); **`-2` = scripted** (a MAXScript class such as a scripted material/CA). **[V]** — this is the primary signal that "this class is a script."
  - **ClassID** is two 32-bit words `(partA, partB)`; on disk the 8 bytes are `partA` then `partB` little-endian, i.e. `uint64 = (partB<<32)|partA`. io_scene_max stores the whole 8 bytes as one `Q` and compares against 64-bit constants where the *high* word is partB (see §6). **[V]**
- `0x2042` — UTF-16LE **class display name** (e.g. `"Editable Poly"`, `"Standard"`, `"VRayMtl"`, `"Scene"`).

**Key linkage rule (the crux of the whole format):** In the `Scene` stream, **each top-level object chunk's id is its index into ClassDirectory3.** So object chunk id `0x0018` ⇒ ClassDirectory3 entry 24 ⇒ ClassID `(0x1bf8338d,0x192f6098)`, SuperClassID `0x10`, name "Editable Poly", DllIndex 7 ⇒ DllDirectory[7] = `EPoly.dlo`. **[V]** — Kaetemi Part 4/5, ryzomcore `CSceneClassContainer::createChunkById` (`m_ClassDirectory3->get(id)`), io_scene_max `get_class(chunk)= CLS_DIR3_LIST[chunk.types]`.
Two hard-coded exceptions independent of the table: **`0x2032` = OSM DerivedObject**, **`0x2033` = WSM DerivedObject** (the modifier-stack wrapper — see §5). **[V]**

### `Config` layout (skip for X-ray) **[V]**
Container keyed by unique ids (`0x2090,0x20e0,0x20a0,0x2080,0x2180…`). `0x2180` = `ConfigScript`, holds `0x0040` `ConfigScriptEntry` blocks whose `0x0050` header repeats a scripted class's ClassID/SuperClassID and whose `0x0007` arrays describe scripted-plugin parameter schemas (type/default/ui rows). Primitive type ids inside: `0x0001` bool(4B), `0x0003` int32, `0x0004` float, `0x0005`/`0x0006` sized string+null, `0x0007` array-container, `0x0008` color(3×float,255=max), `0x0060` = array count. Not needed for the X-ray but confirms where scripted-plugin schemas live.

---

## 3. The `Scene` stream — object records, references, nodes

**Top structure:** one outer container whose id encodes the file/scene version (observed `0x2012` in a 2010 file; Kaetemi notes it varies by version; ryzomcore treats `CScene` version as the id of its single child container). Inside are N object chunks in **dependency order**: lowest-level dependencies first (file *starts* with a ParamBlock2, *ends* with the `Scene` object). **[V]**

**Objects reference each other by positional index** into the scene container (0-based). Two mutually-exclusive reference chunks (never both in one object) — verified ryzomcore `CReferenceMaker::parse`, io_scene_max `get_references`/`get_reference`, Kaetemi Part 5:

- **`0x2034` = flat reference array**: `int32[]`, each entry a scene index (or `-1` = NULL). Position in the array = reference slot number. **[V]**
- **`0x2035` = reference map**: `uint32[]` = `[flags, key0, idx0, key1, idx1, …]`. First value is a header/flag; then (slot-key, scene-index) pairs. **[V]**

**Node (INode) chunks** — the scene-graph carriers. A Node's references (via `0x2035` map) use these **canonical slot keys** (io_scene_max `get_matrix_mesh_material`, corroborated by Kaetemi + a real dump): **[V]**
- slot **0** → transform controller (PRS / Position-Rotation-Scale)
- slot **1** → the **object** it displays (mesh/poly/light/camera/DerivedObject…)  ← this is where mesh/poly/proxy lives
- slot **3** → material
- slot **6** → layer

Real Node `0x2035` payload (36 bytes) from a live dump: `10 00 00 00 | 00→384 | 01→387 | 03→134 | 06→388` = flags 0x10, then (0,384)=PRS, (1,387)=Editable Poly, (3,134)=NeL Material, (6,388)=Base Layer. **[V]**

**Other Node chunks** (ryzomcore `node_impl.cpp`, io_scene_max, live dump) — all directly useful: **[V]**
| id | meaning |
|---|---|
| `0x0960` | parent link: `uint32[2]` = `(parentSceneIndex, flags)`. Root's parent is the RootNode. |
| `0x0962` | UTF-16LE **node name** (the object name shown in the scene) |
| `0x0963` | node flags (2×uint32) |
| `0x09ce` | node version (4B) |
| `0x099c` | render flags |
| `0x096a/0x096b/0x096c` | object-offset pos / rot(quat) / scale |
| `0x2150` | AppData block (per-node custom app data; see §10) |
| `0x2034`/`0x2035` | references (as above) |

**RootNode** ClassID `(0x00000002,0)` SuperClass `0x1`, class chunk id typically distinct; **Node** ClassID `(0x00000001,0)` SuperClass `0x1`. The `Scene` object itself: ClassID `(0x00002222,0)` SuperClass `0x100` (built-in), and its `0x2034` lists all top-level nodes. **[V]**

**AppData chunk `0x2150`** (Kaetemi Part 4): a `0x0100` count header then N `0x0110` entries, each `{0x0120 header, 0x0130 value}`. The `0x0120` header is `ClassID(8) + SuperClassID(4) + subID(4) + size(4)` identifying which plugin owns the appdata. This is how per-node plugin properties (and some malware markers) attach. **[V]**

---

## 4. Reference/class hierarchy (so the parser knows what a chunk *is*)

Classes are serialized **in inheritance order** (Animatable → ReferenceMaker → ReferenceTarget → … → concrete). The first chunk in many objects is AppData (from Animatable); the reference chunk (`0x2034`/`0x2035`) is written by ReferenceMaker; then subclass-specific chunks. **[V]** — Kaetemi Part 5, ryzomcore class tree.

Built-in SuperClassIDs the parser will meet, with ryzomcore's confirmed values (match the SDK, §6): ReferenceMaker `0x100`, ReferenceTarget `0x200`, Node `0x1`, GeomObject `0x10`, Modifier(OSM) `0x810`, WSModifier `0x820`, Material `0xC00`, Texmap `0xC10`, ParamBlock2 `0x82`, ParamBlock `0x8`, Control* `0x900x`, DerivedObject/WSMDerived superclass `0x0` (they masquerade as objects). **[V]**

---

## 5. Modifier stacks — DerivedObject (0x2032 / 0x2033)

**This is the mechanism for "modifier stack depth + modifier classes".** **[V]** — ryzomcore `derived_object.cpp` + `scene_class_container` is the authoritative decode; io_scene_max only skips these.

- A node whose object slot points to a **DerivedObject** (`0x2032` = OSM/object-space stack; `0x2033` = WSM/world-space stack) has a modifier stack. DerivedObject ClassID `(0x29263a68,0x405f22f5)` "OSM Derived"; WSM `(0x4ec13906,0x5578130e)`. These ids are **hard-coded**, not table-driven. **[V]**
- The DerivedObject is a ReferenceTarget whose **reference list is the stack**: each reference whose class has **SuperClassID `0x810` (OSM) or `0x820` (WSM)** is a **modifier**; the remaining (last) reference is the **base object** (the mesh/poly/primitive). So:
  - **modifier-stack depth = count of references with superclass 0x810/0x820.** **[V]**
  - **modifier classes = ClassDirectory3 lookup of each such reference's chunk id.** **[V]**
  - **base object = the last non-modifier reference** (usually GeomObject/Shape, but may be nested DerivedObject/XRef/Helper/Camera). **[V]**
- Orphan chunks in the DerivedObject: a contiguous run of **`0x2500` "ModApp"** containers, one per modifier reference in order (pairs modifier→its application context), then one empty `0x2501` tail. Inside each `0x2500`: `0x2510` = 4×3 float row-major TM (48B)+4B flags (52B total), `0x2511` = bbox (24B), `0x2512` local mod data, `0x2513` (4B). **[V]**

So the traversal for the X-ray is: Node → object ref (slot 1). If that class is `0x2032`/`0x2033` → it's a DerivedObject: enumerate its refs, classify by superclass to get `[modifiers…]` + base; recurse into base for the actual geometry. If the object slot is directly a GeomObject, stack depth = 0. **[V]**

---

## 6. Class IDs & Super Class IDs (SDK) — the identification table

### Super Class IDs — **[V]** verified from `plugapi.h` (mathieumg/inf4715 mirror) and Autodesk SDK docs
```
BASENODE_CLASS_ID        0x000001   GEN_DERIVOB_CLASS_ID     0x000002
DERIVOB_CLASS_ID         0x000003   WSM_DERIVOB_CLASS_ID     0x000004
GEOMOBJECT_CLASS_ID      0x000010   CAMERA_CLASS_ID          0x000020
LIGHT_CLASS_ID           0x000030   SHAPE_CLASS_ID           0x000040
HELPER_CLASS_ID          0x000050   SYSTEM_CLASS_ID          0x000060
REF_MAKER_CLASS_ID       0x000100   REF_TARGET_CLASS_ID      0x000200
OSM_CLASS_ID (Modifier)  0x000810   WSM_CLASS_ID             0x000820
WSM_OBJECT_CLASS_ID      0x000830   SCENE_IMPORT_CLASS_ID    0x000A10
SCENE_EXPORT_CLASS_ID    0x000A20   MATERIAL_CLASS_ID        0x000C00
TEXMAP_CLASS_ID          0x000C10   UVGEN_CLASS_ID           0x000C20
XYZGEN_CLASS_ID          0x000C30   TEXOUTPUT_CLASS_ID       0x000C40
SHADER_CLASS_ID          0x0010B0   ATMOSPHERIC_CLASS_ID     0x001010
UTILITY_CLASS_ID         0x001020   TEXMAP_CONTAINER_CLASS_ID 0x001080
CUST_ATTRIB_CLASS_ID     0x001160   RENDERER_CLASS_ID        0x000F00
RENDER_ELEMENT_CLASS_ID  0x001150   LAYER_CLASS_ID           0x0010F0
CTRL_FLOAT_CLASS_ID      0x009003   CTRL_POSITION_CLASS_ID   0x00900B
CTRL_ROTATION_CLASS_ID   0x00900C   CTRL_SCALE_CLASS_ID      0x00900D
CTRL_MATRIX3_CLASS_ID    0x009008   SCENE_CLASS_ID           0xFFFFFD00
MAXSCRIPT_WRAPPER_CLASS_ID 0xFFFFFF06
```
(Decimal super-class ids you'll see in dumps: 16=GeomObject, 32=Camera, 48=Light, 64=Shape, 80=Helper, 3072=Material, 3088=Texmap, 2064=OSM Modifier, 2080=WSM, 256=ReferenceMaker, 512=ReferenceTarget.) **[V]**

### Predefined built-in Class IDs — **[V]** from `plugapi.h`
```
TRIOBJ_CLASS_ID          0x0009                       (TriObject; classic mesh primitive base)
EDITTRIOBJ_CLASS_ID      0xE44F10B3                   (Editable Mesh)      ClassID=(0xE44F10B3, 0)
POLYOBJ_CLASS_ID         0x5D21369A                   (PolyObject)          =(0x5D21369A, 0)
EPOLYOBJ_CLASS_ID        Class_ID(0x1BF8338D,0x192F6098)  (Editable Poly)
PATCHOBJ_CLASS_ID        0x1030    NURBSOBJ_CLASS_ID   0x4135
BOXOBJ_CLASS_ID          0x0010    SPHERE_CLASS_ID     0x0011   CYLINDER 0x0012  TORUS 0x0020
CONE_CLASS_ID            0xA86C23DD  PLANE_CLASS_ID    Class_ID(0x081F1DFC,0x77566F65)
TEAPOT_CLASS_ID1/2       0xACAD13D3 / 0xACAD26D9
NEWBOOL_CLASS_ID         Class_ID(0x51DB4F2F,0x1C596B1A)  BOOLOBJ(old) 0x387BB2BB
DUMMY_CLASS_ID           0x876234   BONE_CLASS_ID       0x8A63C0   BONE_OBJ_CLASSID Class_ID(0x28BF6E8D,0x2ECCA840)
TARGET_CLASS_ID          0x1020     (light/cam target)   MORPHOBJ 0x1021
XREFOBJ_CLASS_ID         Class_ID(0x92AAB38C, 0)          XREFATMOS Class_ID(0x4869D60F,0x21AF2AE4)
XREFMATERIAL_CLASS_ID    Class_ID(0x272C0D4B,0x432A414B)  XREFCTRL Class_ID(0x32FB4637,0x65FD482B)
DMTL_CLASS_ID (Standard) 0x00000002  MULTI_CLASS_ID (Multi/Sub) 0x0000200
DOUBLESIDED 0x210  MIXMAT(Blend) 0x250  MATTE 0x260
BMTEX_CLASS_ID (Bitmap tex) 0x0000240  CHECKER 0x200 NOISE 0x234 FALLOFF 0x2A0 MIX 0x230 GRADIENT 0x270
NORMAL_BUMP              Class_ID(0x243E22C6,0x63F6A014)
Modifiers:
  BENDOSM 0x10  TAPEROSM 0x20  TWISTOSM 0x90  UVWMAPOSM 0xF72B1  SELECTOSM 0xF8611
  SMOOTHOSM 0xF8613  NORMALOSM 0xF8614  OPTIMIZEOSM 0xC4D31  TESSELLATE 0xA3B26FF2  MESHSELECT 0x73D8FF93
  EDIT_POLY_MODIFIER_CLASS_ID  Class_ID(0x79AA6E1D,0x71A075B7)
  EDITMESH (Edit Mesh mod)     0x00050   DELETE 0xF826EE01
```
Subdivision/smoothing modifiers — **[V]** from Autodesk/3dsmax-usd `SkinMorpherWriter.cpp`:
```
TurboSmooth   Class_ID(0x0D727B3E,0x491D29A7)     Meshsmooth   Class_ID(0x00000032,0x00007F9E)
OpenSubdiv    Class_ID(0x73CCF34A,0x9ABC45FC)     Chamfer      Class_ID(0x332C9510,0x38BB548C)
Subdivide     Class_ID(0x10E36629,0x0E54570E)     ArrayModifier Class_ID(0x8EB2B3F7,0x57DA4442)
Lattice       Class_ID(0x148132A1,0x2ED9401C)     Optimize     Class_ID(0x000C4D31,0)
ProOptimizer  Class_ID(0x3EF24FE4,0x5932330A)     MultiRes     Class_ID(0x6A9E4C6B,0x494744DD)
```
(Cross-check: TurboSmooth also appears as decimal `#(225606462,1226647975)` in Autodesk MAXScript docs = the same `(0x0D727B3E,0x491D29A7)`.) **[V]**

### Plugin (V-Ray / renderer) Class IDs — **[V]** except where noted
From maxjs `maxjs_material_sync.h`, io_scene_max constants, Deadline `customize.ms`, VrayHDRI converter:
```
VRayMtl          Class_ID(0x37BF3F2F,0x7034695C)   (935280431,1882483036)   [V]
VRayBitmap/HDRI  Class_ID(0x6769144B,0x02C1017D)   (1734939723,46203261)    [V]  (VRayHDRI texmap)
VRayNormalMap    Class_ID(0x71FA6E51,0x72057C2F)   (1912237649,1912962095)  [V]
VRay2SidedMtl    (io_scene_max VTWO_MTL) = (0x6066686A,0x11731B4B)          [V]
Standard Surface(Arnold aiStandardSurface) (0x7E73161F,0x62F74B4C)          [V]
PhysicalMtl      Class_ID(0x3D6B1CEC,0xDEADC001)                            [V] (SDK PHYSICALMATERIAL)
Arch & Design    (0x70B05735,0x4A163654)                                    [V]
CoronaMtl        (0x70BE6506,0x448931DD)  CoronaPhysicalMtl (0x6912AB89,0x87151720)
CoronaLayeredMtl (0x65486584,0x8425554E)  CoronaSkin/Hair per io_scene_max  [V] (io_scene_max)
Renderer ClassIDs (Deadline customize.ms, decimal → hex): [V]
  V-Ray      #(1941615238,2012806412)=(0x73BAB286,0x77F8FD0C)
  V-Ray RT   #(1770671000,1323107829)=(0x698A4B98,0x4EDD05F5)
  Corona     #(1655201228,1379677700)=(0x62A85DCC,0x523C3604)
  Arnold(ART renderers distinct) Arnold #(2980329694,2688902778)=(0xB1A438DE,0xA045667A)
  Redshift   #(198269858,1937796512)=(0x0BD15BA2,0x73806DA0)
  Octane     #(2717442453,1319335807)=(0xA1F8E195,0x4EA3777F)
  mental ray #(1492548972,1338981315)=(0x58F67D6C,0x4FCF3BC3)
```
**Scene "Render Data" already stores the renderer ClassID** as plain strings `Renderer ClassIDA=...`, `Renderer ClassIDB=...`, `Renderer Name=...` in DocumentSummaryInformation — a free renderer-detection channel. **[V]**

**VRayProxy / ForestPack / RailClone / tyFlow — Class IDs NOT found:** **[U]**
- Their **DLL filenames ARE known** and detectable via DllDirectory (a reliable proxy for "scene uses this plugin"): `ForestPackPro.dlo`, `railclonepro.dlo`, V-Ray `vrender####.dlr` / `vray*.dll`, `tyFlow.dlo` (+ `tyCache`, `tyMesher`, `tySplineMesher` object classes). **[V]** — maxupdater `plugins.yaml`, tyFlow docs.
- The numeric Class IDs of VRayProxy (the geometry object), ForestPack "Forest_Pro" object, RailClone object, and tyFlow object were **not** locatable in public sources. RailClone forum error message revealed one RailClone-family class: superID `0xC10` classID `(0x2AB75796,0x49DF4F45)` (a texmap, not the object). **[U — must be read empirically from a real scene's ClassDirectory3, matching by the class DISPLAY NAME string `0x2042`, which is the robust approach anyway.]**

### Encoding note for the parser **[V]**
ClassDirectory3 `0x2060` stores ClassID as a `uint64` where **low 32 bits = partA, high 32 bits = partB**. io_scene_max's 64-bit comparison constants therefore read "backwards": e.g. `EDIT_POLY = 0x192F60981BF8338D` = partB(0x192F6098)<<32 | partA(0x1BF8338D). When you compare against SDK `Class_ID(partA,partB)`, compute `guid64 = (partB<<32)|partA`. **[V]**

---

## 7. Mesh / Poly geometry & size heuristics

**Geometry lives in chunk `0x08FE` ("GeomBuffers") inside a GeomObject** (io_scene_max `get_first(0x08FE)`, ryzomcore `geom_object.cpp` `PMB_GEOM_BUFFERS_CHUNK_ID 0x08fe`). **[V]**

### Editable Poly (`0x08FE` children) — **[V]** ryzomcore `geom_buffers.cpp` + io_scene_max + live dump
```
0x0100  poly VERTEX buffer   = uint32 count, then count × { uint32 i1; float x,y,z }  (16 B/vertex)
0x010A  poly EDGE buffer     = count-prefixed × { uint32 i1; uint32 a; uint32 b }
0x011A  poly FACE buffer     = per-face variable record: serialCont(Vertices=uint32[]),
        then a uint16 bitfield: bit0x01→uint32 I1, 0x08→uint16 Material, 0x10→uint32 SmGroups,
        0x20→triangulation ((deg-3) pairs of uint32). Variable length ⇒ counts, not byte/stride.
0x0124/0x0128/0x012B  map-channel index/coords/faces (UV)
0x0310  alternative poly-data (n-gon vertex index lists)
0x0150/0x0140/0x0130/0x0120/0x0200/0x0320  small poly headers/flags
```
Live dump example (one EditablePoly): `0x0100`=3188 B, `0x010A`=4660 B, `0x011A`=7042 B, `0x0128`=3292 B, `0x012B`=3800 B, `0x0310`=7720 B. **Vertex count = (chunkSize(0x0100) − 4) / 16.** **[V]**

### Editable Mesh (TriObject family) `0x08FE` children — **[V]** ryzomcore + io_scene_max
```
0x0914  tri A VERTEX buffer  = uint32 count, then count × { float x,y,z }  (12 B/vertex; CVector)
0x0912  tri A FACE buffer    = uint32 count, then count × { uint32 a,b,c; uint32 alwaysOne; uint32 smGroups } (20 B/face)
0x0916/0x0918  tri B vertex/index    0x0938/0x0942  tri C
0x2394/0x2396  map vertex/face (mesh UVs)   0x0959/0x2398  map channel / support
```
**Vertex count = (chunkSize(0x0914) − 4)/12; Face count = (chunkSize(0x0912) − 4)/20.** io_scene_max `get_point_array` / `get_mesh_polys` confirm exactly this. **[V]**

### Fast counts without geometry decode **[V]**
`DocumentSummaryInformation` already carries `"Vertices: N"`, `"Faces: N"` (whole-scene "Mesh Totals") and per-scene `Objects/Shapes/Lights/Cameras/Helpers/SpaceWarps/Total`. Per-*object* counts require the `0x08FE` arithmetic above. Both are available with no Max.

### SDK memory heuristics (for RAM estimation, not on-disk) **[V]**
- `Mesh` (triangular) `Face` struct = `DWORD v[3]; DWORD smGroup; DWORD flags` = **12 bytes core** per face (indices) + smGroup+flags → ~20 B; verts are separate `Point3` (12 B) arrays. Docs: [Face](https://help.autodesk.com/cloudhelp/2023/ENU/Max-Developer-Help/cpp_ref/class_face.html).
- `MNMesh` (poly) is a **winged-edge** structure: "requires far more memory and processing time than an equivalent normal mesh." `MNVert` = `Point3 p; int orig` + flags; `MNFace` = `int deg; int* vtx; int* edg; int* diag; DWORD smGroup; MtlID material; int track; BitArray visedg/edgsel/bndedg` — variable, pointer-heavy. Rule of thumb: **poly (MNMesh) memory ≫ tri (Mesh) memory** for the same shape. Docs: [MNMesh](https://help.autodesk.com/cloudhelp/2023/ENU/Max-Developer-Help/cpp_ref/class_m_n_mesh.html), [MNFace](https://help.autodesk.com/cloudhelp/2023/ENU/Max-Developer-Help/cpp_ref/class_m_n_face.html).

### mesh/poly/scatter/proxy classification for the X-ray **[V for mesh/poly; I/U for scatter/proxy]**
- **Editable Poly** ⇔ ClassID `(0x1BF8338D,0x192F6098)` or object has `0x08FE`→`0x0100` (poly vertex buffer). **[V]**
- **Editable Mesh / TriObject** ⇔ ClassID `(0xE44F10B3,0)` / TRIOBJ `0x0009`, or `0x08FE`→`0x0914`. **[V]**
- **Primitive** (Box/Sphere/etc.) ⇔ small predefined ClassIDs (§6), geometry rebuilt from a ParamBlock not a vertex buffer. **[V]**
- **Proxy** (VRayProxy) ⇔ detect by ClassDirectory3 **class name** containing "VRayProxy"/"Proxy" and/or DllDirectory `vray*` present; the object has **no** large `0x08FE` vertex buffer (geometry is external at render time). **[I]** (Class ID unknown — match by name string, robust.)
- **Scatter** (Forest Pack / RailClone / tyFlow) ⇔ ClassDirectory3 class name ("Forest", "RailClone", "tyFlow"/"tyCache"…) + DllDirectory `ForestPackPro.dlo` / `railclonepro.dlo` / `tyFlow.dlo`. **[I]** (Class IDs unknown — match by name/DLL.)

**Recommendation:** classify primarily by **ClassDirectory3 `0x2042` display-name string + DllDirectory filename**, not by hard-coded plugin ClassIDs. This survives version drift and covers the plugins whose numeric ids are unknown. **[I]**

---

## 8. Custom file streams (2019+) — `CustomFileStreamDataStorage`

MAXScript `CustomFileStream`/`CustomSceneStreamManager` write user/plugin data into the `.max` OLE using **standard Structured Storage**, explicitly "so you can read and write to these streams outside of 3ds Max." **[V]** Layout (SDK `CustomFileStreamAPI.h`):
- Stored under an OLE storage named **`"CustomFileStreamDataStorage"`**; stream names ≤ **31 chars** (OLE limit), case-sensitive.
- Each stream begins with a header: **`WORD version` (=1, `kCustomFileStreamVersion`), `DWORD privateFlags`, `DWORD publicFlags`**, then wide-char (`wchar_t`, UTF-16) content — single string or string array.
- Private flag bits: `kPersistentStream=1`, `kSaveNonPersistentStream=2`, `kNoLoadOnSceneLoad=4`. **[V]**
- `CustomSceneStreamManager` loads/caches persistent streams on file load, saves on file save. Source: [Custom File Streams](https://help.autodesk.com/cloudhelp/2025/ENU/MAXDEV-CPP-API-REF/_custom_file_stream_a_p_i_8h.html). A read-only tool can enumerate/read these to surface plugin sidecar data.

---

## 9. `FileAssetMetaData2/3` — external asset paths (bitmap/xref detection)

Introduced 2010 (`…2`), changed format 2014 (`…3`). Holds one record per external asset. **[V]** — Neil Marshall reverse-engineered the `…2` layout in C#:
```
per asset record:
  16 bytes  Asset GUID (little-endian GUID, e.g. Python: uuid.UUID(bytes_le=...))
  int32     length of name string (in UTF-16 chars)
  (len+1)*2 UTF-16LE string + 0x0000 terminator     (the "name"/key, e.g. "Filename")
  int32     length of value string
  (len+1)*2 UTF-16LE string + 0x0000 terminator     (the value = the asset path)
  08 00     record/field separator
```
MAXScript exposes the same via `getMAXFileAssetMetadata <file>` → array of `AssetMetadata_StructDef {assetId, filename, type}` where `type` ∈ `#bitmap #xref #photometric #other …`; `setMAXFileAssetMetadata` writes it back. This confirms: **the file lists every bitmap/xref path with a type tag** — ideal for the bitmap/texture-node count and missing-asset reporting. **[V]** io_scene_max reads it as `META_DATA` (via `set_meta_data`) and cross-links bitmaps by metadata index. The `…3` binary layout differs and is **[U]** in exact bytes (must be probed), but `DocumentSummaryInformation`'s "External Dependencies" string list gives the filenames regardless. **[V]**

**Bitmap/texture-node count for the X-ray:** count `Scene` objects whose superclass = Texmap (`0xC10`) — e.g. class name "Bitmap" ClassID `(0x240,0)` — and/or count FileAssetMetaData `#bitmap` records. **[V]**

**Material count:** count `Scene` objects whose superclass = Material (`0xC00`); DocumentSummaryInformation also lists a "Materials" section by name. **[V]**

---

## 10. Embedded-script malware (ALC / CRP / ADSL / PhysXPluginMfx / MSCPROP) — signatures to flag

**Mechanism (all confirmed):** malicious MAXScript rides in ordinary, auto-executing scene constructs. On scene open/merge/xref the script runs, writes hidden `.ms/.mse` files into the startup-scripts folder, and re-infects. **[V]** — 3dground reverse-engineering, Autodesk, CoreLabs CVE-2009-3577.

**Carriers inside the `.max` (what to scan):** **[V]**
1. **Scripted controllers** — ALC hides in a **Scale/position script controller** (a controller object whose class is a MAXScript scripted controller). "When the scene is opened, all script controllers are executed."
2. **Persistent global variables** — CRP/ADSL store the payload in a **persistent global** (saved in the scene, executes on load). Persistent globals serialize into the scene; controlled by `LoadSavePersistentGlobals` in `3dsmax.ini`.
3. **Persistent callbacks** — `callbacks.addScript #filePostOpen ("DOSCommand(\"calc.exe\")") id:#… persistent:true`. `persistent:true` means "stored in the current file and re-registered whenever that file is opened." This is CVE-2009-3577. Controlled by `LoadSaveSceneScripts`.
4. **Scripted custom-attribute definitions** — stored in the **`ScriptedCustAttribDefs`** stream (SDK: `MSCustAttribDef::save_custattrib_defs(ISave*)`, chunk `MSCUSTATTRIB_CHUNK 0x0110`; interface id `I_SCRIPTEDCUSTATTRIB 0x000010C1`). Kryptik/AD-Web/DesireFX CA variants live here and run on open/merge. **[V]**
5. **Empty helper nodes** with junk names (ALC markers): `¡¡×ý×û`, `×þ×ü`. **[V]**
6. **rootNode/rootScene properties + custom attributes** (MSCPROP/Alienbrains). **[V]**

**Name signatures you can grep in the raw stream bytes (UTF-16LE) — [V]** (3dground):
- Persistent-global names: `AutodeskLicSerStuckCleanBeta` (ALC), `AutodeskLicSerStuckCleanAlpha` (ALC2), `AutodeskLicSerStuckAlpha` (ALC3), `ADSL_BScript`, `CRP_BScript`, `CRP_AScript`, `physXCrtRbkInfoCleanBeta` (PhysXPluginMfx).
- Scripted-CA def name signature (Kryptik/spyware CA): `Sound_GrayDeskUnderFourOldDriverGoPlane`.
- Startup files it writes (also may be referenced in-scene): `vrdematcleanbeta.ms/.mse/.msex`, `vrdematcleanalpha.*`, `vrdestermatconvertor.*`, `vrayimportinfo.mse`, `vrdematpropalpha.*`, `PhysXPluginStl.ms/.mse`, `mscprop.dll`, `PropertyParametersLocal.mse`, `Local_temp.ms`.
- Advertised sites (payload strings): `Maxscript.cc/update/upscript.mse`, `3dsmj.com`, `3d.znzmo.com`. `.mse` PhysX payload = "base64 encoded .NET 4.5 assembly."

**Autodesk's own list** (Scene Security Tools 2.1.8 / "Malware Removal"): detects **CRP/ADSL, ALC, ALC2, PhysXPluginMfx, MSCPROP, and variants**; scans startup scripts + scene files (however loaded). It only removes *known* signatures — cannot certify a novel script clean. **[V]**

**Safe Scene Script Execution — the "unsafe command" grep list** (2021.3+) — commands whose presence in an embedded script is a strong malware signal. **[V]** (Autodesk blocked-commands page): `DOSCommand`, `HiddenDOSCommand`, `ShellLaunch`, `createOLEObject`/`registerOLEInterface`/`releaseOLEObject`, `dragAndDrop.downloadPackage`/`.DownloadUrlToDisk`/`.dropPackage`, `internet.CheckConnection`, `LoadDllsFromDir`, `macros.load`/`macros.new`, `registry.*` (createKey/setValue/deleteKey…), `SetDir`, `systemTools.setEnvVariable`, `windows.postMessage`/`sendMessage`, `deleteFile`/`removeDir`/`setFileAttribute`, `openEncryptedFile`/`encryptScript`/`encryptFile`, `fopen`/`openFile`/`createFile`/`copyFile`/`renameFile`/`fileIn`/`executeScriptFile`/`setINISettings`, all **Python** and all **3rd-party .NET** calls.

**X-ray strategy:** (a) presence of `ScriptedCustAttribDefs` stream with content → flag "scene contains scripted CA defs"; (b) grep all stream bytes (UTF-16LE and ASCII) for the name signatures + unsafe-command tokens above; (c) flag any scripted controller / persistent-global / persistent-callback constructs; (d) report `.ms/.mse/.dll` filename strings found in-scene. This gives signature-level detection with no Max. **[V]**

---

## 11. Reading names without loading (MAXScript, for cross-check only)

`getMAXFileObjectNames <file> [quiet:]` returns object names without loading; `getMAXFileAssetMetadata`, `getMAXFileSaveVersion`, `fileProperties.getPropertyValue`, `getMAXFileXRefs` exist too — but all require Max. Our parser reproduces these from `DocumentSummaryInformation` (names/totals/version/plugins) + `Scene`/`ClassDirectory3` (classes) + `FileAssetMetaData` (assets). **[V]**

---

## 12. Files downloaded (all under the research dir)
`/private/tmp/claude-501/-Users-aasish/72256f08-60fc-40f8-a093-73645167fc3a/scratchpad/research/`

Working parser sources (primary evidence):
- `io_scene_max/import_max.py` (86 KB), `io_scene_max/__init__.py`, `io_scene_max/blender_manifest.toml` — **Blender "Import Autodesk MAX (.max)"** by Sebastian Schrand (NRG Sille), from `github.com/nrgsille76/io_scene_max` (main, updated 2026-08-13). **License GPL-2.0-or-later** (repo says GPL-2.0; manifest says GPL-3.0-or-later — dual-stated). Credits Jens M. Plonka's FreeCAD ImportMAX + Philippe Lagadec's olefile. This is the closest thing to a maintained pure-Python `.max` reader.
- `classids/` mirror files: `plugapi.h` (SDK Class/SuperClass IDs), `maxjs_material_sync.h` (V-Ray ids), `SkinMorpherWriter.cpp` (TurboSmooth/MeshSmooth ids), `mxsCustomAttributes.h` (ScriptedCustAttribDefs save/load), `deadline_customize.ms` (renderer ClassIDs), `VrayHDRI_To_ArnoldImage.ms`, `maxupdater_plugins.yaml` (plugin DLL filenames), `pymxs.pyi`, `mxs_functions.txt`, misc `.ms`.
- `max_dump/` — NeKidaem/max_dump (MIT): `storage_parser.py` (chunk reader), `class_directory_3.py`, `dll_directory.py`, `file_props_parser.py` (DocumentSummaryInformation reader), `dump_cameras.py`, `utils.py`, `README.md`, `todo.md`, plus a sample props JSON.
- `ryzomcore/nel/tools/3d/pipeline_max/` — **Jan Boon (Kaetemi)'s C++ `pipeline_max`** full reader/writer (AGPL-3.0): `storage_chunks.{h,cpp}` (authoritative chunk header logic incl. 64-bit escape + write path), `scene.cpp`/`scene_class*.cpp` (index→class linkage, 0x2032/0x2033), `class_directory_3.cpp`, `dll_directory.cpp`, `builtin/*` (animatable/reference_maker/node/derived_object=modifier stacks/mtl/paramblock2/track_view_node), `epoly/editable_poly.cpp`, `update1/editable_mesh.cpp`, `builtin/storage/geom_buffers.{h,cpp}` (0x08FE vertex/face buffer layouts). **This is the single most complete open decode of the Scene stream.**

Reference articles (converted to `.txt`):
- `https:__blog.kaetemi.be_*part-1..6*.txt` — Kaetemi's 6-part reverse-engineering series (Parts 1–5 substantive; 6 is a teaser).
- `http:__dl.kaetemi.be_blog_maxfile_dump_4.txt` (824 KB) — a **full parsed dump** of a real 2010 `.max` (DllDirectory, ClassDirectory3 with 57 entries, and the entire Scene with every object's chunks). Ground-truth for chunk ids, node/reference layout, EPoly `0x08FE`, material chunks.
- `https:__neiltech.wordpress.com_*.txt` — C# MaxFileParser (FileAssetMetaData2 byte layout).
- `http:__fileformats.archiveteam.org_*.txt`, `https:__3dground.net_*.txt` (×2 — malware mechanics), `web.archive.org_*coresecurity*.txt` (CVE-2009-3577 PoC), `apps.autodesk.com_*.txt` (Scene Security Tools).

---

## 13. What remains UNKNOWN / must be discovered empirically against real files

1. **Plugin object Class IDs for VRayProxy, Forest Pack ("Forest_Pro"), RailClone, tyFlow, and most 3rd-party geometry/modifiers.** Only their DLL filenames + class display-name strings are known. **Mitigation:** classify by ClassDirectory3 `0x2042` name string + DllDirectory filename (version-robust anyway). Capture actual ids from real infected/plugin scenes to build a lookup.
2. **`FileAssetMetaData3` exact binary layout** (2014+). The `…2` layout is known (Neil Marshall); `…3` "changed" — probe a modern file. DocumentSummaryInformation "External Dependencies" is a fallback.
3. **Full per-chunk semantics of light/camera/controller/space-warp payloads.** The X-ray only needs class+size+refs (all covered), but decoding e.g. light parameters needs empirical work (ParamBlock2 `0x000E` record decode is in ryzomcore `param_block_2.cpp` but is version-sensitive: Max3 uses `0x000A` 11-byte headers, Max9+ uses `0x000E` 15-byte).
4. **Scene stream version id → Max version mapping.** Observed `0x2012` (2010), `0x2004` (Max 3, per ryzomcore). Build a table by opening files from known versions.
5. **Exact ScriptedCustAttribDefs stream chunk layout** for extracting the MAXScript *source text* of embedded CA defs (SDK confirms `save_custattrib_defs`/`load_custattrib_defs` + chunk `0x0110`, but the serialized field order needs a real sample). Signature grep works without full decode.
6. **Whether a given persistent-callback / scripted-controller is malicious vs legitimate** — only known signatures are certain; novel scripts require heuristic flagging (unsafe-command grep), never a clean guarantee.
7. **64-bit-header prevalence** — the `size==0`→uint64 escape is real but rare (huge meshes); ryzomcore even asserts against >2 GB. Test against a very large scene.
8. **Compressed-save variants** beyond the three zlib magic-longs io_scene_max handles — newer Max "compressed" saves may use other framing; verify on 2020+ files.
