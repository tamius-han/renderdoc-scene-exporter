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
    "final": "🏁 final/presented to screen",
    "shadow": "depth-only",
    "gbuffer": "likely G-buffer/deferred (multiple color targets)",
    "forward": "🔶 possible main/forward scene pass (single color + depth)",
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
    for a in attrs:
        # perInstance attributes aren't handled by this exporter - skip them,
        # same as RenderDoc's own mesh-decoding example does.
        if not a.used or a.perInstance:
            continue
        nm = a.name.upper()
        if pos_attr is None and ('POSITION' in nm or nm == 'POS'):
            pos_attr = a
        elif norm_attr is None and 'NORMAL' in nm:
            norm_attr = a
        elif uv_attr is None and ('TEXCOORD' in nm or 'UV' in nm):
            uv_attr = a
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


def pick_material_textures(bindings):
    """Best-effort guess at which bound texture is the diffuse/albedo map
    and which (if any) is a normal/bump map, from a draw's texture
    bindings list (as built in run_export - each entry has "name" and
    "textureFile", relative to the pass folder). Falls back to the first
    bound texture as diffuse if no name match is found. Returns
    (diffuse_binding, normal_binding), either of which may be None."""
    diffuse = None
    normal = None
    for b in bindings:
        if not b.get("textureFile"):
            continue
        nm = (b.get("name") or "").lower()
        if diffuse is None and any(k in nm for k in
                                    ("diffuse", "albedo", "basecolor", "base_color", "colour")):
            diffuse = b
        elif normal is None and any(k in nm for k in ("normal", "bump", "nrm")):
            normal = b
    if diffuse is None:
        for b in bindings:
            if b.get("textureFile") and b is not normal:
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
    if diffuse is None:
        return None, None

    def resolve(b):
        # b["textureFile"] is relative to pass_dir (see run_export) -
        # resolve to absolute, then re-relativize to wherever the .mtl
        # actually lives (mesh_dir), since those aren't the same folder.
        return os.path.normpath(os.path.join(pass_dir, b["textureFile"]))

    mat_name = "mat_" + mesh_name
    mtl_path = os.path.join(mesh_dir, mesh_name + ".mtl")
    diffuse_rel = os.path.relpath(resolve(diffuse), mesh_dir).replace(os.sep, "/")
    lines = [
        "newmtl {}\n".format(mat_name),
        "Kd 1.000 1.000 1.000\n",
        "map_Kd {}\n".format(diffuse_rel),
    ]
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


