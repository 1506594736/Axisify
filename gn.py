# -*- coding: utf-8 -*-
"""Axisify 的 Geometry Nodes 节点组：实体化 + 侧壁贴轴

用节点实现的原因：Blender 不允许 Python 注册自定义修改器类型，
唯一能进修改器面板、可实时调整的途径就是 Geometry Nodes 节点组。

节点组做的事（全部在节点里，不依赖外部脚本）：
  1. 取曲面的逐点法线 n
  2. 把 n 吸附到最近的坐标轴（夹角超过容差就不吸附）-> 方向 A
  3. 位移 D = A * (-厚度 * |n·A|)
  4. 把 D 存成点属性，挤出边界得到侧壁，新顶点按 D 平移
  5. 复制一份曲面按 D 平移并翻转法线 = 内层
  6. 合并 + 按距离焊接
  7. 可选：把塌缩到一起的顶点焊成一个点（实体化成半径时用）
"""

import math

import bpy

GROUP_NAME = "Axisify Solidify Align"
ATTR = "axisify_D"
BLUR_ITER = 0      # R 估计的平滑次数（测下来 >0 会把直边的 R=∞ 混进来，反而让钳制失效）
DEBUG_STORE_R = False   # True 时把算出的 R 存成顶点属性 "axisify_Rout"，便于测出来


def _new(ng, idname, **kw):
    n = ng.nodes.new(idname)
    for k, v in kw.items():
        setattr(n, k, v)
    return n


def _link(ng, out_sock, in_sock):
    return ng.links.new(out_sock, in_sock)


class _B(object):
    """小包装，简化建图"""

    def __init__(self, ng):
        self.ng = ng
        self.y = 0

    def m(self, op, a=None, b=None, av=None, bv=None):
        """Math 标量节点，返回输出 socket"""
        n = _new(self.ng, 'ShaderNodeMath', operation=op,
                 location=(-800, self.y))
        self.y -= 180
        if a is not None:
            _link(self.ng, a, n.inputs[0])
        elif av is not None:
            n.inputs[0].default_value = av
        if b is not None:
            _link(self.ng, b, n.inputs[1])
        elif bv is not None:
            n.inputs[1].default_value = bv
        return n.outputs[0]

    def v(self, op, a=None, b=None, scale=None):
        """Vector Math 节点"""
        n = _new(self.ng, 'ShaderNodeVectorMath', operation=op,
                 location=(-800, self.y))
        self.y -= 180
        if a is not None:
            _link(self.ng, a, n.inputs[0])
        if b is not None:
            _link(self.ng, b, n.inputs[1])
        if scale is not None:
            _link(self.ng, scale, n.inputs[3])
        return n.outputs['Vector']

    def vs(self, op, a, b):
        """需要标量输出的 Vector Math（DOT_PRODUCT / LENGTH 等）"""
        n = _new(self.ng, 'ShaderNodeVectorMath', operation=op,
                 location=(-800, self.y))
        self.y -= 180
        _link(self.ng, a, n.inputs[0])
        if b is not None:
            _link(self.ng, b, n.inputs[1])
        return n.outputs['Value']


