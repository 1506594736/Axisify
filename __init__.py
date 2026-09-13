# -*- coding: utf-8 -*-
"""Axisify —— 实体化侧壁沿轴对齐

把实体化（Solidify）生成的侧壁精确投影到 X / Y / Z 轴，使其与所选轴严格平行。
"""

bl_info = {
    "name": "Axisify",
    "author": "HULIMIAO",
    "version": (1, 5, 1),
    "location": "3D 视图 > N 面板 > Axisify",
    "description": "实体化并让侧壁精确对齐到 X/Y/Z 轴，可作为可调修改器（带厚度钳制）",
    "doc_url": "https://github.com/1506594736/Axisify",
    "tracker_url": "https://github.com/1506594736/Axisify/issues",
    "category": "Mesh",
}

import bpy
import bmesh
from bpy.props import BoolProperty, EnumProperty, FloatProperty, PointerProperty, StringProperty
from bpy.types import Operator, Panel, PropertyGroup

from . import core, gn

AXIS_ITEMS = [
    ('+X', "+X", "指向 +X"),
    ('-X', "-X", "指向 -X"),
    ('+Y', "+Y", "指向 +Y"),
    ('-Y', "-Y", "指向 -Y"),
    ('+Z', "+Z", "指向 +Z"),
    ('-Z', "-Z", "指向 -Z"),
]
AXIS_VEC = {
    '+X': (1.0, 0.0, 0.0), '-X': (-1.0, 0.0, 0.0),
    '+Y': (0.0, 1.0, 0.0), '-Y': (0.0, -1.0, 0.0),
    '+Z': (0.0, 0.0, 1.0), '-Z': (0.0, 0.0, -1.0),
}

# Solidify 的 offset 符号约定（已用立方体实测确认）：
#   -1 -> 向内（原始面成为外表面）   0 -> 居中   +1 -> 向外（原始面成为内表面）
SOLIDIFY_OFFSET = {'IN': -1.0, 'CENTER': 0.0, 'OUT': 1.0}


def _pick_icon(*names):
    """不同 Blender 版本的图标名不一样，取第一个存在的"""
    try:
        items = bpy.types.UILayout.bl_rna.functions['label'].parameters['icon'].enum_items
        ok = {i.identifier for i in items}
        for n in names:
            if n in ok:
                return n
    except Exception:
        pass
    return 'NONE'


ICON_GEO = _pick_icon('GEOMETRY_NODES', 'NODETREE', 'MOD_NODES', 'MOD_SOLIDIFY')


def ensure_solidify(context, obj, st):
    """按面板参数添加 / 更新 Solidify 修改器。返回 (修改器, 是否新建)。"""
    mod = None
    for m in obj.modifiers:
        if m.type == 'SOLIDIFY':
            mod = m
            break
    made = False
    if mod is None:
        mod = obj.modifiers.new(name="Solidify", type='SOLIDIFY')
        made = True
    mod.thickness = float(st.thickness)
    mod.offset = SOLIDIFY_OFFSET[st.solidify_offset]
    mod.use_even_offset = bool(st.even_thickness)
    mod.use_rim = True
    mod.use_rim_only = False
    # 不用 Blender 自带的 thickness_clamp：它按「网格边长」钳制，
    # 对细网格会过度压制（实测 0.9 × 边长 0.079 → 厚度被压到 0.071）。
    # 钳制改由 GN 修改器按真实曲率半径做。
    try:
        mod.thickness_clamp = 0.0
    except Exception:
        pass

    if made:
        # 新建的放到栈顶，保证在其它修改器之前生效
        try:
            if hasattr(context, "temp_override"):
                with context.temp_override(object=obj, active_object=obj,
                                           selected_objects=[obj],
                                           selected_editable_objects=[obj]):
                    bpy.ops.object.modifier_move_to_index(modifier=mod.name, index=0)
            else:
                bpy.ops.object.modifier_move_to_index(modifier=mod.name, index=0)
        except Exception:
            pass
    context.view_layer.update()
    return mod, made


