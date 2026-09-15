import os
import json
import struct

import renderdoc as rd
import qrenderdoc as qrd

# PySide2 (direct Qt access) isn't guaranteed to be present in every
# RenderDoc build, so dialogs are done through ExtensionManager's portable
# helpers (OpenDirectoryName / MessageDialog) instead, which work everywhere.


# ---------------------------------------------------------------------------
# Draw call collection
# ---------------------------------------------------------------------------

def get_all_actions(root_actions):
    """Flatten the action/drawcall tree into a list of actual draw calls."""
    result = []

    def walk(actions):
        for a in actions:
            if a.flags & rd.ActionFlags.Drawcall:
                result.append(a)
            walk(a.children)

    walk(root_actions)
    return result


def get_root_actions(ctx):
    # The correct CaptureContext method is CurRootActions(). Older RenderDoc
    # versions used GetDrawcalls() on the CaptureContext instead - keep a
    # fallback for those, but CurRootActions() is what current builds expose.
    if hasattr(ctx, "CurRootActions"):
        return ctx.CurRootActions()
    if hasattr(ctx, "GetRootActions"):
        return ctx.GetRootActions()
    return ctx.GetDrawcalls()


def get_pass_key(action):
    """Identify which 'pass' a draw belongs to by the set of render targets
    it writes into - a shadow pass, a G-buffer pass, and a final backbuffer
    pass all write to distinct targets, so this is a reliable, engine-
    agnostic way to separate passes without depending on debug marker names
    (which may not exist at all).

    Uses ActionDescription.outputs/depthOut directly rather than replaying
    to the event and reading PipeState.GetOutputTargets()/GetDepthTarget() -
    these are populated on the action itself at capture-parse time (the
    docs describe `outputs` as existing specifically for "coarse bucketing
    of actions into similar passes"), so this needs no SetFrameEvent/replay
    at all and is effectively free to compute for every action up front.

    Returns a hashable (colorTargets, depth) key - all ResourceIds
    converted to plain ints so it's JSON-serializable and safe to use as a
    dict key."""
    depth_id = int(action.depthOut) if action.depthOut != rd.ResourceId.Null() else 0
    col_ids = tuple(sorted(int(r) for r in action.outputs if r != rd.ResourceId.Null()))
    return (col_ids, depth_id)


# Short filesystem-safe tag -> human-readable label, for the pass-selection
# dialog and folder naming. These are best-effort GUESSES based only on
# render target shape (count of color targets, presence of depth, whether
# a target is the actual swapchain image) - there's no ground truth without
# engine source or debug markers, so treat these as hints, not facts.
PASS_CLASSIFICATIONS = {
    "final": "final/presented to screen",
    "shadow": "depth-only",
    "gbuffer": "[⟐ | GBUFFER] likely G-buffer/deferred scene pass (multiple color targets)",
    "forward": "[⟐ | FORWARD] possible main/forward scene pass (single color + depth)",
    "postprocess": "likely post-process/composite (single color, no depth)",
    "misc": "uncategorized",
}


def classify_pass(ctx, color_ids_raw, depth_id_raw):
    """Best-effort, non-authoritative guess at what a pass is for. Returns
    a (tag, label) pair - tag is a short filesystem-safe string (see
    PASS_CLASSIFICATIONS), label is the human-readable description.
    color_ids_raw/depth_id_raw must be actual ResourceId objects (not the
    plain ints from get_pass_key) since they're used to look up texture
    info via ctx.GetTexture()."""
    try:
        color_texs = [t for t in (ctx.GetTexture(rid) for rid in color_ids_raw) if t]
        depth_tex = None
        if depth_id_raw and depth_id_raw != rd.ResourceId.Null():
            depth_tex = ctx.GetTexture(depth_id_raw)

        if any(t.creationFlags & rd.TextureCategory.SwapBuffer for t in color_texs):
            tag = "final"
        elif not color_texs and depth_tex is not None:
            tag = "shadow"
        elif len(color_texs) >= 2:
            tag = "gbuffer"
        elif len(color_texs) == 1 and depth_tex is not None:
            tag = "forward"
        elif len(color_texs) == 1:
            tag = "postprocess"
        else:
            tag = "misc"
    except Exception as e:
        print("[Renderdoc Scene Exporter] pass classification failed: {}".format(e))
        tag = "misc"
    return tag, PASS_CLASSIFICATIONS[tag]



# ---------------------------------------------------------------------------
# Vertex attribute decoding
# ---------------------------------------------------------------------------

def component_struct_char(comp_type, comp_byte_width):
    if comp_type == rd.CompType.Float:
        return {2: 'e', 4: 'f'}.get(comp_byte_width)
    if comp_type in (rd.CompType.UInt, rd.CompType.UScaled, rd.CompType.UNorm):
        return {1: 'B', 2: 'H', 4: 'I'}.get(comp_byte_width)
    if comp_type in (rd.CompType.SInt, rd.CompType.SScaled, rd.CompType.SNorm):
        return {1: 'b', 2: 'h', 4: 'i'}.get(comp_byte_width)
    return None


def decode_attribute(raw_bytes, fmt):
    """Decode one vertex attribute's raw bytes according to its ResourceFormat."""
    comp_count = fmt.compCount
    comp_width = fmt.compByteWidth
    ch = component_struct_char(fmt.compType, comp_width)
    if ch is None:
        return None
    unpacked = struct.unpack_from('<' + ch * comp_count, raw_bytes, 0)
    if fmt.compType == rd.CompType.UNorm:
        maxv = float((1 << (comp_width * 8)) - 1)
        unpacked = tuple(v / maxv for v in unpacked)
    elif fmt.compType == rd.CompType.SNorm:
        maxv = float((1 << (comp_width * 8 - 1)) - 1)
        unpacked = tuple(max(v / maxv, -1.0) for v in unpacked)
    return unpacked


def find_semantic_attrs(attrs):
    pos_attr = norm_attr = uv_attr = None
    valid_attrs = [a for a in attrs if a.used and not a.perInstance]
    for a in valid_attrs:
        nm = a.name.upper()
        if pos_attr is None and ('POSITION' in nm or nm == 'POS' or nm.startswith('POS') or 'SV_POSITION' in nm):
            pos_attr = a
        elif norm_attr is None and ('NORMAL' in nm or 'NORM' in nm):
            norm_attr = a
        elif uv_attr is None and ('TEXCOORD' in nm or 'UV' in nm or 'TEXTURE' in nm):
            uv_attr = a

    # Fallback for position if unreflected / generic attribute names (e.g., attr0, in_var0)
    if pos_attr is None and valid_attrs:
        for a in valid_attrs:
            if a.format.compCount in (3, 4) and a.format.compType == rd.CompType.Float:
                pos_attr = a
                break
        if pos_attr is None:
            pos_attr = valid_attrs[0]

    return pos_attr, norm_attr, uv_attr


def compact_indices(indices):
    """Map a draw's (possibly huge, sparse-in-a-shared-buffer) index values
    down to a small dense range: only the vertices actually referenced are
    fetched/decoded/written, and each unique vertex is decoded exactly once
    no matter how many triangles reuse it.

    Returns (unique_sorted_locals, min_index, face_local_indices) where
    face_local_indices are 0-based positions into unique_sorted_locals,
    suitable for passing straight to write_obj (which adds 1 for OBJ)."""
    if not indices:
        return [], 0, []
    min_idx = min(indices)
    locals_ = [i - min_idx for i in indices]
    unique_locals = sorted(set(locals_))
    remap = {li: pos for pos, li in enumerate(unique_locals)}
    face_local_indices = [remap[li] for li in locals_]
    return unique_locals, min_idx, face_local_indices


def read_selected(data, stride, byte_offset_in_vertex, fmt, unique_locals):
    """Decode one attribute for exactly the vertices in unique_locals, from
    an already-fetched raw byte window (see fetch_vb_window)."""
    comp_size = fmt.compByteWidth * fmt.compCount
    out = []
    for li in unique_locals:
        base = li * stride + byte_offset_in_vertex
        chunk = data[base:base + comp_size]
        if len(chunk) < comp_size:
            out.append(None)
            continue
        out.append(decode_attribute(chunk, fmt))
    return out


