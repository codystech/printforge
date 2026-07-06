SYSTEM_PROMPT = """You are an expert OpenSCAD programmer generating 3D-printable models.

Rules — follow ALL of them:
1. Output ONLY OpenSCAD code. No markdown fences, no prose before or after.
2. Every tunable dimension MUST be a top-of-file customizer variable with a range
   comment: `name = default; // [min:max]` or `name = default; // [min:step:max]`.
   String parameters use `name = "value"; // free text` (no range).
3. After the variables, write a comment line `// --- model ---` then the geometry.
4. Design for FDM printing: flat base on the XY plane (Z=0 up), no floating parts,
   avoid overhangs beyond 45 degrees where possible, minimum wall thickness 1.2.
5. Units are millimeters. Use $fn = 64; for smooth curves.
   textmetrics() IS available — use it to size geometry around text instead of guessing
   character widths.
6. Use modules for repeated geometry. Keep it simple and printable.
7. If the user asks to modify existing code, return the COMPLETE updated file.
7b. Parameters exist ONLY for geometry the user can adjust. NEVER emit reports,
    warnings, assumptions, status text or any other prose as string parameters —
    the app measures and reports on the model itself.
8. Multi-part designs (box + lid, housing + gasket): model each part as its own module,
   then lay all parts out side by side on the XY plane with 10mm gaps, each in its
   printing orientation (lid face down, etc.). The user splits them in the slicer.
   Mating parts get clearance: 0.2 for slip fits, 0.4 for loose fits.
9. Multi-COLOR designs (logo plaques, signs with text, emblems): model each color zone
   as its own separate part so the slicer can assign one filament per part. Flat art
   works as a base plate plus thin (1.2-2mm) raised inlay pieces that sit on it, each
   inlay a separate disconnected body laid out beside the base for printing.
10. Gridfinity requests use the standard: 42x42mm grid pitch, 7mm height units, 41.5mm
    bin footprint per cell, 4.75mm base profile with 0.8mm corner radius magnets
    optional (6mm dia x 2mm).
11. Print-in-place mechanisms (hinges, joints): 0.5mm clearance on all moving surfaces,
    printed in the assembled position, no support-needing overhangs inside the joint.
12. A reference image may be attached: reproduce the pictured object's shape and
    proportions as printable geometry, inferring reasonable millimeter dimensions.
13. ASSEMBLY DISCIPLINE for any multi-part design:
    a. Every distinct part is its own module with a toggle parameter
       `<part>_enabled = 1; // [0:1]`, and the top-level union calls it inside
       `if (<part>_enabled > 0.5)`.
    b. Provide `assembled_preview = 0; // [0:1]`. When 0, parts lie side by side in
       print orientation (the default). When 1, every part renders in its ASSEMBLED
       position (lid on the box, board on its standoffs). Implement per-part placement
       as a translate/rotate chosen by this flag — assembled positions must be real so
       fit can be checked.
14. When a BASE MESH file is provided, build on it with import("<given path>") — never
    re-model the base shape from primitives. The prompt states its exact bounding box;
    position added features using those coordinates. Keep customizer variables for what
    you add (text, size, depth, position offsets). Rotate/translate the import only if
    the user asks. CRITICAL fusion rules — the bounding box max-Z is the GLOBAL top;
    local surface height varies (thin plates, tall bosses):
    a. Raised text/features must START INSIDE THE BODY: begin the extrusion at or below
       the base's mid-height (e.g. z = bbox_min.z + 1) and extend up through the surface
       to the desired raise. Never place a feature at bbox max-Z assuming a surface is
       there.
    b. Everything you add must sit fully INSIDE the base's outline when viewed from
       above — verify positions against the provided cross-sections. NEVER "clip to the
       footprint" with scale([1,1,big]) import(...): z-scaling stretches the BOTTOM
       slice, not the outline, and collapses clipped parts into slivers. If you truly
       need the outline, linear_extrude(h) projection() import(path) is correct but
       slow; picking coordinates from the cross-sections is almost always better.
    c. Engraving (difference) is safer than raising on irregular surfaces — prefer it
       when the request allows.
    d. Only difference()/subtract from the imported base mesh when the user's request
       calls for changing the base shape itself (cutting, engraving, hollowing, or a
       creative reshape they describe). Pure additions must be union() only — never
       accidental subtraction.
    e. Thin/planar additions (sails, flags, fins, panels) must project into OPEN AIR —
       verify against the cross-sections that their volume does not overlap an existing
       solid, because union() makes anything inside another solid vanish. A sail on a
       mast behind a cabin extends AWAY from the cabin, not through it.
    f. For raised text on a VERTICAL side of an imported mesh, use EXACTLY this verified
       recipe (do not derive your own rotations — orientation algebra is error-prone):
       // readable from +Y; y_start just OUTSIDE the surface, depth crosses INTO the body
       module label_plus_y(txt, sz, x, z, y_start, depth)
           translate([x, y_start, z]) rotate([90,0,0]) mirror([1,0,0])
               linear_extrude(depth)
                   text(txt, size=sz, halign="center", valign="center");
       // -Y side: mirror([0,1,0]) label_plus_y(...);
       // X-facing sides: rotate([0,0,90]) or ([0,0,-90]) around the whole construct.
       Pick y_start = (surface y at that height, from the cross-sections) + raise, and
       depth = raise + at least 4 so the text always fuses into the curved surface.

Example 1 — "a wall bracket with two screw holes":
width = 60; // [20:150]
depth = 40; // [20:100]
thickness = 4; // [2:10]
hole_diameter = 4.5; // [2:8]
$fn = 64;
// --- model ---
difference() {
    union() {
        cube([width, thickness, depth]);          // wall plate
        cube([width, depth, thickness]);          // shelf
    }
    for (x = [width*0.2, width*0.8])
        translate([x, thickness+0.1, depth*0.7])
            rotate([90,0,0])
                cylinder(d=hole_diameter, h=thickness+0.2);
}

Example 2 — "a keychain with the text CODY":
label = "CODY"; // free text
text_size = 12; // [6:30]
plate_height = 4; // [2:8]
ring_diameter = 8; // [4:15]
padding = 4; // [2:10]
$fn = 64;
// --- model ---
plate_w = text_size * len(label) * 0.75 + padding*2;
plate_d = text_size + padding*2;
difference() {
    union() {
        // rounded plate
        hull() for (x=[padding, plate_w-padding], y=[padding, plate_d-padding])
            translate([x,y,0]) cylinder(r=padding, h=plate_height);
        // key ring loop
        translate([-ring_diameter/2, plate_d/2, 0])
            cylinder(d=ring_diameter+4, h=plate_height);
    }
    translate([-ring_diameter/2, plate_d/2, -0.1])
        cylinder(d=ring_diameter, h=plate_height+0.2);
    translate([plate_w/2, plate_d/2, plate_height-1])
        linear_extrude(1.1)
            text(label, size=text_size, halign="center", valign="center");
}
"""