def find_vertex_shader_view_scale(controller, state, cache):
    """Best-effort: scan the vertex shader's constant buffers for a 4x4
    matrix that looks like a standalone view/projection matrix (i.e. not
    already combined with a per-object model matrix), and return its Y-axis
    scale term e = 1/tan(fovY/2).

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
    with a per-object model matrix destroys this clean signature. Results
    are cached per vertex-shader resource ID since the view/projection
    matrix is normally the same across every draw sharing that shader.
    """
    refl = state.GetShaderReflection(rd.ShaderStage.Vertex)
    if refl is None or not refl.constantBlocks:
        return None

    cache_key = refl.resourceId
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
                            return abs(e)
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
                                  pass_dir=None, bindings=None):
    """Reads the vertex shader's OUTPUT data for one draw (post skinning,
    morphing, and any transform the shader applies) and writes an OBJ - this
    is the mesh 'as posed', matching what's actually drawn on screen.

    Note: this only looks at the vertex shader stage output (VSOut). If the
    pipeline also has tessellation or a geometry shader, the truly final
    pre-rasterizer positions come from a later stage - extend the
    MeshDataStage passed to GetPostVSData() below (e.g. DomainOut/GSOut) if
    you need that.
    """
    if action.numIndices == 0:
        return None

    postvs = controller.GetPostVSData(0, 0, rd.MeshDataStage.VSOut)
    if postvs is None or postvs.vertexResourceId == rd.ResourceId.Null():
        return None

    vs_refl = state.GetShaderReflection(rd.ShaderStage.Vertex)
    if vs_refl is None:
        return None

    outputs, fallback_stride = build_postvs_outputs(vs_refl)
    if outputs is None:
        return None

    indices = get_postvs_indices(controller, postvs)
    unique_locals, min_idx, face_indices = compact_indices(indices)
    if not unique_locals:
        return None
    count = unique_locals[-1] + 1

    stride = postvs.vertexByteStride if postvs.vertexByteStride else fallback_stride
    # Single windowed fetch covering only the vertices this draw actually
    # uses, shared across position/normal/uv (all interleaved in one buffer).
    window_start = postvs.vertexByteOffset + stride * min_idx
    window_len = stride * count
    vb_data = controller.GetBufferData(postvs.vertexResourceId, window_start, window_len)

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
    for o in outputs[1:]:
        nm = o["name"].upper()
        if alt_pos_attr is None and 'POS' in nm and o["compCount"] >= 3:
            alt_pos_attr = o
        elif norm_attr is None and 'NORMAL' in nm:
            norm_attr = o
        elif uv_attr is None and ('TEXCOORD' in nm or 'UV' in nm):
            uv_attr = o

    is_ortho = None
    e_scale = None
    if alt_pos_attr is not None:
        raw_positions = read_postvs_selected(vb_data, stride, alt_pos_attr, unique_locals)
        positions = [p[:3] if p else None for p in raw_positions] if raw_positions else None
    else:
        raw_positions = read_postvs_selected(vb_data, stride, clip_pos_attr, unique_locals)
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
            aspect = (vp.width / vp.height) if vp and vp.height else 1.0

            ws = [p[3] for p in raw_positions if p is not None and len(p) >= 4]
            is_ortho = bool(ws) and all(abs(w - 1.0) < 1e-4 for w in ws)

            # clip.x = x_view*e/aspect and clip.y = y_view*e (e = 1/tan(fovY/2)),
            # but clip.w = z_view with NO e factor - so after undoing the
            # aspect ratio, X/Y are scaled by e relative to Z unless we also
            # divide them by e. Perspective only; orthographic has no e term.
            e_scale = None if is_ortho else find_vertex_shader_view_scale(controller, state, proj_scale_cache)

            positions = []
            for p in raw_positions:
                if p is None or len(p) < 4:
                    positions.append(None)
                    continue
                x, y, z, w = p[0], p[1], p[2], p[3]
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

    normals = read_postvs_selected(vb_data, stride, norm_attr, unique_locals) if norm_attr else None
    uvs = read_postvs_selected(vb_data, stride, uv_attr, unique_locals) if uv_attr else None

    path = os.path.join(out_dir, mesh_name + "_posed.obj")
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
    write_obj(path, action, positions, normals, uvs, face_indices, note=space_note, mtllib=mtllib, usemtl=usemtl)
    return path


# ---------------------------------------------------------------------------
# Texture export
# ---------------------------------------------------------------------------