DIFFUSE_KEYWORDS = (
    "diffuse", "albedo", "basecolor", "base_color", "base_col",
    "_bc", "_diff", "_alb", "_col", "_d.", "_d_", "_d",
    "colour", "color", "tex_d", "maintex", "albedomap", "diffusemap",
)

NORMAL_KEYWORDS = (
    "normal", "bump", "nrm", "_norm", "_nrm", "_n.", "_n_", "_n",
    "normalmap", "bumpmap", "flatnormal", "tex_n",
)

NON_MATERIAL_KEYWORDS = (
    "noise", "bluenoise", "lut", "brdf", "dfg", "preintegrated",
    "shadow", "shadowmap", "depth", "jitter", "gradient", "fog",
    "sky", "view_", "scenetexture", "lightmap", "primitive",
    "environment", "irradiance", "radiance", "dummy", "black", "white",
    "ambientocclusion", "ssao",
)

MATERIAL_FALLBACK_KEYWORDS = (
    "materialtexture", "material_texture", "material", "tex2d",
    "texture2d", "tex_", "texture",
)


def pick_material_textures(bindings):
    """Best-effort guess at which bound texture is the diffuse/albedo map
    and which (if any) is a normal/bump map, from a draw's texture
    bindings list (as built in run_export - each entry has "name",
    "resourceName", and "textureFile", relative to the pass folder). Returns
    (diffuse_binding, normal_binding), either of which may be None."""
    valid_bindings = [b for b in bindings if b.get("textureFile")]
    if not valid_bindings:
        return None, None

    def get_names(b):
        parts = []
        if b.get("name"):
            parts.append(str(b["name"]))
        if b.get("resourceName"):
            parts.append(str(b["resourceName"]))
        return (" ".join(parts)).lower()

    diffuse = None
    normal = None

    # Step 1: explicit diffuse match
    for b in valid_bindings:
        nm = get_names(b)
        if any(k in nm for k in DIFFUSE_KEYWORDS):
            diffuse = b
            break

    # Step 2: explicit normal match
    for b in valid_bindings:
        nm = get_names(b)
        if any(k in nm for k in NORMAL_KEYWORDS):
            if b is not diffuse:
                normal = b
                break

    # Step 3: if no diffuse yet, look for material-like texture names
    if diffuse is None:
        for b in valid_bindings:
            if b is normal:
                continue
            nm = get_names(b)
            if any(k in nm for k in NON_MATERIAL_KEYWORDS):
                continue
            if any(k in nm for k in MATERIAL_FALLBACK_KEYWORDS):
                diffuse = b
                break

    # Step 4: if still no diffuse, pick first valid texture that is not normal and not non-material
    if diffuse is None:
        for b in valid_bindings:
            if b is normal:
                continue
            nm = get_names(b)
            if not any(k in nm for k in NON_MATERIAL_KEYWORDS):
                diffuse = b
                break

    # Step 5: absolute fallback
    if diffuse is None:
        for b in valid_bindings:
            if b is not normal:
                diffuse = b
                break

    return diffuse, normal


def write_mtl_and_get_directives(pass_dir, mesh_dir, mesh_name, bindings):
    """Writes a companion .mtl file next to the .obj (if any usable texture
    was bound) so Blender's OBJ importer auto-assigns it as a material,
    wired to the mesh's existing UV coordinates. Returns the (mtllib,
    usemtl) directive lines to embed in the .obj header, or (None, None)
    if there's nothing to reference."""
    if not bindings:
        return None, None
    diffuse, normal = pick_material_textures(bindings)
    if diffuse is None and normal is None:
        return None, None

    def resolve(b):
        # b["textureFile"] is relative to pass_dir (see run_export) -
        # resolve to absolute, then re-relativize to wherever the .mtl
        # actually lives (mesh_dir), since those aren't the same folder.
        return os.path.normpath(os.path.join(pass_dir, b["textureFile"]))

    mat_name = "mat_" + mesh_name
    mtl_path = os.path.join(mesh_dir, mesh_name + ".mtl")
    lines = [
        "newmtl {}\n".format(mat_name),
        "Kd 1.000 1.000 1.000\n",
    ]
    if diffuse is not None:
        diffuse_rel = os.path.relpath(resolve(diffuse), mesh_dir).replace(os.sep, "/")
        lines.append("map_Kd {}\n".format(diffuse_rel))
    if normal is not None:
        normal_rel = os.path.relpath(resolve(normal), mesh_dir).replace(os.sep, "/")
        lines.append("bump {}\n".format(normal_rel))

    with open(mtl_path, "w") as f:
        f.write("".join(lines))

    return "mtllib {}\n".format(mesh_name + ".mtl"), "usemtl {}\n".format(mat_name)


def write_obj(path, action, positions, normals, uvs, indices, note=None, mtllib=None, usemtl=None):
    lines = ["# eid={}{}\n".format(action.eventId, " - " + note if note else "")]
    if mtllib:
        lines.append(mtllib)
    if usemtl:
        lines.append(usemtl)
    for p in positions:
        vals = (list(p) + [0, 0, 0])[:3] if p else [0, 0, 0]
        lines.append("v {} {} {}\n".format(*vals))
    if uvs:
        for uv in uvs:
            vals = (list(uv) + [0, 0])[:2] if uv else [0, 0]
            lines.append("vt {} {}\n".format(vals[0], 1.0 - vals[1]))
    if normals:
        for n in normals:
            vals = (list(n) + [0, 0, 1])[:3] if n else [0, 0, 1]
            lines.append("vn {} {} {}\n".format(*vals))

    has_uv, has_n = bool(uvs), bool(normals)
    tri_count = len(indices) - (len(indices) % 3)
    for t in range(0, tri_count, 3):
        face = []
        for k in range(3):
            idx = indices[t + k] + 1  # OBJ indices are 1-based
            if has_uv and has_n:
                face.append(f"{idx}/{idx}/{idx}")
            elif has_uv:
                face.append(f"{idx}/{idx}")
            elif has_n:
                face.append(f"{idx}//{idx}")
            else:
                face.append(f"{idx}")
        lines.append("f " + " ".join(face) + "\n")

    with open(path, "w") as f:
        f.write("".join(lines))


def export_mesh_for_action(controller, state, action, out_dir, mesh_name, pass_dir=None, bindings=None):
    """Reads raw input-assembler vertex data for one draw and writes an OBJ.
    This is the pre-transform (bind pose / object space) geometry.

    Only fetches the byte window actually referenced by this draw (not the
    whole buffer from the start), and decodes each unique vertex once even
    if it's reused by many triangles - important for shared/world-sized
    vertex and index buffers where a single draw only touches a small slice."""
    if action.numIndices == 0:
        return None

    vbuffers = state.GetVBuffers()
    attrs = state.GetVertexInputs()
    ibuf = state.GetIBuffer()

    pos_attr, norm_attr, uv_attr = find_semantic_attrs(attrs)
    if pos_attr is None:
        return None

    # Only trust the index buffer if this draw is actually indexed - it may
    # still be bound even for a non-indexed draw.
    use_ib = bool(action.flags & rd.ActionFlags.Indexed) and ibuf.resourceId != rd.ResourceId.Null()

    if use_ib:
        idx_width = ibuf.byteStride if ibuf.byteStride in (1, 2, 4) else 4
        fmt_char = {1: 'B', 2: 'H', 4: 'I'}[idx_width]
        # Fetch exactly this draw's slice of the index buffer, not "from the
        # start" and not "to the end" - both of which are wasteful (and can
        # be huge) when many draws share one big index buffer.
        start = ibuf.byteOffset + action.indexOffset * idx_width
        length = action.numIndices * idx_width
        raw = controller.GetBufferData(ibuf.resourceId, start, length)
        indices = list(struct.unpack_from('<' + fmt_char * action.numIndices, raw, 0))
        # baseVertex is an offset applied on top of each raw index
        indices = [i + action.baseVertex for i in indices]
    else:
        # Non-indexed: vertices are consumed sequentially starting at vertexOffset
        indices = [action.vertexOffset + i for i in range(action.numIndices)]

    unique_locals, min_idx, face_indices = compact_indices(indices)
    if not unique_locals:
        return None
    count = unique_locals[-1] + 1

    # Fetch each unique vertex buffer's window exactly once, even if
    # position/normal/uv are all interleaved in the same buffer (the common
    # case) - and only the window this draw actually uses, not from byte 0.
    vb_windows = {}

    def get_window(vb):
        if vb.resourceId not in vb_windows:
            stride = vb.byteStride if vb.byteStride else 0
            start = vb.byteOffset + stride * min_idx
            length = stride * count
            data = controller.GetBufferData(vb.resourceId, start, length)
            vb_windows[vb.resourceId] = (data, stride)
        return vb_windows[vb.resourceId]

    def read_all(attr):
        if attr is None or attr.vertexBuffer >= len(vbuffers) or attr.vertexBuffer < 0:
            return None
        vb = vbuffers[attr.vertexBuffer]
        if vb.resourceId == rd.ResourceId.Null():
            return None
        data, stride = get_window(vb)
        stride = stride if stride else (attr.format.compByteWidth * attr.format.compCount)
        return read_selected(data, stride, attr.byteOffset, attr.format, unique_locals)

    positions = read_all(pos_attr)
    normals = read_all(norm_attr) if norm_attr else None
    uvs = read_all(uv_attr) if uv_attr else None

    if positions is None:
        return None

    path = os.path.join(out_dir, mesh_name + ".obj")
    mtllib = usemtl = None
    if pass_dir is not None and bindings:
        mtllib, usemtl = write_mtl_and_get_directives(pass_dir, out_dir, mesh_name, bindings)
    write_obj(path, action, positions, normals, uvs, face_indices, mtllib=mtllib, usemtl=usemtl)
    return path


