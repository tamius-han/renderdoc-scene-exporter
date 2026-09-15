import qrenderdoc as qrd
import renderdoc as rd

from . import core

# Keep references so menu items / callbacks aren't garbage collected
_ctx_store = {}

# (label suffix, tag_filter, custom) for the 8 options repeated under both
# "export" and "export with instanced geometry".
_DRAW_TYPE_OPTIONS = [
    ("all", None, False),
    ("all (custom selection)", None, True),
    ("forward (all)", {"forward"}, False),
    ("forward (custom)", {"forward"}, True),
    ("g-buffer (all)", {"gbuffer"}, False),
    ("g-buffer (custom)", {"gbuffer"}, True),
    ("forward & g-buffer (all)", {"forward", "gbuffer"}, False),
    ("forward & g-buffer (custom)", {"forward", "gbuffer"}, True),
]

# (submenu label, export_all_instanced)
_SUBMENUS = [
    ("export", False),
    ("export with instanced geometry", True),
]


def register(version: str, ctx: qrd.CaptureContext):
    print("[Renderdoc Scene Exporter] registering against RenderDoc", version)

    menu = ctx.Extensions()

    def dialog_callback(ctx, data):
        core.run_export(ctx)

    _ctx_store["Export scene (dialog)"] = menu.RegisterWindowMenu(
        qrd.WindowMenu.Tools, ["Export scene (dialog)"], dialog_callback)

    for submenu_label, export_all_instanced in _SUBMENUS:
        for option_label, tag_filter, custom in _DRAW_TYPE_OPTIONS:
            def make_callback(tag_filter=tag_filter, custom=custom, export_all_instanced=export_all_instanced):
                def callback(ctx, data):
                    core.run_export_direct(ctx, tag_filter, custom, export_all_instanced)
                return callback

            path = ["Export scene ...", submenu_label, option_label]
            _ctx_store[tuple(path)] = menu.RegisterWindowMenu(
                qrd.WindowMenu.Tools, path, make_callback())


def unregister():
    print("[Renderdoc Scene Exporter] unregistering")
    _ctx_store.clear()
