#!/usr/bin/env python3
"""
lensmap2lbx_gui.py — tkinter front-end for lensmap2lbx.py

Load any number of lens-map XML files, set the label options, watch a live
preview of the exact print raster, and batch-write .lbx (+ .png preview)
files either next to each XML or into one chosen folder.

Needs lensmap2lbx.py in the same directory (it is imported, not shelled).
Key files: with "auto per-lens keys" on, a file named <stem>.key.json next
to each XML (e.g. XA100845.key.json beside XA100845.XML) is applied on top
of the optional global key file. Precedence: manual maps > per-lens key >
global key > decode formula.

Run:  python3 lensmap2lbx_gui.py
macOS: any python.org or Homebrew Python includes Tk; if import fails,
`brew install python-tk`.
"""

import json
import sys
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import lensmap2lbx as core

try:
    from PIL import ImageTk
    HAVE_IMAGETK = True
except Exception:                                   # preview pane degrades gracefully
    HAVE_IMAGETK = False


# ----------------------------------------------------------------------------
# headless core: settings dict -> rendered label (no tk objects involved)
# ----------------------------------------------------------------------------

SETTING_DEFAULTS = dict(
    channel="iris", tape=18, length=200.0, scale=195.0,
    fit=False, reverse=False, rotate180=False, baseline=True, endstops=True,
    t_prefix=False, iris_tenths=True, tick_width=3, focus_units="in",
    info_mode="auto", info_text="",
    global_key="", auto_keys=True, manual_maps="", label_size=None, rotate_marks=True,
)


def parse_manual_maps(text):
    """'16=2.2, 21=2.8'  ->  {'16': '2.2', '21': '2.8'}"""
    out = {}
    for tok in text.replace(",", " ").split():
        k, _, v = tok.partition("=")
        if not v:
            raise ValueError(f"bad override {tok!r} (expected ENC=TEXT)")
        out[k.strip()] = v.strip()
    return out


def gather_overrides(xml_path, s):
    """Merge global key file, per-lens key file, manual maps (that order)."""
    overrides = {}
    if s["global_key"]:
        overrides.update({str(k): str(v) for k, v in
                          json.load(open(s["global_key"])).items()})
    if s["auto_keys"]:
        per = Path(xml_path).parent / (Path(xml_path).stem + ".key.json")
        if per.exists():
            overrides.update({str(k): str(v) for k, v in
                              json.load(open(per)).items()})
    overrides.update(parse_manual_maps(s["manual_maps"]))
    return overrides


def build_label(xml_path, s, fonts):
    """Parse + render one lens map with settings dict `s`.
    Returns (details, img, marks). Raises ValueError with a readable message."""
    try:
        details, channels = core.parse_lensmap(xml_path)
        ch = s["channel"]
        if ch not in channels or not channels[ch]:
            raise ValueError(f"no <{ch}> marks "
                             f"(has: {', '.join(channels) or 'none'})")
        marks = core.mark_labels(channels[ch], ch, gather_overrides(xml_path, s),
                                 s["focus_units"], iris_tenths=s["iris_tenths"],
                                 t_prefix=s["t_prefix"])
        info = (core.build_info(details, ch, s["scale"])
                if s["info_mode"] == "auto" else s["info_text"])
        img, layout, _ = core.render(
            marks, info, s["tape"], s["length"], s["scale"],
            s["reverse"], s["rotate180"], s["fit"], s["baseline"],
            fonts[0], fonts[1], tick_w=s["tick_width"], endstops=s["endstops"],
            num_h_override=s.get("label_size"), rotate_marks=s["rotate_marks"])
        return details, img, marks, layout
    except SystemExit as e:                          # core uses sys.exit(msg)
        raise ValueError(str(e.code)) from None


def write_outputs(xml_path, dest_dir, s, details, img):
    """Write <stem>_<channel>.lbx + .png into dest_dir. Returns lbx Path."""
    stem = Path(xml_path).stem
    out = Path(dest_dir) / f"{stem}_{s['channel']}.lbx"
    title = (f"{details.get('brand', '')} {details.get('name', '')} "
             f"{details.get('focalLength', '')}mm {s['channel']} scale").strip()
    core.write_lbx(out, img, s["tape"], s["length"], title,
                   f"{stem}_{s['channel']}.png")
    img.save(out.with_suffix(".png"), dpi=(core.DPI, core.DPI))
    return out