# ---------------------------------------------------------------------------
# Instanced draw ("export all instanced meshes") geometry export
# ---------------------------------------------------------------------------
#
# export_mesh_for_action() above only reads *per-vertex* input attributes,
# so a DrawInstanced call still only produces a single T-pose mesh - any
# per-instance data (typically a per-instance world transform, but could
# just as easily be a color, a texture-array index, etc.) is read by the
# shader but never touched by this exporter. The function below is the
# best-effort fix for that: it reads every attribute flagged `perInstance`
# on the input layout, decodes one value per actual instance drawn, and
# writes them out verbatim next to the base mesh so a downstream tool (or
# a human) can place/instantiate copies of the mesh correctly.
#
# This is deliberately NOT trying to detect "this is a 4x4 transform
# matrix packed across 4 vec4 attributes" the way find_vertex_shader_view_
# scale() does for view/projection matrices - per-instance layouts vary
# far more than the handful of common view/projection conventions, and a
# wrong guess here would silently misplace every instance. Instead every
# per-instance attribute is dumped by name, and interpreting them (e.g.
# recombining 3-4 float4 attributes into a matrix) is left to whatever
# reads instances.json.

def read_per_instance_attrs(controller, state, action):
    """Returns (attr_names, [{name: value, ...}, ...]) - one dict per
    instance actually drawn by `action` - or (None, None) if this isn't an
    instanced draw, or the pipeline has no per-instance input attributes at
    all (nothing to expand)."""
    if not (action.flags & rd.ActionFlags.Instanced):
        return None, None
    num_instances = action.numInstances
    if num_instances <= 1:
        return None, None

    vbuffers = state.GetVBuffers()
    attrs = state.GetVertexInputs()
    per_instance_attrs = [a for a in attrs if a.used and a.perInstance]
    if not per_instance_attrs:
        return None, None

    # Not every RenderDoc API version exposes a base-instance field on
    # ActionDescription under the same name - fall back to 0 (the common
    # case) rather than hard failing if it's missing.
    base_instance = getattr(action, "instanceOffset", 0)

    attr_names = [a.name for a in per_instance_attrs]
    instances = [dict() for _ in range(num_instances)]

    for a in per_instance_attrs:
        if a.vertexBuffer < 0 or a.vertexBuffer >= len(vbuffers):
            continue
        vb = vbuffers[a.vertexBuffer]
        if vb.resourceId == rd.ResourceId.Null():
            continue
        comp_size = a.format.compByteWidth * a.format.compCount
        stride = vb.byteStride if vb.byteStride else comp_size
        # instanceRate: how many consecutive instances share one element of
        # per-instance data (1 = one element per instance, the common
        # case; RenderDoc only exposes this per-attribute, not per-buffer).
        rate = getattr(a, "instanceRate", 1) or 1
        slots_needed = (num_instances + rate - 1) // rate
        start = vb.byteOffset + stride * base_instance
        length = stride * slots_needed
        raw = controller.GetBufferData(vb.resourceId, start, length)
        for inst_i in range(num_instances):
            slot = inst_i // rate
            off = slot * stride + a.byteOffset
            chunk = raw[off:off + comp_size]
            if len(chunk) < comp_size:
                continue
            val = decode_attribute(chunk, a.format)
            if val is not None:
                instances[inst_i][a.name] = list(val)

    return attr_names, instances


def export_instanced_geometry_for_action(controller, state, action, mesh_path, out_dir, mesh_name):
    """Writes an `<mesh_name>_instances.json` sidecar next to an already-
    exported base mesh (see export_mesh_for_action), listing the raw
    per-instance attribute values for every instance this DrawInstanced
    call actually drew. Returns the sidecar's path, or None if this draw
    wasn't instanced / had no per-instance attributes to record."""
    attr_names, instances = read_per_instance_attrs(controller, state, action)
    if instances is None:
        return None

    sidecar_path = os.path.join(out_dir, mesh_name + "_instances.json")
    with open(sidecar_path, "w") as f:
        json.dump({
            "eventId": action.eventId,
            "baseMesh": os.path.basename(mesh_path) if mesh_path else None,
            "numInstances": len(instances),
            "perInstanceAttributes": attr_names,
            "instances": instances,
        }, f, indent=2)
    return sidecar_path


# ---------------------------------------------------------------------------
# Post-transform ("posed", vertex-shader output) mesh export
# ---------------------------------------------------------------------------

def vartype_struct_char(vartype):
    return {
        rd.VarType.Float: 'f',
        rd.VarType.Double: 'd',
        rd.VarType.Half: 'e',
        rd.VarType.SInt: 'i',
        rd.VarType.UInt: 'I',
        rd.VarType.SShort: 'h',
        rd.VarType.UShort: 'H',
        rd.VarType.SByte: 'b',
        rd.VarType.UByte: 'B',
        rd.VarType.SLong: 'q',
        rd.VarType.ULong: 'Q',
        rd.VarType.Bool: 'I',
    }.get(vartype)


