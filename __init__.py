# -*- coding: utf-8 -*-
"""Axisify —— 实体化侧壁沿轴对齐

把实体化（Solidify）生成的侧壁精确投影到 X / Y / Z 轴，使其与所选轴严格平行。
"""

bl_info = {
    "name": "Axisify",
    "author": "HULIMIAO",
    "version": (1, 2, 0),
    "blender": (3, 0, 0),
    "location": "3D 视图 > N 面板 > Axisify",
    "description": "实体化并让侧壁精确对齐到 X/Y/Z 轴，可作为可调修改器",
    "doc_url": "https://github.com/1506594736/Axisify",
    "tracker_url": "https://github.com/1506594736/Axisify/issues",
    "category": "Mesh",
}

import contextlib
import io

import bpy
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
OFFSET_CN = {'IN': "向内", 'CENTER': "居中", 'OUT': "向外"}


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


def move_modifier_to_top(context, obj, mod):
    """把修改器移到栈顶（需要合法的 active object 上下文）"""
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


def set_group_defaults(ng, st):
    """把面板参数写成节点组的接口默认值（新加的修改器会继承它们）"""
    vals = {
        "厚度": float(st.thickness),
        "对齐到轴": True,
        "吸轴角度": float(st.snap_tol),
        "固定轴 (0=自动)": (AXIS_VEC[st.fixed_axis] if st.axis_mode == 'fixed'
                            else (0.0, 0.0, 0.0)),
    }
    for it in ng.interface.items_tree:
        if it.in_out == 'INPUT' and it.name in vals:
            it.default_value = vals[it.name]


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

    auto_solidify: BoolProperty(
        name="自动设置实体化",
        description="点按钮时自动给物体添加 / 更新 Solidify 修改器，用下面的厚度和方向。取消勾选则沿用物体自己的修改器",
        default=True,
    )
    thickness: FloatProperty(
        name="厚度",
        description="实体化厚度（对应 Solidify 的 Thickness）；侧壁对齐后墙长就等于它",
        default=0.1, min=0.0, soft_max=10.0,
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
    replace_solidify: BoolProperty(
        name="移除旧的实体化修改器",
        description="「作为修改器」时先删掉物体上已有的 Solidify 修改器 —— Axisify 修改器自己就会实体化，留着会在场景里多出一个模型",
        default=True,
    )
    bake_clean_solidify: BoolProperty(
        name="烘焙后移除实体化修改器",
        description="烘焙生成新物体后，删掉源物体上由插件添加的 Solidify 修改器，避免出现两个看起来一样的模型",
        default=True,
    )
    skip_buried: BoolProperty(
        name="自交保护",
        description="跳过自交/重叠区域内的侧壁。实测开启后残留偏差反而更大，默认关闭",
        default=False,
    )
    select_result: BoolProperty(
        name="选中结果物体",
        description="完成后选中新生成的对齐结果",
        default=True,
    )
    last_info: StringProperty(default="")


class AXISIFY_OT_read_solidify(Operator):
    bl_idname = "object.axisify_read_solidify"
    bl_label = "从修改器读取"
    bl_description = "把物体上 Solidify 修改器的厚度 / 方向 / 均匀厚度读进面板"

    @classmethod
    def poll(cls, context):
        ob = context.active_object
        return (ob is not None and ob.type == 'MESH'
                and any(m.type == 'SOLIDIFY' for m in ob.modifiers))

    def execute(self, context):
        ob = context.active_object
        st = context.scene.axisify
        mod = next(m for m in ob.modifiers if m.type == 'SOLIDIFY')
        st.thickness = float(mod.thickness)
        st.even_thickness = bool(mod.use_even_offset)
        st.solidify_offset = ('IN' if mod.offset < -0.33
                              else ('OUT' if mod.offset > 0.33 else 'CENTER'))
        self.report({'INFO'}, "已读取 %s 的实体化参数" % ob.name)
        return {'FINISHED'}


class AXISIFY_OT_use_modifier(Operator):
    bl_idname = "object.axisify_use_modifier"
    bl_label = "作为修改器（可实时调整）"
    bl_description = ("给物体加一个 Geometry Nodes 修改器，在里面同时完成实体化和侧壁贴轴。\n"
                      "参数可在修改器面板实时调整，不生成新物体")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        ob = context.active_object
        return ob is not None and ob.type == 'MESH'

    def execute(self, context):
        st = context.scene.axisify
        ob = context.active_object
        if ob.mode != 'OBJECT':
            try:
                bpy.ops.object.mode_set(mode='OBJECT')
            except Exception as ex:
                self.report({'ERROR'}, "请先切回物体模式（自动切换失败：%s）" % ex)
                return {'CANCELLED'}

        if st.replace_solidify:
            for m in [m for m in ob.modifiers if m.type == 'SOLIDIFY']:
                ob.modifiers.remove(m)

        ng = gn.build_group()
        # 旧的重建一下，让它继承最新的接口默认值
        for m in [m for m in ob.modifiers
                  if m.type == 'NODES' and m.node_group is ng]:
            ob.modifiers.remove(m)
        set_group_defaults(ng, st)
        mod = gn.ensure_modifier(ob)
        move_modifier_to_top(context, ob, mod)
        context.view_layer.update()
        self.report({'INFO'}, "已给 %s 添加 Axisify 修改器（在修改器面板里调参数）" % ob.name)
        return {'FINISHED'}


class AXISIFY_OT_align(Operator):
    bl_idname = "object.axisify_align"
    bl_label = "对齐侧壁到轴（烘焙成新物体）"
    bl_description = "把实体化生成的侧壁投影到 X/Y/Z 轴，使其与所选轴严格平行"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        ob = context.active_object
        return ob is not None and ob.type == 'MESH'

    def execute(self, context):
        st = context.scene.axisify
        src = context.active_object
        if src.mode != 'OBJECT':
            try:
                bpy.ops.object.mode_set(mode='OBJECT')
            except Exception as ex:
                self.report({'ERROR'}, "请先切回物体模式（自动切换失败：%s）" % ex)
                return {'CANCELLED'}

        smod = ""
        has_gn = any(m.type == 'NODES'
                     and getattr(m.node_group, "name", "") == gn.GROUP_NAME
                     for m in src.modifiers)
        if has_gn:
            smod = "已有 Axisify 修改器（不再叠加 Solidify）"
        elif st.auto_solidify:
            _m, made = ensure_solidify(context, src, st)
            smod = "%sSolidify %.4g %s" % ("新建" if made else "更新",
                                         st.thickness,
                                         OFFSET_CN[st.solidify_offset])

        # 把面板参数写进算法模块的同名全局变量（算法本身一行未改）
        core.AXIS_MODE = st.axis_mode
        core.AXIS = AXIS_VEC[st.fixed_axis]
        core.SNAP_TOL = float(st.snap_tol)
        core.MIN_AXIAL = float(st.min_axial)
        core.CAP_ANGLE = float(st.cap_angle)
        core.SKIP_BURIED = bool(st.skip_buried)
        core.SAVE_BLEND = ""

        before = set(bpy.data.objects)
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                core.run()
        except Exception as ex:
            self.report({'ERROR'}, "失败：%s" % ex)
            return {'CANCELLED'}

        log = buf.getvalue()
        for line in log.splitlines():
            print(line)

        new = [o for o in bpy.data.objects if o not in before and o.type == 'MESH']
        if not new:
            st.last_info = "没有生成结果：分不出盖面 / 侧壁"
            self.report({'WARNING'}, "没有生成结果，请检查模型是否有明确的盖面与侧壁")
            return {'CANCELLED'}

        out = new[-1]
        if st.auto_solidify and st.bake_clean_solidify:
            for m in [m for m in src.modifiers if m.type == 'SOLIDIFY']:
                src.modifiers.remove(m)
        if st.select_result:
            for o in context.view_layer.objects:
                o.select_set(False)
            out.select_set(True)
            context.view_layer.objects.active = out

        nwall, align = "", ""
        for line in log.splitlines():
            line = line.strip()
            if line.startswith("[侧壁]"):
                nwall = line[len("[侧壁]"):].strip()
            elif line.startswith("[对齐后]"):
                align = (line[len("[对齐后]"):]
                         .replace("vs 各自所取轴", "对轴偏差")
                         .strip())

        parts = [p for p in (smod, nwall, align) if p]
        st.last_info = " | ".join(parts) if parts else "完成"
        self.report({'INFO'}, "已生成 %s" % out.name)
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
        box.prop(st, "auto_solidify")
        sub = box.column(align=True)
        sub.enabled = st.auto_solidify
        sub.prop(st, "thickness")
        sub.prop(st, "solidify_offset")
        sub.prop(st, "even_thickness")
        row = box.row()
        row.enabled = AXISIFY_OT_read_solidify.poll(context)
        row.operator(AXISIFY_OT_read_solidify.bl_idname, icon='IMPORT')

        layout.label(text="侧壁贴轴")
        col = layout.column(align=True)
        col.prop(st, "snap_tol")
        col.prop(st, "min_axial")
        col.prop(st, "cap_angle")

        col = layout.column(align=True)
        col.prop(st, "skip_buried")
        col.prop(st, "select_result")

        layout.separator()
        row = layout.row()
        row.scale_y = 1.5
        row.operator(AXISIFY_OT_use_modifier.bl_idname, icon=ICON_GEO)
        col = layout.column(align=True)
        col.prop(st, "replace_solidify")
        col.prop(st, "bake_clean_solidify")
        layout.separator()
        row = layout.row()
        row.operator(AXISIFY_OT_align.bl_idname, icon='MESH_GRID')

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
        col.label(text="方式一（推荐）：点「作为修改器」")
        col.label(text="  修改器面板里可实时调厚度、")
        col.label(text="  吸轴角度、固定轴，不生成新物体")
        layout.separator()
        col = layout.column(align=True)
        col.label(text="方式二：点「烘焙成新物体」")
        col.label(text="  结果是一个独立的静态网格")
        layout.separator()
        box = layout.box()
        box.label(text="注意", icon='ERROR')
        r = box.column(align=True)
        r.label(text="侧壁对齐到轴后，弯曲处的")
        r.label(text="内层边长会按 (R-h)/R 收缩，")
        r.label(text="这是等距偏移的几何结果。")


CLASSES = (
    AXISIFY_PG_settings,
    AXISIFY_OT_read_solidify,
    AXISIFY_OT_use_modifier,
    AXISIFY_OT_align,
    AXISIFY_PT_main,
    AXISIFY_PT_help,
)


def register():
    for c in CLASSES:
        bpy.utils.register_class(c)
    bpy.types.Scene.axisify = PointerProperty(type=AXISIFY_PG_settings)


def unregister():
    if hasattr(bpy.types.Scene, "axisify"):
        del bpy.types.Scene.axisify
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)
