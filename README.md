# RenderDoc Scene Exporter (RenderDoc extension)

> _For a non-vibe-coded alternative to this project, [pay €6.50 (+ vat)/mo for NinjaRipper](https://www.ninjaripper.com/)¹._ Then there's also [Intel GPA](https://www.intel.com/content/www/us/en/developer/tools/graphics-performance-analyzers/overview.html) (EOL; [Internet Archive mirror](https://archive.org/details/intel-gpa-framework-25.1)), which gets you easy mesh exports for free. Note that both options are Windows-only, and note that Intel GPA is considered EOL. Intel claimed the download page will be removed few months ago, which means the official (non-IA) link is on borrowed time.
>
> <sub>[1] €6.50+vat/mo at the time of writing. I am not gonna be keeping up with any possible future changes. Do your homework before you spend any money. I am not responsible if NinjaRipper doesn't fulfill your needs, or if NinjaRipper doesn't work with your game(s), or if you can't figure out how to use NinjaRipper, or if NinjaRipper is not fit for purpose.'</sub>

Exports geometry of a scene captured by renderdoc as OBJ, as well as all textures.
Export script attempts to split objects by render pass, and allows you to export objects
from a single render pass.

You can export input meshes (with character, et. al. meshes in a T-pose), as well as posed meshes 
(with character meshes assuming the same pose as seen on screen at the moment of capture).

Note that posed meshes are very squished in their native form. This extension attempts to undo the squish, but doesn't quite get there.

Also see "known limitations" down at the bottom.

## Install

0. Download the zip file from Releases page.
1. Unzip & copy the whole `renderdoc_scene_exporter` folder into your RenderDoc extensions directory:
   - Windows: `%APPDATA%\qrenderdoc\extensions\`
   - Linux: `~/.local/share/qrenderdoc/extensions/`
3. Go to `Tools > Manage Extensions`, tick **Renderdoc Scene Exporter** to load it
   (or restart RenderDoc after copying the folder).
4. `Tools` menu gets two new options:
    * `Export scene (dialog)` option (which would be the preferred way, but it keeps crashing Renderdoc)
    * `Export scene ...` option, which achieves more or less the same with submenu tree
     
## Usage

There are two ways to run an export, both under the `Tools` menu:

- **`Export scene (dialog)`** gives you a series of dialogs that allow you to quickly configure your export
- **`Export scene ...`** does the same, but instead of a dialog you get a submenu tree

1. Load a capture into RenderDoc
2. Go to `Tools > Export scene (dialog)`, or pick one of the
   `Tools > Export scene ...` presets
3. Select draw call type(s), then which render pass(es) you want to export. Most of the time, you probably want 
    only the render forward or g-buffer render pass with most draw calls, as exporting a single pass is significantly faster
    than exporting the entire capture. However, crashing may necessitate exporting all draw calls of a type, or even all draw calls.
4. Create a new folder and select it as your export destination
5. Go get a coffee, export is gonna take five-ever

## Why not [Renderdoc Resource Exporter](https://github.com/rrtt2323/RenderdocResourceExporter/tree/main/RenderdocResourceExporter)

1. Renderdoc Resource Exporter appears to only export a single resource (model), not the entire scene
2. When I try to load the addon, I get an error that says some Python library is missing
3. RenderDoc Resource Exporter relies on `.dll` and `.exe` files, which is less than ideal when you're using Linux

## Other notes from the tin can

### Pass splitting

Games typically render a frame in multiple passes over the same or
overlapping geometry - a shadow-map pass, a depth prepass, a G-buffer
pass, the main forward/deferred pass, post-process full-screen quads,
UI, etc. Combining all of that into one Blender import makes duplicate
copies of the same geometry intersect each other in confusing ways.

Each draw is grouped by the exact set of render targets it writes into
(color targets + depth target) - a shadow pass writes to its own depth
texture, a G-buffer pass writes to a distinct set of color targets, the
final pass writes to the backbuffer, and so on. This is engine-agnostic
(it doesn't depend on debug marker names existing at all) and reliably
separates passes that would otherwise overlap. Output layout:

```
out_dir/
  textures/              (shared across all passes, deduplicated)
  pass_00/
    meshes/eid<N>.obj
    meshes_posed/eid<N>_posed.obj   (if posed export enabled)
    manifest.json
  pass_01/
    ...
  manifest.json           (index of passes - see below)
```

Passes are numbered in the order they're first encountered while
replaying the frame, which is generally chronological rendering order
(shadow/depth passes tend to come before the main pass, etc.), but isn't
guaranteed to match any particular semantic meaning.

#### Choosing which passes to export

`Export scene (dialog)` runs in three steps:

1. **Scan** - reads `ActionDescription.outputs`/`depthOut`, which
   RenderDoc populates on every action directly from capture data (no
   replay/`SetFrameEvent` needed at all), and groups actions by that into
   the list of distinct passes. This is a plain Python loop over data
   already cached on the UI thread, so it's effectively instant regardless
   of capture size - there's no progress bar for it because there's
   nothing worth reporting progress on.
2. **Choose draw call types** - a popup lists an "All" checkbox at the
   top, checked by default and the only thing checked when the dialog
   first opens, followed by the guessed pass roles that are actually
   present in this capture (e.g. `forward`, `gbuffer`, `postprocess`,
   ...) as unchecked checkboxes, each showing its pass and draw counts
   ("x pass(es), y draw(s)"). If "All" is checked when you click "Next",
   every available type is exported regardless of the individual
   checkboxes below it; if you uncheck "All" and check one or more types
   yourself, only those are exported. If you uncheck "All" and don't
   check anything else either, "Custom selection" (checked by default)
   decides the fallback: checked picks just the single busiest type,
   unchecked exports every type (same as leaving "All" checked). Under
   "Other options", there's also an "Export all instanced meshes"
   checkbox (off by default) - see "Instanced draws" below. This is a
   single static dialog - none of these checkboxes reprogram each other
   live, since that (either directly, or via a button that closed and
   reopened the dialog) turned out to crash RenderDoc.
3. **Select passes** - a checkbox dialog lists each detected pass
   matching your chosen draw call type(s) (`pass_00`, `pass_01`, ...)
   along with its guessed role, draw count, and target counts, at most 20
   per page (paginated if there are more). "Select All"/"Select None"
   buttons are provided, all passes are checked by default, and closing
   the dialog without pressing "Export Selected" cancels the whole export
   with nothing written to disk. Only the checked passes are actually
   exported (folders for unchecked passes are never created at all) -
   the real export then runs in the background, filtered to your
   selection, checking each action's pass membership *before* replaying
   to that event so excluded passes cost nothing extra to skip over
   (though replaying between two kept-but-distant events still has to
   process everything in between internally - that part is inherent
   replay cost, not something this extension controls).

   Note: pagination and the "Select All"/"Select None" buttons all close
   this dialog and open a freshly-built one with your selections carried
   over, rather than updating the open dialog in place - directly
   checking/unchecking widgets on an already-open dialog like this
   crashes RenderDoc.

#### Guessed pass roles

Each pass gets a short guessed role based purely on the *shape* of its
render targets - there's no ground truth available without engine source
or debug markers, so treat this as a hint, not a fact:

| Tag | Guess condition | Meaning |
|---|---|---|
| `final` | any color target is the actual swapchain image | This is what ends up on screen (or an overlay drawn on top of it, e.g. UI) |
| `shadow` | depth target, no color targets | Depth-only rendering - typically a shadow map or depth prepass |
| `gbuffer` | 2+ color targets | Multiple simultaneous outputs - typically a deferred G-buffer pass |
| `forward` | exactly 1 color target + a depth target | Typical shape for a main/forward-lit scene pass |
| `postprocess` | exactly 1 color target, no depth | Typical shape for a full-screen effect or composite pass |
| `misc` | anything else, or classification failed | No confident guess |

The tag is baked into the output folder name (e.g. `pass_00_shadow`) for
convenience, but the numeric `pass_NN` prefix is what's authoritative and
stable - the guessed tag can be wrong (e.g. a `misc`-shaped pass that's
actually a shadow pass using an unusual target setup), so don't rely on
it for anything beyond a starting point when sorting through unfamiliar
captures.

There's no persistent, authoritative naming for passes across runs (e.g.
re-running the export on the same capture will number and tag them the
same way since the capture, render order, and target shapes don't
change, but the guessed tag is still just a guess, not a confirmed
identity) - use the draw counts/target counts/guessed role shown in the
dialog, or peek in the output folders after an initial export, to figure
out which numbers correspond to what for a given game.

### What it does

- Walks every draw call (`ActionFlags.Drawcall`) in the frame, grouping
  each into a pass folder as described above.
- For each draw, reads the raw input-assembler vertex buffers directly
  (position / normal / texcoord, matched by semantic name) and the index
  buffer, and writes an `.obj` per draw into that pass's `meshes/`. This
  is the **bind pose / object space** geometry, before any GPU transform.
  UV coordinates are always embedded in the OBJ (`vt` lines referenced by
  the face list) whenever the shader has a texcoord-like input, regardless
  of whether a texture ends up getting matched to it (see below).
- **Materials for Blender import**: alongside each `.obj`, if any texture
  was bound to the pixel shader for that draw, a companion `.mtl` file is
  written (`eid<N>.mtl` / `eid<N>_posed.mtl`) with a `newmtl`/`map_Kd`
  pointing at the guessed diffuse/albedo texture (and `bump` for a guessed
  normal map, if one was bound), and the `.obj` gets `mtllib`/`usemtl`
  lines referencing it. Blender's OBJ importer picks this up automatically
  and creates a material with the texture wired into Base Color using the
  mesh's existing UV map - no manual texture/material setup needed after
  import. Which bound texture is "the diffuse one" is guessed from the
  shader variable name (looking for `diffuse`/`albedo`/`basecolor`/
  `colour`, falling back to the first bound texture if nothing matches) -
  it's a heuristic, not certain, so double check the assigned image in
  Blender's Shader Editor if a mesh comes in with an unexpected texture
  (e.g. a normal map or AO map mistakenly picked as the diffuse map).
- If posed export is enabled, also calls `GetPostVSData()` to read the
  vertex shader's *output* for that draw, and writes a second
  `<name>_posed.obj` (with its own matching `.mtl`) into that pass's
  `meshes_posed/`:
  - If the shader outputs a separate position-like varying (e.g. a
    `WorldPos` passed through for lighting), that's used directly —
    this gives true world-space, posed geometry.
  - Otherwise it reconstructs an approximate **view-space** position from
    the builtin clip-space output (`SV_Position`/`gl_Position`) instead of
    doing a plain perspective divide, handled differently for perspective
    vs orthographic since they behave differently:
    - **Perspective**: `clip.w` *is* the linear view-space depth, but
      `clip.x`/`clip.y` also carry a `1/tan(fovY/2)` scale factor (call it
      `e`) that `clip.w` does NOT have - so naively using `clip.w` as Z
      while only correcting X/Y for aspect ratio leaves X/Y scaled by `e`
      relative to Z, which shows up as depth looking compressed or
      stretched unless `e` happens to be 1 (i.e. exactly 90° vertical
      FOV). To fix this properly the exporter scans the vertex shader's
      constant buffers for a 4x4 matrix that looks like a standalone
      view/projection matrix (recognised by the "copy view-space Z into
      clip.w" structural signature perspective matrices have) and reads
      `e` directly off its diagonal, then divides X/Y by it - this
      recovers exact view-space units, not just proportionally-correct
      shape. This only works when the shader exposes a separate
      view/projection matrix in its constant buffers; if it only has a
      pre-multiplied `ModelViewProjection` matrix (combined with the
      per-object model transform), that signature doesn't survive the
      combination and detection will fail - in that case it falls back to
      the old behavior (unscaled, `e` assumed 1), so depth may still be
      off by an unknown-but-uniform factor. Check the `_posed.obj`
      file's header comment to see which case applied for a given draw.
      The detected matrix is cached **per render pass**, not globally -
      engines commonly reuse the same vertex shader across passes (e.g. a
      shadow/depth prepass reusing the main pass's skinning shader with only
      the pixel shader swapped) while binding a *different* view/projection
      matrix per pass (light-space vs. camera-space). Caching by shader
      alone would silently carry pass A's matrix into pass B's draws; this
      is why the correction is scoped to the pass boundary instead. It does
      **not** protect against the matrix changing between draws *within* the
      same pass (e.g. a cubemap-face loop reusing one render target set with
      six different view matrices per face) - check the recorded matrix (see
      below) if depth still looks inconsistent within a single pass.
      In practice this heuristic hasn't reliably produced correct results
      even when it reports finding a matrix - treat posed-mesh depth for
      perspective draws as approximate, and prefer the object-space
      export (`Export Scene...`) when accurate proportions matter more
      than seeing the actual posed/deformed geometry.
    - **Orthographic**: `clip.w` is always exactly `1.0` for every vertex
      (there's no perspective divide), so using it as Z would make every
      object come out perfectly flat. This is detected per-draw (checking
      whether `w` is uniformly ~1 across the draw) and `clip.z` is used
      instead, which is already linear for an orthographic projection (no
      divide means no non-linear compression, and no `e` scale mismatch
      either since orthographic matrices don't have that term).
    - Sign of Z may come out flipped depending on the engine's projection
      convention (LH vs RH) in either case - flip the Z axis in your 3D
      tool if the mesh appears mirrored in depth.
- Reads the pixel shader stage's bound read-only resources (textures),
  saves each one once to the shared `textures/tex_<resourceId>.png`
  (deduplicated across all passes), and records which bind point /
  shader variable name it's bound to.
- Writes a top-level `manifest.json` indexing the passes:
  ```json
  {
    "passes": [
      {"folder": "pass_00_shadow", "index": 0, "guessedRole": "depth-only (likely shadow map / depth prepass)", "colorTargets": [], "depthTarget": 12345, "drawCount": 340},
      {"folder": "pass_01_forward", "index": 1, "guessedRole": "single color + depth (likely main/forward scene pass)", "colorTargets": [23456], "depthTarget": 23458, "drawCount": 812}
    ]
  }
  ```
  and a `manifest.json` inside each `pass_NN/` folder with the per-draw
  detail:
  ```json
  {
    "draws": [
      {
        "eventId": 123,
        "name": "DrawIndexed(36)",
        "mesh": "meshes/eid123.obj",
        "posedMesh": "meshes_posed/eid123_posed.obj",
        "posedSpace": "reconstructed view-space, perspective (exact units - projection matrix found in shader constants)",
        "viewProjectionShader": "ResourceId::123456",
        "textures": [
          {"bindPoint": 0, "name": "g_DiffuseTex", "textureFile": "../textures/tex_45.png"}
        ]
      }
    ],
    "viewProjections": {
      "ResourceId::123456": {
        "vertexShaderResourceId": "ResourceId::123456",
        "constantBufferResourceId": "ResourceId::654321",
        "eScale": 1.303,
        "matrix": [1.303, 0.0, 0.0, 0.0, 0.0, 1.732, 0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 0.0, 0.0, -0.1, 0.0]
      }
    }
  }
  ```
  `posedMesh` is `null` when posed export wasn't enabled, or when a
  draw's vertex shader had no usable position output. `textureFile` paths
  are relative to that pass's own `manifest.json`.

  `posedSpace` records which case (see "Posed export" below) applied to
  that draw's posed mesh, and `viewProjectionShader` (set only for the
  perspective/exact-units case) points at the matching entry in this same
  pass's `viewProjections` map, which holds the actual recovered
  view/projection matrix and derived `eScale` - one entry per distinct
  vertex shader resource seen in this pass, since the matrix is normally
  constant across draws sharing a shader within one pass. Both fields are
  absent where posed export wasn't enabled or produced nothing.
  `viewProjections` only reflects draws in *this* pass folder - the same
  vertex shader resource appearing in another pass's `manifest.json` may
  (and often does) have a different matrix recorded there; see "Posed
  export" below for why that pass boundary matters.

### Performance

For every draw, the exporter fetches only the byte window of the vertex/
index buffers actually referenced by that draw (not "from the start" or
"to the end of the buffer" - both of which are wasteful for buffers
shared across many draws, e.g. one big world vertex/index buffer), and
decodes each unique vertex exactly once even if many triangles reuse it.
Positions/normals/UVs sharing one interleaved vertex buffer are also
fetched together in a single read rather than three times. OBJ files are
built in memory and written in one `write()` call rather than one call
per line. If exports are still slow on very large captures, the next
things to look at are: `SetFrameEvent` replay cost per draw (inherent to
introspecting every draw precisely) and Python-level overhead in the
per-vertex decode loops for meshes with very high unique-vertex counts.

The actual replay work runs via `ctx.Replay().AsyncInvoke()` - RenderDoc's
own documented pattern for extensions to do replay work without blocking
the UI thread - rather than `BlockInvoke()` directly. `run_export`
returns immediately after kicking this off, so the menu action never
blocks.

Progress is printed to the console (visible in RenderDoc's Python
console / log) roughly 20 times over the run, with a single dialog
shown at the end via `InvokeOntoUIThread()` once export finishes or
fails. This ended up being the simplest reliable option after two
different attempts at a custom progress *widget* both failed in
RenderDoc's UI layer:
- Driving `BlockInvoke()` from a hand-rolled Python `threading.Thread`
  isn't a supported pattern for RenderDoc's Python bridge and hung.
- Showing a `MiniQtHelper` widget via `ShowWidgetAsDialog()` (blocking
  modal) and updating it from the async callback via
  `InvokeOntoUIThread()` deadlocked after the first update, since
  RenderDoc's docs note queued UI-thread callbacks can't be dispatched
  while the calling script is still "executing" - and a script paused
  inside `ShowWidgetAsDialog()` counts as executing.
- Switching to a docked widget via `ctx.AddDockWindow()` instead (which
  returns immediately, unlike the modal dialog) then crashed RenderDoc
  outright - dynamically creating/tearing down a `MiniQtHelper` widget
  for a transient dock panel appears to hit unstable territory in the
  docking system, at least on the version this was tested against.

If you want to try reintroducing a visual progress widget, treat it as
genuinely experimental and test carefully - the console + final-dialog
approach is what's shipped here because it's the one that reliably
doesn't crash or hang.

### Known limitations / things to check for your capture

- Material assignment (`.mtl` files) is per-draw, so meshes that share
  the same texture get separate, duplicate `.mtl` files with identical
  content rather than one shared material - harmless (they're tiny text
  files) but not deduplicated. There's also no `.mtl` at all for a draw
  with no bound textures, or where none of the shader's texture bindings
  have a resolvable name/file.
- Diffuse/normal texture guessing only looks at the shader variable name;
  it has no way to inspect the texture's actual pixel content. If a
  material comes into Blender with the wrong image assigned (e.g. a
  roughness or AO map picked instead of the albedo map), that's this
  heuristic guessing wrong for a shader binding name it didn't recognise -
  reassign the image in Blender's Shader Editor.

- Dialogs use `ExtensionManager.OpenDirectoryName()` / `MessageDialog()`
  rather than importing PySide2 directly, because PySide2 isn't bundled
  in every RenderDoc build. If you extend the UI further, prefer these
  portable helpers (or `MiniQtHelper`) over `import PySide2` unless you
  know your target build includes it.
- Posed export only reads instance 0 and view 0 for instanced/multiview
  draws, and only the `VSOut` stage. If the pipeline uses tessellation,
  a geometry shader, or mesh shaders, the actual final pre-rasterizer
  positions come from a later stage — pass a different
  `rd.MeshDataStage` (e.g. `DomainOut`, `GSOut`) to `GetPostVSData()` in
  `export_posed_mesh_for_action` if you need that.
- Vertex format decoding covers float/unorm/snorm/uint/sint components;
  it does **not** handle packed/compressed formats (e.g. R10G10B10A2 —
  extend `component_struct_char`/`decode_attribute`, and
  `vartype_struct_char` for the posed path, if you hit one).
- Attribute/output matching is by name substring (`POSITION`, `NORMAL`,
  `TEXCOORD`/`UV`, and for posed output also generic `POS`) — adjust
  `find_semantic_attrs` or the matching loop in
  `export_posed_mesh_for_action` if your shaders use different naming.
- Texture bindings use `PipeState.GetReadOnlyResources()`, which returns
  the resources actually bound to the **pixel shader** stage for that draw.
  If a game does its lighting/normal-mapping in a different stage (e.g. a
  deferred renderer sampling G-buffer textures in a compute shader, or a
  vertex shader doing displacement), those won't show up — extend the
  `rd.ShaderStage.Pixel` loop in `run_export` to also check `Compute` or
  `Vertex` if you hit that case.
- On APIs using bindless/descriptor-indexing (e.g. a single huge texture
  array indexed dynamically in the shader), `GetReadOnlyResources()` may
  only report the whole array as one binding rather than the specific
  texture actually sampled for that draw - RenderDoc can't statically know
  which array element a dynamic index resolves to.
- RenderDoc's Python API has shifted some names across versions
  (`DrawcallDescription` → `ActionDescription`, `GetDrawcalls()` →
  `GetRootActions()`/`CurRootActions()`). The code checks for a few
  variants, but if something doesn't line up, open RenderDoc's built-in
  Python shell (`Window > Python Shell`) with a capture loaded and
  inspect `dir(controller.GetPipelineState())` etc. to confirm exact
  names for your installed version.
- Instanced draws: by default this exports one mesh per draw call using
  only per-vertex attributes; any per-instance attribute (`perInstance`
  set on the vertex input) is skipped entirely rather than expanded per
  instance. Ticking "Export all instanced meshes" in the draw-call-types
  popup additionally writes an `eid<N>_instances.json` sidecar next to
  each instanced draw's base mesh, with the raw decoded per-instance
  attribute values (e.g. a per-instance transform, color, or index) for
  every instance actually drawn - it does not try to guess which
  attribute(s) form a transform matrix, so interpreting them is left to
  whatever consumes that file.
- Pass splitting groups by exact render-target set (see "Pass splitting"
  above), so within a single actual pass, small target changes some
  engines do mid-pass (e.g. switching depth targets for a sub-portion of
  shadow cascades, or ping-ponging between two color targets) will show
  up as separate pass folders rather than being merged into one. If that
  happens for your capture, the passes are still safe to import together
  (they were genuinely different render target sets), just more finely
  split than the "one folder per conceptual pass" ideal.
- The pass-selection dialog is a plain vertical list of checkboxes with
  no scroll area, built with `MiniQtHelper`. It's paginated at 20 passes
  per page to keep the window from growing unboundedly tall, but each
  page itself still isn't scrollable, so a capture with unusually many
  passes sharing one page could still produce a tall window.

