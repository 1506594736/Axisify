# -*- coding: utf-8 -*-
"""
实体化侧壁对齐轴 —— 向量投影法（按区域自动取轴 + 自交保护）
================================================================
      Pb' = Pt + ((Pb - Pt) · Â) Â

取轴模式：
  "nearest"  每条墙各自选最贴近它原始方向的 X / Y / Z 轴（横杆+竖柱的模型用这个）
  "fixed"    所有墙统一用 AXIS

自交保护（默认关闭）：
  SKIP_BURIED = True 时，用射线奇偶法找出"外移后仍在实体内部"的面（自交/重叠区域），
  只对这些区域的侧壁面保留原样。
  注意：实测开启后会有 8/34 条墙不对齐（残留最大 8.2°），整体观感反而更差，故默认 False。
  自交本身属于建模问题（中心节点曲面互相插入），本算法不负责解决。
"""

AXIS_MODE = "nearest"
AXIS = (0.0, 0.0, 1.0)
MIN_AXIAL = 0.05
SNAP_TOL = 10.0
CAP_ANGLE = 45.0
SKIP_BURIED = False
SAVE_BLEND = ""

import math
import sys

import bmesh
import bpy
from mathutils import Vector
from mathutils.bvhtree import BVHTree

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

AXES = (Vector((1, 0, 0)), Vector((-1, 0, 0)), Vector((0, 1, 0)),
        Vector((0, -1, 0)), Vector((0, 0, 1)), Vector((0, 0, -1)))
AXNAME = {(1, 0, 0): "+X", (-1, 0, 0): "-X", (0, 1, 0): "+Y",
          (0, -1, 0): "-Y", (0, 0, 1): "+Z", (0, 0, -1): "-Z"}


def build_bmesh(obj):
    if any(m.type == 'SOLIDIFY' for m in obj.modifiers):
        dg = bpy.context.evaluated_depsgraph_get()
        ev = obj.evaluated_get(dg)
        me = ev.to_mesh()
        bm = bmesh.new()
        bm.from_mesh(me)
        ev.to_mesh_clear()
        return bm
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    return bm


def split_caps_rim(bm):
    unvisited = set(bm.faces)
    groups = []
    ca = math.radians(CAP_ANGLE)
    while unvisited:
        seed = unvisited.pop()
        grp = {seed}
        stack = [seed]
        while stack:
            f = stack.pop()
            for e in f.edges:
                for g in e.link_faces:
                    if g in unvisited and f.normal.angle(g.normal) < ca:
                        unvisited.discard(g)
                        grp.add(g)
                        stack.append(g)
        groups.append(grp)
    groups.sort(key=len, reverse=True)
    if len(groups) < 2:
        return None, None, set(), groups
    rest = set()
    for g in groups[2:]:
        rest |= g
    return groups[0], groups[1], rest, groups


def find_walls(bm, capA, capB, rim, M3):
    walls = []
    Minv_t = M3.inverted().transposed()
    for e in bm.edges:
        lf = e.link_faces
        if len(lf) == 2 and lf[0] in rim and lf[1] in rim:
            a, b = e.verts
            n = Vector()
            for f in a.link_faces:
                if f in capA or f in capB:
                    n = n + (Minv_t @ f.normal)
            if n.length < 1e-9:
                continue
            n.normalize()
            walls.append((a, b) if (b.co - a.co).dot(n) < 0 else (b, a))
    return walls


def buried_faces(bm, M, M3):
    """射线奇偶法：面心沿法线外移一点后仍在实体内部 -> 该面在自交/重叠区域。"""
    bvh = BVHTree.FromBMesh(bm)
    Minv_t = M3.inverted().transposed()
    up = Vector((0.0, 0.0, 1.0))
    bad = set()
    for f in bm.faces:
        n = (Minv_t @ f.normal).normalized()
        p = (M @ f.calc_center_median()) + n * 1e-5
        cnt, o = 0, p.copy()
        for _ in range(80):
            hit, hn, idx, dist = bvh.ray_cast(o, up)
            if hit is None:
                break
            cnt += 1
            o = hit + up * 1e-5
            if (o - p).length > 30.0:
                break
        if cnt % 2 == 1:
            bad.add(f)
    return bad


def pick_axis(d):
    best, bi = -1.0, AXES[-1]
    for ax in AXES:
        v = abs(d.dot(ax))
        if v > best:
            best, bi = v, ax
    return bi