def find_vertex_shader_view_scale(controller, state, cache, cache_key_prefix=None):
    """Best-effort: scan the vertex shader's constant buffers for a 4x4
    matrix that looks like a standalone view/projection matrix (i.e. not
    already combined with a per-object model matrix), and return a dict
    with its Y-axis scale term e = 1/tan(fovY/2), the raw matrix, and which
    constant buffer resource it came from - or None if nothing matched.

    Why this matters: clip.x = x_view*e/aspect and clip.y = y_view*e, but
    clip.w = z_view exactly with NO e factor. So after undoing the aspect
    ratio, X/Y end up scaled by e relative to Z - if e != 1 (i.e. the FOV
    isn't exactly 90 degrees), depth comes out compressed or stretched
    relative to X/Y. Dividing X/Y by e (done by the caller) recovers exact
    view-space units.

    e sits on the matrix diagonal, which survives a row-major/column-major
    transpose, so we don't need to know the shader's packing convention to
    read it - we only need to recognise a projection matrix in the first
    place, which we do by checking both transposes' usual slot for the
    telltale "copy z into w" entry.

    Returns None if nothing matching is found - this happens if the shader
    only exposes a pre-multiplied ModelViewProjection matrix, since combining
    with a per-object model matrix destroys this clean signature.

    IMPORTANT about caching: the same vertex shader resource is very often
    reused across *different* render passes (e.g. a shadow/depth prepass
    reusing the main pass's skinning vertex shader, only swapping the pixel
    shader) with a *different* view/projection matrix bound - a shadow pass
    uses the light's view/projection, the main pass uses the camera's. A
    plain "keyed by shader resource ID" cache would silently reuse whichever
    pass's matrix was scanned first for every later draw that shares the
    shader, corrupting the correction for every pass after the first.
    `cache_key_prefix` lets the caller (see export_posed_mesh_for_action)
    fold in something that changes between passes - callers should pass the
    pass key so the cache is scoped per (pass, shader) instead of just
    per-shader. This doesn't protect against the matrix changing *within*
    the same pass (e.g. a cubemap face loop reusing one render target set
    with six different view matrices) - that remains a known limitation;
    check the recorded matrix in the pass manifest if depth still looks off
    within a single pass.
    """
    refl = state.GetShaderReflection(rd.ShaderStage.Vertex)
    if refl is None or not refl.constantBlocks:
        return None

    cache_key = (cache_key_prefix, refl.resourceId)
    if cache_key in cache:
        return cache[cache_key]

    result = None
    try:
        pipe = state.GetGraphicsPipelineObject()
        entry = state.GetShaderEntryPoint(rd.ShaderStage.Vertex)

        def cell(flat, r, c):
            return flat[r * 4 + c]

        def scan(variables):
            for v in variables:
                if v.members:
                    found = scan(v.members)
                    if found is not None:
                        return found
                    continue
                if v.rows == 4 and v.columns == 4:
                    try:
                        flat = list(v.value.f32v[:16])
                    except Exception:
                        continue
                    if len(flat) < 16:
                        continue
                    near1_23 = abs(abs(cell(flat, 2, 3)) - 1.0) < 0.01 and abs(cell(flat, 3, 3)) < 1e-3
                    near1_32 = abs(abs(cell(flat, 3, 2)) - 1.0) < 0.01 and abs(cell(flat, 3, 3)) < 1e-3
                    if near1_23 or near1_32:
                        e = cell(flat, 1, 1)
                        if e:
                            return {"eScale": abs(e), "matrix": flat}
            return None

        for i in range(len(refl.constantBlocks)):
            try:
                cb = state.GetConstantBlock(rd.ShaderStage.Vertex, i, 0)
                if cb.descriptor.resource == rd.ResourceId.Null():
                    continue
                variables = controller.GetCBufferVariableContents(
                    pipe, refl.resourceId, rd.ShaderStage.Vertex, entry, i,
                    cb.descriptor.resource, 0, 0)
            except Exception:
                continue
            found = scan(variables)
            if found is not None:
                found["constantBufferResourceId"] = str(cb.descriptor.resource)
                found["vertexShaderResourceId"] = str(refl.resourceId)
                result = found
                break
    except Exception as e:
        print("[Renderdoc Scene Exporter] projection scale detection failed: {}".format(e))

    cache[cache_key] = result
    return result


def build_postvs_outputs(vs_refl):
    """Mirrors RenderDoc's own mesh-decoding example: lay out the vertex
    shader's output signature the same way GetPostVSData's buffer is packed
    (builtin Position first, everything else tightly packed at 4/8 bytes
    per component depending on type)."""
    outputs = []
    pos_idx = None
    for attr in vs_refl.outputSignature:
        outputs.append({
            "name": attr.varName if attr.varName else attr.semanticIdxName,
            "systemValue": attr.systemValue,
            "compCount": attr.compCount,
            "varType": attr.varType,
        })
        if attr.systemValue == rd.ShaderBuiltin.Position:
            pos_idx = len(outputs) - 1

    if pos_idx is None:
        return None, 0

    if pos_idx != 0:
        pos_entry = outputs.pop(pos_idx)
        outputs.insert(0, pos_entry)

    offset = 0
    for o in outputs:
        comp_width = rd.VarTypeByteSize(o["varType"])
        o["byteOffset"] = offset
        o["compByteWidth"] = comp_width
        offset += (8 if comp_width > 4 else 4) * o["compCount"]

    return outputs, offset


def read_postvs_selected(data, stride, out_attr, unique_locals):
    ch = vartype_struct_char(out_attr["varType"])
    if ch is None:
        return None
    comp_count = out_attr["compCount"]
    comp_size = out_attr["compByteWidth"] * comp_count
    byte_offset = out_attr["byteOffset"]
    out = []
    for li in unique_locals:
        base = li * stride + byte_offset
        chunk = data[base:base + comp_size]
        if len(chunk) < comp_size:
            out.append(None)
            continue
        out.append(struct.unpack_from('<' + ch * comp_count, chunk, 0))
    return out


def get_postvs_indices(controller, postvs):
    if postvs.indexResourceId != rd.ResourceId.Null():
        idx_width = postvs.indexByteStride if postvs.indexByteStride in (1, 2, 4) else 4
        fmt_char = {1: 'B', 2: 'H', 4: 'I'}[idx_width]
        # Exact-length fetch, not "read to end of buffer".
        length = postvs.numIndices * idx_width
        raw = controller.GetBufferData(postvs.indexResourceId, postvs.indexByteOffset, length)
        indices = list(struct.unpack_from('<' + fmt_char * postvs.numIndices, raw, 0))
        return [i + postvs.baseVertex for i in indices]
    return list(range(postvs.numIndices))