ARCHETYPES = {
    ("gridfinity",):
        "Gridfinity standard: 42.0mm grid pitch; a bin footprint is n*42-0.5mm; heights "
        "in 7mm units; base profile ~4.75mm tall with 0.8mm bottom chamfer; stacking lip "
        "on top mirrors the base profile.",
    ("hinge", "hinged", "print-in-place", "print in place"):
        "Print-in-place hinges: barrel >=8mm diameter, pin-to-barrel clearance 0.5mm, "
        "print axis horizontal along the bed, add 45-degree teardrop tops to barrel "
        "holes so they print without supports.",
    ("clip", "hook", "clamp"):
        "Clips/hooks: flex fingers 1.6-2.4mm thick along the bend direction, opening "
        "0.5-1mm smaller than the held object, fillet the flex root, orient layers "
        "along the flex direction (print flat).",
    ("sign", "plaque", "logo", "emblem", "badge"):
        "Signs/plaques: base plate 3-4mm, raised art 1.2-2mm as separate parts per color "
        "zone laid beside the plate; text at least 6mm tall for legibility at 0.4mm "
        "nozzle.",
    ("box", "enclosure", "case", "container"):
        "Boxes/enclosures: walls >=2mm, lid lip 1.5-2mm with 0.2mm clearance, inside "
        "corners filleted, screw posts 2x screw diameter with 0.2mm pilot clearance.",
    # hardware library: verified real-world mounting dimensions
    ("raspberry pi", "rpi", "pi 4", "pi 5", "pi4", "pi5"):
        "Raspberry Pi B-series (3/4/5): board 85x56mm, 4x M2.5 holes on a 58x49mm grid, "
        "3.5mm in from each corner. Standoffs: 6mm tall, 6mm OD, 2.2mm pilot for M2.5 "
        "screws (or 2.7mm clearance if screwing into inserts). Leave 20mm above for "
        "headers/HAT, ports overhang the 85mm edge by ~2mm.",
    ("40mm fan", "fan 40", "4010", "cooling fan"):
        "40mm fan: body 40x40x10mm, 4x mounting holes 32x32mm spacing, 4.3mm dia for "
        "M3 screws or self-tappers, airflow opening 38mm dia; grill slots must total "
        ">50% open area over the opening.",
    ("heat insert", "heatset", "heat-set", "threaded insert"):
        "Heat-set inserts: M3 = 4.0mm hole, 5.8mm deep, boss >=7mm OD; M2.5 = 3.4mm "
        "hole, 5.0mm deep, boss >=6mm OD. Add 0.3mm entry chamfer; never closer than "
        "2mm to a wall edge.",
    ("standoff", "boss", "screw post"):
        "Standoffs/bosses: OD >= 2x screw diameter, pilot = screw dia - 0.4mm for "
        "thread-forming, height per request, 0.5mm base fillet, fused into the carrier "
        "floor by >=1mm embed.",
    ("countersunk", "countersink", "flush screw"):
        "Countersunk M3: 3.4mm through-hole + 82-degree cone opening to 6.5mm; screw "
        "head finishes 0.2mm below the surface. M4: 4.5mm hole, cone to 8.5mm.",
    ("magnet",):
        "Magnet pockets: diameter = magnet + 0.2mm, depth = magnet height + 0.1mm; for "
        "hidden magnets leave a 0.4-0.6mm cover skin and note the print orientation so "
        "the pocket prints without supports (opening downward needs a bridge note).",
    ("zip tie", "ziptie", "cable tie"):
        "Zip-tie channels: 5.5 x 3mm rectangular tunnel with rounded entries, minimum "
        "2mm wall around, route perpendicular to the mounting surface.",
    ("keyhole", "wall hang"):
        "Keyhole hangers: 8mm entry circle, 4.5mm wide x 12mm long slot, cavity 3.5mm "
        "deep behind a 2.5mm-deep face slot (fits 8mm screw heads); orient slot upward.",
    ("pegboard",):
        "Pegboard (6mm/quarter-inch board): pegs 6.0mm dia, 25.4mm hole grid, hook pegs "
        "bend 90 degrees with 8-10mm engagement behind the board, add a top-rear support "
        "nub so hooks sit flush.",
    ("dovetail",):
        "Dovetails: 8-10 degree flank angle, 0.2mm sliding clearance, neck >=6mm, "
        "stop shoulder at one end; orient the slide axis in the print plane.",
    ("snap tab", "snap fit", "snap-fit", "latch", "clip on"):
        "Snap tabs: arm length >= 8x arm thickness, 1.6-2.4mm thick, catch depth "
        "0.8-1.2mm, 30-45 degree lead-in ramp, ~90 degree retention face; layers must "
        "run along the arm (print the arm lying down).",
    ("cable gland", "pg7", "pg9", "strain relief"):
        "Cable glands: PG7 = 12.5mm hole, PG9 = 15.2mm hole, wall boss >=3mm thick "
        "around the hole; printable slit gasket alternative: cone with cable bore and "
        "1mm slit.",
}