class AXISIFY_PG_settings(PropertyGroup):
    axis_mode: EnumProperty(
        name="取轴方式",
        description="自动：每条侧壁各自选最贴近它原始方向的轴；固定：全部统一用一根轴",
        items=[
            ('nearest', "自动（就近轴）", "每条侧壁各自选最贴近它原始方向的 X/Y/Z 轴"),
            ('fixed', "固定轴", "所有侧壁统一投影到指定的轴"),
        ],
        default='nearest',
    )
    fixed_axis: EnumProperty(name="固定轴", items=AXIS_ITEMS, default='+Z')

    snap_tol: FloatProperty(
        name="吸轴容差 (°)",
        description="侧壁原始方向与轴的夹角超过该值就保持原样不搬（0 = 全部搬）",
        default=10.0, min=0.0, max=90.0,
    )
    min_axial: FloatProperty(
        name="最小轴向位移",
        description="投影后的轴向位移小于 原厚度×该比例 就跳过不搬",
        default=0.05, min=0.0, max=1.0,
    )
    cap_angle: FloatProperty(
        name="盖/侧壁分界角 (°)",
        description="二面角小于该值的相邻面视为同一片；用来把盖面和侧壁分开",
        default=45.0, min=1.0, max=89.0,
    )

    thickness: FloatProperty(
        name="厚度",
        description="实体化厚度（对应 Solidify 的 Thickness）；侧壁对齐后墙长就等于它",
        default=0.01, min=0.0, soft_max=10.0,
    )
    thickness_clamp: FloatProperty(
        name="厚度钳制",
        description=("防止厚度超过局部曲率半径时内层翻转自交。\n"
                     "实际厚度会被限制在 局部曲率半径 × 该值 以内。\n"
                     "0 = 关闭钳制（厚度大时会翻转）"),
        default=0.9, min=0.0, max=1.0, subtype='FACTOR',
    )
    merge_verts: FloatProperty(
        name="合并顶点",
        description=("把塌缩到一起的顶点焊成一个点。焊接距离 = 厚度 × 该值。\n"
                     "厚度钳制 = 1.0 时内层刚好塌到圆心，开启本项就得到\n"
                     "干净的「实体化成半径」收口（关闭则会翻折自交）。\n"
                     "0 = 关闭"),
        default=0.0, min=0.0, max=0.5, subtype='FACTOR',
    )
    max_thickness: FloatProperty(
        name="最大厚度 (0=不限)",
        description=("硬上限，完全可预测：不管自动钳制算出什么，实际厚度都不会超过它。\n"
                     "0 = 不限。圆弧半径 R 时填 0.9R 就能保证不翻转自交。"),
        default=0.0, min=0.0, soft_max=10.0,
    )
    solidify_offset: EnumProperty(
        name="方向",
        description="实体化往哪一侧长（对应 Solidify 的 Offset）",
        items=[
            ('IN', "向内（原件在外）", "Offset = -1：原始面成为外表面，新几何向内长"),
            ('CENTER', "居中", "Offset = 0：两侧各长一半"),
            ('OUT', "向外（原件在内）", "Offset = +1：原始面成为内表面，新几何向外长"),
        ],
        default='IN',
    )
    even_thickness: BoolProperty(
        name="均匀厚度",
        description="对应 Solidify 的 Even Thickness；让垂直厚度处处相等，但会改变转角处的布线",
        default=False,
    )
    skip_buried: BoolProperty(
        name="自交保护",
        description="跳过自交/重叠区域内的侧壁。实测开启后残留偏差反而更大，默认关闭",
        default=False,
    )
    last_info: StringProperty(default="")