def export_posed_mesh_for_action(controller, state, action, out_dir, mesh_name, proj_scale_cache,
                                  pass_dir=None, bindings=None, pass_key=None):
    """Reads the vertex shader's OUTPUT data for one draw (post skinning,
    morphing, and any transform the shader applies) and writes an OBJ - this
    is the mesh 'as posed', matching what's actually drawn on screen.

    Note: this only looks at the vertex shader stage output (VSOut). If the
    pipeline also has tessellation or a geometry shader, the truly final
    pre-rasterizer positions come from a later stage - extend the
    MeshDataStage passed to GetPostVSData() below (e.g. DomainOut/GSOut) if
    you need that.

    Returns a dict {"path", "spaceNote", "projInfo"} instead of a bare path,
    so the caller can record which projection matrix (if any) was used to
    correct this specific draw into that pass's manifest.json - useful for
    verifying/debugging the correction after the fact, since the heuristic
    in find_vertex_shader_view_scale() is best-effort. `pass_key` is folded
    into the projection-matrix cache key (see find_vertex_shader_view_scale)
    so draws in different passes that happen to share a vertex shader don't
    reuse each other's view/projection matrix.
    """

    print('-')
    if action.numIndices == 0:
        return None

    postvs = controller.GetPostVSData(0, 0, rd.MeshDataStage.VSOut)
    print('-')
    if postvs is None or postvs.vertexResourceId == rd.ResourceId.Null():
        return None
    
    vs_refl = state.GetShaderReflection(rd.ShaderStage.Vertex)
    print('-')
    if vs_refl is None:
        return None

    outputs, fallback_stride = build_postvs_outputs(vs_refl)
    print('-')
    if outputs is None:
        return None

    indices = get_postvs_indices(controller, postvs)
    print('- 5 -')
    unique_locals, min_idx, face_indices = compact_indices(indices)
    print('-')
    if not unique_locals:
        return None
    count = unique_locals[-1] + 1

    stride = postvs.vertexByteStride if postvs.vertexByteStride else fallback_stride
    print('-')
    # Single windowed fetch covering only the vertices this draw actually
    # uses, shared across position/normal/uv (all interleaved in one buffer).
    window_start = postvs.vertexByteOffset + stride * min_idx
    window_len = stride * count
    vb_data = controller.GetBufferData(postvs.vertexResourceId, window_start, window_len)
    print('-')

    # outputs[0] is always the builtin clip-space position (SV_Position /
    # gl_Position). If the shader ALSO passes through a separate, named
    # position-like varying (very common for lighting, e.g. "WorldPos"),
    # prefer that - it's the mesh's pose without camera-projection distortion.
    # Otherwise fall back to reconstructing an approximate view-space
    # position from the clip-space output (see below) rather than a plain
    # perspective divide, to avoid aspect/depth distortion.
    clip_pos_attr = outputs[0]
    alt_pos_attr = None
    norm_attr = None
    uv_attr = None
    print('-')
    for o in outputs[1:]:
        nm = o["name"].upper()
        if alt_pos_attr is None and 'POS' in nm and o["compCount"] >= 3:
            alt_pos_attr = o
        elif norm_attr is None and 'NORMAL' in nm:
            norm_attr = o
        elif uv_attr is None and ('TEXCOORD' in nm or 'UV' in nm):
            uv_attr = o

    print('= 10 =')
    is_ortho = None
    e_scale = None
    proj_info = None
    if alt_pos_attr is not None:
        print('-a')
        raw_positions = read_postvs_selected(vb_data, stride, alt_pos_attr, unique_locals)
        print('-a')
        positions = [p[:3] if p else None for p in raw_positions] if raw_positions else None
    else:
        print('-b')
        raw_positions = read_postvs_selected(vb_data, stride, clip_pos_attr, unique_locals)
        print('-b')
        positions = None
        if raw_positions:
            # No separate world/view-space output exists, so reconstruct an
            # approximate (uniformly-scaled) view-space position from the
            # builtin clip-space position instead of doing a perspective
            # divide. For a standard PERSPECTIVE projection matrix, clip.w
            # IS the view-space depth (that's the value the GPU divides by
            # to project) - so instead of collapsing into NDC (which bakes
            # in the aspect ratio on X and a non-linear depth curve on Z),
            # we use w directly as z, and undo the projection matrix's
            # aspect-ratio scaling on x.
            #
            # For an ORTHOGRAPHIC projection, though, w is always exactly
            # 1.0 for every vertex (there's no perspective divide) - using
            # it as z would make every object come out perfectly flat. In
            # that case clip.z is already linear (orthographic has no
            # divide, so no non-linear compression to worry about), so we
            # use it directly instead. Detect which case applies per-draw
            # by checking whether w is uniformly ~1 across this draw.
            vp = state.GetViewport(0)
            print('-b')
            aspect = (vp.width / vp.height) if vp and vp.height else 1.0

            ws = [p[3] for p in raw_positions if p is not None and len(p) >= 4]
            is_ortho = bool(ws) and all(abs(w - 1.0) < 1e-4 for w in ws)
            print('-b')
            
            # clip.x = x_view*e/aspect and clip.y = y_view*e (e = 1/tan(fovY/2)),
            # but clip.w = z_view with NO e factor - so after undoing the
            # aspect ratio, X/Y are scaled by e relative to Z unless we also
            # divide them by e. Perspective only; orthographic has no e term.
            # Cache is scoped per (pass_key, shader) - see docstring on
            # find_vertex_shader_view_scale for why that matters.
            if not is_ortho:
                proj_info = find_vertex_shader_view_scale(controller, state, proj_scale_cache, pass_key)
                e_scale = proj_info["eScale"] if proj_info else None
            print('-b  5')
            
            positions = []
            print('>>' + str(len(raw_positions)))
            for p in raw_positions:
                print('-b>')
                if p is None or len(p) < 4:
                    positions.append(None)
                    continue
                x, y, z, w = p[0], p[1], p[2], p[3]
                print('- ..')
                
                if is_ortho:
                    positions.append((x * aspect, y, z))
                elif w:
                    if e_scale:
                        positions.append((x * aspect / e_scale, y / e_scale, w))
                    else:
                        positions.append((x * aspect, y, w))
                else:
                    positions.append((x * aspect, y, z))

    if not positions:
        return None

    print('-')
    normals = read_postvs_selected(vb_data, stride, norm_attr, unique_locals) if norm_attr else None
    print('-')
    uvs = read_postvs_selected(vb_data, stride, uv_attr, unique_locals) if uv_attr else None

    print('[]')
    path = os.path.join(out_dir, mesh_name + "_posed.obj")
    print('[]')
    mtllib = usemtl = None
    if pass_dir is not None and bindings:
        mtllib, usemtl = write_mtl_and_get_directives(pass_dir, out_dir, mesh_name + "_posed", bindings)
    if alt_pos_attr is not None:
        space_note = "world/view-space output"
    elif is_ortho:
        space_note = "reconstructed view-space, orthographic (undistorted - see README)"
    elif e_scale:
        space_note = "reconstructed view-space, perspective (exact units - projection matrix found in shader constants)"
    else:
        space_note = "reconstructed view-space, perspective (X/Y vs Z scale unknown - no projection matrix found in shader constants, see README)"
    print('>')
    write_obj(path, action, positions, normals, uvs, face_indices, note=space_note, mtllib=mtllib, usemtl=usemtl)
    print('_')
    return {"path": path, "spaceNote": space_note, "isOrtho": bool(is_ortho), "projInfo": proj_info}


# ---------------------------------------------------------------------------
# Texture export
# ---------------------------------------------------------------------------

def export_texture(controller, resource_id, out_dir, tex_cache, ctx=None):
    if resource_id in tex_cache:
        return tex_cache[resource_id]
    if resource_id == rd.ResourceId.Null():
        tex_cache[resource_id] = None
        return None

    # Verify that this resource is actually a texture (not a buffer)
    if ctx and hasattr(ctx, "GetTexture"):
        try:
            tex_desc = ctx.GetTexture(resource_id)
            if tex_desc is None:
                tex_cache[resource_id] = None
                return None
        except Exception:
            pass

    filename = os.path.join(out_dir, "tex_{}.png".format(int(resource_id)))
    save = rd.TextureSave()
    save.resourceId = resource_id
    save.destType = rd.FileType.PNG
    save.mip = 0
    save.slice.sliceIndex = 0
    save.alpha = rd.AlphaMapping.Preserve
    ok = controller.SaveTexture(save, filename)
    tex_cache[resource_id] = filename if ok else None
    return tex_cache[resource_id]


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Popup 1: "Export draw call types" dialog
# ---------------------------------------------------------------------------
#
# Replaces the old five separate Tools-menu entries (all / geometry /
# gbuffer only / forward only / hide post-process) with checkboxes over the
# same underlying pass tags (see classify_pass/PASS_CLASSIFICATIONS),
# restricted to whichever tags are actually present in this capture ("the
# available options"), plus a top-of-list "All" checkbox (checked by
# default - it's the only thing checked when this dialog first opens).
#
# This is a single static dialog with no close-and-rebuild step: an
# earlier version tried to let "Custom selection" reprogram the other
# checkboxes live (either directly from its own toggle, or via a separate
# "Apply" button that closed and reopened the dialog) and both crashed
# RenderDoc intermittently. So "Custom selection" no longer mutates any
# widget - it's read once, like every other checkbox here, only when
# "Next" is clicked, and only acts as a fallback for the case where the
# user unchecked "All" but didn't check any individual type either (see
# show_draw_type_selection).

def build_draw_type_page(mqt, tag_options, total_passes, total_draws):
    """Builds and shows the draw-call-types dialog once.

    Returns {"action": "next"|"cancel", "all": bool, "instanced": bool,
             "custom": bool, "checked": {tag: bool}}."""
    result = {"action": "cancel"}
    tag_checkboxes = []

    top = mqt.CreateToplevelWidget("Renderdoc Scene Exporter - export draw call types", lambda c, w, t: None)
    outer = mqt.CreateVerticalContainer()
    mqt.AddWidget(top, outer)

    label = mqt.CreateLabel()
    mqt.SetWidgetText(label, "Select which draw call type(s) (guessed pass roles) to export:")
    mqt.AddWidget(outer, label)

    all_cb = mqt.CreateCheckbox(lambda c, w, t: None)
    mqt.SetWidgetChecked(all_cb, True)
    mqt.SetWidgetText(all_cb, "All  —  {} pass(es), {} draw(s)".format(total_passes, total_draws))
    mqt.AddWidget(outer, all_cb)

    for opt in tag_options:
        cb = mqt.CreateCheckbox(lambda c, w, t: None)
        mqt.SetWidgetChecked(cb, False)
        mqt.SetWidgetText(cb, "{}  —  {} pass(es), {} draw(s)".format(
            opt["label"], opt["passCount"], opt["count"]))
        mqt.AddWidget(outer, cb)
        tag_checkboxes.append((opt["tag"], cb))

    blank = mqt.CreateLabel()
    mqt.SetWidgetText(blank, "")
    mqt.AddWidget(outer, blank)

    other_label = mqt.CreateLabel()
    mqt.SetWidgetText(other_label, "Other options")
    mqt.AddWidget(outer, other_label)

    instanced_cb = mqt.CreateCheckbox(lambda c, w, t: None)
    mqt.SetWidgetChecked(instanced_cb, False)
    mqt.SetWidgetText(instanced_cb, "Export all instanced meshes (geometry from DrawInstanced calls)")
    mqt.AddWidget(outer, instanced_cb)

    custom_cb = mqt.CreateCheckbox(lambda c, w, t: None)
    mqt.SetWidgetChecked(custom_cb, True)
    mqt.SetWidgetText(custom_cb,
        "Custom selection (used only if \"All\" is off and no type above is individually checked: "
        "picks just the busiest type instead of every type)")
    mqt.AddWidget(outer, custom_cb)

    action_row = mqt.CreateHorizontalContainer()
    mqt.AddWidget(outer, action_row)

    def do_next(c, w, t):
        result["action"] = "next"
        result["all"] = mqt.IsWidgetChecked(all_cb)
        result["instanced"] = mqt.IsWidgetChecked(instanced_cb)
        result["custom"] = mqt.IsWidgetChecked(custom_cb)
        result["checked"] = {tag: mqt.IsWidgetChecked(cb) for tag, cb in tag_checkboxes}
        mqt.CloseCurrentDialog(True)

    next_btn = mqt.CreateButton(do_next)
    mqt.SetWidgetText(next_btn, "Next")
    mqt.AddWidget(action_row, next_btn)

    def do_cancel(c, w, t):
        result["action"] = "cancel"
        mqt.CloseCurrentDialog(False)

    cancel_btn = mqt.CreateButton(do_cancel)
    mqt.SetWidgetText(cancel_btn, "Cancel")
    mqt.AddWidget(action_row, cancel_btn)

    mqt.ShowWidgetAsDialog(top)
    mqt.DestroyWidget(top)
    return result


