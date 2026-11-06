from z2ems.field_view import FieldChoice, line_segments, simplify, tag


def test_simplify_keeps_corners_drops_collinear():
    pts = [(0, 0), (1, 0), (2, 0), (3, 0), (3, 1), (3, 2)]
    assert simplify(pts, 0.01) == [(0, 0), (3, 0), (3, 2)]


def test_line_segments_flip_width_and_chevrons():
    lines = [dict(pts=[[0, 1], [5, 1], [10, 1]], mag=1.0), dict(pts=[[0, 2], [10, 2]], mag=0.25)]
    segs = line_segments(lines, flip=True, w_min=0.02, w_max=0.1, tol=0.01, chevron_every=4, chevron_len=0.5)
    shafts = [s for s in segs if s[0][1] == s[1][1]]
    assert all(a[1] in (-1, -2) for a, b, w in shafts)              # y mirrored into KiCad's frame
    assert max(w for *_, w in segs) == 0.1 and min(w for *_, w in segs) < 0.1
    chev = [s for s in segs if s[0][1] != s[1][1]]
    assert chev and all(b[0] < a[0] for a, b, w in chev)             # chevrons trail behind the +x current


def test_tag_names_the_quantity_layer_frequency_and_port():
    assert tag(FieldChoice(exc=2, quantity="V"), "F.Cu", 3.0) == "V_F_Cu_3000MHz_P2"
