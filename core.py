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


def read_attr(controller, vbuffers, attr, max_index):
    if attr is None or attr.vertexBuffer >= len(vbuffers) or attr.vertexBuffer < 0:
        return None
    vb = vbuffers[attr.vertexBuffer]
    if vb.resourceId == rd.ResourceId.Null():
        return None

    comp_size = attr.format.compByteWidth * attr.format.compCount
    stride = vb.byteStride if vb.byteStride else comp_size
    base_offset = attr.byteOffset + vb.byteOffset
    needed = base_offset + stride * max_index
    data = controller.GetBufferData(vb.resourceId, 0, needed)

    out = []
    for i in range(max_index):
        base = base_offset + stride * i
        chunk = data[base:base + comp_size]
        if len(chunk) < comp_size:
            out.append(None)
            continue
        out.append(decode_attribute(chunk, attr.format))
    return out


def write_obj(path, action, positions, normals, uvs, indices):
    with open(path, "w") as f:
        f.write("# eid={}\n".format(action.eventId))
        for p in positions:
            vals = (list(p) + [0, 0, 0])[:3] if p else [0, 0, 0]
            f.write("v {} {} {}\n".format(*vals))
        if uvs:
            for uv in uvs:
                vals = (list(uv) + [0, 0])[:2] if uv else [0, 0]
                f.write("vt {} {}\n".format(vals[0], 1.0 - vals[1]))
        if normals:
            for n in normals:
                vals = (list(n) + [0, 0, 1])[:3] if n else [0, 0, 1]
                f.write("vn {} {} {}\n".format(*vals))

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
            f.write("f " + " ".join(face) + "\n")


def export_mesh_for_action(controller, state, action, out_dir, mesh_name):
    """Reads raw input-assembler vertex data for one draw and writes an OBJ.
    This is the pre-transform (bind pose / object space) geometry."""
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
        raw = controller.GetBufferData(ibuf.resourceId, ibuf.byteOffset, 0)
        offset = action.indexOffset * idx_width
        indices = list(struct.unpack_from(
            '<' + fmt_char * action.numIndices, raw, offset))
        # baseVertex is an offset applied on top of each raw index
        indices = [i + action.baseVertex for i in indices]
    else:
        # Non-indexed: vertices are consumed sequentially starting at vertexOffset
        indices = [action.vertexOffset + i for i in range(action.numIndices)]

    max_index = (max(indices) + 1) if indices else 0
    positions = read_attr(controller, vbuffers, pos_attr, max_index)
    normals = read_attr(controller, vbuffers, norm_attr, max_index) if norm_attr else None
    uvs = read_attr(controller, vbuffers, uv_attr, max_index) if uv_attr else None

    if positions is None:
        return None

    path = os.path.join(out_dir, mesh_name + ".obj")
    write_obj(path, action, positions, normals, uvs, indices)
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


def read_postvs_attr(controller, postvs, out_attr, max_index, fallback_stride):
    ch = vartype_struct_char(out_attr["varType"])
    if ch is None:
        return None
    comp_count = out_attr["compCount"]
    comp_size = out_attr["compByteWidth"] * comp_count
    stride = postvs.vertexByteStride if postvs.vertexByteStride else fallback_stride
    base_offset = postvs.vertexByteOffset + out_attr["byteOffset"]
    needed = base_offset + stride * max_index
    data = controller.GetBufferData(postvs.vertexResourceId, 0, needed)

    out = []
    for i in range(max_index):
        base = base_offset + stride * i
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
        raw = controller.GetBufferData(postvs.indexResourceId, postvs.indexByteOffset, 0)
        indices = list(struct.unpack_from('<' + fmt_char * postvs.numIndices, raw, 0))
        return [i + postvs.baseVertex for i in indices]
    return list(range(postvs.numIndices))


