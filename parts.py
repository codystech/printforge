"""Split a fused STL into connected components and write a minimal 3MF."""
import zipfile
from pathlib import Path

import trimesh

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
</Types>"""

RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>"""


def split_parts(stl_path: str | Path) -> list[tuple[str, trimesh.Trimesh]]:
    mesh = trimesh.load_mesh(str(stl_path))
    comps = mesh.split(only_watertight=False)
    if len(comps) <= 1:
        comps = [mesh]
    # stable ordering: biggest part first
    comps = sorted(comps, key=lambda m: -m.volume if m.is_volume else -len(m.faces))
    return [(f"part_{i + 1}", m) for i, m in enumerate(comps)]


# distinct default colors so multi-part models arrive in Bambu pre-differentiated
PALETTE = ["#DA2F2F", "#F5F5F5", "#2B6CB0", "#F6C344", "#3AA655", "#2D2D2D"]


def write_3mf(parts: list[tuple[str, trimesh.Trimesh]], out_path: str | Path) -> Path:
    objects, items = [], []
    bases = "".join(
        f'<base name="color{i + 1}" displaycolor="{c}"/>' for i, c in enumerate(PALETTE)
    )
    materials = f'<basematerials id="100">{bases}</basematerials>'
    for oid, (name, m) in enumerate(parts, start=1):
        verts = "".join(
            f'<vertex x="{v[0]:.4f}" y="{v[1]:.4f}" z="{v[2]:.4f}"/>' for v in m.vertices
        )
        tris = "".join(
            f'<triangle v1="{f[0]}" v2="{f[1]}" v3="{f[2]}"/>' for f in m.faces
        )
        pindex = (oid - 1) % len(PALETTE)
        objects.append(
            f'<object id="{oid}" name="{name}" type="model" pid="100" pindex="{pindex}">'
            f"<mesh><vertices>{verts}</vertices><triangles>{tris}</triangles></mesh></object>"
        )
        items.append(f'<item objectid="{oid}"/>')
    model = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<model unit="millimeter" xml:lang="en-US" '
        'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
        f"<resources>{materials}{''.join(objects)}</resources>"
        f"<build>{''.join(items)}</build></model>"
    )
    out_path = Path(out_path)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", RELS)
        z.writestr("3D/3dmodel.model", model)
    return out_path


def floating_starts(stl_path, step=1.0, min_area=0.3, report_area=4.0, tol=0.8):
    """Slice bottom-up and report cross-section islands that appear with nothing
    beneath them — the 'floating regions' slicers refuse to print.
    Two-layer lookback tolerates section noise on thin cylinders; findings are
    deduped per XY column so one bad feature reports once."""
    import numpy as np
    m = trimesh.load_mesh(str(stl_path))
    z0, z1 = m.bounds[0][2], m.bounds[1][2]
    below = []  # polygons from the last two layers
    out = []
    for z in np.arange(z0 + step / 2, z1, step):
        sec = m.section(plane_origin=[0, 0, z], plane_normal=[0, 0, 1])
        if sec is None:
            below = []
            continue
        planar, T = sec.to_2D()
        polys = [p for p in planar.polygons_full if p is not None and p.area > min_area]
        if below:
            support = [q.buffer(tol) for layer in below for q in layer]
            for p in polys:
                if p.area < report_area:  # thin columns/arcs: spot-support territory
                    continue
                if not any(p.intersects(s) for s in support):
                    c = (T @ np.array([p.centroid.x, p.centroid.y, 0, 1.0]))[:3]
                    # dedupe: same XY column already reported → one finding per feature
                    if any(abs(o["x"] - c[0]) < 4 and abs(o["y"] - c[1]) < 4 for o in out):
                        continue
                    out.append({"z": round(float(z), 1), "x": round(float(c[0]), 1),
                                "y": round(float(c[1]), 1), "area": round(float(p.area), 1)})
        below = ([polys] + below)[:2]
    return out


if __name__ == "__main__":
    # self-check: two disjoint cubes split into two named parts and survive a 3MF round-trip
    import tempfile

    a = trimesh.creation.box((10, 10, 10))
    b = trimesh.creation.box((5, 5, 5))
    b.apply_translation((20, 0, 0))
    with tempfile.TemporaryDirectory() as d:
        stl = Path(d) / "t.stl"
        (a + b).export(stl)
        parts = split_parts(stl)
        assert [n for n, _ in parts] == ["part_1", "part_2"], parts
        out = write_3mf(parts, Path(d) / "t.3mf")
        import xml.etree.ElementTree as ET

        with zipfile.ZipFile(out) as z:
            root = ET.fromstring(z.read("3D/3dmodel.model"))
        ns = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
        objs = root.findall(f"{ns}resources/{ns}object")
        assert len(objs) == 2 and root.find(f"{ns}build") is not None, objs
    print("parts.py self-check OK")
