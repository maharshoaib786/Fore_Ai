from __future__ import annotations
import threading
import queue
import tkinter as tk
from tkinter import ttk, messagebox
import subprocess
import tempfile
import os
import sys
import hashlib
import json
import logging
import time
from datetime import datetime

# Ensure local folder on sys.path to support folder with spaces
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)
# Also add app root (parent) to import path
_APP_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _APP_ROOT not in sys.path:
    sys.path.insert(0, _APP_ROOT)

import fore_ai_bot as Bot
from signal_parser import ParsedSignal


class TkTextHandler(logging.Handler):
    def __init__(self, log_queue: queue.Queue):
        super().__init__()
        self.log_queue = log_queue
    def emit(self, record):
        try:
            msg = self.format(record)
            self.log_queue.put((record.levelno, msg))
        except Exception:
            pass


class ForeAiDashboard(tk.Tk):
    def __init__(self):
        super().__init__()
        # Paths and prefs
        self._root_dir = os.path.dirname(os.path.abspath(__file__))
        self.prefs_path = os.path.join(self._root_dir, "dashboard_prefs.json")
        self.prefs = self._load_prefs()

        # Versioning: auto-bump minor when source changes are detected
        self.app_version = "1.00"
        self.build_date = datetime.now().strftime("%Y-%m-%d")
        try:
            self._init_versioning()
        except Exception:
            # Fail-safe: keep defaults if any issue
            pass
        self.title(f"Fore_Ai v{self.app_version} (Build {self.build_date})")
        self.geometry("1024x640")
        self.minsize(900, 560)

        # Background
        self.telegram_thread_started = False
        self.log_queue: queue.Queue = queue.Queue()
        self._attach_logger()

        self._build_layout()
        self._restore_layout()
        self.after(250, self._drain_log_queue)
        self.after(1000, self._tick)
        # Initialize fixed lot into bot
        try:
            self._apply_fixed_lot()
        except Exception:
            pass
        # Initialize auto-place into bot
        try:
            self._apply_auto_place()
        except Exception:
            pass

    def _load_prefs(self) -> dict:
        try:
            if os.path.exists(self.prefs_path):
                with open(self.prefs_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            # Legacy location (before rename): try reading from old folder once
            legacy_dir = os.path.abspath(os.path.join(self._root_dir, "..", "Mazhar Bot"))
            legacy_path = os.path.join(legacy_dir, "dashboard_prefs.json")
            if os.path.exists(legacy_path):
                with open(legacy_path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    # --- Versioning helpers ---
    def _init_versioning(self):
        """Load current version, detect code changes, and bump minor version automatically.
        Stores version in VERSION.txt and meta (last hash + build date) in version_meta.json.
        """
        version_file = os.path.join(self._root_dir, "VERSION.txt")
        meta_file = os.path.join(self._root_dir, "version_meta.json")

        # Load current version
        version = self._read_version_file(version_file) or self.app_version

        # Load previous meta
        last_hash = None
        last_build_date = None
        try:
            if os.path.exists(meta_file):
                with open(meta_file, "r", encoding="utf-8") as f:
                    m = json.load(f)
                    last_hash = m.get("last_hash")
                    last_build_date = m.get("build_date")
        except Exception:
            pass

        # Compute current hash of source files
        cur_hash = self._compute_source_hash()

        # Handle first run (no meta) and bumps on change
        if last_hash is None:
            # First run: do not bump, just record current hash and keep version
            try:
                with open(meta_file, "w", encoding="utf-8") as f:
                    json.dump({"last_hash": cur_hash, "build_date": self.build_date}, f, indent=2)
            except Exception:
                pass
        elif last_hash != cur_hash:
            # Source changed: bump version and update build date
            version = self._bump_version(version)
            self.build_date = datetime.now().strftime("%Y-%m-%d")
            # persist version + meta
            self._write_version_file(version_file, version)
            try:
                with open(meta_file, "w", encoding="utf-8") as f:
                    json.dump({"last_hash": cur_hash, "build_date": self.build_date}, f, indent=2)
            except Exception:
                pass
        else:
            # No change: keep prior build date if available
            if last_build_date:
                self.build_date = last_build_date

        self.app_version = version

    def _compute_source_hash(self) -> str:
        """Compute a stable hash of Python source files in this folder (excluding __pycache__)."""
        h = hashlib.sha256()
        try:
            for root, dirs, files in os.walk(self._root_dir):
                # Skip cache directories
                dirs[:] = [d for d in dirs if d != "__pycache__"]
                for fn in sorted(files):
                    if not fn.endswith(".py"):
                        continue
                    try:
                        p = os.path.join(root, fn)
                        with open(p, "rb") as f:
                            while True:
                                chunk = f.read(8192)
                                if not chunk:
                                    break
                                h.update(chunk)
                    except Exception:
                        # Ignore unreadable files
                        pass
        except Exception:
            pass
        return h.hexdigest()

    def _read_version_file(self, path: str) -> str | None:
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    s = f.read().strip()
                    return s if s else None
        except Exception:
            pass
        return None

    def _write_version_file(self, path: str, version: str):
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(version)
        except Exception:
            pass

    def _bump_version(self, v: str) -> str:
        """Increment minor by 1 in format M.mm (e.g., 1.00 -> 1.01). Wraps at 1.99 -> 2.00."""
        try:
            if "." in v:
                major_str, minor_str = v.split(".", 1)
                major = int(major_str)
                # keep exactly two digits for minor; non-numeric fallback handled by except
                minor = int(minor_str)
            else:
                major, minor = int(v), 0
            minor += 1
            if minor >= 100:
                major += 1
                minor = 0
            return f"{major}.{minor:02d}"
        except Exception:
            # Fallback: reset to 1.00 if unexpected format
            return "1.00"

    def _attach_logger(self):
        h = TkTextHandler(self.log_queue)
        h.setLevel(logging.INFO)
        h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        Bot.log.addHandler(h)

    def _drain_log_queue(self):
        try:
            while True:
                level, msg = self.log_queue.get_nowait()
                self.txt_log.configure(state="normal")
                tag = "INFO" if level < logging.WARNING else ("WARN" if level < logging.ERROR else "ERROR")
                self.txt_log.insert("end", msg + "\n", (tag,))
                self.txt_log.see("end")
                self.txt_log.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(250, self._drain_log_queue)

    def _build_layout(self):
        self.style = ttk.Style(self)
        self.style.theme_use("clam")

        # Tabs
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True)
        tab_main = ttk.Frame(self.notebook)
        tab_cfg = ttk.Frame(self.notebook)
        self.notebook.add(tab_main, text="Dashboard")
        self.notebook.add(tab_cfg, text="Configuration")

        wrap = ttk.Frame(tab_main, padding=10)
        wrap.pack(fill="both", expand=True)

        # Controls
        controls = ttk.LabelFrame(wrap, text="Controls", padding=8)
        controls.pack(fill="x")
        ttk.Button(controls, text="Start Bot", command=self._start_bot).pack(side="left")
        ttk.Button(controls, text="Stop Bot", command=self._stop_bot).pack(side="left", padx=6)
        ttk.Button(controls, text="Place Orders", command=self._place_orders).pack(side="left", padx=6)
        ttk.Button(controls, text="Close All Pending", command=self._cancel_pending_orders).pack(side="left", padx=6)
        ttk.Button(controls, text="Close All Positions", command=self._close_all_positions).pack(side="left", padx=6)

        # Status block
        statusf = ttk.LabelFrame(wrap, text="Status", padding=8)
        statusf.pack(fill="x", pady=(8, 8))
        sgrid = ttk.Frame(statusf)
        sgrid.pack(anchor="w")
        self.var_mt5 = tk.StringVar(value="No")
        self.var_tg = tk.StringVar(value="No")
        self.var_tg_name = tk.StringVar(value="-")
        ttk.Label(sgrid, text="MT5 Connected", width=16, anchor="w").grid(row=0, column=0, sticky="w")
        ttk.Label(sgrid, textvariable=self.var_mt5, width=18, anchor="w").grid(row=0, column=1, sticky="w")
        ttk.Label(sgrid, text="Telegram Connected", width=16, anchor="w").grid(row=1, column=0, sticky="w")
        ttk.Label(sgrid, textvariable=self.var_tg, width=18, anchor="w").grid(row=1, column=1, sticky="w")
        ttk.Label(sgrid, text="Telegram Chat", width=16, anchor="w").grid(row=2, column=0, sticky="w")
        ttk.Label(sgrid, textvariable=self.var_tg_name, width=40, anchor="w").grid(row=2, column=1, sticky="w")

        # Signal view
        sigf = ttk.LabelFrame(wrap, text="Last Parsed Signal", padding=8)
        sigf.pack(fill="x", pady=(8, 8))
        grid = ttk.Frame(sigf)
        grid.pack(anchor="w")

        self.var_sym = tk.StringVar(value="-")
        self.var_dir = tk.StringVar(value="-")
        self.var_lot = tk.StringVar(value="-")
        self.var_zone = tk.StringVar(value="-")
        self.var_sl = tk.StringVar(value="-")
        self.var_tps = tk.StringVar(value="-")

        def kv(r, k, v):
            ttk.Label(grid, text=f"{k}:", width=16, anchor="w").grid(row=r, column=0, sticky="w", pady=2)
            ttk.Label(grid, textvariable=v, width=60, anchor="w").grid(row=r, column=1, sticky="w", pady=2)

        kv(0, "Symbol", self.var_sym)
        kv(1, "Direction", self.var_dir)
        kv(2, "Lot Size", self.var_lot)
        kv(3, "Zone", self.var_zone)
        kv(4, "Stop Loss", self.var_sl)
        kv(5, "TPs", self.var_tps)

        # Log
        logf = ttk.LabelFrame(wrap, text="Log", padding=8)
        logf.pack(fill="both", expand=True)
        self.txt_log = tk.Text(logf, wrap="word")
        self.txt_log.pack(fill="both", expand=True)
        self.txt_log.tag_configure("INFO", foreground="#2e3b4e")
        self.txt_log.tag_configure("WARN", foreground="#b58900")
        self.txt_log.tag_configure("ERROR", foreground="#dc322f")

        # Build configuration tab
        self._build_config_tab(tab_cfg)
        # Build update tab
        tab_upd = ttk.Frame(self.notebook)
        self.notebook.add(tab_upd, text="Software Update")
        self._build_update_tab(tab_upd)

    def _build_config_tab(self, parent):
        frame = ttk.Frame(parent, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Account & Telegram", font=("Segoe UI", 12, "bold")).pack(anchor="w", pady=(0, 8))

        grid = ttk.Frame(frame)
        grid.pack(anchor="w")

        self.acc_vars = {
            "MT5_LOGIN": tk.StringVar(),
            "MT5_PASSWORD": tk.StringVar(),
            "MT5_SERVER": tk.StringVar(),
            "TELEGRAM_TOKEN": tk.StringVar(),
            "TELEGRAM_CHANNEL_ID": tk.StringVar(),
            "SYMBOL_SUFFIX": tk.StringVar(),
        }

        ttk.Label(grid, text="MT5 Login", width=22, anchor="w").grid(row=0, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.acc_vars["MT5_LOGIN"], width=28).grid(row=0, column=1, pady=2)
        ttk.Label(grid, text="MT5 Password", width=22, anchor="w").grid(row=1, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.acc_vars["MT5_PASSWORD"], width=28, show="*").grid(row=1, column=1, pady=2)
        ttk.Label(grid, text="MT5 Server", width=22, anchor="w").grid(row=2, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.acc_vars["MT5_SERVER"], width=28).grid(row=2, column=1, pady=2)

        ttk.Label(grid, text="Telegram Bot Token", width=22, anchor="w").grid(row=3, column=0, sticky="w", pady=(8,2))
        ttk.Entry(grid, textvariable=self.acc_vars["TELEGRAM_TOKEN"], width=42).grid(row=3, column=1, pady=(8,2))
        ttk.Label(grid, text="Telegram Chat ID", width=22, anchor="w").grid(row=4, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.acc_vars["TELEGRAM_CHANNEL_ID"], width=42).grid(row=4, column=1, pady=2)

        ttk.Label(grid, text="Symbol Suffix", width=22, anchor="w").grid(row=5, column=0, sticky="w", pady=(8,2))
        ttk.Entry(grid, textvariable=self.acc_vars["SYMBOL_SUFFIX"], width=12).grid(row=5, column=1, sticky="w", pady=(8,2))

        btns = ttk.Frame(frame)
        btns.pack(anchor="w", pady=(10, 0))
        ttk.Button(btns, text="Apply", command=self._apply_account).pack(side="left")
        ttk.Button(btns, text="Save to .env", command=self._save_account_env).pack(side="left", padx=6)
        ttk.Button(btns, text="Restart Telegram", command=self._restart_telegram).pack(side="left", padx=6)

        # Load existing values into UI
        self._load_account_to_vars()

        # Trade Settings (moved from Dashboard)
        ttk.Label(frame, text="Trade Settings", font=("Segoe UI", 12, "bold")).pack(anchor="w", pady=(16, 6))
        ts = ttk.LabelFrame(frame, text="Trade Settings", padding=8)
        ts.pack(fill="x")
        ttk.Label(ts, text="Default Lot (when missing)", width=28, anchor="w").grid(row=0, column=0, sticky="w")
        self.var_fixed_lot = tk.StringVar(value=str(self.prefs.get("fixed_lot", "0.10")))
        lot_entry = ttk.Entry(ts, textvariable=self.var_fixed_lot, width=12)
        lot_entry.grid(row=0, column=1, sticky="w")
        ttk.Button(ts, text="Save Lot", command=self._apply_fixed_lot).grid(row=0, column=2, sticky="w", padx=(6,0))
        # Auto place toggle
        self.var_auto_place = tk.BooleanVar(value=bool(self.prefs.get("auto_place", False)))
        ttk.Checkbutton(ts, text="Auto place on signal", variable=self.var_auto_place, command=self._apply_auto_place).grid(row=1, column=1, sticky="w", pady=(6,0))

    def _load_account_to_vars(self):
        try:
            def setv(k, v):
                if k in self.acc_vars:
                    self.acc_vars[k].set("" if v is None else str(v))
            setv("MT5_LOGIN", getattr(Bot, "LOGIN", None))
            setv("MT5_PASSWORD", getattr(Bot, "PASSWORD", None))
            setv("MT5_SERVER", getattr(Bot, "SERVER", None))
            setv("TELEGRAM_TOKEN", getattr(Bot, "TELEGRAM_TOKEN", ""))
            setv("TELEGRAM_CHANNEL_ID", getattr(Bot, "TELEGRAM_CHANNEL_ID", ""))
            setv("SYMBOL_SUFFIX", getattr(Bot, "SYMBOL_SUFFIX", ""))
        except Exception:
            pass

    def _apply_account(self):
        try:
            login_txt = self.acc_vars["MT5_LOGIN"].get().strip()
            login_val = int(login_txt) if login_txt else None
            vals = {
                "LOGIN": login_val,
                "PASSWORD": self.acc_vars["MT5_PASSWORD"].get(),
                "SERVER": self.acc_vars["MT5_SERVER"].get(),
                "TELEGRAM_TOKEN": self.acc_vars["TELEGRAM_TOKEN"].get(),
                "TELEGRAM_CHANNEL_ID": self.acc_vars["TELEGRAM_CHANNEL_ID"].get(),
                "SYMBOL_SUFFIX": self.acc_vars["SYMBOL_SUFFIX"].get(),
            }
            Bot.apply_config(vals)
            messagebox.showinfo("Configuration", "Settings applied. Restart Telegram to use new token.")
        except ValueError as e:
            messagebox.showerror("Configuration", f"Login must be a number.\n\n{e}")

    def _restart_telegram(self):
        try:
            Bot.stop_telegram()
        except Exception:
            pass
        def runner():
            try:
                # small delay to allow clean stop
                time.sleep(0.7)
                Bot.run_telegram_bot()
            except Exception:
                pass
        t = threading.Thread(target=runner, daemon=True)
        t.start()
        self.telegram_thread_started = True
        Bot.log.info("Dashboard: Telegram restarted.")

    def _save_account_env(self):
        # Always save .env into the same folder as this dashboard/bot
        env_path = os.path.join(self._root_dir, ".env")
        existing = {}
        if os.path.exists(env_path):
            try:
                with open(env_path, "r", encoding="utf-8") as f:
                    for line in f:
                        if not line.strip() or line.strip().startswith("#") or "=" not in line:
                            continue
                        k, v = line.split("=", 1)
                        existing[k.strip()] = v.strip()
            except Exception:
                pass
        updates = {
            "MT5_LOGIN": self.acc_vars["MT5_LOGIN"].get().strip(),
            "MT5_PASSWORD": self.acc_vars["MT5_PASSWORD"].get(),
            "MT5_SERVER": self.acc_vars["MT5_SERVER"].get(),
            "TELEGRAM_TOKEN": self.acc_vars["TELEGRAM_TOKEN"].get(),
            "TELEGRAM_CHANNEL_ID": self.acc_vars["TELEGRAM_CHANNEL_ID"].get(),
            "SYMBOL_SUFFIX": self.acc_vars["SYMBOL_SUFFIX"].get(),
        }
        existing.update(updates)
        try:
            with open(env_path, "w", encoding="utf-8") as f:
                for k, v in existing.items():
                    f.write(f"{k}={v}\n")
            messagebox.showinfo("Configuration", f"Saved to {env_path}.")
        except Exception as e:
            messagebox.showerror("Configuration", f"Could not write .env file.\n\n{e}")

    def _start_bot(self):
        if self.telegram_thread_started:
            messagebox.showinfo("Bot", "Already running.")
            return
        # Connect to MT5 first, then start Telegram
        def runner():
            try:
                ok = Bot.mt5_login_safe()
                if not ok:
                    self.after(0, lambda: messagebox.showerror("MT5", "Failed to connect to MT5. Check credentials and terminal."))
                    return
                Bot.run_telegram_bot()
            except Exception:
                pass
        t = threading.Thread(target=runner, daemon=True)
        t.start()
        self.telegram_thread_started = True

    def _stop_bot(self):
        try:
            Bot.stop_telegram()
            self.telegram_thread_started = False
            Bot.log.info("Dashboard: stop requested for Telegram bot.")
        except Exception:
            pass

    def _place_orders(self):
        sig = Bot.get_last_signal()
        if not sig:
            messagebox.showwarning("Place Orders", "No parsed signal available yet.")
            return
        try:
            Bot.mt5_login()
            ok = Bot.place_orders_from_signal(sig)
            if ok:
                messagebox.showinfo("Orders", "Pending orders placed.")
            else:
                messagebox.showerror("Orders", "Failed to place one or more orders.")
        except Exception as e:
            messagebox.showerror("Orders", str(e))

    def _cancel_pending_orders(self):
        try:
            if not messagebox.askyesno("Confirm", "Cancel ALL pending orders?"):
                return
        except Exception:
            # if messagebox fails for some reason, proceed silently
            pass

        def runner():
            try:
                try:
                    Bot.mt5_login()
                except Exception:
                    pass
                n = Bot.cancel_all_pending()
                msg = f"Cancelled {n} pending order(s)."
                # If nothing cancelled, automatically try all magics as a fallback
                if n == 0:
                    n2 = Bot.cancel_all_pending(include_all_magics=True)
                    msg = f"Cancelled {n2} pending order(s) (all magics)."
                self.after(0, lambda m=msg: messagebox.showinfo("Pending Orders", m))
                Bot.log.info("Dashboard: Cancel all pending orders requested.")
            except Exception as e:
                try:
                    self.after(0, lambda: messagebox.showerror("Cancel Failed", str(e)))
                except Exception:
                    pass

        threading.Thread(target=runner, daemon=True).start()

    def _close_all_positions(self):
        try:
            if not messagebox.askyesno("Confirm", "Close ALL open positions?"):
                return
        except Exception:
            pass

        def runner():
            try:
                try:
                    Bot.mt5_login()
                except Exception:
                    pass
                Bot.close_all_positions()
                self.after(0, lambda: messagebox.showinfo("Positions", "Requested close of all open positions."))
                Bot.log.info("Dashboard: Close all positions requested.")
            except Exception as e:
                try:
                    self.after(0, lambda: messagebox.showerror("Close Failed", str(e)))
                except Exception:
                    pass

        threading.Thread(target=runner, daemon=True).start()

    def _tick(self):
        sig = Bot.get_last_signal()
        if sig:
            self.var_sym.set(sig.symbol)
            self.var_dir.set(sig.direction)
            self.var_lot.set(f"{sig.lot_size}")
            self.var_zone.set(f"{sig.zone_low} / {sig.zone_high} (mid {sig.zone_mid:.2f})")
            self.var_sl.set(f"{sig.stop_loss}")
            self.var_tps.set(
                ", ".join([str(tp) if tp is not None else "open" for tp in sig.take_profits])
            )
        # Update status
        try:
            st = Bot.get_status()
            self.var_mt5.set("Yes" if st.get("mt5_connected") else "No")
            self.var_tg.set("Yes" if st.get("telegram_connected") else "No")
            name = st.get("telegram_channel_name") or "-"
            self.var_tg_name.set(name)
        except Exception:
            pass
        # Maintain trailing stops periodically
        try:
            Bot.maintain_trailing_stops()
        except Exception:
            pass
        self.after(1000, self._tick)

    def _restore_layout(self):
        try:
            if os.path.exists(self.prefs_path):
                with open(self.prefs_path, "r", encoding="utf-8") as f:
                    self.prefs.update(json.load(f))
            geom = self.prefs.get("geometry")
            if geom:
                self.geometry(geom)
            if "fixed_lot" in self.prefs:
                # ensure UI reflects saved fixed lot
                try:
                    self.var_fixed_lot.set(str(self.prefs.get("fixed_lot")))
                except Exception:
                    pass
            # restore selected tab
            idx = self.prefs.get("selected_tab")
            try:
                if isinstance(idx, int) and hasattr(self, 'notebook'):
                    tabs = self.notebook.tabs()
                    if 0 <= idx < len(tabs):
                        self.notebook.select(idx)
            except Exception:
                pass
        except Exception:
            pass

    def _save_layout(self):
        try:
            self.prefs["geometry"] = self.winfo_geometry()
            # persist fixed lot
            self.prefs["fixed_lot"] = self.var_fixed_lot.get().strip()
            try:
                if hasattr(self, 'notebook'):
                    sel = self.notebook.select()
                    tabs = self.notebook.tabs()
                    self.prefs["selected_tab"] = tabs.index(sel) if sel in tabs else 0
            except Exception:
                pass
            with open(self.prefs_path, "w", encoding="utf-8") as f:
                json.dump(self.prefs, f, indent=2)
        except Exception:
            pass

    # -------------- Update Tab --------------
    def _build_update_tab(self, parent):
        try:
            import auto_updater  # type: ignore
        except Exception:
            auto_updater = None

        wrap = ttk.Frame(parent, padding=12)
        wrap.pack(fill="both", expand=True)

        ttk.Label(wrap, text="Software Update", font=("Segoe UI", 12, "bold")).pack(anchor="w")

        grid = ttk.Frame(wrap)
        grid.pack(anchor="w", pady=(6, 8))

        self.var_cur_ver = tk.StringVar(value=f"v{self.app_version} (Build {self.build_date})")
        self.var_repo = tk.StringVar(value=self.prefs.get("update_repo", "owner/repo"))
        self.var_latest = tk.StringVar(value="-")
        self.var_upd_status = tk.StringVar(value="Idle")

        ttk.Label(grid, text="Current Version", width=18, anchor="w").grid(row=0, column=0, sticky="w", pady=2)
        ttk.Label(grid, textvariable=self.var_cur_ver, width=38, anchor="w").grid(row=0, column=1, sticky="w", pady=2)
        ttk.Label(grid, text="GitHub Repo", width=18, anchor="w").grid(row=1, column=0, sticky="w", pady=2)
        ttk.Entry(grid, textvariable=self.var_repo, width=38).grid(row=1, column=1, sticky="w", pady=2)
        ttk.Label(grid, text="Latest Release", width=18, anchor="w").grid(row=2, column=0, sticky="w", pady=2)
        ttk.Label(grid, textvariable=self.var_latest, width=38, anchor="w").grid(row=2, column=1, sticky="w", pady=2)
        ttk.Label(grid, text="Status", width=18, anchor="w").grid(row=3, column=0, sticky="w", pady=2)
        ttk.Label(grid, textvariable=self.var_upd_status, width=38, anchor="w").grid(row=3, column=1, sticky="w", pady=2)

        btns = ttk.Frame(wrap)
        btns.pack(anchor="w", pady=(6, 8))
        ttk.Button(btns, text="Check for Updates", command=self._check_updates).pack(side="left")
        ttk.Button(btns, text="Update Now", command=self._update_now).pack(side="left", padx=6)
        ttk.Button(btns, text="Save Repo", command=self._save_repo_pref).pack(side="left", padx=6)

        # Notes box
        nb = ttk.LabelFrame(wrap, text="Release Notes", padding=6)
        nb.pack(fill="both", expand=True)
        self.txt_notes = tk.Text(nb, wrap="word", height=12)
        self.txt_notes.pack(fill="both", expand=True)
        self.txt_notes.insert("end", "Click 'Check for Updates' to load latest release info.")
        self.txt_notes.configure(state="disabled")

        # Save initial repo back to prefs if missing
        if not self.prefs.get("update_repo"):
            self._save_repo_pref()

    def _save_repo_pref(self):
        try:
            self.prefs["update_repo"] = self.var_repo.get().strip()
            with open(self.prefs_path, "w", encoding="utf-8") as f:
                json.dump(self.prefs, f, indent=2)
        except Exception:
            pass

    def _check_updates(self):
        def worker():
            try:
                self.var_upd_status.set("Checking...")
                repo = self.var_repo.get().strip()
                try:
                    import auto_updater  # type: ignore
                except Exception:
                    auto_updater = None
                if not repo or not auto_updater:
                    self.after(0, lambda: self.var_upd_status.set("Updater not available or repo missing."))
                    return
                info = auto_updater.check_latest_release(repo)
                if not info:
                    self.after(0, lambda: self.var_upd_status.set("Failed to fetch release info."))
                    return
                latest = info.get("tag") or "-"
                self._latest_info = info
                self.after(0, lambda: self.var_latest.set(latest))
                # Compare versions
                newer = auto_updater.is_newer(latest, self.app_version)
                status = "Update available" if newer else "Up to date"
                self.after(0, lambda: self.var_upd_status.set(status))
                # Notes
                body = info.get("body") or "(No release notes)"
                def _put_notes():
                    self.txt_notes.configure(state="normal")
                    self.txt_notes.delete("1.0", "end")
                    self.txt_notes.insert("end", body)
                    self.txt_notes.configure(state="disabled")
                self.after(0, _put_notes)
            except Exception:
                self.after(0, lambda: self.var_upd_status.set("Error during check."))
        threading.Thread(target=worker, daemon=True).start()

    def _update_now(self):
        def worker():
            try:
                import auto_updater  # type: ignore
            except Exception:
                auto_updater = None
            try:
                self.var_upd_status.set("Preparing update...")
                repo = self.var_repo.get().strip()
                info = getattr(self, "_latest_info", None)
                if not info:
                    if auto_updater and repo:
                        info = auto_updater.check_latest_release(repo)
                        self._latest_info = info
                if not info or not auto_updater:
                    self.after(0, lambda: self.var_upd_status.set("No release info."))
                    return
                tag = info.get("tag") or ""
                zip_url = auto_updater.get_release_zip_url(info)
                if not zip_url and repo and tag:
                    zip_url = f"https://github.com/{repo}/archive/refs/tags/{tag}.zip"
                if not zip_url:
                    self.after(0, lambda: self.var_upd_status.set("No zip asset available."))
                    return
                tmpdir = tempfile.mkdtemp(prefix="mz_upd_")
                zpath = os.path.join(tmpdir, "update.zip")
                self.after(0, lambda: self.var_upd_status.set("Downloading update..."))
                ok = auto_updater.download_file(zip_url, zpath)
                if not ok:
                    self.after(0, lambda: self.var_upd_status.set("Download failed."))
                    return
                self.after(0, lambda: self.var_upd_status.set("Extracting..."))
                root = auto_updater.extract_zip(zpath, tmpdir)
                if not root:
                    self.after(0, lambda: self.var_upd_status.set("Extract failed."))
                    return
                # Determine target app root
                app_root = _APP_ROOT
                helper = os.path.join(app_root, "update_helper.py")
                if not os.path.exists(helper):
                    self.after(0, lambda: self.var_upd_status.set("update_helper.py not found."))
                    return
                # Preserve critical files (relative to root of repo)
                preserve = [
                    ".env",
                    "dashboard_prefs.json",
                    "profiles.json",
                    "grid_bot.log",
                    os.path.join("Fore_Ai", ".env"),
                    os.path.join("Fore_Ai", "dashboard_prefs.json"),
                    # legacy paths to preserve during rename
                    os.path.join("Mazhar Bot", ".env"),
                    os.path.join("Mazhar Bot", "dashboard_prefs.json"),
                ]
                # Relaunch command: prefer Fore_Ai/run_dashboard.bat (fallback to legacy path or root)
                relaunch = os.path.join("Fore_Ai", "run_dashboard.bat")
                relaunch_full = os.path.join(app_root, relaunch)
                if not os.path.exists(relaunch_full):
                    legacy = os.path.join(app_root, "Mazhar Bot", "run_dashboard.bat")
                    relaunch_full = legacy if os.path.exists(legacy) else os.path.join(app_root, "run_dashboard.bat")
                relaunch_cmd = f'"{relaunch_full}"'
                # Build and launch helper command
                cmd = (
                    f'"{sys.executable}" "{helper}" '
                    f'--source "{root}" --target "{app_root}" '
                    f'--preserve "{";".join(preserve)}" '
                    f'--relaunch {relaunch_cmd} --wait 2.0'
                )
                self.after(0, lambda: self.var_upd_status.set("Launching updater; app will restart..."))
                try:
                    subprocess.Popen(cmd, shell=True)
                except Exception:
                    pass
                # Close app after a short delay
                self.after(1200, self.destroy)
            except Exception:
                self.after(0, lambda: self.var_upd_status.set("Update failed."))
        threading.Thread(target=worker, daemon=True).start()

    def destroy(self):
        self._save_layout()
        super().destroy()

    def _apply_fixed_lot(self):
        # Be forgiving: if invalid or empty, just clear fixed lot and do not alert
        txt = (self.var_fixed_lot.get() or "").strip()
        try:
            val = float(txt) if txt else 0.0
        except Exception:
            val = 0.0
        Bot.set_fixed_lot(val if val > 0 else None)
        # persist immediately
        self.prefs["fixed_lot"] = txt
        try:
            with open(self.prefs_path, "w", encoding="utf-8") as f:
                json.dump(self.prefs, f, indent=2)
        except Exception:
            pass

    def _apply_auto_place(self):
        try:
            Bot.set_auto_place(bool(self.var_auto_place.get()))
            # persist immediately
            self.prefs["auto_place"] = bool(self.var_auto_place.get())
            with open(self.prefs_path, "w", encoding="utf-8") as f:
                json.dump(self.prefs, f, indent=2)
        except Exception:
            pass


if __name__ == "__main__":
    app = ForeAiDashboard()
    app.mainloop()
