"""Modelling helpers that run inside Blender, for MARK LII's builder.

Generated build scripts call these instead of touching bpy directly. That is
the whole point of the module: it pins down the parts of the API that move
between Blender releases (Blender 5.x renamed the Principled BSDF's "Emission"
to "Emission Color" and "Specular" to "Specular IOR Level", so code written
against older docs sets nothing and fails silently), and it bakes in the
choices that separate a model that looks built from one that looks like a pile
of default primitives: shading, bevels, studio lighting, a framed camera.

Everything here returns real objects, so a script can keep working on what it
made. Lengths are Blender units (1 unit = 1 metre) and angles are degrees --
degrees because every generated script that took radians got them wrong.
"""
from __future__ import annotations

import math

import bpy
from mathutils import Vector

__all__ = [
    "reset_scene", "material", "cylinder", "cone", "sphere", "icosphere",
    "cube", "torus", "tube", "curve_tube", "helix", "text3d",
    "revolve", "loft", "mesh_from",
    "bevel", "smooth", "shade_flat", "subdivide", "solidify", "mirror",
    "radial_array", "linear_array", "boolean", "join", "move", "rotate",
    "scale_obj", "duplicate", "set_origin", "add", "taper",
    "look_at", "frame_camera", "frame_viewport", "studio_lights", "ground",
    "bounds", "select_only", "deg",
]


def deg(d: float) -> float:
    """Degrees to radians, for the rare call that needs raw bpy."""
    return math.radians(d)


def _rot(rotation):
    return tuple(math.radians(a) for a in (rotation or (0, 0, 0)))


def select_only(objs) -> None:
    """Make `objs` the selection, with the first one active."""
    if isinstance(objs, bpy.types.Object):
        objs = [objs]
    objs = [o for o in objs if o is not None]
    for ob in bpy.context.view_layer.objects:
        ob.select_set(False)
    for ob in objs:
        ob.select_set(True)
    if objs:
        bpy.context.view_layer.objects.active = objs[0]


def _added(operator, **kwargs):
    """Run an `_add` operator and return the object it made.

    Not bpy.context.object, which is how every Blender example does it:
    starting a new file with read_homefile strips `object` and `active_object`
    off the context for the remainder of that execution, so a script that
    resets the scene and then builds — which is every build script — would die
    on its first primitive. Diffing bpy.data.objects needs no context at all.
    """
    before = set(bpy.data.objects)
    operator(**kwargs)
    made = [ob for ob in bpy.data.objects if ob not in before]
    if not made:
        raise RuntimeError(f"{operator} created no object")
    ob = made[-1]
    try:
        bpy.context.view_layer.objects.active = ob
    except Exception:
        pass
    return ob


def _op_context(objs):
    """A context override for object-mode operators.

    join() and friends poll against context.active_object, which is exactly
    what a fresh file has stripped away. Handing them the objects explicitly
    is the only thing that works in that window.
    """
    if isinstance(objs, bpy.types.Object):
        objs = [objs]
    objs = [o for o in objs if o is not None]
    return bpy.context.temp_override(
        active_object=objs[0] if objs else None,
        object=objs[0] if objs else None,
        selected_objects=objs,
        selected_editable_objects=objs,
    )


# ── scene ───────────────────────────────────────────────────────────────────