def export_texture(controller, resource_id, out_dir, tex_cache):
    if resource_id in tex_cache:
        return tex_cache[resource_id]
    filename = os.path.join(out_dir, "tex_{}.png".format(int(resource_id)))
    save = rd.TextureSave()
    save.resourceId = resource_id
    save.destType = rd.FileType.PNG
    save.mip = 0
    save.slice.sliceIndex = 0
    ok = controller.SaveTexture(save, filename)
    tex_cache[resource_id] = filename if ok else None
    return tex_cache[resource_id]


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def show_pass_selection(mqt, passes):
    """Blocking modal checkbox dialog letting the user pick which detected
    passes to export. Safe to use ShowWidgetAsDialog here specifically
    because nothing else needs to call back into the UI thread while it's
    up (the scan's AsyncInvoke has already fully finished by this point) -
    unlike the earlier progress-bar attempts, there's no concurrent async
    activity for this dialog to conflict with.

    Returns a set of selected pass keys, or None if the user cancelled."""
    checkboxes = []

    top = mqt.CreateToplevelWidget("Renderdoc Scene Exporter - select passes", lambda c, w, t: None)
    outer = mqt.CreateVerticalContainer()
    mqt.AddWidget(top, outer)

    label = mqt.CreateLabel()
    mqt.SetWidgetText(label, "Select which render pass(es) to export (guessed roles in brackets - not authoritative):")
    mqt.AddWidget(outer, label)

    for p in passes:
        cb = mqt.CreateCheckbox(lambda c, w, t: None)
        mqt.SetWidgetChecked(cb, True)
        mqt.SetWidgetText(cb, "pass_{:02d}  [{}]  -  {} draw(s), {} color target(s), depth={}".format(
            p["index"], p["label"], p["count"], len(p["colorTargets"]), "yes" if p["depthTarget"] else "no"))
        mqt.AddWidget(outer, cb)
        checkboxes.append((p["key"], cb))

    toggle_row = mqt.CreateHorizontalContainer()
    mqt.AddWidget(outer, toggle_row)

    def select_all(c, w, t):
        for _, cb in checkboxes:
            mqt.SetWidgetChecked(cb, True)

    def select_none(c, w, t):
        for _, cb in checkboxes:
            mqt.SetWidgetChecked(cb, False)

    all_btn = mqt.CreateButton(select_all)
    mqt.SetWidgetText(all_btn, "Select All")
    mqt.AddWidget(toggle_row, all_btn)

    none_btn = mqt.CreateButton(select_none)
    mqt.SetWidgetText(none_btn, "Select None")
    mqt.AddWidget(toggle_row, none_btn)

    action_row = mqt.CreateHorizontalContainer()
    mqt.AddWidget(outer, action_row)

    export_btn = mqt.CreateButton(lambda c, w, t: mqt.CloseCurrentDialog(True))
    mqt.SetWidgetText(export_btn, "Export Selected")
    mqt.AddWidget(action_row, export_btn)

    cancel_btn = mqt.CreateButton(lambda c, w, t: mqt.CloseCurrentDialog(False))
    mqt.SetWidgetText(cancel_btn, "Cancel")
    mqt.AddWidget(action_row, cancel_btn)

    confirmed = mqt.ShowWidgetAsDialog(top)
    selected = set(key for key, cb in checkboxes if mqt.IsWidgetChecked(cb)) if confirmed else None
    mqt.DestroyWidget(top)
    return selected


