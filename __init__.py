import qrenderdoc as qrd
import renderdoc as rd

from . import core

# Keep a reference so the menu item / callback isn't garbage collected
_ctx_store = {}


def register(version: str, ctx: qrd.CaptureContext):
    print("[Renderdoc Scene Exporter] registering against RenderDoc", version)

    def export_posed_callback(ctx, data):
        core.run_export(ctx, export_posed=True)

    _ctx_store['menu_item_posed'] = ctx.Extensions().RegisterWindowMenu(
        qrd.WindowMenu.Tools, ["Export Scene ..."], export_posed_callback)


def unregister():
    print("[Renderdoc Scene Exporter] unregistering")
    _ctx_store.clear()