# ----------------------------------------------------------------------------
# tkinter app
# ----------------------------------------------------------------------------

class App:
    def __init__(self, root):
        self.root = root
        root.title(f"lensmap2lbx {core.__version__} — Lens Scale Labels")
        root.minsize(880, 560)

        fb, fr = core.find_font(core.FONT_BOLD), core.find_font(core.FONT_REG)
        if not fb or not fr:
            messagebox.showerror("lensmap2lbx",
                                 "No usable TTF font found — install DejaVu or "
                                 "Liberation fonts (see FONT_DIRS in lensmap2lbx.py).")
            sys.exit(1)
        self.fonts = (fb, fr)
        self.preview_img = None                      # keep PhotoImage referenced

        # ---- settings variables --------------------------------------------
        v = SETTING_DEFAULTS
        self.channel = tk.StringVar(value=v["channel"])
        self.tape = tk.IntVar(value=v["tape"])
        self.length = tk.StringVar(value=str(v["length"]))
        self.scale = tk.StringVar(value=str(v["scale"]))
        self.fit = tk.BooleanVar(value=v["fit"])
        self.reverse = tk.BooleanVar(value=v["reverse"])
        self.rotate180 = tk.BooleanVar(value=v["rotate180"])
        self.baseline = tk.BooleanVar(value=v["baseline"])
        self.endstops = tk.BooleanVar(value=v["endstops"])
        self.t_prefix = tk.BooleanVar(value=v["t_prefix"])
        self.iris_tenths = tk.BooleanVar(value=v["iris_tenths"])
        self.tick_width = tk.IntVar(value=v["tick_width"])
        self.focus_units = tk.StringVar(value=v["focus_units"])
        self.info_mode = tk.StringVar(value=v["info_mode"])
        self.info_text = tk.StringVar(value=v["info_text"])
        self.global_key = tk.StringVar(value=v["global_key"])
        self.auto_keys = tk.BooleanVar(value=v["auto_keys"])
        self.manual_maps = tk.StringVar(value=v["manual_maps"])
        self.rotate_marks = tk.BooleanVar(value=v["rotate_marks"])
        self.uniform_size = tk.BooleanVar(value=True)
        self._size_cache = {}                             # (path, settings) -> px
        self.dest_mode = tk.StringVar(value="beside")     # beside | folder
        self.dest_folder = tk.StringVar(value="")

        self._build_ui()

        for var in (self.channel, self.tape, self.length, self.scale, self.fit,
                    self.reverse, self.rotate180, self.baseline, self.endstops,
                    self.t_prefix, self.iris_tenths, self.tick_width,
                    self.focus_units, self.info_mode, self.info_text,
                    self.global_key, self.auto_keys, self.manual_maps,
                    self.uniform_size, self.rotate_marks):
            var.trace_add("write", lambda *_: self.schedule_preview())
        self._preview_job = None

    # ---- UI construction ---------------------------------------------------

    def _build_ui(self):
        pad = dict(padx=4, pady=2)
        top = ttk.Frame(self.root, padding=6)
        top.pack(fill="both", expand=True)
        top.columnconfigure(0, weight=1)
        top.columnconfigure(1, weight=0)
        top.rowconfigure(0, weight=1)

        # -- left: file list --
        left = ttk.LabelFrame(top, text="Lens Map XML Files", padding=4)
        left.grid(row=0, column=0, sticky="nsew", **pad)
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(left, columns=("status",), selectmode="browse")
        self.tree.heading("#0", text="File")
        self.tree.heading("status", text="Status")
        self.tree.column("#0", width=300, anchor="w")
        self.tree.column("status", width=170, anchor="w")
        self.tree.grid(row=0, column=0, columnspan=4, sticky="nsew")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.schedule_preview())
        ttk.Button(left, text="Add…", command=self.add_files).grid(row=1, column=0, sticky="ew", **pad)
        ttk.Button(left, text="Remove", command=self.remove_selected).grid(row=1, column=1, sticky="ew", **pad)
        ttk.Button(left, text="Clear", command=self.clear_files).grid(row=1, column=2, sticky="ew", **pad)
        ttk.Label(left, text="").grid(row=1, column=3, sticky="ew")

        # -- destination --
        dest = ttk.LabelFrame(left, text="Destination for .lbx / .png", padding=4)
        dest.grid(row=2, column=0, columnspan=4, sticky="ew", **pad)
        dest.columnconfigure(2, weight=1)
        ttk.Radiobutton(dest, text="Next to Each XML", value="beside",
                        variable=self.dest_mode).grid(row=0, column=0, sticky="w", **pad)
        ttk.Radiobutton(dest, text="Folder:", value="folder",
                        variable=self.dest_mode).grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(dest, textvariable=self.dest_folder).grid(row=1, column=2, sticky="ew", **pad)
        ttk.Button(dest, text="Choose…", command=self.pick_dest).grid(row=1, column=3, **pad)

        # -- right: settings --
        s = ttk.LabelFrame(top, text="Label Settings", padding=4)
        s.grid(row=0, column=1, sticky="nsew", **pad)
        r = 0
        ttk.Label(s, text="Channel").grid(row=r, column=0, sticky="w", **pad)
        ttk.Combobox(s, textvariable=self.channel, values=("iris", "focus"),
                     state="readonly", width=8).grid(row=r, column=1, sticky="w", **pad)
        ttk.Label(s, text="Tape (mm)").grid(row=r, column=2, sticky="w", **pad)
        ttk.Combobox(s, textvariable=self.tape, values=sorted(core.TAPES),
                     state="readonly", width=5).grid(row=r, column=3, sticky="w", **pad); r += 1
        ttk.Label(s, text="Label Length (mm)").grid(row=r, column=0, sticky="w", **pad)
        ttk.Entry(s, textvariable=self.length, width=8).grid(row=r, column=1, sticky="w", **pad)
        ttk.Label(s, text="Scale Span (mm)").grid(row=r, column=2, sticky="w", **pad)
        ttk.Entry(s, textvariable=self.scale, width=8).grid(row=r, column=3, sticky="w", **pad); r += 1
        ttk.Label(s, text="Tick Width (dots)").grid(row=r, column=0, sticky="w", **pad)
        ttk.Spinbox(s, from_=1, to=6, textvariable=self.tick_width,
                    width=5, state="readonly").grid(row=r, column=1, sticky="w", **pad)
        ttk.Label(s, text="Focus Units").grid(row=r, column=2, sticky="w", **pad)
        ttk.Combobox(s, textvariable=self.focus_units, values=("in", "cm"),
                     state="readonly", width=5).grid(row=r, column=3, sticky="w", **pad); r += 1

        checks = [("Fit Min→Max Marks (Ignore Absolute Travel)", self.fit),
                  ("Reverse (Mirror Scale)", self.reverse),
                  ("Rotate 180°", self.rotate180),
                  ("Baseline Along Edge", self.baseline),
                  ("Mark End Stops", self.endstops),
                  ("T Prefix on Iris Numbers", self.t_prefix),
                  ("Iris as Tenths of a Stop (2 3/10)", self.iris_tenths),
                  ("Vertical Mark Numbers (Centered on Ticks)", self.rotate_marks),
                  ("Uniform Mark Size Across Files", self.uniform_size)]
        for text, var in checks:
            ttk.Checkbutton(s, text=text, variable=var).grid(
                row=r, column=0, columnspan=4, sticky="w", **pad); r += 1

        ttk.Separator(s).grid(row=r, column=0, columnspan=4, sticky="ew", pady=4); r += 1
        ttk.Label(s, text="Info Line").grid(row=r, column=0, sticky="w", **pad)
        ttk.Radiobutton(s, text="Auto", value="auto",
                        variable=self.info_mode).grid(row=r, column=1, sticky="w", **pad)
        ttk.Radiobutton(s, text="Custom:", value="custom",
                        variable=self.info_mode).grid(row=r, column=2, sticky="w", **pad); r += 1
        ttk.Entry(s, textvariable=self.info_text).grid(
            row=r, column=0, columnspan=4, sticky="ew", **pad); r += 1

        ttk.Separator(s).grid(row=r, column=0, columnspan=4, sticky="ew", pady=4); r += 1
        ttk.Label(s, text="Global Key File").grid(row=r, column=0, sticky="w", **pad)
        ttk.Entry(s, textvariable=self.global_key).grid(
            row=r, column=1, columnspan=2, sticky="ew", **pad)
        ttk.Button(s, text="…", width=3, command=self.pick_key).grid(row=r, column=3, **pad); r += 1
        ttk.Checkbutton(s, text="Auto Per-Lens Keys (<stem>.key.json beside XML)",
                        variable=self.auto_keys).grid(row=r, column=0, columnspan=4,
                                                      sticky="w", **pad); r += 1
        ttk.Label(s, text="Manual Maps (ENC=TEXT …)").grid(row=r, column=0,
                                                           columnspan=2, sticky="w", **pad)
        ttk.Entry(s, textvariable=self.manual_maps).grid(
            row=r, column=2, columnspan=2, sticky="ew", **pad); r += 1

        ttk.Button(s, text="Generate All", command=self.generate_all).grid(
            row=r, column=0, columnspan=2, sticky="ew", padx=4, pady=8)
        ttk.Button(s, text="Generate Selected", command=self.generate_selected).grid(
            row=r, column=2, columnspan=2, sticky="ew", padx=4, pady=8)

        # -- bottom: preview + status --
        bot = ttk.LabelFrame(self.root, text="Preview (Exact Print Raster, Scaled to Fit)",
                             padding=4)
        bot.pack(fill="x", padx=6, pady=(0, 6))
        self.canvas = tk.Canvas(bot, height=150, background="#d0d0d0",
                                highlightthickness=0)
        self.canvas.pack(fill="x")
        self.canvas.bind("<Configure>", lambda e: self.schedule_preview())
        self.status = ttk.Label(self.root, anchor="w", padding=(8, 2))
        self.status.pack(fill="x")
        if not HAVE_IMAGETK:
            self.status.config(text="Pillow ImageTk missing — preview disabled "
                                    "(generation still works).")

    # ---- helpers -----------------------------------------------------------

    def settings(self):
        try:
            length, scale = float(self.length.get()), float(self.scale.get())
        except ValueError:
            raise ValueError("length / scale must be numbers")
        return dict(channel=self.channel.get(), tape=int(self.tape.get()),
                    length=length, scale=scale, fit=self.fit.get(),
                    reverse=self.reverse.get(), rotate180=self.rotate180.get(),
                    baseline=self.baseline.get(), endstops=self.endstops.get(),
                    t_prefix=self.t_prefix.get(), iris_tenths=self.iris_tenths.get(),
                    tick_width=int(self.tick_width.get()),
                    focus_units=self.focus_units.get(),
                    info_mode=self.info_mode.get(), info_text=self.info_text.get(),
                    global_key=self.global_key.get().strip(),
                    auto_keys=self.auto_keys.get(),
                    manual_maps=self.manual_maps.get(), label_size=None,
                    rotate_marks=self.rotate_marks.get())

    def files(self):
        return [self.tree.item(i, "text") for i in self.tree.get_children()]

    def add_files(self):
        for p in filedialog.askopenfilenames(
                title="Add Lens Map XML Files",
                filetypes=[("Lens map XML", "*.xml *.XML"), ("All files", "*")]):
            if p not in self.files():
                self.tree.insert("", "end", text=p, values=("",))
        kids = self.tree.get_children()
        if kids and not self.tree.selection():
            self.tree.selection_set(kids[0])
        self.schedule_preview()

    def remove_selected(self):
        for i in self.tree.selection():
            self.tree.delete(i)
        self.schedule_preview()

    def clear_files(self):
        self.tree.delete(*self.tree.get_children())
        self.schedule_preview()

    def pick_dest(self):
        d = filedialog.askdirectory(title="Destination Folder")
        if d:
            self.dest_folder.set(d)
            self.dest_mode.set("folder")

    def pick_key(self):
        p = filedialog.askopenfilename(title="Global Key JSON",
                                       filetypes=[("JSON", "*.json"), ("All files", "*")])
        if p:
            self.global_key.set(p)

    def apply_uniform(self, s):
        """When Uniform Mark Size is on, pin label_size to the smallest size
        the auto-fit would pick across all loaded files."""
        if not self.uniform_size.get():
            return s
        probe = dict(s, label_size=None)
        pkey = tuple(sorted((k, v) for k, v in probe.items()))
        sizes = []
        for p in self.files():
            key = (p, pkey)
            if key not in self._size_cache:
                try:
                    *_, layout = build_label(p, probe, self.fonts)
                    self._size_cache[key] = layout["num_h"]
                except Exception:
                    self._size_cache[key] = None
            if self._size_cache[key]:
                sizes.append(self._size_cache[key])
        return dict(s, label_size=min(sizes)) if sizes else s

    def selected_file(self):
        sel = self.tree.selection()
        if sel:
            return self.tree.item(sel[0], "text")
        kids = self.tree.get_children()
        return self.tree.item(kids[0], "text") if kids else None

    # ---- preview -----------------------------------------------------------

    def schedule_preview(self):
        if self._preview_job:
            self.root.after_cancel(self._preview_job)
        self._preview_job = self.root.after(180, self.refresh_preview)

    def refresh_preview(self):
        self._preview_job = None
        path = self.selected_file()
        self.canvas.delete("all")
        if not path:
            self.status.config(text="Add lens map XML files to begin.")
            return
        try:
            s = self.apply_uniform(self.settings())
            details, img, marks, layout = build_label(path, s, self.fonts)
        except Exception as e:
            self.status.config(text=f"{Path(path).name}: {e}")
            return
        mm_w = img.width / core.DPMM
        self.status.config(
            text=f"{Path(path).name} — {len(marks)} marks, {img.width}×{img.height}px "
                 f"({mm_w:.1f} × {img.height / core.DPMM:.1f} mm printable) "
                 f"on {s['tape']} mm tape, mark size {layout['num_h']} px")
        if not HAVE_IMAGETK:
            return
        cw = max(self.canvas.winfo_width(), 50)
        ch = max(self.canvas.winfo_height(), 50)
        f = min((cw - 12) / img.width, (ch - 12) / img.height, 1.0)
        disp = img.resize((max(1, round(img.width * f)),
                           max(1, round(img.height * f))))
        self.preview_img = ImageTk.PhotoImage(disp)
        self.canvas.create_image(cw // 2, ch // 2, image=self.preview_img)

    # ---- generation --------------------------------------------------------

    def generate(self, paths):
        if not paths:
            messagebox.showinfo("lensmap2lbx", "No files to generate.")
            return
        try:
            s = self.settings()
        except ValueError as e:
            messagebox.showerror("lensmap2lbx", str(e))
            return
        s = self.apply_uniform(s)
        if s["fit"] and s["endstops"]:
            pass                                    # core warns per-file if out of range
        if self.dest_mode.get() == "folder":
            if not self.dest_folder.get().strip():
                messagebox.showerror("lensmap2lbx", "Choose a destination folder "
                                     "(or select 'Next to each XML').")
                return
            dest = Path(self.dest_folder.get())
            try:
                dest.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                messagebox.showerror("lensmap2lbx", f"Cannot create destination:\n{e}")
                return
        ok = err = 0
        for item in self.tree.get_children():
            path = self.tree.item(item, "text")
            if path not in paths:
                continue
            try:
                details, img, _, _ = build_label(path, s, self.fonts)
                d = Path(path).parent if self.dest_mode.get() == "beside" \
                    else Path(self.dest_folder.get())
                out = write_outputs(path, d, s, details, img)
                self.tree.set(item, "status", f"✓ {out.name}")
                ok += 1
            except Exception as e:
                self.tree.set(item, "status", f"✗ {e}")
                err += 1
            self.root.update_idletasks()
        self.status.config(text=f"Generated {ok} label(s)"
                                + (f", {err} failed — see Status column" if err else "."))

    def generate_all(self):
        self.generate(self.files())

    def generate_selected(self):
        f = self.selected_file()
        self.generate([f] if f else [])


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("clam")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
