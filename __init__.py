import qrenderdoc as qrd
import renderdoc as rd

from . import core

# Keep a reference so the menu item / callback isn't garbage collected
_ctx_store = {}


def register(version: str, ctx: qrd.CaptureContext):
    print("[SceneExporter] registering against RenderDoc", version)

    def export_callback(ctx, data):
        core.run_export(ctx, export_posed=False)

    def export_posed_callback(ctx, data):
        core.run_export(ctx, export_posed=True)

    _ctx_store['menu_item'] = ctx.Extensions().RegisterWindowMenu(
        qrd.WindowMenu.Tools, ["Export Scene..."], export_callback)
    _ctx_store['menu_item_posed'] = ctx.Extensions().RegisterWindowMenu(
        qrd.WindowMenu.Tools, ["Export Scene (with posed meshes)..."], export_posed_callback)


def unregister():
    print("[SceneExporter] unregistering")
    _ctx_store.clear()