def show_draw_type_selection(mqt, passes):
    """Drives Popup 1. Returns (selected_tags, export_all_instanced), or
    None if the user cancelled. selected_tags is a set of pass tags (see
    PASS_CLASSIFICATIONS) to keep for Popup 2."""
    seen = {}
    for p in passes:
        opt = seen.setdefault(p["tag"], {
            "tag": p["tag"],
            "label": PASS_CLASSIFICATIONS.get(p["tag"], p["label"]),
            "count": 0,
            "passCount": 0,
        })
        opt["count"] += p["count"]
        opt["passCount"] += 1
    tag_options = sorted(seen.values(), key=lambda o: -o["count"])
    total_passes = len(passes)
    total_draws = sum(p["count"] for p in passes)

    result = build_draw_type_page(mqt, tag_options, total_passes, total_draws)
    if result.get("action") != "next":
        return None

    if result.get("all", True):
        selected_tags = set(opt["tag"] for opt in tag_options)
    else:
        checked = result.get("checked", {})
        manually_checked = set(tag for tag, is_checked in checked.items() if is_checked)
        if manually_checked:
            selected_tags = manually_checked
        elif result.get("custom", True):
            # Nothing individually checked and "All" is off - fall back to
            # just the busiest available type.
            best = max(tag_options, key=lambda o: o["count"]) if tag_options else None
            selected_tags = {best["tag"]} if best else set()
        else:
            selected_tags = set(opt["tag"] for opt in tag_options)

    return selected_tags, result.get("instanced", False)


# ---------------------------------------------------------------------------
# Popup 2: paginated pass-selection dialog
# ---------------------------------------------------------------------------

PASSES_PER_PAGE = 20