class AXISIFY_OT_bake(Operator):
    bl_idname = "object.axisify_bake"
    bl_label = "烘焙为新物体"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == 'MESH'

    def execute(self, context):
        st = context.scene.axisify
        ob = context.active_object
        if ob.mode != 'OBJECT':
            try:
                bpy.ops.object.mode_set(mode='OBJECT')
            except Exception as ex:
                self.report({'ERROR'}, "请先切回物体模式（自动切换失败：%s）" % ex)
                return {'CANCELLED'}

        bm = bmesh.new()
        bm.from_mesh(ob.data)
        has_boundary = any(len(e.link_faces) == 1 for e in bm.edges)
        bm.free()
        if not has_boundary:
            self.report({'ERROR'}, "Axisify 需要开放曲面，闭合网格没有可实体化的边界")
            return {'CANCELLED'}

        src = context.active_object
        st = context.scene.axisify
        if src.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        # Apply the remembered panel thickness and direction through a
        # temporary Solidify modifier so the Python baker uses the same input.
        ensure_solidify(context, src, st)
        core.AXIS_MODE = st.axis_mode
        core.AXIS = AXIS_VEC[st.fixed_axis]
        core.SNAP_TOL = float(st.snap_tol)
        core.MIN_AXIAL = float(getattr(st, 'min_axial', 0.05))
        core.CAP_ANGLE = float(getattr(st, 'cap_angle', 45.0))
        core.SKIP_BURIED = bool(getattr(st, 'skip_buried', False))
        core.SAVE_BLEND = ""
        before = set(bpy.data.objects)
        try:
            core.run()
        except Exception as ex:
            self.report({'ERROR'}, str(ex))
            return {'CANCELLED'}
        result = next((o for o in bpy.data.objects if o not in before and o.type == 'MESH'), None)
        if result is None:
            self.report({'WARNING'}, "没有生成结果，请检查模型是否为开放曲面")
            return {'CANCELLED'}
        # Free the source name first so the baked result can keep it.
        old_name = src.name
        original_collections = list(src.users_collection)
        src.name = old_name + "_原模型"
        result.name = old_name
        result.data.name = old_name + "_Mesh"
        # Put the baked result in the same collections as the source object.
        for c in original_collections:
            c.objects.link(result)
        # core.run links the result to the scene collection by default; keep
        # only the source collections to avoid duplicate Outliner entries.
        for c in list(result.users_collection):
            if c not in original_collections:
                c.objects.unlink(result)
        for poly in result.data.polygons:
            poly.use_smooth = True
        archive = bpy.data.collections.get("Axisify 原模型（隐藏）") or bpy.data.collections.new("Axisify 原模型（隐藏）")
        if archive.name not in context.scene.collection.children:
            context.scene.collection.children.link(archive)
        try:
            context.scene.collection.children.move(len(context.scene.collection.children) - 1, 0)
        except (AttributeError, TypeError, RuntimeError):
            pass
        # Move the source exclusively into the hidden archive collection.
        for c in list(src.users_collection):
            c.objects.unlink(src)
        archive.objects.link(src)
        src.hide_set(True)
        src.hide_render = True
        for m in list(src.modifiers):
            if m.type in {'SOLIDIFY', 'NODES'} and (m.type == 'SOLIDIFY' or (getattr(m, 'node_group', None) and m.node_group.name == gn.GROUP_NAME)):
                src.modifiers.remove(m)
        self.report({'INFO'}, "已生成新物体，原模型已归档隐藏")
        return {'FINISHED'}


class AXISIFY_PT_main(Panel):
    bl_label = "Axisify"
    bl_idname = "AXISIFY_PT_main"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Axisify"
    bl_order = 0

    def draw(self, context):
        layout = self.layout
        st = context.scene.axisify
        ob = context.active_object

        if ob is None or ob.type != 'MESH':
            box = layout.box()
            box.label(text="请先选中一个网格物体", icon='ERROR')
        elif ob.mode != 'OBJECT':
            box = layout.box()
            box.label(text="请切回物体模式", icon='ERROR')

        col = layout.column(align=True)
        col.prop(st, "axis_mode")
        if st.axis_mode == 'fixed':
            col.prop(st, "fixed_axis")

        box = layout.box()
        box.label(text="实体化", icon='MOD_SOLIDIFY')
        sub = box.column(align=True)
        sub.prop(st, "thickness")
        sub.prop(st, "solidify_offset")
        layout.label(text="侧壁贴轴")
        col = layout.column(align=True)
        col.prop(st, "snap_tol")
        layout.separator()
        row = layout.row()
        row.scale_y = 1.5
        row.operator(AXISIFY_OT_bake.bl_idname, icon='MESH_GRID')

        if st.last_info:
            box = layout.box()
            box.label(text="上次结果", icon='INFO')
            for seg in st.last_info.split("|"):
                seg = seg.strip()
                if seg:
                    box.label(text=seg)


class AXISIFY_PT_help(Panel):
    bl_label = "说明"
    bl_idname = "AXISIFY_PT_help"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Axisify"
    bl_order = 1
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)
        col.label(text="使用方式：设置参数后点「烘焙为新物体」")
        layout.separator()
        box = layout.box()
        box.label(text="注意", icon='ERROR')
        r = box.column(align=True)
        r.label(text="侧壁对齐到轴后，弯曲处的")
        r.label(text="内层边长会按 (R-h)/R 收缩，")
        r.label(text="这是等距偏移的几何结果。")


CLASSES = (
    AXISIFY_PG_settings,
    AXISIFY_OT_bake,
    AXISIFY_PT_main,
    AXISIFY_PT_help,
)


def register():
    if hasattr(bpy.types.Scene, "axisify"):
        return
    for c in CLASSES:
        bpy.utils.register_class(c)
    bpy.types.Scene.axisify = PointerProperty(type=AXISIFY_PG_settings)


def unregister():
    if hasattr(bpy.types.Scene, "axisify"):
        del bpy.types.Scene.axisify
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)

