"""Offline tests for zmkbak: no keyboard needed.

Builds synthetic snapshots that mimic a Cornix (8 layers, custom hold-taps,
layer-referencing params) and checks rendering, diff normalisation and the
restore planner's remapping / refusal logic.

    python test_offline.py
"""

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import zmkbak as zb  # noqa: E402
from zmkproto_decode import encode_hid_usage  # noqa: E402

KP, MO, LT, HM, TRANS, NONE, BT, OUT = 1, 2, 3, 40, 4, 5, 6, 7


def details(bid, name, p1=None, p2=None):
    def desc(kind):
        d = {"name": kind or "", "value_type": kind}
        if kind:
            d[kind] = {}
        return d
    meta = [{"param1": [desc(p1)] if p1 else [], "param2": [desc(p2)] if p2 else []}]
    return {"id": bid, "display_name": name, "metadata": meta}


BEHAVIORS = {
    KP: details(KP, "Key Press", "hid_usage", "nil"),
    MO: details(MO, "Momentary Layer", "layer_id", "nil"),
    LT: details(LT, "Layer-Tap", "layer_id", "hid_usage"),
    HM: details(HM, "hm_l", "hid_usage", "hid_usage"),
    TRANS: details(TRANS, "Transparent"),
    NONE: details(NONE, "None"),
    BT: details(BT, "Bluetooth", "constant", "range"),
    OUT: details(OUT, "Output Selection", "constant"),
}


def snap_behaviors(beh):
    return {
        str(bid): {
            "display_name": d["display_name"],
            "param1_types": sorted(t for t in zb.param_types(d, "param1") if t),
            "param2_types": sorted(t for t in zb.param_types(d, "param2") if t),
            "metadata": d["metadata"],
        }
        for bid, d in beh.items()
    }


def make_snapshot(layer_ids, beh=BEHAVIORS, key_count=4, id_offset=0):
    """id_offset shifts every behavior_id, mimicking a firmware whose ids moved."""
    A = encode_hid_usage(7, 0x04)
    N1 = encode_hid_usage(7, 0x1E)
    EXCL = encode_hid_usage(7, 0x1E, 0x02)
    LCTRL = encode_hid_usage(7, 0xE0)
    layers = []
    for index, lid in enumerate(layer_ids):
        next_id = layer_ids[(index + 1) % len(layer_ids)]
        raw = [
            (KP, A if index == 0 else EXCL, 0),
            (MO, next_id, 0),
            (LT, next_id, N1),
            (HM, LCTRL, A) if index == 0 else (TRANS, 0, 0),
        ][:key_count]
        bindings = []
        for pos, (bid, p1, p2) in enumerate(raw):
            bid += id_offset
            b = {"behavior_id": bid, "param1": p1, "param2": p2}
            bindings.append({"pos": pos, **b, "behavior_name": beh[bid]["display_name"],
                             "zmk": zb.render_binding(b, beh[bid])})
        layers.append({"index": index, "id": lid, "name": f"L{index}", "bindings": bindings})
    return {
        "schema": 1, "tool": "zmkbak", "tool_version": "t", "captured_at": "2026-09-04T00:00:00",
        "note": None, "port": "COMX", "unsaved_changes": False,
        "device": {"name": "Cornix", "serial_hex": "aa"},
        "physical_layout": {"active_index": 0, "name": "Cornix", "key_count": key_count,
                            "keys": [{"x": 100 * i, "y": 0, "width": 100, "height": 100,
                                      "r": 0, "rx": 0, "ry": 0} for i in range(key_count)]},
        "keymap": {"available_layers": 0, "layers": layers},
        "behaviors": snap_behaviors(beh),
    }


