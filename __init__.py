# -*- coding: utf-8 -*-
"""Axisify —— 实体化侧壁沿轴对齐

把实体化（Solidify）生成的侧壁精确投影到 X / Y / Z 轴，使其与所选轴严格平行。
"""

bl_info = {
    "name": "Axisify",
    "author": "HULIMIAO",
    "version": (1, 0, 0),
    "blender": (3, 0, 0),
    "location": "3D 视图 > N 面板 > Axisify",
    "description": "实体化后把侧壁精确对齐到 X/Y/Z 轴",
    "doc_url": "https://github.com/1506594736/axisify",
    "tracker_url": "https://github.com/1506594736/axisify/issues",
    "category": "Mesh",
}

import contextlib
import io

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, PointerProperty, StringProperty
from bpy.types import Operator, Panel, PropertyGroup

from . import core

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


class AXISIFY_OT_align(Operator):
    bl_idname = "object.axisify_align"
    bl_label = "对齐侧壁到轴"
    bl_description = "把实体化生成的侧壁投影到 X/Y/Z 轴，使其与所选轴严格平行"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        ob = context.active_object
        return ob is not None and ob.type == 'MESH'

    def execute(self, context):
        st = context.scene.axisify

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

        parts = [p for p in (nwall, align) if p]
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
        col.label(text="1. 给物体加 Solidify，厚度调好")
        col.label(text="2. 应用或保留都行")
        col.label(text="3. 选中物体，点上面的按钮")
        layout.separator()
        col = layout.column(align=True)
        col.label(text="结果会生成一个新物体（原名_对齐）")
        col.label(text="原理：把侧壁方向投影到最近的轴")
        col.label(text="      Pb' = Pt + ((Pb-Pt)·Â)Â")
        layout.separator()
        box = layout.box()
        box.label(text="注意", icon='ERROR')
        r = box.column(align=True)
        r.label(text="侧壁对齐到轴后，弯曲处的")
        r.label(text="内层边长会按 (R-h)/R 收缩，")
        r.label(text="这是等距偏移的几何结果。")


CLASSES = (
    AXISIFY_PG_settings,
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
