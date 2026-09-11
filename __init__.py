import qrenderdoc as qrd
import renderdoc as rd

from . import core

# Keep a reference so the menu item / callback isn't garbage collected
_ctx_store = {}


def register(version: str, ctx: qrd.CaptureContext):
    print("[Renderdoc Scene Exporter] registering against RenderDoc", version)

    def make_export_callback(pass_mode):
        def export_callback(ctx, data):
            core.run_export(ctx, export_posed=True, pass_mode=pass_mode)
        return export_callback

    menu = ctx.Extensions()
    for name, pass_mode in (
        ("Export scene (all passes)", "all"),
        ("Export scene (forward passes only)", "forward"),
        ("Export scene (hide post-processing)", "hide_postprocess"),
    ):
        _ctx_store[name] = menu.RegisterWindowMenu(
            qrd.WindowMenu.Tools, [name], make_export_callback(pass_mode))


def unregister():
    print("[Renderdoc Scene Exporter] unregistering")
    _ctx_store.clear()