def main():
    obj = bpy.context.object
    if not obj or obj.type != 'MESH':
        raise RuntimeError("请先选中一个网格物体（Mesh）")
    M = obj.matrix_world
    M3 = M.to_3x3()
    Minv_t = M3.inverted().transposed()

    bm = build_bmesh(obj)
    bm.normal_update()
    bm.faces.ensure_lookup_table()
    bm.edges.ensure_lookup_table()

    capA, capB, rim, groups = split_caps_rim(bm)
    print("")
    print("[物体] %s  面 %d" % (obj.name, len(bm.faces)))
    print("[分类] 洪泛分组 = %s" % [len(g) for g in groups[:6]])
    if capA is None or not rim:
        print("[中止] 分不出盖面/侧壁")
        bm.free()
        return
    print("[分类] 盖面 %d + %d，侧壁 %d" % (len(capA), len(capB), len(rim)))

    walls = find_walls(bm, capA, capB, rim, M3)
    print("[侧壁] %d 条墙" % len(walls))
    if not walls:
        bm.free()
        return

    # ---- 自交保护：找出重叠/自交区的顶点，这些墙不搬 ----
    guard = set()
    if SKIP_BURIED:
        bad = buried_faces(bm, M, M3)
        bad_rim = [f for f in bad if f in rim]
        for f in bad_rim:
            for v in f.verts:
                guard.add(v)
        print("[自交] 埋面 %d 个（其中侧壁面 %d 个），涉及顶点 %d 个 -> 这些墙保持原样"
              % (len(bad), len(bad_rim), len(guard)))
        ctr = {}
        for f in bad:
            c = M @ f.calc_center_median()
            k = (round(c.x, 1), round(c.y, 1), round(c.z, 0))
            ctr[k] = ctr.get(k, 0) + 1
        for k, v in sorted(ctr.items(), key=lambda kv: -kv[1])[:5]:
            print("       聚集 (%.1f, %.1f, %.0f) : %d 个面" % (k[0], k[1], k[2], v))

    orig = [M3 @ (i.co - o.co) for o, i in walls]
    assign = []
    for d in orig:
        ax = Vector(AXIS).normalized() if AXIS_MODE == "fixed" else pick_axis(d)
        a = math.degrees(d.angle(ax))
        assign.append(ax if min(a, 180 - a) <= SNAP_TOL else None)

    def report(tag, ds):
        rows = []
        for d, ax in zip(ds, assign):
            if d.length < 1e-9 or ax is None:
                continue
            a = math.degrees(d.angle(ax))
            rows.append((min(a, 180 - a), d.length))
        if not rows:
            return
        rows.sort()
        ang = [r[0] for r in rows]
        thk = sorted(r[1] for r in rows)
        n = len(rows)
        print("%s vs 各自所取轴 中位 %6.2f° 最大 %6.2f° | 墙长 中位 %.4f 最薄 %.4f 最厚 %.4f"
              % (tag, ang[n // 2], ang[-1], thk[n // 2], thk[0], thk[-1]))

    print("[取轴] %s" % ("固定 %s" % (tuple(round(c, 2) for c in AXIS),) if AXIS_MODE == "fixed"
                        else "每条墙自动选最贴近的 X/Y/Z"))
    report("[原始]", orig)

    moved, skipped, guarded, over_tol, cnt = 0, 0, 0, 0, {}
    for (o_v, i_v), d, ax_w in zip(walls, orig, assign):
        if ax_w is None:
            over_tol += 1
            continue
        if o_v in guard or i_v in guard:
            guarded += 1
            continue
        ax_l = (M3.inverted() @ ax_w).normalized()
        t = (i_v.co - o_v.co).dot(ax_l)
        if abs(t) < MIN_AXIAL * max(d.length, 1e-9):
            skipped += 1
            continue
        i_v.co = o_v.co + ax_l * t
        moved += 1
        k = (round(ax_w.x), round(ax_w.y), round(ax_w.z))
        cnt[AXNAME.get(k, "?")] = cnt.get(AXNAME.get(k, "?"), 0) + 1

    print("[投影] 搬了 %d 条，跳过 %d 条，保护未动 %d 条，超出吸轴容差 %.0f° 未动 %d 条"
          % (moved, skipped, guarded, SNAP_TOL, over_tol))
    print("[分布] " + "  ".join("%s=%d" % kv for kv in sorted(cnt.items())))
    report("[对齐后]", [M3 @ (i.co - o.co) for o, i in walls])

    bm.normal_update()
    new_me = bpy.data.meshes.new(obj.name + "_axis")
    bm.to_mesh(new_me)
    bm.free()
    no = bpy.data.objects.new(obj.name + "_对齐", new_me)
    no.matrix_world = M
    bpy.context.scene.collection.objects.link(no)
    print("[输出] %s" % no.name)
    if SAVE_BLEND:
        bpy.ops.wm.save_as_mainfile(filepath=SAVE_BLEND)
        print("[存盘] %s" % SAVE_BLEND)
    print("")


def run():
    """插件入口。算法与基准脚本 solidify_axis_align.py 逐行一致，
    只把末尾的自动执行 main() 改成了显式调用。"""
    main()
