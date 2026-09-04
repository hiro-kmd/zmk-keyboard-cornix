#!/usr/bin/env python
"""Cornix Keymap Backup - GUI for zmkbak.

One window: keyboard / ZMK Studio status at the top (polled every 2 s),
one-click backup / restore / dry-run / verify, a list of saved snapshots
on the left and the selected snapshot drawn as a keyboard on the right.

    python zmkbak_gui.py
"""

import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import serial  # noqa: E402
import zmkbak as zb  # noqa: E402

POLL_MS = 2000
REPO_ROOT = HERE.parent

STATUS_COLORS = {"ok": "#1f8a4c", "busy": "#c0392b", "none": "#7f8c8d", "working": "#2e6db4"}


def probe_port():
    """-> (state, port, text). state: ok | busy | none."""
    ports = zb.find_ports()
    if not ports:
        return "none", None, "キーボードが見つかりません — USB で接続してください"
    port = ports[0].device
    try:
        s = serial.Serial(port)
        s.close()
    except serial.SerialException as exc:
        text = str(exc)
        if "アクセス" in text or "denied" in text.lower() or "busy" in text.lower():
            return "busy", port, f"{port}: ZMK Studio が接続中です — Studio で Save → Disconnect してください"
        return "busy", port, f"{port}: ポートを開けません ({text[:60]})"
    return "ok", port, f"{port}: 接続可能 (ZMK Studio は切断済み)"


