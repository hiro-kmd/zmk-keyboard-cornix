"""Decode the raw protobuf payloads returned by zmk_studio_api's *_bytes() calls.

Which message each call serializes (srwi/zmk-studio-api, src/python.rs):
  get_keymap_bytes()                 -> zmk.keymap.Keymap                   (python.rs:165-168)
  get_physical_layouts_bytes()       -> zmk.keymap.PhysicalLayouts          (python.rs:170-176)
  get_device_info_bytes()            -> zmk.core.GetDeviceInfoResponse      (python.rs:151-154)
  get_behavior_details_bytes(id)     -> zmk.behaviors.GetBehaviorDetailsResponse, ONE behavior
                                        per call (python.rs:156-163); ids come from
                                        list_all_behaviors() -> list[int].

Every decoder returns plain JSON-serializable dicts with proto field names.
"""

from google.protobuf.json_format import MessageToDict

from zmkproto import behaviors_pb2, core_pb2, keymap_pb2

HID_USAGE_KEYBOARD = 0x07
HID_USAGE_CONSUMER = 0x0C

HID_MODIFIERS = (
    (0x01, "LCTRL"), (0x02, "LSHFT"), (0x04, "LALT"), (0x08, "LGUI"),
    (0x10, "RCTRL"), (0x20, "RSHFT"), (0x40, "RALT"), (0x80, "RGUI"),
)


def _to_dict(msg):
    # protobuf >= 5 renamed including_default_value_fields; support both.
    try:
        return MessageToDict(
            msg,
            preserving_proto_field_name=True,
            always_print_fields_with_no_presence=True,
        )
    except TypeError:
        return MessageToDict(
            msg,
            preserving_proto_field_name=True,
            including_default_value_fields=True,
        )


def decode_hid_usage(encoded):
    """Split ZMK's packed usage: bits 31:24 modifiers, 23:16 page, 15:0 id.

    Page 0 means the keyboard page (0x07), matching zmk-studio-api's
    HidUsage::from_encoded (src/hid_usage.rs).
    """
    encoded = int(encoded) & 0xFFFFFFFF
    page = (encoded >> 16) & 0xFF or HID_USAGE_KEYBOARD
    mods = encoded >> 24
    return {
        "page": page,
        "id": encoded & 0xFFFF,
        "modifiers": mods,
        "modifier_names": [name for bit, name in HID_MODIFIERS if mods & bit],
    }


def encode_hid_usage(page, hid_id, modifiers=0):
    return ((int(modifiers) & 0xFF) << 24) | ((int(page) & 0xFF) << 16) | (int(hid_id) & 0xFFFF)


def decode_keymap(blob):
    """-> {"layers": [{"id", "name", "bindings": [{"behavior_id","param1","param2"}]}],
           "available_layers": int, "max_layer_name_length": int}"""
    msg = keymap_pb2.Keymap()
    msg.ParseFromString(bytes(blob))
    out = _to_dict(msg)
    out.setdefault("layers", [])
    for layer in out["layers"]:
        layer.setdefault("bindings", [])
        for b in layer["bindings"]:
            # proto3 omits zero-valued scalars even with the "always print"
            # flag on some versions; make the three ints always present.
            b["behavior_id"] = int(b.get("behavior_id", 0))
            b["param1"] = int(b.get("param1", 0))
            b["param2"] = int(b.get("param2", 0))
    out["available_layers"] = int(out.get("available_layers", 0))
    out["max_layer_name_length"] = int(out.get("max_layer_name_length", 0))
    return out


def decode_behavior_details(blob):
    """-> {"id": int, "display_name": str, "metadata": [ {"param1": [...], "param2": [...]} ]}

    One GetBehaviorDetailsResponse per call.  Each entry in param1/param2 is a
    BehaviorParameterValueDescription: {"name", and exactly one of
    "nil" | "constant" | "range" {"min","max"} | "hid_usage" {"keyboard_max","consumer_max"} | "layer_id"}.
    """
    msg = behaviors_pb2.GetBehaviorDetailsResponse()
    msg.ParseFromString(bytes(blob))
    out = _to_dict(msg)
    out["id"] = int(out.get("id", 0))
    out.setdefault("display_name", "")
    out.setdefault("metadata", [])
    for pset in out["metadata"]:
        pset.setdefault("param1", [])
        pset.setdefault("param2", [])
        for desc in pset["param1"] + pset["param2"]:
            desc["value_type"] = next(
                (k for k in ("nil", "constant", "range", "hid_usage", "layer_id") if k in desc),
                None,
            )
    return out


def decode_behavior_details_list(blobs):
    """Convenience for a list of per-behavior payloads -> list[dict]."""
    return [decode_behavior_details(b) for b in blobs]


def decode_physical_layouts(blob):
    """-> {"active_layout_index": int, "layouts": [{"name", "keys": [{"width","height","x","y","r","rx","ry"}]}]}"""
    msg = keymap_pb2.PhysicalLayouts()
    msg.ParseFromString(bytes(blob))
    out = _to_dict(msg)
    out["active_layout_index"] = int(out.get("active_layout_index", 0))
    out.setdefault("layouts", [])
    for layout in out["layouts"]:
        layout.setdefault("name", "")
        layout.setdefault("keys", [])
        for k in layout["keys"]:
            for f in ("width", "height", "x", "y", "r", "rx", "ry"):
                k[f] = int(k.get(f, 0))
    return out