def export_posed_mesh_for_action(controller, state, action, out_dir, mesh_name):
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
    max_index = (max(indices) + 1) if indices else 0

    # outputs[0] is always the builtin clip-space position (SV_Position /
    # gl_Position). If the shader ALSO passes through a separate, named
    # position-like varying (very common for lighting, e.g. "WorldPos"),
    # prefer that - it's the mesh's pose without camera-projection distortion.
    # Otherwise fall back to the clip-space position and perspective-divide
    # it, which gives you the shape literally as projected on screen.
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

    if alt_pos_attr is not None:
        raw_positions = read_postvs_attr(controller, postvs, alt_pos_attr, max_index, fallback_stride)
        positions = [p[:3] if p else None for p in raw_positions] if raw_positions else None
    else:
        raw_positions = read_postvs_attr(controller, postvs, clip_pos_attr, max_index, fallback_stride)
        positions = None
        if raw_positions:
            positions = []
            for p in raw_positions:
                if p is None or len(p) < 4 or p[3] == 0:
                    positions.append(None)
                else:
                    positions.append((p[0] / p[3], p[1] / p[3], p[2] / p[3]))

    if not positions:
        return None

    normals = read_postvs_attr(controller, postvs, norm_attr, max_index, fallback_stride) if norm_attr else None
    uvs = read_postvs_attr(controller, postvs, uv_attr, max_index, fallback_stride) if uv_attr else None

    path = os.path.join(out_dir, mesh_name + "_posed.obj")
    write_obj(path, action, positions, normals, uvs, indices)
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

def run_export(ctx: qrd.CaptureContext, export_posed: bool = False):
    ext = ctx.Extensions()

    if not ctx.IsCaptureLoaded():
        ext.MessageDialog("No capture is loaded.", "SceneExporter")
        return

    out_dir = ext.OpenDirectoryName("Choose export folder")
    if not out_dir:
        return

    meshes_dir = os.path.join(out_dir, "meshes")
    textures_dir = os.path.join(out_dir, "textures")
    os.makedirs(meshes_dir, exist_ok=True)
    os.makedirs(textures_dir, exist_ok=True)
    posed_dir = None
    if export_posed:
        posed_dir = os.path.join(out_dir, "meshes_posed")
        os.makedirs(posed_dir, exist_ok=True)

    manifest = {"draws": []}
    tex_cache = {}
    errors = []

    def do_export(controller: rd.ReplayController):
        try:
            actions = get_all_actions(get_root_actions(ctx))
            sdfile = controller.GetStructuredFile()

            if not actions:
                errors.append("No draw calls were found in this capture's action tree.")
                return

            for action in actions:
                controller.SetFrameEvent(action.eventId, False)
                state = controller.GetPipelineState()

                mesh_name = "eid{}".format(action.eventId)
                mesh_path = export_mesh_for_action(controller, state, action, meshes_dir, mesh_name)

                posed_path = None
                if export_posed:
                    try:
                        posed_path = export_posed_mesh_for_action(controller, state, action, posed_dir, mesh_name)
                    except Exception as e:
                        print("[SceneExporter] posed mesh export failed at eid {}: {}".format(action.eventId, e))

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
                        idx = used.access.index
                        if refl and 0 <= idx < len(refl.readOnlyResources):
                            var_name = refl.readOnlyResources[idx].name
                        bindings.append({
                            "bindPoint": idx,
                            "name": var_name,
                            "textureFile": os.path.relpath(tex_file, out_dir) if tex_file else None,
                        })
                except Exception as e:
                    print("[SceneExporter] texture read failed at eid {}: {}".format(action.eventId, e))

                try:
                    name = action.GetName(sdfile)
                except Exception:
                    name = str(action.eventId)

                manifest["draws"].append({
                    "eventId": action.eventId,
                    "name": name,
                    "mesh": os.path.relpath(mesh_path, out_dir) if mesh_path else None,
                    "posedMesh": os.path.relpath(posed_path, out_dir) if posed_path else None,
                    "textures": bindings,
                })
        except Exception:
            import traceback
            tb = traceback.format_exc()
            print("[SceneExporter] export failed:\n" + tb)
            errors.append(tb)

    ctx.Replay().BlockInvoke(do_export)

    if errors:
        ext.ErrorDialog("Export hit an error:\n\n" + errors[0], "SceneExporter")
        return

    with open(os.path.join(out_dir, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)


    ext.MessageDialog(
        "Exported {} draws to:\n{}".format(len(manifest["draws"]), out_dir),
        "SceneExporter")