def reset_scene(world_colour=(0.05, 0.05, 0.06), lights: bool = True) -> None:
    """Empty the file and lay in a world and studio lighting.

    Cleared by hand rather than with read_homefile, and that matters more than
    it looks: read_homefile strips `object` and `active_object` off the context
    for the rest of the execution, so any script that reset the scene and then
    used an ordinary `bpy.context.object` — the pattern in every Blender
    tutorial — died on its first primitive. Wiping the datablocks leaves the
    context intact, so raw bpy works normally alongside this API.
    """
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    for collection in (bpy.data.meshes, bpy.data.curves, bpy.data.materials,
                       bpy.data.lights, bpy.data.cameras, bpy.data.images,
                       bpy.data.node_groups, bpy.data.worlds):
        for item in list(collection):
            if item.users == 0:
                try:
                    collection.remove(item)
                except Exception:
                    pass

    scene = bpy.context.scene
    scene.unit_settings.system = "METRIC"

    # Establish the render aspect up front. frame_camera fits the model to
    # whatever aspect the scene has at the moment it runs, so leaving this to
    # be set later would frame for one shape and render another.
    scene.render.resolution_x = 1600
    scene.render.resolution_y = 1200
    scene.render.resolution_percentage = 100
    try:
        scene.render.engine = "BLENDER_EEVEE"
    except TypeError:
        pass          # a build with a different engine identifier; keep default

    world = bpy.data.worlds.new("JarvisWorld")
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if bg:
        bg.inputs["Color"].default_value = (*world_colour, 1.0)
        bg.inputs["Strength"].default_value = 1.0
    scene.world = world

    # AgX is Blender's default and it is a good film response, but it rolls
    # saturated colour hard toward pastel: a blood-red (0.35, 0.06, 0.07)
    # renders pale pink. These previews are the evidence the refine pass
    # judges the build on, so colour fidelity matters more here than filmic
    # highlight handling.
    try:
        scene.view_settings.view_transform = "Standard"
    except Exception:
        pass

    if lights:
        studio_lights()


def studio_lights(strength: float = 1.0) -> list:
    """Three-point lighting: a bright key, a soft fill, a rim from behind.

    Flat, single-light scenes are what make an automatic build look cheap;
    this is the smallest setup that gives form to a shape.
    """
    # Idempotent on purpose. A generated script that calls this after
    # reset_scene() has already lit the scene would otherwise stack a second
    # rig on the first: double exposure, blown highlights, and washed-out
    # colour that reads as "no detail" no matter how good the model is.
    for old_light in [o for o in bpy.data.objects if o.type == "LIGHT"]:
        bpy.data.objects.remove(old_light, do_unlink=True)

    made = []
    spec = (
        ("Key",  "AREA", 900.0 * strength, (4.0, -5.0, 5.5), 6.0),
        ("Fill", "AREA", 250.0 * strength, (-5.5, -3.0, 2.5), 8.0),
        ("Rim",  "AREA", 600.0 * strength, (0.0, 6.0, 4.0), 5.0),
    )
    for name, kind, energy, loc, size in spec:
        data = bpy.data.lights.new(name=name, type=kind)
        data.energy = energy
        try:
            data.size = size
        except AttributeError:
            pass
        ob = bpy.data.objects.new(name, data)
        bpy.context.collection.objects.link(ob)
        ob.location = loc
        look_at(ob, (0, 0, 1))
        made.append(ob)
    return made


def ground(size: float = 40.0, colour=(0.08, 0.08, 0.09), roughness: float = 0.8):
    """A floor plane. Gives the model somewhere to sit and something to catch
    its shadow, which reads as depth."""
    ob = _added(bpy.ops.mesh.primitive_plane_add, size=size, location=(0, 0, 0))
    ob.name = "Ground"
    ob.data.materials.append(material("Ground", colour, roughness=roughness))
    return ob


# ── materials ───────────────────────────────────────────────────────────────