def decode_device_info(blob):
    """-> {"name": str, "serial_number": base64 str, "serial_number_hex": str}"""
    msg = core_pb2.GetDeviceInfoResponse()
    msg.ParseFromString(bytes(blob))
    out = _to_dict(msg)
    out.setdefault("name", "")
    out["serial_number_hex"] = msg.serial_number.hex()
    return out


def annotate_keymap(keymap, behavior_details_by_id):
    """Attach display_name (and decoded HID usages where the behavior says a
    param is a keycode) to each binding, in place. Returns the keymap dict."""
    for layer in keymap["layers"]:
        for b in layer["bindings"]:
            det = behavior_details_by_id.get(b["behavior_id"])
            b["behavior_name"] = det["display_name"] if det else None
            if not det:
                continue
            for pname in ("param1", "param2"):
                kinds = {
                    d["value_type"]
                    for pset in det["metadata"]
                    for d in pset.get(pname, [])
                }
                if "hid_usage" in kinds:
                    b[pname + "_hid"] = decode_hid_usage(b[pname])
                elif kinds == {"layer_id"}:
                    b[pname + "_layer"] = b[pname]
    return keymap


if __name__ == "__main__":
    # Synthetic round trips: build -> serialize -> decode -> compare.
    km = keymap_pb2.Keymap(available_layers=3, max_layer_name_length=20)
    base = km.layers.add(id=0, name="Base")
    base.bindings.add(behavior_id=1, param1=encode_hid_usage(7, 0x1E, 0x02), param2=0)  # &kp EXCL
    base.bindings.add(behavior_id=7, param1=2, param2=0)                                # &mo 2
    base.bindings.add(behavior_id=-1, param1=0, param2=0)                               # negative id survives sint32
    km.layers.add(id=5, name="Navi")                                                    # empty layer
    d = decode_keymap(km.SerializeToString())
    assert d["available_layers"] == 3 and d["max_layer_name_length"] == 20
    assert [l["id"] for l in d["layers"]] == [0, 5]
    assert d["layers"][0]["name"] == "Base" and d["layers"][1]["bindings"] == []
    b0 = d["layers"][0]["bindings"]
    assert b0[0] == {"behavior_id": 1, "param1": 0x0207001E, "param2": 0}
    assert b0[1] == {"behavior_id": 7, "param1": 2, "param2": 0}
    assert b0[2]["behavior_id"] == -1
    u = decode_hid_usage(0x0207001E)
    assert (u["page"], u["id"], u["modifiers"], u["modifier_names"]) == (7, 0x1E, 2, ["LSHFT"])
    assert decode_hid_usage(0x0000001E)["page"] == 7          # page 0 -> keyboard
    assert encode_hid_usage(7, 0x1E, 2) == 0x0207001E

    bd = behaviors_pb2.GetBehaviorDetailsResponse(id=1, display_name="Key Press")
    ps = bd.metadata.add()
    p = ps.param1.add(name="Key")
    p.hid_usage.keyboard_max = 0xFF
    p.hid_usage.consumer_max = 0x2FF
    ps.param2.add(name="").nil.SetInParent()
    bd2 = behaviors_pb2.GetBehaviorDetailsResponse(id=27, display_name="hm_l")
    ps2 = bd2.metadata.add()
    ps2.param1.add(name="Mod").hid_usage.keyboard_max = 0xFF
    ps2.param2.add(name="Tap").hid_usage.keyboard_max = 0xFF
    bd3 = behaviors_pb2.GetBehaviorDetailsResponse(id=7, display_name="Momentary Layer")
    bd3.metadata.add().param1.add(name="Layer").layer_id.SetInParent()
    dd = decode_behavior_details(bd.SerializeToString())
    assert dd["id"] == 1 and dd["display_name"] == "Key Press"
    assert dd["metadata"][0]["param1"][0]["value_type"] == "hid_usage"
    assert dd["metadata"][0]["param1"][0]["hid_usage"]["keyboard_max"] == 0xFF
    assert dd["metadata"][0]["param2"][0]["value_type"] == "nil"
    details = {x["id"]: x for x in decode_behavior_details_list(
        [bd.SerializeToString(), bd2.SerializeToString(), bd3.SerializeToString()])}
    annotate_keymap(d, details)
    assert b0[0]["behavior_name"] == "Key Press" and b0[0]["param1_hid"]["id"] == 0x1E
    assert b0[1]["behavior_name"] == "Momentary Layer" and b0[1]["param1_layer"] == 2
    assert b0[2]["behavior_name"] is None

    pl = keymap_pb2.PhysicalLayouts(active_layout_index=1)
    lay = pl.layouts.add(name="Cornix 54")
    lay.keys.add(width=100, height=100, x=0, y=25, r=-1500, rx=50, ry=50)
    pl.layouts.add(name="Cornix 42")
    dp = decode_physical_layouts(pl.SerializeToString())
    assert dp["active_layout_index"] == 1 and [l["name"] for l in dp["layouts"]] == ["Cornix 54", "Cornix 42"]
    assert dp["layouts"][0]["keys"][0] == {"width": 100, "height": 100, "x": 0, "y": 25, "r": -1500, "rx": 50, "ry": 50}

    di = core_pb2.GetDeviceInfoResponse(name="Cornix", serial_number=b"\xde\xad\xbe\xef")
    dv = decode_device_info(di.SerializeToString())
    assert dv["name"] == "Cornix" and dv["serial_number_hex"] == "deadbeef"

    # Sanity: an empty payload is a valid all-defaults message, not an error.
    assert decode_keymap(b"") == {"layers": [], "available_layers": 0, "max_layer_name_length": 0}

    print("all synthetic round-trip checks passed")