class App:
    def __init__(self, root):
        self.root = root
        root.title("Cornix Keymap Backup")
        root.geometry("1180x760")
        root.minsize(900, 600)

        self.log_queue = queue.Queue()
        self.ui_queue = queue.Queue()   # (callable, args) to run on the Tk thread
        self.busy = False
        self.port_state = "none"
        self.port = None
        self.snapshots = []          # [(dir Path, snap dict)]
        self.current = None          # snap dict being displayed
        self.highlight = set()       # {(layer_index, pos)} to colour in the canvas

        zb.set_output(lambda m: self.log_queue.put(("say", m)),
                      lambda m: self.log_queue.put(("warn", m)))

        self._build()
        self.refresh_snapshots()
        self.root.after(100, self._drain)
        self.root.after(300, self._poll)

    def ui(self, fn, *args):
        """Schedule fn(*args) on the Tk thread. Tkinter is not thread-safe, so
        worker threads must never touch widgets or call root.after directly."""
        self.ui_queue.put((fn, args))

    # ------------------------------------------------------------------ UI
    def _build(self):
        top = ttk.Frame(self.root, padding=(10, 8))
        top.pack(fill="x")
        self.status_dot = tk.Canvas(top, width=16, height=16, highlightthickness=0)
        self.status_dot.pack(side="left")
        self.status_var = tk.StringVar(value="確認中...")
        ttk.Label(top, textvariable=self.status_var, font=("", 10, "bold")).pack(side="left", padx=8)

        bar = ttk.Frame(self.root, padding=(10, 0, 10, 8))
        bar.pack(fill="x")
        self.btn_backup = ttk.Button(bar, text="バックアップ", command=self.on_backup)
        self.btn_backup.pack(side="left")
        ttk.Label(bar, text="メモ:").pack(side="left", padx=(12, 2))
        self.note_var = tk.StringVar()
        ttk.Entry(bar, textvariable=self.note_var, width=32).pack(side="left")
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=12)
        self.btn_restore = ttk.Button(bar, text="選択したものを復元", command=self.on_restore)
        self.btn_restore.pack(side="left")
        self.btn_dry = ttk.Button(bar, text="ドライラン", command=lambda: self.on_restore(dry_run=True))
        self.btn_dry.pack(side="left", padx=(6, 0))
        self.btn_verify = ttk.Button(bar, text="キーボードと比較", command=self.on_verify)
        self.btn_verify.pack(side="left", padx=(6, 0))
        self.btn_live = ttk.Button(bar, text="キーボードの今の配列を表示", command=self.on_show_live)
        self.btn_live.pack(side="left", padx=(6, 0))
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=12)
        self.btn_git = ttk.Button(bar, text="GitHub に保存 (commit & push)", command=self.on_git_push)
        self.btn_git.pack(side="left")
        self.op_buttons = [self.btn_backup, self.btn_restore, self.btn_dry, self.btn_verify,
                           self.btn_live, self.btn_git]

        body = ttk.Panedwindow(self.root, orient="horizontal")
        body.pack(fill="both", expand=True, padx=10)

        left = ttk.Frame(body, padding=(0, 0, 6, 0))
        body.add(left, weight=1)
        ttk.Label(left, text="保存済みスナップショット", font=("", 10, "bold")).pack(anchor="w")
        cols = ("when", "layers", "note")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="browse", height=12)
        self.tree.heading("when", text="日時")
        self.tree.heading("layers", text="層")
        self.tree.heading("note", text="メモ")
        self.tree.column("when", width=150, stretch=False)
        self.tree.column("layers", width=36, anchor="center", stretch=False)
        self.tree.column("note", width=200)
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        lbtn = ttk.Frame(left)
        lbtn.pack(fill="x", pady=(4, 0))
        ttk.Button(lbtn, text="一覧を更新", command=self.refresh_snapshots).pack(side="left")
        ttk.Button(lbtn, text="フォルダを開く", command=self.open_folder).pack(side="left", padx=(6, 0))
        self.btn_delete = ttk.Button(lbtn, text="削除", command=self.on_delete)
        self.btn_delete.pack(side="right")

        right = ttk.Frame(body)
        body.add(right, weight=3)
        self.view_title = tk.StringVar(value="スナップショットを選択してください")
        ttk.Label(right, textvariable=self.view_title, font=("", 10, "bold")).pack(anchor="w")
        self.tabs = ttk.Notebook(right)
        self.tabs.pack(fill="both", expand=True)
        self.canvases = []

        logf = ttk.Frame(self.root, padding=(10, 6, 10, 10))
        logf.pack(fill="both")
        ttk.Label(logf, text="ログ", font=("", 10, "bold")).pack(anchor="w")
        self.log = tk.Text(logf, height=9, wrap="word", state="disabled", font=("Consolas", 9))
        self.log.pack(fill="both", expand=True)
        self.log.tag_configure("warn", foreground="#c0392b")
        self.log.tag_configure("head", foreground="#2e6db4", font=("Consolas", 9, "bold"))

    # ------------------------------------------------------------ status
    def _poll(self):
        if not self.busy:
            threading.Thread(target=self._probe_bg, daemon=True).start()
        self.root.after(POLL_MS, self._poll)

    def _probe_bg(self):
        state, port, text = probe_port()
        self.ui(self._apply_status, state, port, text)

    def _apply_status(self, state, port, text):
        if self.busy:
            return
        self.port_state, self.port = state, port
        self.status_var.set(text)
        self._dot(STATUS_COLORS[state])
        ready = state == "ok"
        for b in (self.btn_backup, self.btn_restore, self.btn_dry, self.btn_verify, self.btn_live):
            b.state(["!disabled"] if ready else ["disabled"])

    def _dot(self, color):
        self.status_dot.delete("all")
        self.status_dot.create_oval(2, 2, 14, 14, fill=color, outline=color)

    def _set_busy(self, busy, text=None):
        self.busy = busy
        for b in self.op_buttons:
            b.state(["disabled"] if busy else ["!disabled"])
        if busy:
            self.status_var.set(text or "処理中...")
            self._dot(STATUS_COLORS["working"])
        else:
            self.root.after(100, lambda: threading.Thread(target=self._probe_bg, daemon=True).start())

    # --------------------------------------------------------------- log
    def _drain(self):
        try:
            while True:
                kind, msg = self.log_queue.get_nowait()
                self.log.configure(state="normal")
                prefix = "[注意] " if kind == "warn" else ""
                self.log.insert("end", prefix + str(msg) + "\n", kind if kind != "say" else ())
                self.log.see("end")
                self.log.configure(state="disabled")
        except queue.Empty:
            pass
        try:
            while True:
                fn, args = self.ui_queue.get_nowait()
                fn(*args)
        except queue.Empty:
            pass
        self.root.after(100, self._drain)

    def head(self, msg):
        self.log_queue.put(("head", f"── {msg}"))

    # ------------------------------------------------------- snapshots
    def refresh_snapshots(self):
        self.snapshots = []
        for d in reversed(zb.list_snapshots()):
            try:
                snap = zb.load_snapshot(str(d))
            except zb.Fail:
                continue
            self.snapshots.append((d, snap))
        latest = zb.LATEST_FILE.read_text(encoding="utf-8").strip() if zb.LATEST_FILE.exists() else None
        self.tree.delete(*self.tree.get_children())
        for d, snap in self.snapshots:
            when = snap["captured_at"].replace("T", " ")
            if d.name == latest:
                when += "  ★"
            self.tree.insert("", "end", iid=d.name, values=(when, len(snap["keymap"]["layers"]), snap.get("note") or ""))
        if self.snapshots and not self.tree.selection():
            first = self.snapshots[0][0].name
            self.tree.selection_set(first)
            self.tree.see(first)

    def selected(self):
        sel = self.tree.selection()
        if not sel:
            return None
        for d, snap in self.snapshots:
            if d.name == sel[0]:
                return d, snap
        return None

    def on_select(self, _event=None):
        item = self.selected()
        if item:
            self.highlight = set()
            self.show_snapshot(item[1], f"{item[0].name}   {item[1].get('note') or ''}")

    def open_folder(self):
        zb.SNAPSHOT_ROOT.mkdir(exist_ok=True)
        if sys.platform == "win32":
            subprocess.Popen(["explorer", str(zb.SNAPSHOT_ROOT)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(zb.SNAPSHOT_ROOT)])
        else:
            subprocess.Popen(["xdg-open", str(zb.SNAPSHOT_ROOT)])

    def on_delete(self):
        item = self.selected()
        if not item:
            return
        d, snap = item
        latest = zb.LATEST_FILE.read_text(encoding="utf-8").strip() if zb.LATEST_FILE.exists() else None
        if d.name == latest:
            messagebox.showinfo("削除", "最新 (★) のスナップショットは削除できません。")
            return
        if not messagebox.askyesno("削除", f"{d.name} を削除しますか?\n(ファイルはゴミ箱に入らず消えます)"):
            return
        import shutil
        shutil.rmtree(d)
        self.head(f"削除しました: {d.name}")
        self.refresh_snapshots()

    # ------------------------------------------------------------ canvas
    def show_snapshot(self, snap, title):
        self.current = snap
        self.view_title.set(title)
        for tab in self.tabs.tabs():
            self.tabs.forget(tab)
        self.canvases = []
        rows = zb.layout_rows(snap)
        keys = snap["physical_layout"].get("keys") or []
        for layer in snap["keymap"]["layers"]:
            frame = ttk.Frame(self.tabs)
            self.tabs.add(frame, text=f"{layer['index']}: {layer['name'] or '(無名)'}")
            canvas = tk.Canvas(frame, background="#f4f5f7", highlightthickness=0)
            canvas.pack(fill="both", expand=True)
            self.canvases.append((canvas, layer, keys, rows))
            canvas.bind("<Configure>", lambda e, c=canvas: self._draw(c))
        if self.canvases:
            self._draw(self.canvases[0][0])

    def _draw(self, canvas):
        entry = next((c for c in self.canvases if c[0] is canvas), None)
        if not entry:
            return
        canvas, layer, keys, rows = entry
        canvas.delete("all")
        w, h = canvas.winfo_width(), canvas.winfo_height()
        if w < 50 or h < 50:
            return
        by_pos = {b["pos"]: b for b in layer["bindings"]}
        if keys:
            max_x = max(k["x"] + k["width"] for k in keys)
            max_y = max(k["y"] + k["height"] for k in keys)
            scale = min((w - 20) / max_x, (h - 20) / max_y)
            ox = (w - max_x * scale) / 2
            oy = (h - max_y * scale) / 2
            geom = [(k["x"], k["y"], k["width"], k["height"]) for k in keys]
        else:
            # no physical layout: fall back to a plain grid of 12 per row
            n = len(by_pos)
            cols = 12
            scale = min((w - 20) / (cols * 100), (h - 20) / (((n + cols - 1) // cols) * 100))
            ox = oy = 10
            geom = [((i % cols) * 100, (i // cols) * 100, 100, 100) for i in range(n)]
        font_size = max(7, int(11 * scale * 100 / 60))
        for pos, (x, y, kw, kh) in enumerate(geom):
            b = by_pos.get(pos)
            x0, y0 = ox + x * scale, oy + y * scale
            x1, y1 = x0 + kw * scale - 3, y0 + kh * scale - 3
            hot = (layer["index"], pos) in self.highlight
            fill = "#ffd6d6" if hot else ("#ffffff" if b else "#e6e8eb")
            outline = "#c0392b" if hot else "#b8bec7"
            canvas.create_rectangle(x0, y0, x1, y1, fill=fill, outline=outline, width=2 if hot else 1)
            if b:
                label = self._short(b["zmk"])
                canvas.create_text((x0 + x1) / 2, (y0 + y1) / 2, text=label, width=(x1 - x0) - 6,
                                   font=("", font_size if len(label) <= 6 else max(6, font_size - 3)),
                                   justify="center")
            canvas.create_text(x0 + 3, y0 + 2, text=str(pos), anchor="nw", fill="#9aa1ab", font=("", 6))

    @staticmethod
    def _short(zmk):
        if zmk.startswith("&kp "):
            return zmk[4:]
        if zmk == "&trans":
            return "▽"
        if zmk == "&none":
            return ""
        return zmk[1:] if zmk.startswith("&") else zmk

    # -------------------------------------------------------- operations
    def _run(self, title, fn, on_done=None):
        if self.busy:
            return
        self._set_busy(True, f"{title}...")
        self.head(title)

        def worker():
            try:
                result = fn()
                err = None
            except zb.Fail as exc:
                result, err = exc.code, f"エラー: {exc}"
            except Exception as exc:  # noqa: BLE001
                result, err = zb.EXIT_PROTOCOL, f"予期しないエラー: {type(exc).__name__}: {exc}"
            self.ui(self._finish, err, result, on_done)

        threading.Thread(target=worker, daemon=True).start()

    def _finish(self, err, result, on_done):
        if err:
            self.log_queue.put(("warn", err))
        self._set_busy(False)
        if on_done:
            on_done(result)

    def _device(self):
        if self.port_state != "ok":
            raise zb.Fail(zb.EXIT_PORT_BUSY, "キーボードに接続できる状態ではありません。上のステータスを確認してください。")
        return zb.Device(self.port)

    def on_backup(self):
        note = self.note_var.get().strip() or None

        def job():
            dev = self._device()
            dev.ensure_unlocked()
            snap, out = zb.do_backup(dev, note=note)
            zb.say(f"保存しました: {out.name}  (layers={len(snap['keymap']['layers'])}, "
                   f"keys={snap['physical_layout']['key_count']})")
            return out.name

        def done(name):
            self.note_var.set("")
            self.refresh_snapshots()
            if isinstance(name, str) and self.tree.exists(name):
                self.tree.selection_set(name)
                self.tree.see(name)
                self.on_select()

        self._run("バックアップ", job, done)

    def on_show_live(self):
        def job():
            dev = self._device()
            dev.ensure_unlocked()
            snap, _ = zb.capture(dev)
            if snap["unsaved_changes"]:
                zb.warn("キーボードに未保存の変更があります (Studio で Save していない状態)")
            self.ui(lambda: (self.tree.selection_remove(self.tree.selection()),
                             self.show_snapshot(snap, "キーボードの今の配列 (未保存)")))
            zb.say("キーボードから読み取りました。")
            return 0

        self._run("キーボードの配列を読み取り", job)

    def on_verify(self):
        item = self.selected()
        if not item:
            messagebox.showinfo("比較", "左の一覧からスナップショットを選んでください。")
            return
        d, snap = item

        def job():
            dev = self._device()
            dev.ensure_unlocked()
            live, _ = zb.capture(dev)
            changes = zb.print_diff(snap, live, d.name, "キーボード")
            hot = {c[0] for c in changes}
            self.ui(self._set_highlight, hot)
            return 0 if not changes else zb.EXIT_MISMATCH

        self._run(f"比較: {d.name} vs キーボード", job)

    def _set_highlight(self, hot):
        self.highlight = hot
        for canvas, *_ in self.canvases:
            self._draw(canvas)

    def on_restore(self, dry_run=False):
        item = self.selected()
        if not item:
            messagebox.showinfo("復元", "左の一覧から復元するスナップショットを選んでください。")
            return
        d, snap = item

        def confirm(writes, changes, live, snap_):
            lines = [f"{len(writes)} 箇所を書き換えてフラッシュに保存します。", ""]
            for (li, pos), _, _ in changes[:12]:
                lines.append(f"layer {li} pos {pos}: {zb.describe_binding(live, li, pos)} → "
                             f"{zb.describe_binding(snap_, li, pos)}")
            if len(changes) > 12:
                lines.append(f"... 他 {len(changes) - 12} 箇所")
            lines += ["", "復元前の配列は自動でバックアップされます。実行しますか?"]
            return self._ask_on_main("復元の確認", "\n".join(lines))

        def job():
            dev = self._device()
            return zb.do_restore(dev, snap, d.name, dry_run=dry_run, confirm=confirm)

        def done(code):
            if not dry_run:
                self.refresh_snapshots()

        self._run(("ドライラン" if dry_run else "復元") + f": {d.name}", job, done)

    def _ask_on_main(self, title, text):
        """Run a yes/no dialog on the Tk thread and wait for the answer."""
        result = {}
        ev = threading.Event()

        def ask():
            result["ok"] = messagebox.askyesno(title, text)
            ev.set()

        self.ui(ask)
        ev.wait()
        return result["ok"]

    def on_git_push(self):
        def job():
            def git(*args):
                proc = subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True,
                                      encoding="utf-8", errors="replace")
                out = (proc.stdout + proc.stderr).strip()
                if out:
                    zb.say(out)
                return proc.returncode

            git("add", "studio-backup/snapshots")
            status = subprocess.run(["git", "status", "--porcelain", "studio-backup/snapshots"],
                                    cwd=REPO_ROOT, capture_output=True, text=True).stdout.strip()
            if not status:
                zb.say("コミットする変更はありません (スナップショットは既に GitHub にあります)。")
                return 0
            if git("commit", "-q", "-m", "keymap snapshot") != 0:
                raise zb.Fail(zb.EXIT_PROTOCOL, "git commit に失敗しました。")
            if git("push") != 0:
                raise zb.Fail(zb.EXIT_PROTOCOL, "git push に失敗しました (ネットワーク / 認証を確認)。")
            zb.say("GitHub に保存しました。")
            return 0

        self._run("GitHub に保存", job)


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista" if sys.platform == "win32" else "clam")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