def test_render():
    a = make_snapshot([0, 1, 2])
    l0 = {b["pos"]: b["zmk"] for b in a["keymap"]["layers"][0]["bindings"]}
    l1 = {b["pos"]: b["zmk"] for b in a["keymap"]["layers"][1]["bindings"]}
    assert l0[0] == "&kp A", l0
    assert l1[0] == "&kp EXCL", l1                       # LS(N1) renders as the alias
    assert l0[1] == "&mo 1" and l0[2] == "&lt 1 N1", l0
    assert l0[3] == "&hm_l LCTRL A", l0                  # custom hold-tap keeps both keycodes
    assert l1[3] == "&trans"
    b = {"behavior_id": BT, "param1": 3, "param2": 2}
    assert zb.render_binding(b, BEHAVIORS[BT]) == "&bt BT_SEL 2"
    b = {"behavior_id": OUT, "param1": 2, "param2": 0}
    assert zb.render_binding(b, BEHAVIORS[OUT]) == "&out OUT_BLE"
    assert zb.hid_to_zmk(encode_hid_usage(7, 0x04, 0x01 | 0x08)) in ("LG(LC(A))", "LC(LG(A))")
    text = zb.render_snapshot(a)
    assert "=== layer 0" in text and "&hm_l LCTRL A" in text


def test_diff_is_id_independent():
    a = make_snapshot([0, 1, 2])
    b = make_snapshot([0, 5, 9])         # same layout, Studio re-numbered layers
    assert zb.diff_snapshots(a, b) == []
    c = copy.deepcopy(b)
    c["keymap"]["layers"][2]["bindings"][0]["param1"] = encode_hid_usage(7, 0x05)
    changes = zb.diff_snapshots(a, c)
    assert [k for k, _, _ in changes] == [(2, 0)], changes


def test_plan_remaps_ids_and_layers():
    snap = make_snapshot([0, 1, 2])
    # Target firmware: behaviours got new ids, layers got new ids, one key differs.
    dev_beh = {bid + 100: dict(d, id=bid + 100) for bid, d in BEHAVIORS.items()}
    live = make_snapshot([0, 5, 9], beh=dev_beh, id_offset=100)
    live["keymap"]["layers"][0]["bindings"][0]["param1"] = encode_hid_usage(7, 0x05)  # B instead of A
    writes, notes = zb.plan_restore(snap, live, dev_beh, force=False, allow_raw_ids=False)
    assert writes == [(0, 0, KP + 100, encode_hid_usage(7, 0x04), 0)], writes
    # Now make every binding differ so we can inspect the layer remap.
    for layer in live["keymap"]["layers"]:
        for b in layer["bindings"]:
            b["behavior_id"], b["param1"], b["param2"] = NONE + 100, 0, 0
    writes, _ = zb.plan_restore(snap, live, dev_beh, force=False, allow_raw_ids=False)
    by = {(w[0], w[1]): w[2:] for w in writes}
    assert by[(0, 1)] == (MO + 100, 5, 0)            # &mo 1 -> &mo <new id of layer index 1>
    assert by[(5, 2)] == (LT + 100, 9, encode_hid_usage(7, 0x1E))
    assert by[(9, 1)] == (MO + 100, 0, 0)            # wraps back to layer index 0