def build_group(rebuild=False):
    """创建 / 取回 Axisify 的节点组"""
    if GROUP_NAME in bpy.data.node_groups:
        if not rebuild:
            return bpy.data.node_groups[GROUP_NAME]
        bpy.data.node_groups.remove(bpy.data.node_groups[GROUP_NAME])

    ng = bpy.data.node_groups.new(GROUP_NAME, 'GeometryNodeTree')
    ng.use_fake_user = True

    # ---------- 接口 ----------
    itf = ng.interface
    itf.new_socket("Geometry", in_out='INPUT', socket_type='NodeSocketGeometry')

    s_thk = itf.new_socket("厚度", in_out='INPUT', socket_type='NodeSocketFloat')
    s_thk.default_value = 0.01
    s_thk.min_value = 0.0

    s_snap = itf.new_socket("对齐到轴", in_out='INPUT', socket_type='NodeSocketBool')
    s_snap.default_value = True

    s_tol = itf.new_socket("吸轴角度", in_out='INPUT', socket_type='NodeSocketFloat')
    s_tol.default_value = 10.0
    s_tol.min_value = 0.0
    s_tol.max_value = 90.0

    s_axis = itf.new_socket("固定轴 (0=自动)", in_out='INPUT',
                            socket_type='NodeSocketVector')
    s_axis.default_value = (0.0, 0.0, 0.0)

    s_clamp = itf.new_socket("厚度钳制", in_out='INPUT', socket_type='NodeSocketFloat')
    s_clamp.default_value = 0.9
    s_clamp.min_value = 0.0
    s_clamp.max_value = 1.0

    s_merge = itf.new_socket("合并顶点", in_out='INPUT', socket_type='NodeSocketFloat')
    s_merge.default_value = 0.0
    s_merge.min_value = 0.0
    s_merge.max_value = 0.5

    s_hmax = itf.new_socket("最大厚度 (0=不限)", in_out='INPUT',
                            socket_type='NodeSocketFloat')
    s_hmax.default_value = 0.0
    s_hmax.min_value = 0.0

    itf.new_socket("Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry')

    # ---------- 节点 ----------
    gi = _new(ng, 'NodeGroupInput', location=(-1800, 0))
    go = _new(ng, 'NodeGroupOutput', location=(1200, 0))

    B = _B(ng)
    B.y = 1600

    nrm = _new(ng, 'GeometryNodeInputNormal', location=(-1800, 1000))
    sep = _new(ng, 'ShaderNodeSeparateXYZ', location=(-1800, 800))
    _link(ng, nrm.outputs['Normal'], sep.inputs['Vector'])
    nx, ny, nz = sep.outputs['X'], sep.outputs['Y'], sep.outputs['Z']

    # 绝对值 + 符号
    ax = B.m('ABSOLUTE', a=nx)
    ay = B.m('ABSOLUTE', a=ny)
    az = B.m('ABSOLUTE', a=nz)
    sx = B.m('SIGN', a=nx)
    sy = B.m('SIGN', a=ny)
    sz = B.m('SIGN', a=nz)

    # 选最大分量：m1 = max(ax, ay)
    # 注意 Math 的 GREATER_THAN 输出 1/0
    e1 = B.m('GREATER_THAN', a=ax, b=ay)
    m1 = B.m('MAXIMUM', a=ax, b=ay)
    ne1 = B.m('SUBTRACT', b=e1, av=1.0)          # 1 - e1

    v1x = B.m('MULTIPLY', a=e1, b=sx)
    v1y = B.m('MULTIPLY', a=ne1, b=sy)

    e2 = B.m('GREATER_THAN', a=m1, b=az)
    ne2 = B.m('SUBTRACT', b=e2, av=1.0)

    araw_x = B.m('MULTIPLY', a=e2, b=v1x)
    araw_y = B.m('MULTIPLY', a=e2, b=v1y)
    araw_z = B.m('MULTIPLY', a=ne2, b=sz)

    comb = _new(ng, 'ShaderNodeCombineXYZ', location=(-400, 1600))
    _link(ng, araw_x, comb.inputs['X'])
    _link(ng, araw_y, comb.inputs['Y'])
    _link(ng, araw_z, comb.inputs['Z'])
    araw = comb.outputs['Vector']

    # dot = n · A_raw   （= 最大分量的绝对值，恒 >= 0）
    dot = B.vs('DOT_PRODUCT', nrm.outputs['Normal'], araw)

    # cos(容差)
    rad = B.m('RADIANS', a=gi.outputs["吸轴角度"])
    ctol = B.m('COSINE', a=rad)
    ge = B.m('GREATER_THAN', a=dot, b=ctol, av=None)
    # 与「对齐到轴」开关相乘（bool -> float 走隐式转换）
    f = B.m('MULTIPLY', a=ge, b=gi.outputs["对齐到轴"])

    # 三叉/多叉交汇点不能共用同一个轴向投影点：不同弧段在此处
    # 需要各自的切线交点。对连接边超过两条的顶点关闭轴向投影，
    # 保留法线偏移，避免交汇区域塌陷和自相交。
    vn = _new(ng, 'GeometryNodeInputMeshVertexNeighbors', location=(-1800, 650))
    vcmp = _new(ng, 'FunctionNodeCompare', data_type='INT',
                operation='GREATER_THAN', location=(-1500, 650))
    vcmp.inputs['B'].default_value = 2
    _link(ng, vn.outputs['Vertex Count'], vcmp.inputs['A'])
    vkeep = B.m('SUBTRACT', b=vcmp.outputs['Result'], av=1.0)
    f = B.m('MULTIPLY', a=f, b=vkeep)

    # A1 = n + (A_raw - n) * f
    dif = B.v('SUBTRACT', a=araw, b=nrm.outputs['Normal'])
    difs = B.v('SCALE', a=dif, scale=f)
    a1 = B.v('ADD', a=nrm.outputs['Normal'], b=difs)

    # 固定轴模式
    fal = B.vs('LENGTH', gi.outputs["固定轴 (0=自动)"], None)
    fan = B.v('NORMALIZE', a=gi.outputs["固定轴 (0=自动)"])
    # Fixed-axis mode follows the same snap tolerance as automatic mode.
    # A zero vector keeps automatic selection enabled.
    fdot = B.vs('DOT_PRODUCT', nrm.outputs['Normal'], fan)
    fabs = B.m('ABSOLUTE', a=fdot)
    fge = B.m('GREATER_THAN', a=fabs, b=ctol)
    has_axis = B.m('GREATER_THAN', a=fal, bv=1e-4)
    g = B.m('MULTIPLY', a=fge, b=has_axis)
    # Fixed-axis mode: within tolerance use the requested axis; outside it
    # preserve the original normal (do not silently switch to another axis).
    dif2 = B.v('SUBTRACT', a=fan, b=nrm.outputs['Normal'])
    fixed_a = B.v('ADD', a=nrm.outputs['Normal'],
                  b=B.v('SCALE', a=dif2, scale=fge))
    dif2b = B.v('SUBTRACT', a=fixed_a, b=a1)
    dif2s = B.v('SCALE', a=dif2b, scale=has_axis)
    A = B.v('ADD', a=a1, b=dif2s)

    # ---------- 厚度钳制：按局部曲率半径限制位移 ----------
    # 厚度一旦超过局部曲率半径 R，等距偏移就会穿过圆心翻转（自交）。
    # 每条边算一个等效半径 Re = 边长 / |两端点法线之差|（直边→分母0，Re→∞）。
    # 必须用【原始顶点法线】，不能用吸附后的 A —— A 会被 SNAP_TOL 打断。
    # 「厚度钳制」= 0 关闭；「最大厚度」是另一个不依赖估计的硬上限。
    ev = _new(ng, 'GeometryNodeInputMeshEdgeVertices', location=(-2200, -700))
    fa1 = _new(ng, 'GeometryNodeFieldAtIndex', data_type='FLOAT_VECTOR',
               domain='EDGE', location=(-1900, -700))
    _link(ng, nrm.outputs['Normal'], fa1.inputs['Value'])
    _link(ng, ev.outputs['Vertex Index 1'], fa1.inputs['Index'])
    fa2 = _new(ng, 'GeometryNodeFieldAtIndex', data_type='FLOAT_VECTOR',
               domain='EDGE', location=(-1900, -950))
    _link(ng, nrm.outputs['Normal'], fa2.inputs['Value'])
    _link(ng, ev.outputs['Vertex Index 2'], fa2.inputs['Index'])

    dpos = B.v('SUBTRACT', a=ev.outputs['Position 2'], b=ev.outputs['Position 1'])
    elen = B.vs('LENGTH', dpos, None)
    dA = B.v('SUBTRACT', a=fa2.outputs['Value'], b=fa1.outputs['Value'])
    dAl = B.vs('LENGTH', dA, None)
    den = B.m('MAXIMUM', a=dAl, bv=1e-4)
    Re = B.m('DIVIDE', a=elen, b=den)

    # 存成 EDGE 属性 → 转点云 → 顶点取最近边的 R
    st2 = _new(ng, 'GeometryNodeStoreNamedAttribute', data_type='FLOAT',
               domain='EDGE', location=(-1000, -700))
    st2.inputs['Name'].default_value = "axisify_R"
    _link(ng, gi.outputs['Geometry'], st2.inputs['Geometry'])
    _link(ng, Re, st2.inputs['Value'])

    m2p = _new(ng, 'GeometryNodeMeshToPoints', mode='EDGES', location=(-700, -700))
    _link(ng, st2.outputs['Geometry'], m2p.inputs['Mesh'])

    ppos = _new(ng, 'GeometryNodeInputPosition', location=(-1000, -1100))
    snn = _new(ng, 'GeometryNodeSampleNearest', location=(-400, -700))
    _link(ng, m2p.outputs['Points'], snn.inputs['Geometry'])
    _link(ng, ppos.outputs['Position'], snn.inputs['Sample Position'])

    naR = _new(ng, 'GeometryNodeInputNamedAttribute', data_type='FLOAT',
               location=(-700, -1000))
    naR.inputs['Name'].default_value = "axisify_R"
    sidx = _new(ng, 'GeometryNodeSampleIndex', data_type='FLOAT',
                domain='POINT', location=(-150, -700))
    _link(ng, m2p.outputs['Points'], sidx.inputs['Geometry'])
    _link(ng, naR.outputs['Attribute'], sidx.inputs['Value'])
    _link(ng, snn.outputs['Index'], sidx.inputs['Index'])

    # 平滑 R：逐顶点的曲率估计噪声很大（实测极差 150%），
    # 不平滑的话每个顶点会落在它「自己」的曲率中心，塌缩不成一个点。
    blr = _new(ng, 'GeometryNodeBlurAttribute', data_type='FLOAT',
               location=(150, -700))
    blr.inputs['Iterations'].default_value = BLUR_ITER
    _link(ng, sidx.outputs['Value'], blr.inputs['Value'])

    # 有效厚度 = 关闭钳制时=厚度；开启时 = min(厚度, 钳制强度 × R)
    cOn = B.m('GREATER_THAN', a=gi.outputs["厚度钳制"], bv=1e-4)
    cMax = B.m('MULTIPLY', a=gi.outputs["厚度钳制"], b=blr.outputs['Value'])
    cLim = B.m('MINIMUM', a=gi.outputs["厚度"], b=cMax)
    cOff = B.m('SUBTRACT', b=cOn, av=1.0)
    hA = B.m('MULTIPLY', a=gi.outputs["厚度"], b=cOff)
    hB = B.m('MULTIPLY', a=cLim, b=cOn)
    hE = B.m('ADD', a=hA, b=hB)

    # 手动上限（完全可预测的兜底）：0 = 不限
    mxOn = B.m('GREATER_THAN', a=gi.outputs["最大厚度 (0=不限)"], bv=1e-6)
    mxA = B.m('MULTIPLY', a=hE, b=B.m('SUBTRACT', b=mxOn, av=1.0))
    mxB = B.m('MULTIPLY', a=B.m('MINIMUM', a=hE,
                                b=gi.outputs["最大厚度 (0=不限)"]), b=mxOn)
    hF = B.m('ADD', a=mxA, b=mxB)

    # D = A * (-有效厚度 * |n·A|)
    t2 = B.vs('DOT_PRODUCT', nrm.outputs['Normal'], A)
    k = B.m('MULTIPLY', a=hF, b=t2)
    kneg = B.m('MULTIPLY', a=k, bv=-1.0)
    D = B.v('SCALE', a=A, scale=kneg)

    # 存成点属性（挤出时新顶点会继承）
    st = _new(ng, 'GeometryNodeStoreNamedAttribute',
              data_type='FLOAT_VECTOR', domain='POINT', location=(-400, 900))
    st.inputs['Name'].default_value = ATTR
    _link(ng, gi.outputs['Geometry'], st.inputs['Geometry'])
    _link(ng, D, st.inputs['Value'])
    src_geo = st.outputs['Geometry']

    if DEBUG_STORE_R:
        dbg = _new(ng, 'GeometryNodeStoreNamedAttribute', data_type='FLOAT',
                   domain='POINT', location=(-200, 1100))
        dbg.inputs['Name'].default_value = "axisify_Rout"
        _link(ng, src_geo, dbg.inputs['Geometry'])
        _link(ng, blr.outputs['Value'], dbg.inputs['Value'])
        src_geo = dbg.outputs['Geometry']

        dbg2 = _new(ng, 'GeometryNodeStoreNamedAttribute', data_type='FLOAT',
                    domain='POINT', location=(-200, 1250))
        dbg2.inputs['Name'].default_value = "axisify_Hout"
        _link(ng, src_geo, dbg2.inputs['Geometry'])
        _link(ng, hE, dbg2.inputs['Value'])
        src_geo = dbg2.outputs['Geometry']

    na = _new(ng, 'GeometryNodeInputNamedAttribute',
              data_type='FLOAT_VECTOR', location=(-400, 700))
    na.inputs['Name'].default_value = ATTR

    # 边界边：Face Count == 1
    en = _new(ng, 'GeometryNodeInputMeshEdgeNeighbors', location=(-1800, 400))
    cpm = _new(ng, 'FunctionNodeCompare', data_type='INT',
               operation='EQUAL', location=(-1400, 400))
    cpm.inputs['B'].default_value = 1
    _link(ng, en.outputs['Face Count'], cpm.inputs['A'])

    # 挤出边界 -> 侧壁（mode 必须是 EDGES；不同模式下输入槽名不一样，按名找）
    ext = _new(ng, 'GeometryNodeExtrudeMesh', mode='EDGES', location=(0, 900))
    for s in ext.inputs:
        if s.name == 'Individual':
            s.default_value = False
        elif s.name == 'Offset Scale':
            s.default_value = 0.0
    _link(ng, src_geo, ext.inputs['Mesh'])
    _link(ng, cpm.outputs['Result'], ext.inputs['Selection'])

    # 侧壁的新顶点（Top）按 D 平移
    spt = _new(ng, 'GeometryNodeSetPosition', location=(400, 1000))
    _link(ng, ext.outputs['Mesh'], spt.inputs['Geometry'])
    _link(ng, ext.outputs['Top'], spt.inputs['Selection'])
    _link(ng, na.outputs['Attribute'], spt.inputs['Offset'])

    # 内层 = 曲面整体平移 D，翻转法线
    spb = _new(ng, 'GeometryNodeSetPosition', location=(0, 500))
    _link(ng, src_geo, spb.inputs['Geometry'])
    _link(ng, na.outputs['Attribute'], spb.inputs['Offset'])

    flip = _new(ng, 'GeometryNodeFlipFaces', location=(400, 400))
    _link(ng, spb.outputs['Geometry'], flip.inputs['Mesh'])

    # 合并
    jn = _new(ng, 'GeometryNodeJoinGeometry', location=(700, 700))
    _link(ng, spt.outputs['Geometry'], jn.inputs['Geometry'])
    _link(ng, flip.outputs['Mesh'], jn.inputs['Geometry'])

    mg = _new(ng, 'GeometryNodeMergeByDistance', location=(900, 700))
    mg.inputs['Distance'].default_value = 1e-5
    _link(ng, jn.outputs['Geometry'], mg.inputs['Geometry'])

    # 第二次焊接：距离 = max(合并顶点 × 厚度, 1e-5)。
    # 厚度钳制到 1.0 时内层会塌到圆心，这里把它们焊成一个点，
    # 得到干净的「实体化成半径」收口（而不是翻折自交）。
    mg2 = _new(ng, 'GeometryNodeMergeByDistance', location=(1100, 700))
    _link(ng, mg.outputs['Geometry'], mg2.inputs['Geometry'])
    mdst = B.m('MULTIPLY', a=gi.outputs["合并顶点"], b=gi.outputs["厚度"])
    mdst2 = B.m('MAXIMUM', a=mdst, bv=1e-5)
    _link(ng, mdst2, mg2.inputs['Distance'])
    _link(ng, mg2.outputs['Geometry'], go.inputs['Geometry'])

    ng.nodes.remove  # noop, 保持可读
    return ng


def ensure_modifier(obj, rebuild_group=False):
    """给物体加 / 取回 Axisify 的 Geometry Nodes 修改器"""
    ng = build_group(rebuild=rebuild_group)
    mod = None
    for m in obj.modifiers:
        if m.type == 'NODES' and m.node_group is ng:
            mod = m
            break
    if mod is None:
        mod = obj.modifiers.new(name="Axisify", type='NODES')
        mod.node_group = ng
    return mod