def build_pass_selection_page(mqt, passes, page_idx, page_size, selection_state):
    """Builds and shows one page (at most `page_size` passes) of the pass-
    selection dialog. Pagination and Select All/Select None all close this
    dialog and hand control back to show_pass_selection() to build a fresh
    one - directly mutating checkboxes on an already-open ShowWidgetAsDialog
    (the previous Select All/Select None implementation) crashes RenderDoc,
    so nothing here ever calls SetWidgetChecked after the dialog is shown.

    Returns {"action": "export"|"cancel"|"page"|"select_all"|"select_none",
             "page": <target page, only set when action == "page">}."""
    total_pages = max(1, (len(passes) + page_size - 1) // page_size)
    page_idx = max(0, min(page_idx, total_pages - 1))
    start = page_idx * page_size
    page_passes = passes[start:start + page_size]

    result = {"action": "cancel"}
    checkboxes = []

    title = "Renderdoc Scene Exporter - select passes (page {}/{})".format(page_idx + 1, total_pages)
    top = mqt.CreateToplevelWidget(title, lambda c, w, t: None)
    outer = mqt.CreateVerticalContainer()
    mqt.AddWidget(top, outer)

    label = mqt.CreateLabel()
    mqt.SetWidgetText(label, "Select which render pass(es) to export (guessed roles in brackets - not authoritative):")
    mqt.AddWidget(outer, label)

    for p in page_passes:
        cb = mqt.CreateCheckbox(lambda c, w, t: None)
        mqt.SetWidgetChecked(cb, selection_state.get(p["key"], True))
        mqt.SetWidgetText(cb, "pass_{:02d}  —  {}  —  {} draw(s), {} color target(s), depth={}".format(
            p["index"], p["label"], p["count"], len(p["colorTargets"]), "yes" if p["depthTarget"] else "no"))
        mqt.AddWidget(outer, cb)
        checkboxes.append((p["key"], cb))

    def capture_current_page():
        for key, cb in checkboxes:
            selection_state[key] = mqt.IsWidgetChecked(cb)

    if total_pages > 1:
        nav_row = mqt.CreateHorizontalContainer()
        mqt.AddWidget(outer, nav_row)

        if page_idx > 0:
            def go_prev(c, w, t):
                capture_current_page()
                result["action"] = "page"
                result["page"] = page_idx - 1
                mqt.CloseCurrentDialog(True)

            prev_btn = mqt.CreateButton(go_prev)
            mqt.SetWidgetText(prev_btn, "<< Previous")
            mqt.AddWidget(nav_row, prev_btn)

        if page_idx < total_pages - 1:
            def go_next(c, w, t):
                capture_current_page()
                result["action"] = "page"
                result["page"] = page_idx + 1
                mqt.CloseCurrentDialog(True)

            next_btn = mqt.CreateButton(go_next)
            mqt.SetWidgetText(next_btn, "Next >>")
            mqt.AddWidget(nav_row, next_btn)

    toggle_row = mqt.CreateHorizontalContainer()
    mqt.AddWidget(outer, toggle_row)

    def select_all(c, w, t):
        capture_current_page()
        for p in passes:
            selection_state[p["key"]] = True
        result["action"] = "select_all"
        mqt.CloseCurrentDialog(True)

    def select_none(c, w, t):
        capture_current_page()
        for p in passes:
            selection_state[p["key"]] = False
        result["action"] = "select_none"
        mqt.CloseCurrentDialog(True)

    all_btn = mqt.CreateButton(select_all)
    mqt.SetWidgetText(all_btn, "Select All")
    mqt.AddWidget(toggle_row, all_btn)

    none_btn = mqt.CreateButton(select_none)
    mqt.SetWidgetText(none_btn, "Select None")
    mqt.AddWidget(toggle_row, none_btn)

    action_row = mqt.CreateHorizontalContainer()
    mqt.AddWidget(outer, action_row)

    def do_export_click(c, w, t):
        capture_current_page()
        result["action"] = "export"
        mqt.CloseCurrentDialog(True)

    export_btn = mqt.CreateButton(do_export_click)
    mqt.SetWidgetText(export_btn, "Export Selected")
    mqt.AddWidget(action_row, export_btn)

    def do_cancel_click(c, w, t):
        result["action"] = "cancel"
        mqt.CloseCurrentDialog(False)

    cancel_btn = mqt.CreateButton(do_cancel_click)
    mqt.SetWidgetText(cancel_btn, "Cancel")
    mqt.AddWidget(action_row, cancel_btn)

    mqt.ShowWidgetAsDialog(top)
    mqt.DestroyWidget(top)
    return result


def show_pass_selection(mqt, passes, page_size=PASSES_PER_PAGE, default_busiest_only=False):
    """Drives Popup 2. By default all passes start checked; pass
    default_busiest_only=True (used by the direct "... (custom)" menu
    items) to instead start with only the single pass with the most draw
    calls checked. Every pagination / Select All / Select None click
    tears the dialog down and opens a fresh one built from the persisted
    `selection_state`, rather than mutating the live dialog - see
    build_pass_selection_page().

    Returns a set of selected pass keys, or None if the user cancelled."""
    if default_busiest_only and passes:
        busiest_key = max(passes, key=lambda p: p["count"])["key"]
        selection_state = {p["key"]: (p["key"] == busiest_key) for p in passes}
    else:
        selection_state = {p["key"]: True for p in passes}
    page = 0
    while True:
        result = build_pass_selection_page(mqt, passes, page, page_size, selection_state)
        action = result.get("action")
        if action == "cancel":
            return None
        if action == "export":
            return set(key for key, checked in selection_state.items() if checked)
        if action == "page":
            page = result.get("page", page)
            continue
        # select_all / select_none: selection_state already updated above;
        # redisplay the same page with the new state.
        continue


# ---------------------------------------------------------------------------
# Pass scanning (shared by the full dialog flow and the direct menu items)
# ---------------------------------------------------------------------------

def scan_passes(ctx, actions):
    """Groups actions into distinct passes by render-target set (see
    get_pass_key/classify_pass). Reads only data already cached on
    ActionDescription at capture-parse time, so this needs no replay and
    is effectively instant regardless of capture size."""
    found = {}
    for action in actions:
        key = get_pass_key(action)
        p = found.get(key)
        if p is None:
            color_ids, depth_id = key
            tag, label = classify_pass(
                ctx,
                [r for r in action.outputs if r != rd.ResourceId.Null()],
                action.depthOut)
            p = {
                "key": key,
                "index": len(found),
                "colorTargets": list(color_ids),
                "depthTarget": depth_id,
                "count": 0,
                "firstEventId": action.eventId,
                "tag": tag,
                "label": label,
            }
            found[key] = p
        p["count"] += 1
    return sorted(found.values(), key=lambda p: p["index"])


# ---------------------------------------------------------------------------
# Shared export pipeline (Phase 3) - runs the actual replay/export once the
# folder, draw call type(s) and passes have already been decided, whether
# that came from the full multi-popup dialog or one of the direct "Export
# scene ..." submenu items.
# ---------------------------------------------------------------------------

def run_export_pipeline(ctx, ext, mqt, out_dir, textures_dir, passes, selected_keys,
                         export_posed, export_all_instanced):
    def safe_ui_update(fn):
        try:
            mqt.InvokeOntoUIThread(fn)
        except Exception:
            pass

    tex_cache = {}
    proj_scale_cache = {}
    errors = []
    passes_out = {}  # pass_key -> {index, dir, meshes_dir, posed_dir, instanced_dir, colorTargets, depthTarget, draws}
    scanned_by_key = {p["key"]: p for p in passes}

    def get_pass_dir(key):
        pinfo = passes_out.get(key)
        if pinfo is None:
            idx = len(passes_out)
            scanned = scanned_by_key.get(key)
            tag = scanned["tag"] if scanned else "misc"
            label = scanned["label"] if scanned else PASS_CLASSIFICATIONS["misc"]
            pass_dir = os.path.join(out_dir, "pass_{:02d}_{}".format(idx, tag))
            meshes_dir = os.path.join(pass_dir, "meshes")
            os.makedirs(meshes_dir, exist_ok=True)
            posed_dir = None
            if export_posed:
                posed_dir = os.path.join(pass_dir, "meshes_posed")
                os.makedirs(posed_dir, exist_ok=True)
            instanced_dir = None
            if export_all_instanced:
                instanced_dir = os.path.join(pass_dir, "meshes_instanced")
                os.makedirs(instanced_dir, exist_ok=True)
            color_ids, depth_id = key
            pinfo = {
                "index": idx,
                "dir": pass_dir,
                "meshes_dir": meshes_dir,
                "posed_dir": posed_dir,
                "instanced_dir": instanced_dir,
                "colorTargets": list(color_ids),
                "depthTarget": depth_id,
                "guessedRole": label,
                "draws": [],
            }
            passes_out[key] = pinfo
        return pinfo

    def do_export(controller: rd.ReplayController):
        try:
            actions = get_all_actions(get_root_actions(ctx))
            sdfile = controller.GetStructuredFile()
            total = len(actions)
            print("[Renderdoc Scene Exporter] exporting {} selected pass(es) from {} candidate draws...".format(
                len(selected_keys), total))
            update_every = max(1, total // 20)

            for i, action in enumerate(actions):
                print('|')
                # Check membership BEFORE replaying to this event at all -
                # get_pass_key() needs no replay, so excluded passes pay
                # zero SetFrameEvent cost instead of being stepped through
                # and then discarded.
                key = get_pass_key(action)
                if key not in selected_keys:
                    continue

                print('.')
                controller.SetFrameEvent(action.eventId, False)
                print('.')
                state = controller.GetPipelineState()
                print('.')
                pinfo = get_pass_dir(key)
                print(':')


                bindings = []
                try:
                    # GetReadOnlyResources returns a flat List[UsedDescriptor].
                    # Each entry's actual bound resource is at .descriptor.resource,
                    # and .access.index gives the position in the shader
                    # reflection's readOnlyResources list (for the variable name).
                    ro = state.GetReadOnlyResources(rd.ShaderStage.Pixel)
                    print('.')
                    refl = state.GetShaderReflection(rd.ShaderStage.Pixel)
                    print('.')
                    for used in ro:
                        desc = used.descriptor
                        if desc is None or desc.resource == rd.ResourceId.Null():
                            continue
                        tex_file = export_texture(controller, desc.resource, textures_dir, tex_cache, ctx=ctx)
                        var_name = None
                        bindidx = used.access.index
                        if refl and 0 <= bindidx < len(refl.readOnlyResources):
                            var_name = refl.readOnlyResources[bindidx].name

                        res_name = None
                        if hasattr(ctx, "GetResourceName"):
                            try:
                                res_name = ctx.GetResourceName(desc.resource)
                            except Exception:
                                pass

                        bindings.append({
                            "bindPoint": bindidx,
                            "name": var_name,
                            "resourceName": res_name,
                            # Relative to this pass's own manifest.json, e.g. "../textures/tex_45.png"
                            "textureFile": os.path.relpath(tex_file, pinfo["dir"]) if tex_file else None,
                        })
                except Exception as e:
                    print("[Renderdoc Scene Exporter] texture read failed at eid {}: {}".format(action.eventId, e))

                print('.')
                mesh_name = "eid{}".format(action.eventId)
                print('.')
                mesh_path = export_mesh_for_action(
                    controller, state, action, pinfo["meshes_dir"], mesh_name,
                    pass_dir=pinfo["dir"], bindings=bindings)
                print('.')

                posed_path = None
                posed_info = None
                if export_posed:
                    print('+')
                    try:
                        posed_info = export_posed_mesh_for_action(
                            controller, state, action, pinfo["posed_dir"], mesh_name, proj_scale_cache,
                            pass_dir=pinfo["dir"], bindings=bindings, pass_key=key
                        )
                        if posed_info:
                            posed_path = posed_info["path"]
                            proj_info = posed_info.get("projInfo")
                            if proj_info is not None:
                                # Record the matrix once per (pass, source shader) rather
                                # than once per draw - it's normally the same for every
                                # draw in a pass that shares a shader, and this keeps the
                                # manifest from repeating an identical 4x4 matrix per draw.
                                vs_id = proj_info.get("vertexShaderResourceId")
                                pinfo.setdefault("viewProjections", {})
                                if vs_id not in pinfo["viewProjections"]:
                                    pinfo["viewProjections"][vs_id] = {
                                        "vertexShaderResourceId": vs_id,
                                        "constantBufferResourceId": proj_info.get("constantBufferResourceId"),
                                        "eScale": proj_info.get("eScale"),
                                        "matrix": proj_info.get("matrix"),
                                    }
                        print('.')
                    except Exception as e:
                        print("[Renderdoc Scene Exporter] posed mesh export failed at eid {}: {}".format(action.eventId, e))

                instanced_file = None
                if export_all_instanced and pinfo["instanced_dir"] is not None:
                    try:
                        instanced_file = export_instanced_geometry_for_action(
                            controller, state, action, mesh_path, pinfo["instanced_dir"], mesh_name)
                    except Exception as e:
                        print("[Renderdoc Scene Exporter] instanced mesh export failed at eid {}: {}".format(
                            action.eventId, e))

                try:
                    name = action.GetName(sdfile)
                    print('.')
                except Exception:
                    name = str(action.eventId)

                print('.')
                draw_entry = {
                    "eventId": action.eventId,
                    "name": name,
                    "numInstances": action.numInstances if (action.flags & rd.ActionFlags.Instanced) else 1,
                    "mesh": os.path.relpath(mesh_path, pinfo["dir"]) if mesh_path else None,
                    "posedMesh": os.path.relpath(posed_path, pinfo["dir"]) if posed_path else None,
                    "instancedMeshData": os.path.relpath(instanced_file, pinfo["dir"]) if instanced_file else None,
                    "textures": bindings,
                }
                if posed_info:
                    # Which correction (if any) was applied to THIS draw's posed
                    # mesh - see "viewProjections" in this pass's manifest.json for
                    # the actual matrix, keyed by vertexShaderResourceId below.
                    draw_entry["posedSpace"] = posed_info.get("spaceNote")
                    proj_info = posed_info.get("projInfo")
                    draw_entry["viewProjectionShader"] = (
                        proj_info.get("vertexShaderResourceId") if proj_info else None
                    )
                pinfo["draws"].append(draw_entry)

                print('v')
                done = i + 1
                if done % update_every == 0 or done == total:
                    print("[Renderdoc Scene Exporter] scanned {}/{}, exported {} so far".format(
                        done, total, sum(len(p["draws"]) for p in passes_out.values())))
        except Exception:
            import traceback
            tb = traceback.format_exc()
            print("[Renderdoc Scene Exporter] export failed:\n" + tb)
            errors.append(tb)
        finally:
            # File I/O is fine directly on the replay thread; only dialog
            # calls need to be marshalled onto the UI thread.
            try:
                index = {"passes": []}
                for pinfo in sorted(passes_out.values(), key=lambda p: p["index"]):
                    with open(os.path.join(pinfo["dir"], "manifest.json"), "w") as f:
                        json.dump({
                            "draws": pinfo["draws"],
                            # Recovered view/projection matrix (and derived eScale, see
                            # find_vertex_shader_view_scale) per source vertex shader used
                            # by posed-mesh draws in THIS pass only - a pass boundary is
                            # exactly where these are expected to change (e.g. a shadow
                            # pass's light-space matrix vs. the main pass's camera
                            # matrix), so they're intentionally not merged with other
                            # passes' entries even if the same shader resource ID
                            # reappears there with different bound data. Empty/absent
                            # when posed export was off or no matrix was recognised for
                            # any draw in this pass.
                            "viewProjections": pinfo.get("viewProjections", {}),
                        }, f, indent=2)
                    index["passes"].append({
                        "folder": os.path.basename(pinfo["dir"]),
                        "index": pinfo["index"],
                        "guessedRole": pinfo["guessedRole"],
                        "colorTargets": pinfo["colorTargets"],
                        "depthTarget": pinfo["depthTarget"],
                        "drawCount": len(pinfo["draws"]),
                    })
                with open(os.path.join(out_dir, "manifest.json"), "w") as f:
                    json.dump(index, f, indent=2)
            except Exception:
                import traceback
                print("[Renderdoc Scene Exporter] failed writing manifest.json:\n" + traceback.format_exc())

            def finish():
                if errors:
                    ext.ErrorDialog("Export hit an error:\n\n" + errors[0], "Renderdoc Scene Exporter")
                else:
                    total_draws = sum(len(p["draws"]) for p in passes_out.values())
                    ext.MessageDialog(
                        "Exported {} draws across {} pass(es) to:\n{}".format(
                            total_draws, len(passes_out), out_dir),
                        "Renderdoc Scene Exporter")
            safe_ui_update(finish)

    print("[Renderdoc Scene Exporter] export started in the background - progress prints to the console.")
    ctx.Replay().AsyncInvoke("SceneExporterExport", do_export)


def run_export(ctx: qrd.CaptureContext):
    """Full dialog flow: Popup 0 (folder) -> Popup 1 (draw call types) ->
    Popup 2 (pass selection). This is "Export scene (dialog)" on the
    Tools menu."""
    export_posed = True
    ext = ctx.Extensions()

    if not ctx.IsCaptureLoaded():
        ext.MessageDialog("No capture is loaded.", "Renderdoc Scene Exporter")
        return

    # --- Popup 0: folder choice (unchanged) ---------------------------------
    out_dir = ext.OpenDirectoryName("Choose export folder")
    if not out_dir:
        return

    # Textures are shared/deduplicated across the whole export (the same
    # texture is often read by multiple passes), so they live in one common
    # folder. Meshes are split per-pass instead - see get_pass_key().
    textures_dir = os.path.join(out_dir, "textures")
    os.makedirs(textures_dir, exist_ok=True)

    mqt = ext.GetMiniQtHelper()

    # --- Phase 1: scan the capture for distinct passes ----------------------
    actions = get_all_actions(get_root_actions(ctx))
    if not actions:
        ext.MessageDialog("No draw calls were found in this capture's action tree.", "Renderdoc Scene Exporter")
        return

    passes = scan_passes(ctx, actions)
    print("[Renderdoc Scene Exporter] found {} pass(es) across {} draws.".format(len(passes), len(actions)))

    # --- Popup 1: draw call types --------------------------------------------
    type_result = show_draw_type_selection(mqt, passes)
    if type_result is None:
        print("[Renderdoc Scene Exporter] export cancelled.")
        return
    selected_tags, export_all_instanced = type_result

    passes = [p for p in passes if p["tag"] in selected_tags]
    if not passes:
        ext.MessageDialog("No passes matched the selected draw call type(s).", "Renderdoc Scene Exporter")
        return

    # --- Popup 2: let the user pick which passes to export -------------------
    selected_keys = show_pass_selection(mqt, passes)
    if selected_keys is None:
        print("[Renderdoc Scene Exporter] export cancelled.")
        return
    if not selected_keys:
        ext.MessageDialog("No passes selected - nothing to export.", "Renderdoc Scene Exporter")
        return

    run_export_pipeline(ctx, ext, mqt, out_dir, textures_dir, passes, selected_keys,
                         export_posed, export_all_instanced)


def run_export_direct(ctx: qrd.CaptureContext, tag_filter, custom: bool, export_all_instanced: bool):
    """Direct "Export scene ..." submenu items - skips the draw-call-types
    popup (Popup 1) entirely, since the type is already fixed by which
    menu item was clicked.

    tag_filter is None for "all", or a set of pass tags (see
    PASS_CLASSIFICATIONS) to restrict to (e.g. {"forward"},
    {"gbuffer"}, {"forward", "gbuffer"}).

    When custom is False ("... (all)"), every matching pass is exported
    with no further popup beyond the folder picker. When custom is True
    ("... (custom)"), Popup 2 (paginated pass selection) is shown,
    defaulting to just the single busiest matching pass checked."""
    export_posed = True
    ext = ctx.Extensions()

    if not ctx.IsCaptureLoaded():
        ext.MessageDialog("No capture is loaded.", "Renderdoc Scene Exporter")
        return

    out_dir = ext.OpenDirectoryName("Choose export folder")
    if not out_dir:
        return

    textures_dir = os.path.join(out_dir, "textures")
    os.makedirs(textures_dir, exist_ok=True)

    mqt = ext.GetMiniQtHelper()

    actions = get_all_actions(get_root_actions(ctx))
    if not actions:
        ext.MessageDialog("No draw calls were found in this capture's action tree.", "Renderdoc Scene Exporter")
        return

    passes = scan_passes(ctx, actions)
    if tag_filter is not None:
        passes = [p for p in passes if p["tag"] in tag_filter]
    if not passes:
        ext.MessageDialog("No passes matched the selected draw call type.", "Renderdoc Scene Exporter")
        return

    if custom:
        selected_keys = show_pass_selection(mqt, passes, default_busiest_only=True)
        if selected_keys is None:
            print("[Renderdoc Scene Exporter] export cancelled.")
            return
        if not selected_keys:
            ext.MessageDialog("No passes selected - nothing to export.", "Renderdoc Scene Exporter")
            return
    else:
        selected_keys = set(p["key"] for p in passes)

    run_export_pipeline(ctx, ext, mqt, out_dir, textures_dir, passes, selected_keys,
                         export_posed, export_all_instanced)
