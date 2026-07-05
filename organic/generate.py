"""Image -> printable STL via Hunyuan3D-2 (shape only). Runs inside organic/.venv."""
import argparse
import sys

import numpy as np
import trimesh


def postprocess(m: trimesh.Trimesh, target_mm: float) -> trimesh.Trimesh:
    # Hunyuan3D outputs Y-up; printing is Z-up
    m.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, [1, 0, 0]))
    parts = m.split(only_watertight=False)
    if len(parts) > 1:
        m = max(parts, key=lambda p: len(p.faces))
    trimesh.repair.fill_holes(m)
    trimesh.repair.fix_normals(m)
    if len(m.faces) > 400_000:
        try:
            m = m.simplify_quadric_decimation(face_count=400_000)
        except TypeError:  # older/newer trimesh: takes a 0-1 reduction fraction
            m = m.simplify_quadric_decimation(1 - 400_000 / len(m.faces))
    m.apply_scale(target_mm / max(m.extents))
    # center XY, floor to z=0, shave a small slice for a flat first layer
    c = m.bounds.mean(axis=0)
    m.apply_translation([-c[0], -c[1], -m.bounds[0][2]])
    try:
        cut = m.slice_plane([0, 0, 0.4], [0, 0, 1], cap=True)
        if cut is not None and len(cut.faces) > 100:
            cut.apply_translation([0, 0, -cut.bounds[0][2]])
            m = cut
    except Exception:
        pass  # ponytail: no flat base is a soft failure, still printable on a raft
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--target-mm", type=float, default=80)
    ap.add_argument("--steps", type=int, default=30)
    ap.add_argument("--model", default="tencent/Hunyuan3D-2mini")
    ap.add_argument("--subfolder", default="hunyuan3d-dit-v2-mini")
    a = ap.parse_args()

    from PIL import Image
    img = Image.open(a.image).convert("RGBA")
    if img.mode == "RGBA" and img.getextrema()[3][0] == 255:  # no alpha: remove bg
        try:
            from hy3dgen.rembg import BackgroundRemover
            img = BackgroundRemover()(img.convert("RGB"))
        except Exception as e:
            print(f"bg removal skipped: {e}", file=sys.stderr)

    from hy3dgen.shapegen import Hunyuan3DDiTFlowMatchingPipeline
    pipe = Hunyuan3DDiTFlowMatchingPipeline.from_pretrained(a.model, subfolder=a.subfolder)
    out = pipe(image=img, num_inference_steps=a.steps)
    mesh = out[0] if isinstance(out, (list, tuple)) else out
    if not isinstance(mesh, trimesh.Trimesh):
        mesh = trimesh.Trimesh(vertices=np.asarray(mesh.vertices),
                               faces=np.asarray(mesh.faces))
    postprocess(mesh, a.target_mm).export(a.out)
    print("ok")


if __name__ == "__main__":
    main()