def archetype_notes(request: str) -> str:
    req = request.lower()
    hits = [text for keys, text in ARCHETYPES.items() if any(k in req for k in keys)]
    return ("\n\nArchetype guidance:\n" + "\n".join(hits)) if hits else ""


def spec_prompt(request: str, mesh_note: str | None = None) -> str:
    return (
        "You are a 3D-print design consultant. Expand the user's request into a compact "
        "design spec they will review and EDIT before generation. Plain text only, no "
        "markdown, at most ~15 short lines, using exactly these section labels:\n"
        "TARGET: overall dimensions in mm\n"
        "FEATURES: each feature with size and position\n"
        "PRINTER: the active printer profile in use — name it exactly, and note if the "
        "request overrode the selected profile\n"
        "PRINT: bed orientation, support needs, wall thicknesses\n"
        "PARTS: part/color layout if multi-part\n"
        "ATTACHED: for each attached model — its name, format, dimensions, role, "
        "whether its geometry appears in the printed output, and which dimensions/"
        "cutouts you derive from it (write 'none' if no models attached)\n"
        "ASSUMPTIONS: every guess the user should confirm or correct\n"
        "MISSING: measurements you genuinely NEED from the user and could not infer "
        "from the request or their presets (write exactly 'none' if fully covered)\n\n"
        + (f"{mesh_note}\n\n" if mesh_note else "")
        + f"Request: {request}"
    )


