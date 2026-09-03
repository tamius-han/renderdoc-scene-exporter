# Rer (RenderDoc extension)

Renderdoc addon that exports all meshes and textures in a given capture.

Exports every draw call's geometry (as OBJ), every bound pixel-shader
texture (as PNG), and a `manifest.json` mapping each draw to its mesh
file(s) and the textures bound to it at that point in the frame.

## Install

1. Clone this repository and copy the folder into your RenderDoc extensions directory:
   - Windows: `%APPDATA%\qrenderdoc\extensions\`
   - Linux: `~/.local/share/qrenderdoc/extensions/`
   Eventually, I will make a proper release.
2. Open RenderDoc, load a capture.
3. `Tools > Manage Extensions`, tick **SceneExporter** to load it
   (or restart RenderDoc after copying the folder).
4. Two commands are added under **Tools**:
   - **Export Scene...** — object-space (bind pose) meshes only.
   - **Export Scene (with posed meshes)...** — also exports each draw's
     vertex-shader *output* data, i.e. the geometry after whatever
     skinning/morphing/transform the shader itself applies.

## What it does

- Walks every draw call (`ActionFlags.Drawcall`) in the frame.
- For each draw, reads the raw input-assembler vertex buffers directly
  (position / normal / texcoord, matched by semantic name) and the index
  buffer, and writes an `.obj` per draw into `meshes/`. This is the
  **bind pose / object space** geometry, before any GPU transform.
- If posed export is enabled, also calls `GetPostVSData()` to read the
  vertex shader's *output* for that draw, and writes a second
  `<name>_posed.obj` into `meshes_posed/`:
  - If the shader outputs a separate position-like varying (e.g. a
    `WorldPos` passed through for lighting), that's used directly —
    this gives true world-space, posed geometry.
  - Otherwise it falls back to the builtin clip-space position
    (`SV_Position`/`gl_Position`) and divides by `w` to get NDC
    coordinates — the shape exactly as projected for that draw's
    camera/view. This will look flattened/warped in a 3D tool since
    it's projected space, not world space; it's a fallback for when no
    world-space output exists to use instead.
- Reads the pixel shader stage's bound read-only resources (textures),
  saves each one once to `textures/tex_<resourceId>.png`, and records
  which bind point / shader variable name it's bound to.
- Writes `manifest.json`:
  ```json
  {
    "draws": [
      {
        "eventId": 123,
        "name": "DrawIndexed(36)",
        "mesh": "meshes/eid123.obj",
        "posedMesh": "meshes_posed/eid123_posed.obj",
        "textures": [
          {"bindPoint": 0, "name": "g_DiffuseTex", "textureFile": "textures/tex_45.png"}
        ]
      }
    ]
  }
  ```
  `posedMesh` is `null` when posed export wasn't enabled, or when a
  draw's vertex shader had no usable position output.

## Known limitations / things to check for your capture

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
- Instanced draws: this exports one mesh per draw call using only
  per-vertex attributes; any per-instance attribute (`perInstance` set on
  the vertex input) is skipped entirely rather than expanded per instance.