def material(
    name: str,
    colour=(0.8, 0.8, 0.8),
    metallic: float = 0.0,
    roughness: float = 0.5,
    emission=None,
    emission_strength: float = 1.0,
    alpha: float = 1.0,
):
    """A Principled BSDF material.

    `colour` and `emission` are (r, g, b) in 0..1. The input names below are
    the Blender 4.x/5.x spellings; older names silently do nothing, which is
    why nothing outside this function should be setting them.
    """
    mat = bpy.data.materials.new(name=name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf is None:
        return mat

    def put(socket: str, value):
        if socket in bsdf.inputs:
            bsdf.inputs[socket].default_value = value

    put("Base Color", (*colour, 1.0))
    put("Metallic", float(metallic))
    put("Roughness", float(roughness))
    put("Alpha", float(alpha))
    if emission is not None:
        put("Emission Color", (*emission, 1.0))
        put("Emission Strength", float(emission_strength))
    if alpha < 1.0:
        mat.blend_method = "BLEND"
    return mat


def _finish(ob, name, mat, smooth_it, bevel_amt):
    if name:
        ob.name = name
    if mat is not None:
        ob.data.materials.append(mat)
    if bevel_amt:
        bevel(ob, bevel_amt)
    if smooth_it:
        smooth(ob)
    return ob


# ── primitives ──────────────────────────────────────────────────────────────

def cylinder(radius=1.0, height=2.0, location=(0, 0, 0), rotation=(0, 0, 0),
             name=None, mat=None, vertices=48, smooth=True, bevel=0.0):
    """A cylinder standing on Z, centred on `location`."""
    ob = _added(
        bpy.ops.mesh.primitive_cylinder_add,
        radius=radius, depth=height, vertices=vertices,
        location=location, rotation=_rot(rotation),
    )
    return _finish(ob, name, mat, smooth, bevel)


def cone(radius=1.0, radius_top=0.0, height=2.0, location=(0, 0, 0),
         rotation=(0, 0, 0), name=None, mat=None, vertices=48,
         smooth=True, bevel=0.0):
    """A cone or truncated cone. radius_top=0 gives a point."""
    ob = _added(
        bpy.ops.mesh.primitive_cone_add,
        radius1=radius, radius2=radius_top, depth=height, vertices=vertices,
        location=location, rotation=_rot(rotation),
    )
    return _finish(ob, name, mat, smooth, bevel)


def sphere(radius=1.0, location=(0, 0, 0), rotation=(0, 0, 0), name=None,
           mat=None, segments=48, rings=24, smooth=True):
    ob = _added(
        bpy.ops.mesh.primitive_uv_sphere_add,
        radius=radius, segments=segments, ring_count=rings,
        location=location, rotation=_rot(rotation),
    )
    return _finish(ob, name, mat, smooth, 0.0)


def cube(size=1.0, location=(0, 0, 0), rotation=(0, 0, 0), dimensions=None,
         name=None, mat=None, smooth=False, bevel=0.0):
    """A cube. Pass `dimensions=(x, y, z)` for a box of an exact size."""
    ob = _added(
        bpy.ops.mesh.primitive_cube_add,
        size=size, location=location, rotation=_rot(rotation),
    )
    if dimensions:
        ob.scale = tuple(d / size for d in dimensions)
        apply_transform(ob, scale=True)
    return _finish(ob, name, mat, smooth, bevel)


def torus(major_radius=1.0, minor_radius=0.25, location=(0, 0, 0),
          rotation=(0, 0, 0), name=None, mat=None, major_segments=48,
          minor_segments=16, smooth=True):
    ob = _added(
        bpy.ops.mesh.primitive_torus_add,
        major_radius=major_radius, minor_radius=minor_radius,
        major_segments=major_segments, minor_segments=minor_segments,
        location=location, rotation=_rot(rotation),
    )
    return _finish(ob, name, mat, smooth, 0.0)


def tube(radius=1.0, thickness=0.1, height=2.0, location=(0, 0, 0),
         rotation=(0, 0, 0), name=None, mat=None, vertices=48, smooth=True):
    """A hollow cylinder — a pipe, a ring, a nozzle throat."""
    ob = cylinder(radius, height, location, rotation, name, mat,
                  vertices, smooth)
    md = ob.modifiers.new(name="Hollow", type="SOLIDIFY")
    md.thickness = thickness
    md.offset = -1.0
    return ob


def icosphere(radius=1.0, location=(0, 0, 0), rotation=(0, 0, 0), name=None,
              mat=None, subdivisions=3, smooth=True):
    """An icosphere. Evener than a UV sphere and the right base for anything
    organic, or for a faceted low-poly look at subdivisions=1-2."""
    ob = _added(
        bpy.ops.mesh.primitive_ico_sphere_add,
        radius=radius, subdivisions=subdivisions,
        location=location, rotation=_rot(rotation),
    )
    return _finish(ob, name, mat, smooth, 0.0)


def curve_tube(points, radius=0.05, name=None, mat=None, closed=False,
               smooth_path=True, resolution=12, sides=6):
    """A round tube following a path — cable, wire, handle, branch, pipe, rail.

    Built from curve data rather than mesh operators, so it is exact and cheap:
    `points` is a list of (x, y, z) the tube passes through. This is the tool
    for anything bent or flowing, which primitives cannot do at all.
    """
    curve = bpy.data.curves.new(name or "Tube", type="CURVE")
    curve.dimensions = "3D"
    curve.bevel_depth = radius
    curve.bevel_resolution = max(sides // 2, 1)
    curve.resolution_u = resolution
    curve.use_fill_caps = True

    spline = curve.splines.new("BEZIER" if smooth_path else "POLY")
    pts = list(points)
    if smooth_path:
        spline.bezier_points.add(len(pts) - 1)
        for bp, pt in zip(spline.bezier_points, pts):
            bp.co = pt
            bp.handle_left_type = bp.handle_right_type = "AUTO"
    else:
        spline.points.add(len(pts) - 1)
        for sp, pt in zip(spline.points, pts):
            sp.co = (*pt, 1.0)
    spline.use_cyclic_u = closed

    ob = bpy.data.objects.new(name or "Tube", curve)
    bpy.context.collection.objects.link(ob)
    if mat is not None:
        ob.data.materials.append(mat)
    return ob


def helix(turns=5.0, radius=0.5, height=2.0, thickness=0.06, location=(0, 0, 0),
          name=None, mat=None, steps_per_turn=24):
    """A spiral — spring, coil, screw thread, staircase rail, DNA backbone."""
    total = max(int(turns * steps_per_turn), 2)
    pts = []
    for i in range(total + 1):
        t = i / total
        angle = 2 * math.pi * turns * t
        pts.append((
            location[0] + math.cos(angle) * radius,
            location[1] + math.sin(angle) * radius,
            location[2] + height * t,
        ))
    return curve_tube(pts, radius=thickness, name=name or "Helix", mat=mat)


def text3d(body, size=1.0, extrude=0.06, location=(0, 0, 0), rotation=(0, 0, 0),
           name=None, mat=None, align="CENTER", to_mesh=True):
    """Solid 3D lettering — signs, logos, labels, numbers on a dial."""
    curve = bpy.data.curves.new(name or "Text", type="FONT")
    curve.body = str(body)
    curve.size = size
    curve.extrude = extrude
    curve.align_x = align
    curve.align_y = "CENTER"

    ob = bpy.data.objects.new(name or "Text", curve)
    bpy.context.collection.objects.link(ob)
    ob.location = location
    ob.rotation_euler = _rot(rotation)
    if mat is not None:
        ob.data.materials.append(mat)
    if to_mesh:
        # As a mesh it can be joined, bevelled and boolean'd like anything else.
        bpy.context.view_layer.objects.active = ob
        with _op_context(ob):
            bpy.ops.object.convert(target="MESH")
    return ob


def mesh_from(verts, faces, name=None, mat=None, location=(0, 0, 0),
              rotation=(0, 0, 0), smooth=False, bevel=0.0):
    """Build an object straight from vertices and faces.

    The bottom of the toolbox: when a shape is neither a primitive nor a
    surface of revolution, describe it directly.
    """
    mesh = bpy.data.meshes.new(name or "Mesh")
    mesh.from_pydata([tuple(v) for v in verts], [], [list(f) for f in faces])
    mesh.validate(verbose=False)
    mesh.update()
    ob = bpy.data.objects.new(name or "Mesh", mesh)
    bpy.context.collection.objects.link(ob)
    ob.location = location
    ob.rotation_euler = _rot(rotation)
    return _finish(ob, name, mat, smooth, bevel)


def revolve(profile, segments=48, location=(0, 0, 0), rotation=(0, 0, 0),
            name=None, mat=None, smooth=True, cap=True):
    """Spin a 2D outline around the Z axis — a lathe.

    `profile` is a list of (radius, z) going up the silhouette. This is the
    right tool for anything with rotational symmetry, and there is a lot of it:
    bottles, vases, glasses, domes, wheels, tyres, columns, chess pieces,
    nose cones, bowls, lampshades, barrels. A radius of 0 closes that end to a
    point, so a teardrop or a dome needs no special handling.
    """
    profile = [(float(r), float(z)) for r, z in profile]
    if len(profile) < 2:
        raise ValueError("revolve needs at least two profile points")

    verts: list[tuple] = []
    rings: list[list[int]] = []
    for radius, z in profile:
        if abs(radius) < 1e-6:
            index = len(verts)
            verts.append((0.0, 0.0, z))
            rings.append([index] * segments)      # a pole: one shared vertex
        else:
            ring = []
            for i in range(segments):
                angle = 2 * math.pi * i / segments
                ring.append(len(verts))
                verts.append((math.cos(angle) * radius,
                              math.sin(angle) * radius, z))
            rings.append(ring)

    faces: list[list[int]] = []
    for lower, upper in zip(rings, rings[1:]):
        for i in range(segments):
            j = (i + 1) % segments
            quad = [lower[i], lower[j], upper[j], upper[i]]
            unique: list[int] = []
            for v in quad:
                if v not in unique:
                    unique.append(v)
            if len(unique) >= 3:          # a pole collapses the quad to a tri
                faces.append(unique)

    if cap:
        for ring, flip in ((rings[0], True), (rings[-1], False)):
            if len(set(ring)) == segments:
                faces.append(list(reversed(ring)) if flip else list(ring))

    return mesh_from(verts, faces, name or "Revolve", mat, location, rotation,
                     smooth=smooth)


def loft(sections, location=(0, 0, 0), rotation=(0, 0, 0), name=None,
         mat=None, cap=True, smooth=True, closed=True):
    """Skin a surface through a stack of cross-sections.

    `sections` is a list of rings, each the same number of (x, y, z) points.
    Primitives cannot make a continuous curved shell, so this is what a car
    body, a boat hull, an aircraft fuselage, a guitar body, a shoe or a
    fairing is made of: describe the silhouette at a few stations and let the
    surface run between them.
    """
    sections = [[tuple(p) for p in ring] for ring in sections]
    if len(sections) < 2:
        raise ValueError("loft needs at least two sections")
    width = len(sections[0])
    if any(len(ring) != width for ring in sections):
        raise ValueError("every loft section needs the same number of points")

    verts: list[tuple] = []
    rings: list[list[int]] = []
    for ring in sections:
        indices = []
        for point in ring:
            indices.append(len(verts))
            verts.append(point)
        rings.append(indices)

    faces: list[list[int]] = []
    span = width if closed else width - 1
    for lower, upper in zip(rings, rings[1:]):
        for i in range(span):
            j = (i + 1) % width
            faces.append([lower[i], lower[j], upper[j], upper[i]])
    if cap and closed:
        faces.append(list(reversed(rings[0])))
        faces.append(list(rings[-1]))

    return mesh_from(verts, faces, name or "Loft", mat, location, rotation,
                     smooth=smooth)


# ── operations ──────────────────────────────────────────────────────────────

def apply_transform(ob, location=False, rotation=False, scale=False) -> None:
    """Bake the object's transform into its mesh."""
    select_only(ob)
    with _op_context(ob):
        bpy.ops.object.transform_apply(
            location=location, rotation=rotation, scale=scale
        )


def bevel(ob, width=0.02, segments=3, angle=30.0):
    """Round off hard edges. The single biggest upgrade to how a build reads:
    nothing in the real world has a perfectly sharp edge, so unbevelled
    primitives always look like primitives."""
    md = ob.modifiers.new(name="Bevel", type="BEVEL")
    md.width = width
    md.segments = segments
    md.limit_method = "ANGLE"
    md.angle_limit = math.radians(angle)
    md.harden_normals = False
    return md


def smooth(ob, angle: float = 30.0):
    """Smooth shading above `angle`, flat below it.

    Set on the mesh data rather than through the shading operator: the
    operator needs a 3D viewport in context, which a script running from a
    socket callback does not reliably have.
    """
    mesh = ob.data
    for poly in mesh.polygons:
        poly.use_smooth = True
    # Smoothing every face alone would drag a cylinder's flat caps into the
    # curve and make it look melted. Blender 4.1+ replaced mesh auto-smooth
    # with a "Smooth by Angle" asset that is not loaded in a fresh file, so
    # use Edge Split, which is always present and does the same job: keep the
    # shading break on edges sharper than `angle`.
    if not any(m.type == "EDGE_SPLIT" for m in ob.modifiers):
        md = ob.modifiers.new(name="Sharpen", type="EDGE_SPLIT")
        md.split_angle = math.radians(angle)
        md.use_edge_angle = True
    return ob


def shade_flat(ob):
    """Faceted shading — low-poly styling, crystals, cut gems."""
    for poly in ob.data.polygons:
        poly.use_smooth = False
    for md in [m for m in ob.modifiers if m.type == "EDGE_SPLIT"]:
        ob.modifiers.remove(md)
    return ob


def solidify(ob, thickness=0.05, offset=-1.0):
    """Give a flat or hollow surface real thickness — panels, shells, walls."""
    md = ob.modifiers.new(name="Solidify", type="SOLIDIFY")
    md.thickness = thickness
    md.offset = offset
    return md


def linear_array(ob, count=3, offset=(1.2, 0, 0), relative=True):
    """Repeat along a line — fence posts, stair treads, windows, teeth."""
    md = ob.modifiers.new(name="Array", type="ARRAY")
    md.count = max(int(count), 1)
    if relative:
        md.use_relative_offset = True
        md.use_constant_offset = False
        md.relative_offset_displace = offset
    else:
        md.use_relative_offset = False
        md.use_constant_offset = True
        md.constant_offset_displace = offset
    return md


def set_origin(ob, kind="ORIGIN_GEOMETRY"):
    """Move an object's pivot. ORIGIN_GEOMETRY centres it on the mesh;
    ORIGIN_CURSOR puts it at the 3D cursor. Matters before rotating a part."""
    select_only(ob)
    with _op_context(ob):
        bpy.ops.object.origin_set(type=kind, center="MEDIAN")
    return ob


def taper(ob, axis="Z", factor=0.5, pivot=None):
    """Squeeze one end of a mesh — a blade, a fuselage, a tree trunk.

    Scales each vertex by how far along `axis` it sits, so the shape narrows
    smoothly from one end to the other. `factor` is the width at the far end
    relative to the near one.
    """
    idx = {"X": 0, "Y": 1, "Z": 2}[axis.upper()]
    verts = ob.data.vertices
    if not verts:
        return ob
    lo = min(v.co[idx] for v in verts)
    hi = max(v.co[idx] for v in verts)
    span = hi - lo
    if span <= 1e-6:
        return ob
    base = lo if pivot is None else pivot
    others = [i for i in range(3) if i != idx]
    for v in verts:
        t = (v.co[idx] - base) / span
        k = 1.0 + (factor - 1.0) * max(0.0, min(1.0, t))
        for o in others:
            v.co[o] *= k
    ob.data.update()
    return ob


def add(operator, **kwargs):
    """Run any raw bpy add-operator and get the object back.

    An escape hatch for the rare shape this API does not cover:
        ob = add(bpy.ops.mesh.primitive_grid_add, x_subdivisions=10)
    """
    return _added(operator, **kwargs)


def subdivide(ob, levels: int = 2, render_levels: int | None = None):
    md = ob.modifiers.new(name="Subdivision", type="SUBSURF")
    md.levels = levels
    md.render_levels = levels if render_levels is None else render_levels
    return md


def mirror(ob, axis: str = "X", across=None):
    """Mirror across an axis. `across` is an object to mirror about."""
    md = ob.modifiers.new(name="Mirror", type="MIRROR")
    md.use_axis[0] = axis.upper() == "X"
    md.use_axis[1] = axis.upper() == "Y"
    md.use_axis[2] = axis.upper() == "Z"
    if across is not None:
        md.mirror_object = across
    return md


def radial_array(ob, count: int = 4, axis: str = "Z", centre=(0, 0, 0)):
    """Copy `ob` evenly around `axis` — fins, legs, blades, spokes.

    Real duplicates rather than an array modifier, so each copy can be edited
    and the whole set can be joined into one mesh afterwards.
    """
    made = [ob]
    pivot = Vector(centre)
    idx = {"X": 0, "Y": 1, "Z": 2}[axis.upper()]
    for i in range(1, max(count, 1)):
        dup = ob.copy()
        dup.data = ob.data.copy()
        bpy.context.collection.objects.link(dup)
        angle = 2 * math.pi * i / count
        offset = dup.location - pivot
        rot = _axis_rotation(idx, angle)
        dup.location = pivot + rot @ offset
        eul = dup.rotation_euler.copy()
        eul[idx] += angle
        dup.rotation_euler = eul
        made.append(dup)
    return made


def _axis_rotation(idx: int, angle: float):
    from mathutils import Matrix
    return Matrix.Rotation(angle, 3, "XYZ"[idx])


def boolean(target, cutter, operation: str = "DIFFERENCE", apply: bool = True):
    """Cut one object with another — windows, holes, hollows."""
    md = target.modifiers.new(name="Boolean", type="BOOLEAN")
    md.object = cutter
    md.operation = operation.upper()
    md.solver = "EXACT"
    if apply:
        select_only(target)
        with _op_context(target):
            bpy.ops.object.modifier_apply(modifier=md.name)
        bpy.data.objects.remove(cutter, do_unlink=True)
    else:
        cutter.display_type = "WIRE"
        cutter.hide_render = True
    return target


def join(objs, name=None):
    """Merge meshes into one object. Materials from every part are kept."""
    objs = [o for o in objs if o is not None]
    if len(objs) < 2:
        ob = objs[0] if objs else None
        if ob is not None and name:
            ob.name = name
        return ob
    select_only(objs)
    with _op_context(objs):
        bpy.ops.object.join()
    ob = bpy.context.view_layer.objects.active
    if name:
        ob.name = name
    return ob


def duplicate(ob, location=None, name=None):
    dup = ob.copy()
    dup.data = ob.data.copy()
    bpy.context.collection.objects.link(dup)
    if location is not None:
        dup.location = location
    if name:
        dup.name = name
    return dup


def move(ob, offset=(0, 0, 0)):
    ob.location = tuple(a + b for a, b in zip(ob.location, offset))
    return ob


def rotate(ob, rotation=(0, 0, 0)):
    """Add to the object's rotation, in degrees."""
    eul = ob.rotation_euler
    for i, d in enumerate(rotation):
        eul[i] += math.radians(d)
    return ob


def scale_obj(ob, factor=(1, 1, 1)):
    if isinstance(factor, (int, float)):
        factor = (factor, factor, factor)
    ob.scale = tuple(a * b for a, b in zip(ob.scale, factor))
    return ob


# ── framing ─────────────────────────────────────────────────────────────────

# Words that mark a mesh as scenery rather than the subject.
_SCENERY = ("ground", "floor", "backdrop", "terrain", "baseplate",
            "base_plate", "grass", "tabletop", "table_top")


def _is_scenery(ob, reference: float) -> bool:
    """Is this the floor rather than the thing standing on it?

    Framing has to ignore scenery or the subject ends up a speck in the middle
    of a 25-metre plane. Name alone is not enough — a script that calls its
    floor "Base_Slab" would still swallow the shot — so anything very large and
    very thin counts too.
    """
    if any(word in ob.name.lower() for word in _SCENERY):
        return True
    dims = ob.dimensions
    footprint = max(dims.x, dims.y)
    if footprint <= 0:
        return False
    return footprint > 4.0 * reference and dims.z < 0.05 * footprint


def bounds(objs=None):
    """(min, max, centre, size) over the world-space corners of `objs`.

    Scenery is excluded by default so the camera frames the subject.
    """
    if objs is None:
        meshes = [o for o in bpy.context.scene.objects if o.type == "MESH"]
        sizes = sorted(max(o.dimensions) for o in meshes) or [1.0]
        reference = sizes[len(sizes) // 2]        # median object size
        objs = [o for o in meshes if not _is_scenery(o, reference)]
        if not objs:
            objs = meshes
    if not objs:
        return Vector((0, 0, 0)), Vector((0, 0, 0)), Vector((0, 0, 0)), Vector((0, 0, 0))
    pts = []
    for ob in objs:
        pts.extend(ob.matrix_world @ Vector(c) for c in ob.bound_box)
    lo = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
    hi = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
    return lo, hi, (lo + hi) / 2, (hi - lo)


def look_at(ob, target=(0, 0, 0)) -> None:
    """Aim an object's -Z at `target`, the way cameras and lights point."""
    direction = Vector(target) - ob.location
    if direction.length == 0:
        return
    ob.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()


def frame_camera(margin: float = 1.12, angle: float = 55.0, height: float = 0.30,
                 lens: float = 50.0):
    """Place a camera three-quarter-on, at the distance that actually fits.

    The distance comes from the lens and the render aspect rather than a
    guessed multiplier: fit the model's bounding sphere inside the narrower of
    the horizontal and vertical fields of view. Guessing leaves the subject
    swimming in empty frame on tall models and clipped on wide ones.
    """
    _lo, _hi, centre, size = bounds()
    radius = max(size.length / 2.0, 0.25)

    cam_data = bpy.data.cameras.new("JarvisCamera")
    cam_data.lens = lens
    cam = bpy.data.objects.new("JarvisCamera", cam_data)
    bpy.context.collection.objects.link(cam)

    scene = bpy.context.scene
    res_x = scene.render.resolution_x * scene.render.pixel_aspect_x
    res_y = scene.render.resolution_y * scene.render.pixel_aspect_y
    sensor = cam_data.sensor_width
    fov_x = 2.0 * math.atan(sensor / (2.0 * lens))
    fov_y = 2.0 * math.atan((sensor * res_y / res_x) / (2.0 * lens))
    half_fov = min(fov_x, fov_y) / 2.0
    dist = radius * margin / max(math.sin(half_fov), 1e-3)

    a = math.radians(angle)
    horiz = dist * math.cos(math.radians(height * 60.0))
    cam.location = (
        centre.x + math.cos(a) * horiz,
        centre.y - math.sin(a) * horiz,
        centre.z + dist * math.sin(math.radians(height * 60.0)),
    )
    look_at(cam, centre)
    scene.camera = cam
    return cam


def frame_viewport() -> None:
    """Zoom every 3D viewport onto the build. Silent no-op in background."""
    wm = bpy.context.window_manager
    for window in getattr(wm, "windows", []):
        for area in window.screen.areas:
            if area.type != "VIEW_3D":
                continue
            region = next((r for r in area.regions if r.type == "WINDOW"), None)
            if region is None:
                continue
            try:
                with bpy.context.temp_override(
                    window=window, area=area, region=region
                ):
                    bpy.ops.view3d.view_all(center=False)
                    area.spaces.active.shading.type = "MATERIAL"
            except Exception:
                pass