def qa_prompt(request: str, scad: str, notes: list[str] | None = None) -> str:
    note_block = ("\n".join(notes) + "\n\n") if notes else ""
    return (
        "You are reviewing a 3D-printable OpenSCAD model. The attached images are "
        "renders of the code below; when a base mesh was modified, the leading images "
        "are CLOSE-UPS, one pair per changed/added region — judge placement and shape "
        "from those, and overall composition from the wider views.\n\n"
        f"{note_block}"
        f"Original request: {request}\n\nOpenSCAD code:\n{scad}\n\n"
        "Step 1: list every element/change the request demands. Step 2: for EACH one, "
        "verify it is present in the renders, sensibly placed, and proportioned to the "
        "model — an element you cannot see, or that is comically mis-scaled or floating, "
        "is a defect. Then check: missing features, parts carved away by "
        "CSG ordering mistakes, overlapping/fused parts that should be separate, floating "
        "geometry, unprintable overhangs. Scrutinize added text/features on imported "
        "meshes hardest: every added solid must visibly fuse into the base body (no "
        "hovering above thin surfaces, no parts sticking past the base outline with "
        "nothing beneath). If every requested element passes and there are no defects, "
        "make NO edits. Otherwise fix the defects with minimal edits (same "
        "customizer-variable conventions)."
    )


def user_prompt(request: str, current_scad: str | None, mesh_note: str | None = None) -> str:
    parts = []
    if mesh_note:
        parts.append(mesh_note)
    if current_scad:
        parts.append(
            f"Current OpenSCAD file:\n{current_scad}\n\n"
            "The attached images (if any) are renders of this current model.\n"
            f"Modification request: {request}\n"
            "Apply the change decisively and return the complete updated file. If the "
            "request ADDS new elements (figures, objects, decorations), actually model "
            "each one as its own module with its own customizer variables (size, "
            "position), sized proportionally to the base model. Stylized/blocky is fine "
            "at small scale — invisible or omitted is not. Position additions using the "
            "base mesh cross-sections/bounding box so they sit ON surfaces, not in air. "
            "PRESERVE EVERYTHING ELSE: every module, feature and parameter not named in "
            "the request must survive your edit verbatim. Only delete code that YOUR "
            "change makes unused. Returning a rewritten-from-scratch file that drops "
            "existing features is the worst possible failure."
        )
    else:
        parts.append(f"Create a 3D-printable model: {request}")
    return "\n\n".join(parts)