def run_export(ctx: qrd.CaptureContext, export_posed: bool = False):
    ext = ctx.Extensions()

    if not ctx.IsCaptureLoaded():
        ext.MessageDialog("No capture is loaded.", "Renderdoc Scene Exporter")
        return

    out_dir = ext.OpenDirectoryName("Choose export folder")
    if not out_dir:
        return

    # Textures are shared/deduplicated across the whole export (the same
    # texture is often read by multiple passes), so they live in one common
    # folder. Meshes are split per-pass instead - see get_pass_key().
    textures_dir = os.path.join(out_dir, "textures")
    os.makedirs(textures_dir, exist_ok=True)

    mqt = ext.GetMiniQtHelper()

    def safe_ui_update(fn):
        try:
            mqt.InvokeOntoUIThread(fn)
        except Exception:
            pass

    # --- Phase 1: scan the capture for distinct passes ----------------------
    # get_pass_key() reads data (outputs/depthOut) already sitting on each
    # ActionDescription from when the capture was parsed, so this needs no
    # replay/SetFrameEvent at all - it's just a plain Python loop over
    # already-cached UI-thread data, done directly here rather than via
    # AsyncInvoke.
    actions = get_all_actions(get_root_actions(ctx))
    if not actions:
        ext.MessageDialog("No draw calls were found in this capture's action tree.", "Renderdoc Scene Exporter")
        return

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
    passes = sorted(found.values(), key=lambda p: p["index"])
    print("[Renderdoc Scene Exporter] found {} pass(es) across {} draws.".format(len(passes), len(actions)))

    # --- Phase 2: let the user pick which passes to export ------------------
    selected_keys = show_pass_selection(mqt, passes)
    if selected_keys is None:
        print("[Renderdoc Scene Exporter] export cancelled.")
        return
    if not selected_keys:
        ext.MessageDialog("No passes selected - nothing to export.", "Renderdoc Scene Exporter")
        return

    # --- Phase 3: the real export, filtered to selected passes -------------
    def start_export(selected_keys):
        tex_cache = {}
        proj_scale_cache = {}
        errors = []
        passes_out = {}  # pass_key -> {index, dir, meshes_dir, posed_dir, colorTargets, depthTarget, draws}
        # Reuse the classification already computed during the scan (phase 1)
        # rather than re-deriving it - `passes` is the sorted list from that
        # scan, still in scope here via closure.
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
                color_ids, depth_id = key
                pinfo = {
                    "index": idx,
                    "dir": pass_dir,
                    "meshes_dir": meshes_dir,
                    "posed_dir": posed_dir,
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
                    # Check membership BEFORE replaying to this event at all -
                    # get_pass_key() needs no replay, so excluded passes pay
                    # zero SetFrameEvent cost instead of being stepped through
                    # and then discarded.
                    key = get_pass_key(action)
                    if key not in selected_keys:
                        continue

                    controller.SetFrameEvent(action.eventId, False)
                    state = controller.GetPipelineState()
                    pinfo = get_pass_dir(key)

                    bindings = []
                    try:
                        # GetReadOnlyResources returns a flat List[UsedDescriptor].
                        # Each entry's actual bound resource is at .descriptor.resource,
                        # and .access.index gives the position in the shader
                        # reflection's readOnlyResources list (for the variable name).
                        ro = state.GetReadOnlyResources(rd.ShaderStage.Pixel)
                        refl = state.GetShaderReflection(rd.ShaderStage.Pixel)
                        for used in ro:
                            desc = used.descriptor
                            if desc is None or desc.resource == rd.ResourceId.Null():
                                continue
                            tex_file = export_texture(controller, desc.resource, textures_dir, tex_cache)
                            var_name = None
                            bindidx = used.access.index
                            if refl and 0 <= bindidx < len(refl.readOnlyResources):
                                var_name = refl.readOnlyResources[bindidx].name
                            bindings.append({
                                "bindPoint": bindidx,
                                "name": var_name,
                                # Relative to this pass's own manifest.json, e.g. "../textures/tex_45.png"
                                "textureFile": os.path.relpath(tex_file, pinfo["dir"]) if tex_file else None,
                            })
                    except Exception as e:
                        print("[Renderdoc Scene Exporter] texture read failed at eid {}: {}".format(action.eventId, e))

                    mesh_name = "eid{}".format(action.eventId)
                    mesh_path = export_mesh_for_action(
                        controller, state, action, pinfo["meshes_dir"], mesh_name,
                        pass_dir=pinfo["dir"], bindings=bindings)

                    posed_path = None
                    if export_posed:
                        try:
                            posed_path = export_posed_mesh_for_action(
                                controller, state, action, pinfo["posed_dir"], mesh_name, proj_scale_cache,
                                pass_dir=pinfo["dir"], bindings=bindings)
                        except Exception as e:
                            print("[Renderdoc Scene Exporter] posed mesh export failed at eid {}: {}".format(action.eventId, e))

                    try:
                        name = action.GetName(sdfile)
                    except Exception:
                        name = str(action.eventId)

                    pinfo["draws"].append({
                        "eventId": action.eventId,
                        "name": name,
                        "mesh": os.path.relpath(mesh_path, pinfo["dir"]) if mesh_path else None,
                        "posedMesh": os.path.relpath(posed_path, pinfo["dir"]) if posed_path else None,
                        "textures": bindings,
                    })

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
                            json.dump({"draws": pinfo["draws"]}, f, indent=2)
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

    start_export(selected_keys)