def test_plan_refusals():
    snap = make_snapshot([0, 1, 2])
    live = make_snapshot([0, 1, 2])
    # Missing behaviour on target -> unresolved.
    dev_beh = {bid: d for bid, d in BEHAVIORS.items() if bid != HM}
    try:
        zb.plan_restore(snap, live, dev_beh, force=False, allow_raw_ids=False)
        raise AssertionError("expected EXIT_UNRESOLVED")
    except zb.Fail as exc:
        assert exc.code == zb.EXIT_UNRESOLVED and "hm_l" in str(exc)
    # Layer count mismatch -> incompatible unless --force.
    short = make_snapshot([0, 1])
    try:
        zb.plan_restore(snap, short, BEHAVIORS, force=False, allow_raw_ids=False)
        raise AssertionError("expected EXIT_INCOMPATIBLE")
    except zb.Fail as exc:
        assert exc.code == zb.EXIT_INCOMPATIBLE
    writes, notes = zb.plan_restore(snap, short, BEHAVIORS, force=True, allow_raw_ids=False)
    assert notes and all(w[0] in (0, 1) for w in writes)
    # Key count mismatch.
    wide = make_snapshot([0, 1, 2], key_count=3)
    try:
        zb.plan_restore(snap, wide, BEHAVIORS, force=False, allow_raw_ids=False)
        raise AssertionError("expected EXIT_INCOMPATIBLE")
    except zb.Fail as exc:
        assert exc.code == zb.EXIT_INCOMPATIBLE
    # NULL bindings (behavior id 0) are skipped, never written, never block resolution.
    nul = copy.deepcopy(snap)
    nul["keymap"]["layers"][1]["bindings"][3]["behavior_id"] = 0
    nul["keymap"]["layers"][1]["bindings"][3]["behavior_name"] = None
    target = make_snapshot([0, 1, 2])
    for layer in target["keymap"]["layers"]:
        for b in layer["bindings"]:
            b["behavior_id"], b["param1"], b["param2"] = NONE, 0, 0
    writes, notes = zb.plan_restore(nul, target, BEHAVIORS, force=False, allow_raw_ids=False)
    assert all(not (w[0] == 1 and w[1] == 3) for w in writes), writes
    assert any("id 0" in n for n in notes), notes
    assert zb.render_binding({"behavior_id": 0, "param1": 0, "param2": 0}, None) == "(未設定)"
    # Duplicate display names: pick the one equal to the snapshot id, else refuse.
    dup = dict(BEHAVIORS)
    dup[99] = details(99, "Key Press", "hid_usage", "nil")
    writes, _ = zb.plan_restore(snap, live, dup, force=False, allow_raw_ids=False)  # KP==1 exists -> ok
    dup2 = {bid + 100: dict(d, id=bid + 100) for bid, d in dup.items()}
    try:
        zb.plan_restore(snap, make_snapshot([0, 1, 2], beh=dup2, id_offset=100), dup2,
                        force=False, allow_raw_ids=False)
        raise AssertionError("expected EXIT_UNRESOLVED for ambiguous name")
    except zb.Fail as exc:
        assert exc.code == zb.EXIT_UNRESOLVED and "複数" in str(exc)


def test_snapshot_roundtrip(tmp_root):
    zb.SNAPSHOT_ROOT = tmp_root
    zb.LATEST_FILE = tmp_root / "LATEST"
    snap = make_snapshot([0, 1, 2])
    out = zb.write_snapshot(snap, {"keymap.bin": b"\x00", "behaviors/1.bin": b"\x01"})
    assert (out / "snapshot.json").exists() and (out / "keymap.txt").exists()
    assert (out / "raw" / "behaviors" / "1.bin").read_bytes() == b"\x01"
    loaded = zb.load_snapshot("--latest")
    assert zb.diff_snapshots(snap, loaded) == []
    assert zb.load_snapshot(out.name)["device"]["name"] == "Cornix"
    assert zb.load_snapshot(str(out))["device"]["name"] == "Cornix"


def test_cli_parsing():
    p = zb.build_parser()
    a = p.parse_args(["show", "--latest"])
    assert a.latest and a.snapshot is None and not a.device
    a = p.parse_args(["show", "--device"])
    assert a.device
    a = p.parse_args(["verify", "--latest"])
    assert a.latest and zb.snapshot_ref(a) == "latest"
    a = p.parse_args(["restore", "--latest", "--dry-run", "--yes"])
    assert a.latest and a.dry_run and a.yes
    a = p.parse_args(["restore", "20260904-000000_Cornix"])
    assert zb.snapshot_ref(a) == "20260904-000000_Cornix"
    a = p.parse_args(["diff", "latest", "--device"])
    assert a.a == "latest" and a.b is None and a.device
    try:
        zb.snapshot_ref(p.parse_args(["verify"]))
        raise AssertionError("expected usage error")
    except zb.Fail as exc:
        assert exc.code == zb.EXIT_USAGE


if __name__ == "__main__":
    import tempfile
    test_cli_parsing()
    test_render()
    test_diff_is_id_independent()
    test_plan_remaps_ids_and_layers()
    test_plan_refusals()
    with tempfile.TemporaryDirectory() as tmp:
        test_snapshot_roundtrip(Path(tmp))
    print("offline tests passed")
