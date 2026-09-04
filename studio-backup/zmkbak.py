#!/usr/bin/env python
"""zmkbak - ZMK Studio で設定したキーマップのバックアップ / 復元ツール。

ZMK Studio には書き出し機能が無いため、Studio が内部で使っている RPC
(zmk-studio-api 経由, USB シリアル) でキーボードから直接キーマップを読み書きする。

    zmkbak doctor                       接続状態の診断
    zmkbak backup [--note TEXT]         今のキーマップを snapshots/ に保存
    zmkbak show   <snap|--latest|--device>
    zmkbak diff   <A> [<B>|--device]
    zmkbak verify <snap|--latest>       キーボードとスナップショットが一致するか
    zmkbak restore <snap|--latest> [--dry-run] [--yes] [--discard-unsaved]
                                        [--force] [--allow-raw-ids]

復元の安全設計:
  * 書き込みは RAM 上の作業コピーにだけ行い、全て読み戻して一致を確認してから
    save_changes() でフラッシュに保存する。不一致・エラー時は discard_changes()。
  * 復元前に必ず自動バックアップを取る。
  * behavior_id はファームウェア固有で変わり得るので、表示名で再解決する。
    レイヤーを参照するパラメータ (&mo N など) はレイヤーの並び順で付け直す。
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import zmk_studio_api as z  # noqa: E402
from serial.tools import list_ports  # noqa: E402

from zmkproto_decode import (  # noqa: E402
    decode_behavior_details,
    decode_device_info,
    decode_hid_usage,
    decode_keymap,
    decode_physical_layouts,
)

TOOL_VERSION = "1.0.0"
SCHEMA = 1
ZMK_VID = 0x1D50
SNAPSHOT_ROOT = HERE / "snapshots"
LATEST_FILE = SNAPSHOT_ROOT / "LATEST"

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_NO_DEVICE = 2
EXIT_PORT_BUSY = 3
EXIT_LOCKED = 4
EXIT_UNSAVED = 5
EXIT_INCOMPATIBLE = 6
EXIT_UNRESOLVED = 7
EXIT_MISMATCH = 8
EXIT_SAVE_FAILED = 9
EXIT_PROTOCOL = 10

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


class Fail(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _default_say(msg):
    print(msg, flush=True)


def _default_warn(msg):
    print(f"[注意] {msg}", file=sys.stderr, flush=True)


_OUTPUT = {"say": _default_say, "warn": _default_warn}


def set_output(say_fn=None, warn_fn=None):
    """Redirect all user-facing messages (used by the GUI)."""
    _OUTPUT["say"] = say_fn or _default_say
    _OUTPUT["warn"] = warn_fn or _default_warn


def say(msg=""):
    _OUTPUT["say"](msg)


def warn(msg):
    _OUTPUT["warn"](msg)


# --------------------------------------------------------------------------
# keycode rendering
# --------------------------------------------------------------------------

HID_MODS = [
    (0x01, "LC"), (0x02, "LS"), (0x04, "LA"), (0x08, "LG"),
    (0x10, "RC"), (0x20, "RS"), (0x40, "RA"), (0x80, "RG"),
]

_KEYCODE_NAMES = None


def keycode_names():
    """packed u32 -> preferred ZMK keycode name, built from the enum."""
    global _KEYCODE_NAMES
    if _KEYCODE_NAMES is None:
        table = {}
        for kc in z.Keycode:
            prev = table.get(kc.value)
            if prev is None or (len(kc.name), kc.name) < (len(prev), prev):
                table[kc.value] = kc.name
        # the enum only knows NUM_x; keymap files conventionally write Nx
        for value, name in list(table.items()):
            m = re.fullmatch(r"NUM_(\d)", name)
            if m:
                table[value] = f"N{m.group(1)}"
        _KEYCODE_NAMES = table
    return _KEYCODE_NAMES


def hid_to_zmk(packed):
    packed = int(packed) & 0xFFFFFFFF
    names = keycode_names()
    if packed in names:
        return names[packed]
    u = decode_hid_usage(packed)
    base = (u["page"] << 16) | u["id"]
    text = names.get(base) or names.get(u["id"]) or f"<page 0x{u['page']:X} id 0x{u['id']:X}>"
    for bit, prefix in HID_MODS:
        if u["modifiers"] & bit:
            text = f"{prefix}({text})"
    return text


BT_CMDS = {0: "BT_CLR", 1: "BT_NXT", 2: "BT_PRV", 3: "BT_SEL", 4: "BT_CLR_ALL", 5: "BT_DISC"}
OUT_CMDS = {0: "OUT_TOG", 1: "OUT_USB", 2: "OUT_BLE", 3: "OUT_NONE"}
EP_CMDS = {0: "EP_OFF", 1: "EP_ON", 2: "EP_TOG"}
SIMPLE = {
    "transparent": "&trans", "none": "&none", "reset": "&sys_reset",
    "bootloader": "&bootloader", "caps word": "&caps_word", "key repeat": "&key_repeat",
    "soft off": "&soft_off", "studio unlock": "&studio_unlock", "grave escape": "&gresc",
}


def norm(name):
    return (name or "").strip().lower()


def param_types(details, pname):
    """Set of value_type strings the metadata allows for param1/param2."""
    types = set()
    for pset in details.get("metadata", []):
        for d in pset.get(pname, []):
            types.add(d.get("value_type"))
    return types


def is_layer_param(details, pname):
    return param_types(details, pname) == {"layer_id"}


def render_binding(b, details):
    """Human-readable ZMK-style text for one binding."""
    name = norm(details.get("display_name") if details else None)
    p1, p2 = b["param1"], b["param2"]
    if b["behavior_id"] == 0:
        return "(未設定)"  # firmware emits id 0 for a NULL binding
    if not details:
        return f"&<id {b['behavior_id']}> {p1} {p2}"
    if name in SIMPLE:
        return SIMPLE[name]
    if name == "key press":
        return f"&kp {hid_to_zmk(p1)}"
    if name == "key toggle":
        return f"&kt {hid_to_zmk(p1)}"
    if name == "sticky key":
        return f"&sk {hid_to_zmk(p1)}"
    if name == "momentary layer":
        return f"&mo {p1}"
    if name == "toggle layer":
        return f"&tog {p1}"
    if name == "to layer":
        return f"&to {p1}"
    if name == "sticky layer":
        return f"&sl {p1}"
    if name == "layer-tap":
        return f"&lt {p1} {hid_to_zmk(p2)}"
    if name == "mod-tap":
        return f"&mt {hid_to_zmk(p1)} {hid_to_zmk(p2)}"
    if name == "bluetooth":
        cmd = BT_CMDS.get(p1, str(p1))
        return f"&bt {cmd} {p2}" if p1 in (3, 5) else f"&bt {cmd}"
    if name == "output selection":
        return f"&out {OUT_CMDS.get(p1, p1)}"
    if name == "external power":
        return f"&ext_power {EP_CMDS.get(p1, p1)}"
    if name == "mouse key press":
        buttons = [f"MB{i + 1}" for i in range(8) if p1 & (1 << i)]
        return "&mkp " + ("|".join(buttons) if buttons else str(p1))
    if name in ("mouse move", "mouse scroll"):
        x = (p1 >> 16) & 0xFFFF
        y = p1 & 0xFFFF
        x = x - 0x10000 if x >= 0x8000 else x
        y = y - 0x10000 if y >= 0x8000 else y
        return f"{'&mmv' if name == 'mouse move' else '&msc'} ({x},{y})"

    parts = [f"&{details['display_name'].strip()}"]
    for pname, value in (("param1", p1), ("param2", p2)):
        types = param_types(details, pname)
        if not types or types == {"nil"}:
            continue
        if "hid_usage" in types and value:
            parts.append(hid_to_zmk(value))
        else:
            parts.append(str(value))
    return " ".join(parts)


# --------------------------------------------------------------------------
# device access
# --------------------------------------------------------------------------

def find_ports():
    return [p for p in list_ports.comports() if p.vid == ZMK_VID]


def choose_port(explicit):
    if explicit:
        return explicit
    ports = find_ports()
    if not ports:
        raise Fail(EXIT_NO_DEVICE,
                   "ZMK キーボードが見つかりません。USB で接続してから再実行してください "
                   "(--port COMx で明示指定も可)。")
    if len(ports) > 1:
        names = ", ".join(p.device for p in ports)
        raise Fail(EXIT_USAGE, f"ZMK デバイスが複数あります ({names})。--port で指定してください。")
    return ports[0].device


class Device:
    def __init__(self, port):
        self.port = port
        try:
            self.client = z.StudioClient.open_serial(port)
        except RuntimeError as exc:
            text = str(exc)
            low = text.lower()
            if "アクセス" in text or "denied" in low or "busy" in low:
                hint = ("  ZMK Studio が接続したままになっています。Studio 側で Save を押し、"
                        "Disconnect (またはタブを閉じる) してから再実行してください。")
                if sys.platform.startswith("linux") and "permission" in low:
                    hint += ("\n  Linux ではシリアルポートの権限不足の可能性もあります "
                             "(例: sudo usermod -aG dialout $USER 後に再ログイン)。")
                raise Fail(EXIT_PORT_BUSY, f"{port} を開けません (他のアプリが使用中)。\n{hint}")
            raise Fail(EXIT_PROTOCOL, f"{port} を開けません: {text}")
        self._behaviors = None

    def lock_state(self):
        return str(self.client.get_lock_state())

    def ensure_unlocked(self):
        state = self.lock_state()
        if "unlock" not in state.lower():
            raise Fail(EXIT_LOCKED,
                       f"キーボードがロックされています ({state})。&studio_unlock に割り当てた"
                       "キーを押すか、ZMK Studio でロック解除してください。")

    def unsaved(self):
        return bool(self.client.check_unsaved_changes())

    def device_info(self):
        return decode_device_info(bytes(self.client.get_device_info_bytes()))

    def keymap(self):
        return decode_keymap(bytes(self.client.get_keymap_bytes()))

    def keymap_raw(self):
        return bytes(self.client.get_keymap_bytes())

    def physical_layouts(self):
        return decode_physical_layouts(bytes(self.client.get_physical_layouts_bytes()))

    def behaviors(self):
        """{id: details dict}; details also carries "raw" bytes."""
        if self._behaviors is None:
            out = {}
            for bid in self.client.list_all_behaviors():
                blob = bytes(self.client.get_behavior_details_bytes(bid))
                det = decode_behavior_details(blob)
                det["raw"] = blob
                out[int(bid)] = det
            self._behaviors = out
        return self._behaviors

    def set_binding(self, layer_id, pos, behavior_id, p1, p2):
        self.client.set_key_at(int(layer_id), int(pos), z.Raw(int(behavior_id), int(p1), int(p2)))

    def save(self):
        self.client.save_changes()

    def discard(self):
        return self.client.discard_changes()

    def safe_discard(self):
        """discard_changes that never masks the error being handled."""
        try:
            self.client.discard_changes()
            return True
        except Exception as exc:  # noqa: BLE001
            warn(f"discard_changes に失敗しました: {exc} — キーボードの電源を入れ直せば"
                 "保存済みの状態に戻ります (フラッシュは無変更)")
            return False


# --------------------------------------------------------------------------
# snapshot model
# --------------------------------------------------------------------------

def capture(dev, note=None):
    """Read everything from the device into a snapshot dict (+ raw blobs)."""
    info = dev.device_info()
    unsaved = dev.unsaved()
    raw_keymap = dev.keymap_raw()
    keymap = decode_keymap(raw_keymap)
    layouts = dev.physical_layouts()
    behaviors = dev.behaviors()

    active = layouts["active_layout_index"]
    if 0 <= active < len(layouts["layouts"]):
        layout = layouts["layouts"][active]
    else:
        warn(f"物理レイアウト index {active} が範囲外です (layouts={len(layouts['layouts'])})")
        layout = {"name": "", "keys": []}

    layers = []
    for index, layer in enumerate(keymap["layers"]):
        bindings = []
        for pos, b in enumerate(layer["bindings"]):
            det = behaviors.get(b["behavior_id"])
            bindings.append({
                "pos": pos,
                "behavior_id": b["behavior_id"],
                "behavior_name": det["display_name"] if det else None,
                "param1": b["param1"],
                "param2": b["param2"],
                "zmk": render_binding(b, det),
            })
        layers.append({"index": index, "id": layer["id"], "name": layer.get("name", ""),
                       "bindings": bindings})

    snap = {
        "schema": SCHEMA,
        "tool": "zmkbak",
        "tool_version": TOOL_VERSION,
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "note": note,
        "port": dev.port,
        "unsaved_changes": unsaved,
        "device": {"name": info.get("name", ""), "serial_hex": info.get("serial_number_hex", "")},
        "physical_layout": {"active_index": active, "name": layout.get("name", ""),
                            "key_count": len(layout.get("keys", [])),
                            "keys": layout.get("keys", [])},
        "keymap": {"available_layers": keymap["available_layers"], "layers": layers},
        "behaviors": {
            str(bid): {
                "display_name": det["display_name"],
                "param1_types": sorted(t for t in param_types(det, "param1") if t),
                "param2_types": sorted(t for t in param_types(det, "param2") if t),
                "metadata": det["metadata"],
            }
            for bid, det in behaviors.items()
        },
    }
    raw = {
        "keymap.bin": raw_keymap,
        "device_info.bin": bytes(dev.client.get_device_info_bytes()),
        "physical_layouts.bin": bytes(dev.client.get_physical_layouts_bytes()),
    }
    for bid, det in behaviors.items():
        raw[f"behaviors/{bid}.bin"] = det["raw"]
    return snap, raw


def snapshot_dir_name(snap):
    stamp = datetime.fromisoformat(snap["captured_at"]).strftime("%Y%m%d-%H%M%S")
    dev = re.sub(r"[^A-Za-z0-9_-]+", "_", snap["device"]["name"] or "device").strip("_")
    return f"{stamp}_{dev}"


def write_snapshot(snap, raw, suffix=None, update_latest=True):
    name = snapshot_dir_name(snap) + (f"_{suffix}" if suffix else "")
    out = SNAPSHOT_ROOT / name
    (out / "raw" / "behaviors").mkdir(parents=True, exist_ok=True)
    (out / "snapshot.json").write_text(json.dumps(snap, indent=2, ensure_ascii=False), encoding="utf-8")
    (out / "keymap.txt").write_text(render_snapshot(snap), encoding="utf-8")
    for rel, blob in raw.items():
        (out / "raw" / rel).write_bytes(blob)
    if update_latest:
        LATEST_FILE.write_text(name + "\n", encoding="utf-8")
    return out


def load_snapshot(ref):
    if ref in ("--latest", "latest"):
        if not LATEST_FILE.exists():
            raise Fail(EXIT_USAGE, "snapshots/LATEST がありません。先に backup を実行してください。")
        ref = LATEST_FILE.read_text(encoding="utf-8").strip()
    path = Path(ref)
    candidates = [path, SNAPSHOT_ROOT / ref, path / "snapshot.json", SNAPSHOT_ROOT / ref / "snapshot.json"]
    for c in candidates:
        if c.is_file() and c.suffix == ".json":
            snap = json.loads(c.read_text(encoding="utf-8"))
            break
    else:
        raise Fail(EXIT_USAGE, f"スナップショットが見つかりません: {ref}")
    if snap.get("schema") != SCHEMA:
        raise Fail(EXIT_INCOMPATIBLE, f"未対応のスナップショット形式 schema={snap.get('schema')}")
    return snap


def list_snapshots():
    if not SNAPSHOT_ROOT.exists():
        return []
    return sorted(p.parent for p in SNAPSHOT_ROOT.glob("*/snapshot.json"))


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def layout_rows(snap):
    """Group key positions into physical rows.

    ZMK physical layouts list keys in reading order, so a new row starts
    wherever x jumps back; this survives column stagger, which makes y
    unreliable. Returns rows of position indices with None marking the
    gap between the two halves of a split board."""
    keys = snap["physical_layout"].get("keys") or []
    if not keys:
        return None
    rows, current = [], []
    for i, k in enumerate(keys):
        if current:
            prev = keys[current[-1]] if current[-1] is not None else keys[current[-2]]
            if k["x"] < prev["x"]:
                rows.append(current)
                current = []
            elif k["x"] - prev["x"] > prev["width"] * 1.6:
                current.append(None)
        current.append(i)
    if current:
        rows.append(current)
    return rows


def render_snapshot(snap):
    lines = [
        f"zmkbak snapshot  {snap['captured_at']}   device={snap['device']['name']} "
        f"serial={snap['device']['serial_hex'] or '-'}",
        f"layout={snap['physical_layout']['name']!r}  keys={snap['physical_layout']['key_count']}  "
        f"layers={len(snap['keymap']['layers'])}  unsaved_changes_at_capture={snap['unsaved_changes']}",
    ]
    if snap.get("note"):
        lines.append(f"note: {snap['note']}")
    lines.append("")
    rows = layout_rows(snap)
    for layer in snap["keymap"]["layers"]:
        lines.append(f"=== layer {layer['index']}  id={layer['id']}  \"{layer['name']}\" ===")
        by_pos = {b["pos"]: b for b in layer["bindings"]}
        if rows:
            for row in rows:
                cells = ["     ||     " if p is None else (by_pos[p]["zmk"] if p in by_pos else "?")
                         for p in row]
                lines.append("  " + " | ".join(f"{c:^14}" for c in cells))
        else:
            for b in layer["bindings"]:
                lines.append(f"  {b['pos']:3d}  {b['zmk']}")
        lines.append("")
        lines.append("  pos  binding                      behavior            raw")
        for b in layer["bindings"]:
            lines.append(f"  {b['pos']:3d}  {b['zmk']:<28} {str(b['behavior_name']):<19} "
                         f"id={b['behavior_id']} p1={b['param1']} p2={b['param2']}")
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# comparison (id-independent)
# --------------------------------------------------------------------------

def layer_index_by_id(snap):
    return {layer["id"]: layer["index"] for layer in snap["keymap"]["layers"]}


def normalized_bindings(snap):
    """{(layer_index, pos): (behavior_name, p1, p2)} with layer-id params
    replaced by layer *index* so two devices with different ids compare equal."""
    idx = layer_index_by_id(snap)
    out = {}
    for layer in snap["keymap"]["layers"]:
        for b in layer["bindings"]:
            det = snap["behaviors"].get(str(b["behavior_id"]), {})
            p1, p2 = b["param1"], b["param2"]
            if det.get("param1_types") == ["layer_id"]:
                p1 = ("L", idx.get(p1, f"?{p1}"))
            if det.get("param2_types") == ["layer_id"]:
                p2 = ("L", idx.get(p2, f"?{p2}"))
            out[(layer["index"], b["pos"])] = (norm(b["behavior_name"]), p1, p2)
    return out


def diff_snapshots(a, b):
    na, nb = normalized_bindings(a), normalized_bindings(b)
    changes = []
    for key in sorted(set(na) | set(nb)):
        if na.get(key) != nb.get(key):
            changes.append((key, na.get(key), nb.get(key)))
    return changes


def describe_binding(snap, layer_index, pos):
    for layer in snap["keymap"]["layers"]:
        if layer["index"] == layer_index:
            for b in layer["bindings"]:
                if b["pos"] == pos:
                    return b["zmk"]
    return "(なし)"


def print_diff(a, b, label_a, label_b):
    changes = diff_snapshots(a, b)
    if not changes:
        say(f"差分なし: {label_a} と {label_b} は一致しています。")
        return changes
    say(f"{len(changes)} 箇所が異なります ({label_a} -> {label_b}):")
    for (li, pos), _, _ in changes:
        say(f"  layer {li} pos {pos:3d}:  {describe_binding(a, li, pos):<28} -> {describe_binding(b, li, pos)}")
    return changes


# --------------------------------------------------------------------------
# restore planning
# --------------------------------------------------------------------------

def resolve_behaviors(snap, used, dev_behaviors, allow_raw_ids):
    """snapshot behavior_id -> device behavior_id, by display name.

    `used` is the set of snapshot behavior ids that will actually be written."""
    by_name = {}
    for bid, det in dev_behaviors.items():
        by_name.setdefault(norm(det["display_name"]), []).append(bid)
    mapping, problems = {}, []
    for old_id in sorted(used):
        det = snap["behaviors"].get(str(old_id))
        name = norm(det["display_name"]) if det else None
        candidates = by_name.get(name, []) if name else []
        if len(candidates) == 1:
            mapping[old_id] = candidates[0]
        elif len(candidates) > 1:
            if old_id in candidates:
                mapping[old_id] = old_id
            else:
                problems.append(f"表示名 '{det['display_name']}' がキーボード側に複数あり、決められません (ids {candidates})")
        elif allow_raw_ids and old_id in dev_behaviors:
            warn(f"'{name or old_id}' を表示名で見つけられず、ID {old_id} をそのまま使います "
                 f"(キーボード側では '{dev_behaviors[old_id]['display_name']}')")
            mapping[old_id] = old_id
        else:
            problems.append(f"ビヘイビア '{det['display_name'] if det else old_id}' (id {old_id}) が"
                            "キーボード側のファームウェアに存在しません")
    return mapping, problems


def plan_restore(snap, dev_snap, dev_behaviors, force, allow_raw_ids):
    """Return (writes, notes). writes = [(layer_id, pos, behavior_id, p1, p2)]."""
    notes = []
    s_layers, d_layers = snap["keymap"]["layers"], dev_snap["keymap"]["layers"]
    s_keys, d_keys = snap["physical_layout"]["key_count"], dev_snap["physical_layout"]["key_count"]

    if snap["physical_layout"]["name"] != dev_snap["physical_layout"]["name"]:
        notes.append(f"物理レイアウトが異なります: '{snap['physical_layout']['name']}' -> "
                     f"'{dev_snap['physical_layout']['name']}'")
    if s_keys != d_keys:
        msg = f"キー数が異なります: スナップショット {s_keys} / キーボード {d_keys}"
        if not force:
            raise Fail(EXIT_INCOMPATIBLE, msg + "  (--force で重なる範囲のみ復元)")
        notes.append(msg + " -> 重なる範囲のみ")
    if len(s_layers) != len(d_layers):
        msg = (f"レイヤー数が異なります: スナップショット {len(s_layers)} / キーボード {len(d_layers)}"
               " (Python API からはレイヤーの追加・削除ができません。ZMK Studio で揃えてください)")
        if not force:
            raise Fail(EXIT_INCOMPATIBLE, msg + "  (--force で重なる範囲のみ復元)")
        notes.append(msg + " -> 重なる範囲のみ")

    for s, d in zip(s_layers, d_layers):
        if s["name"] and d["name"] and s["name"] != d["name"]:
            notes.append(f"layer {s['index']} の名前が違います: '{s['name']}' -> '{d['name']}' "
                         "(名前は復元されません。必要なら Studio で変更)")

    # Only behaviours in the overlapping region need to exist on the target.
    used, null_bindings = set(), 0
    for s_layer, d_layer in zip(s_layers, d_layers):
        d_positions = {b["pos"] for b in d_layer["bindings"]}
        for b in s_layer["bindings"]:
            if b["pos"] not in d_positions:
                continue
            if b["behavior_id"] == 0:
                null_bindings += 1
            else:
                used.add(b["behavior_id"])
    if null_bindings:
        notes.append(f"未設定 (behavior id 0) のキーが {null_bindings} 箇所あり、書き込み対象から外します")

    behavior_map, problems = resolve_behaviors(snap, used, dev_behaviors, allow_raw_ids)
    if problems:
        raise Fail(EXIT_UNRESOLVED, "復元できません:\n  " + "\n  ".join(problems))

    old_layer_ids = [layer["id"] for layer in s_layers]
    new_layer_ids = [layer["id"] for layer in d_layers]
    layer_map = dict(zip(old_layer_ids, new_layer_ids))
    mixed_warned = set()

    def remap_layer_param(value, where):
        if value in layer_map:
            return layer_map[value]
        msg = f"{where}: 参照先レイヤー id {value} がスナップショットに存在しません"
        if force:
            notes.append(msg + " -> そのまま書き込み")
            return value
        raise Fail(EXIT_INCOMPATIBLE, msg)

    writes = []
    for s_layer, d_layer in zip(s_layers, d_layers):
        d_bindings = {b["pos"]: b for b in d_layer["bindings"]}
        for b in s_layer["bindings"]:
            if b["pos"] not in d_bindings or b["behavior_id"] == 0:
                continue
            det = snap["behaviors"].get(str(b["behavior_id"]), {})
            p1, p2 = b["param1"], b["param2"]
            where = f"layer {s_layer['index']} pos {b['pos']} ({b['zmk']})"
            for pname in ("param1", "param2"):
                types = det.get(f"{pname}_types", [])
                if "layer_id" in types and types != ["layer_id"] and det.get("display_name") not in mixed_warned:
                    mixed_warned.add(det.get("display_name"))
                    notes.append(f"'{det.get('display_name')}' の {pname} はレイヤー番号かもしれませんが"
                                 "型が混在しているため付け直さずそのまま書きます")
            if det.get("param1_types") == ["layer_id"]:
                p1 = remap_layer_param(p1, where)
            if det.get("param2_types") == ["layer_id"]:
                p2 = remap_layer_param(p2, where)
            new_id = behavior_map[b["behavior_id"]]
            cur = d_bindings[b["pos"]]
            if (cur["behavior_id"], cur["param1"], cur["param2"]) != (new_id, p1, p2):
                writes.append((d_layer["id"], b["pos"], new_id, p1, p2))
    return writes, notes


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_doctor(args):
    say("=== シリアルポート ===")
    for p in list_ports.comports():
        tag = "  <- ZMK" if p.vid == ZMK_VID else ""
        vid = f"{p.vid:04x}:{p.pid:04x}" if p.vid else "----:----"
        say(f"  {p.device:<8} {vid}  {p.description}{tag}")
    try:
        port = choose_port(args.port)
    except Fail as exc:
        say(f"\n{exc}")
        return exc.code
    say(f"\n=== {port} に接続 ===")
    dev = Device(port)
    say(f"  lock state      : {dev.lock_state()}")
    info = dev.device_info()
    say(f"  device          : {info.get('name')}  serial={info.get('serial_number_hex') or '-'}")
    say(f"  unsaved changes : {dev.unsaved()}")
    km = dev.keymap()
    layouts = dev.physical_layouts()
    idx = layouts["active_layout_index"]
    active = layouts["layouts"][idx] if 0 <= idx < len(layouts["layouts"]) else None
    say(f"  physical layout : {active['name'] if active else '?'}  ({len(active['keys']) if active else '?'} keys)")
    say(f"  layers          : {len(km['layers'])}  (空き {km['available_layers']})")
    for index, layer in enumerate(km["layers"]):
        say(f"    index={index:<2} id={layer['id']:<3} name={layer['name']!r:<14} bindings={len(layer['bindings'])}")
    beh = dev.behaviors()
    say(f"  behaviors       : {len(beh)}")
    say("    " + ", ".join(sorted(d["display_name"] for d in beh.values())))
    return EXIT_OK


def do_backup(dev, note=None, suffix=None, update_latest=True):
    if dev.unsaved():
        warn("キーボードに未保存の変更があります。今見えている作業中の状態を保存しますが、"
             "LATEST は更新しません。確定させるには ZMK Studio で Save してください。")
        update_latest = False
        suffix = f"{suffix}_unsaved" if suffix else "unsaved"
    snap, raw = capture(dev, note)
    out = write_snapshot(snap, raw, suffix=suffix, update_latest=update_latest)
    return snap, out


def cmd_backup(args):
    dev = Device(choose_port(args.port))
    dev.ensure_unlocked()
    snap, out = do_backup(dev, note=args.note)
    n_layers = len(snap["keymap"]["layers"])
    n_keys = snap["physical_layout"]["key_count"]
    say(f"保存しました: {out}")
    say(f"  device={snap['device']['name']}  layout={snap['physical_layout']['name']}  "
        f"layers={n_layers}  keys={n_keys}")
    say("  snapshot.json (構造化データ) / keymap.txt (一覧) / raw/*.bin (生データ)")
    return EXIT_OK


def snapshot_ref(args):
    """Resolve the snapshot argument: --latest flag, or a name/path."""
    if getattr(args, "latest", False):
        return "latest"
    if getattr(args, "snapshot", None):
        return args.snapshot
    raise Fail(EXIT_USAGE, "スナップショットを指定してください (名前 / パス / --latest)")


def cmd_show(args):
    if args.device:
        dev = Device(choose_port(args.port))
        dev.ensure_unlocked()
        snap, _ = capture(dev)
    else:
        snap = load_snapshot(snapshot_ref(args))
    say(render_snapshot(snap))
    return EXIT_OK


def cmd_list(args):
    dirs = list_snapshots()
    if not dirs:
        say("スナップショットはまだありません。")
        return EXIT_OK
    latest = LATEST_FILE.read_text(encoding="utf-8").strip() if LATEST_FILE.exists() else None
    for d in dirs:
        snap = json.loads((d / "snapshot.json").read_text(encoding="utf-8"))
        mark = " (latest)" if d.name == latest else ""
        note = f"  - {snap['note']}" if snap.get("note") else ""
        say(f"  {d.name}  layers={len(snap['keymap']['layers'])}{mark}{note}")
    return EXIT_OK


def cmd_diff(args):
    a = load_snapshot(args.a)
    if args.device or args.b is None:
        dev = Device(choose_port(args.port))
        dev.ensure_unlocked()
        b, _ = capture(dev)
        label_b = "キーボード"
    else:
        b = load_snapshot(args.b)
        label_b = args.b
    changes = print_diff(a, b, args.a, label_b)
    return EXIT_OK if not changes else EXIT_MISMATCH


def cmd_verify(args):
    ref = snapshot_ref(args)
    snap = load_snapshot(ref)
    dev = Device(choose_port(args.port))
    dev.ensure_unlocked()
    live, _ = capture(dev)
    if dev.unsaved():
        warn("キーボードに未保存の変更があります (比較対象は作業中の状態です)")
    changes = print_diff(snap, live, ref, "キーボード")
    return EXIT_OK if not changes else EXIT_MISMATCH


def cli_confirm(writes, changes, live, snap):
    say("")
    answer = input("この内容でキーボードに書き込みますか? [yes/No]: ").strip().lower()
    return answer in ("y", "yes")


def cmd_restore(args):
    ref = snapshot_ref(args)
    snap = load_snapshot(ref)
    dev = Device(choose_port(args.port))
    return do_restore(dev, snap, ref, dry_run=args.dry_run, discard_unsaved=args.discard_unsaved,
                      force=args.force, allow_raw_ids=args.allow_raw_ids,
                      confirm=None if args.yes else cli_confirm)


def do_restore(dev, snap, ref, dry_run=False, discard_unsaved=False, force=False,
               allow_raw_ids=False, confirm=None):
    """Restore `snap` onto `dev`. `confirm(writes, changes, live, snap)` may veto
    the write; None means proceed without asking. Returns an EXIT_* code."""
    dev.ensure_unlocked()

    if dev.unsaved():
        if not discard_unsaved:
            raise Fail(EXIT_UNSAVED,
                       "キーボードに未保存の変更があります。ZMK Studio で Save するか、"
                       "捨ててよければ --discard-unsaved を付けてください。")
        warn("未保存の変更を捨てます (discard_changes)")
        if not dev.safe_discard():
            raise Fail(EXIT_PROTOCOL, "未保存の変更を捨てられませんでした。")

    live, _ = capture(dev)
    if snap["device"]["serial_hex"] and live["device"]["serial_hex"] and \
            snap["device"]["serial_hex"] != live["device"]["serial_hex"]:
        warn(f"別の個体です: snapshot serial={snap['device']['serial_hex']} / "
             f"接続中 serial={live['device']['serial_hex']}")

    writes, notes = plan_restore(snap, live, dev.behaviors(), force, allow_raw_ids)
    for n in notes:
        warn(n)

    say(f"復元元: {ref}  ({snap['captured_at']}"
        f"{', ' + snap['note'] if snap.get('note') else ''})")
    changes = diff_snapshots(live, snap)
    say(f"変更されるキー: {len(writes)} 箇所 (現在との差分 {len(changes)} 箇所)")
    for (li, pos), _, _ in changes[:60]:
        say(f"  layer {li} pos {pos:3d}:  {describe_binding(live, li, pos):<28} -> {describe_binding(snap, li, pos)}")
    if len(changes) > 60:
        say(f"  ... 他 {len(changes) - 60} 箇所")

    if not writes:
        say("キーボードは既にこのスナップショットと一致しています。何もしません。")
        return EXIT_OK

    if dry_run:
        say("\n[dry-run] RAM 上に書き込んで読み戻し確認後、discard します (フラッシュは変更しません)")
    elif confirm is not None and not confirm(writes, changes, live, snap):
        say("中止しました。")
        return EXIT_USAGE

    if not dry_run:
        _, pre_dir = do_backup(dev, note="restore 前の自動バックアップ", suffix="pre-restore",
                               update_latest=False)
        say(f"復元前バックアップ: {pre_dir}")

    def check_writes(keymap):
        actual = {(l["id"], i): (b["behavior_id"], b["param1"], b["param2"])
                  for l in keymap["layers"] for i, b in enumerate(l["bindings"])}
        return [(w, actual.get((w[0], w[1]))) for w in writes if actual.get((w[0], w[1])) != w[2:]]

    say(f"書き込み中 ({len(writes)} 箇所)...")
    try:
        for layer_id, pos, bid, p1, p2 in writes:
            dev.set_binding(layer_id, pos, bid, p1, p2)
    except RuntimeError as exc:
        dev.safe_discard()
        raise Fail(EXIT_PROTOCOL, f"書き込みに失敗したため取り消しました (フラッシュは無変更): {exc}")
    except BaseException:
        dev.safe_discard()
        raise

    bad = check_writes(dev.keymap())
    if bad:
        dev.safe_discard()
        detail = "\n  ".join(f"layer id {w[0]} pos {w[1]}: 期待 {w[2:]} / 実際 {got}" for w, got in bad[:10])
        raise Fail(EXIT_MISMATCH, f"読み戻しが一致しないため取り消しました (フラッシュは無変更):\n  {detail}")
    say("読み戻し確認 OK")

    if dry_run:
        ok = dev.safe_discard()
        still = dev.unsaved()
        say(f"discard 完了。未保存フラグ: {still}")
        return EXIT_OK if ok and not still else EXIT_PROTOCOL

    try:
        dev.save()
    except RuntimeError as exc:
        raise Fail(EXIT_SAVE_FAILED,
                   f"フラッシュへの保存に失敗しました: {exc}\n"
                   "  RAM 上のキーマップは復元済みですが電源を切ると戻ります。"
                   "ZMK Studio で Save を試すか、レイヤー数を減らしてください。")

    # Post-save check: every planned write landed, and the overlapping region
    # matches the snapshot (with --force the non-overlapping part is expected to differ).
    final, _ = capture(dev)
    bad = check_writes(dev.keymap())
    overlap = set(normalized_bindings(snap)) & set(normalized_bindings(final))
    remaining = [c for c in diff_snapshots(snap, final) if c[0] in overlap]
    if bad or remaining:
        raise Fail(EXIT_MISMATCH,
                   f"保存後の確認で不一致があります (書き込み {len(bad)} / 重なり範囲 {len(remaining)} 箇所)。"
                   " `verify` で内容を確認してください。")
    say(f"復元完了。フラッシュに保存しました。 ({len(writes)} 箇所を書き換え)")
    return EXIT_OK


# --------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(prog="zmkbak", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", help="シリアルポート (省略時は VID 0x1d50 の ZMK デバイスを自動検出)")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="接続診断").set_defaults(func=cmd_doctor)
    b = sub.add_parser("backup", help="現在のキーマップを保存")
    b.add_argument("--note", help="メモ (snapshot.json に記録)")
    b.set_defaults(func=cmd_backup)
    def add_snapshot_arg(parser):
        parser.add_argument("snapshot", nargs="?", help="スナップショット名 または パス")
        parser.add_argument("--latest", action="store_true", help="最新のスナップショットを使う")

    s = sub.add_parser("show", help="スナップショット (または --device) を表示")
    add_snapshot_arg(s)
    s.add_argument("--device", action="store_true", help="接続中のキーボードの内容を表示")
    s.set_defaults(func=cmd_show)
    sub.add_parser("list", help="保存済みスナップショット一覧").set_defaults(func=cmd_list)
    d = sub.add_parser("diff", help="2 つを比較 (B 省略時はキーボード)")
    d.add_argument("a", help="スナップショット名 / パス / latest")
    d.add_argument("b", nargs="?", help="比較先スナップショット (省略時はキーボード)")
    d.add_argument("--device", action="store_true", help="キーボードと比較する")
    d.set_defaults(func=cmd_diff)
    v = sub.add_parser("verify", help="キーボードがスナップショットと一致するか")
    add_snapshot_arg(v)
    v.set_defaults(func=cmd_verify)
    r = sub.add_parser("restore", help="スナップショットをキーボードに書き戻す")
    add_snapshot_arg(r)
    r.add_argument("--dry-run", action="store_true", help="RAM 上で試すだけ (保存しない)")
    r.add_argument("--yes", action="store_true", help="確認プロンプトを省略")
    r.add_argument("--discard-unsaved", action="store_true", help="Studio の未保存変更を捨てて続行")
    r.add_argument("--force", action="store_true", help="キー数/レイヤー数が違っても重なる範囲を復元")
    r.add_argument("--allow-raw-ids", action="store_true",
                   help="表示名で解決できないビヘイビアを ID のまま書く (非推奨)")
    r.set_defaults(func=cmd_restore)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except Fail as exc:
        say(f"\nエラー: {exc}")
        return exc.code
    except KeyboardInterrupt:
        say("\n中断しました。")
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
