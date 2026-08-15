#!/usr/bin/env python3
"""Disk AI Analyzer v4.1 — Self-learning disk analysis with autonomous cleanup."""

import argparse
import ctypes
import fnmatch
import hashlib
import json
import os
import queue
import shutil
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

APP_NAME    = "Disk AI Analyzer"
APP_VERSION = "4.1"
DEFAULT_LARGE_MB = 500
DEFAULT_OLD_DAYS = 365
HASH_CHUNK_SIZE  = 1024 * 1024
HISTORY_PATH   = Path.home() / ".disk_ai_analyzer_history.json"
LEARNING_PATH  = Path.home() / ".disk_ai_analyzer_learning.json"
TRASH_LOG_PATH = Path.home() / ".disk_ai_analyzer_trash.json"
UI_WIDTH = 100

SKIP_PRESETS = {
    "none": [],
    "safe": [
        "$Recycle.Bin","System Volume Information","Windows\\WinSxS",
        "Windows\\SoftwareDistribution\\Download","Windows\\Temp",
        "ProgramData\\Microsoft\\Windows Defender","AppData\\Local\\Temp",
        "AppData\\Local\\Microsoft\\Windows\\INetCache","AppData\\Local\\Packages",
        "AppData\\Local\\Google\\Chrome\\User Data\\*\\Cache",
        "node_modules",".git",".venv","venv","__pycache__",
    ],
    "aggressive": [
        "$Recycle.Bin","System Volume Information","Windows","Program Files",
        "Program Files (x86)","ProgramData","AppData\\Local","AppData\\Roaming\\Microsoft",
        "node_modules",".git",".venv","venv","__pycache__",".cache","Cache","Temp",
    ],
}

# ════════════════════════════════════════════════════════════════════════════
#  SAFE DELETE (move to trash / recycle bin, fallback to rename+log)
# ════════════════════════════════════════════════════════════════════════════

def safe_delete(path: Path) -> tuple:
    """Move file to OS trash if possible, else rename to .daitrash. Returns (ok, msg)."""
    try:
        import send2trash
        send2trash.send2trash(str(path))
        return True, "moved to trash"
    except ImportError:
        pass
    # Fallback: rename alongside original so it's recoverable
    trash_path = path.with_suffix(path.suffix + ".daitrash")
    counter = 0
    while trash_path.exists():
        counter += 1
        trash_path = path.with_suffix(f"{path.suffix}.daitrash{counter}")
    try:
        path.rename(trash_path)
        return True, f"renamed → {trash_path.name}"
    except OSError as exc:
        return False, str(exc)

def log_deletion(path: Path, reason: str, method: str, size: int):
    """Append deletion to persistent log for undo reference."""
    try:
        log = []
        if TRASH_LOG_PATH.exists():
            log = json.loads(TRASH_LOG_PATH.read_text(encoding="utf-8"))
        log.insert(0, {
            "ts": datetime.now().isoformat(),
            "path": str(path),
            "size": size,
            "reason": reason,
            "method": method,
        })
        log = log[:500]
        TRASH_LOG_PATH.write_text(json.dumps(log, indent=2), encoding="utf-8")
    except OSError:
        pass

# ════════════════════════════════════════════════════════════════════════════
#  AI LEARNING MODEL
# ════════════════════════════════════════════════════════════════════════════

class AILearningModel:
    def __init__(self):
        self.data = self._load()

    def _load(self):
        try:
            if LEARNING_PATH.exists():
                d = json.loads(LEARNING_PATH.read_text(encoding="utf-8"))
                if isinstance(d, dict):
                    return d
        except (OSError, json.JSONDecodeError):
            pass
        return {
            "version": 1,
            "scan_count": 0,
            "confirmed_safe_extensions": {},
            "confirmed_waste_extensions": {},
            "path_risk_patterns": {},
            "size_percentiles_history": [],
            "duplicate_ratio_history": [],
            "feedback_log": [],
            "global_large_baseline": DEFAULT_LARGE_MB * 1024 * 1024,
            "global_old_baseline": DEFAULT_OLD_DAYS,
            "pattern_weights": {
                "age_weight": 1.0, "access_weight": 1.0,
                "extension_weight": 1.0, "size_weight": 1.0, "path_weight": 1.0,
            },
        }

    def save(self):
        try:
            LEARNING_PATH.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        except OSError:
            pass

    def record_scan(self, files, duplicates, options):
        sizes = sorted(r.size for r in files if r.size > 0)
        if sizes:
            n = len(sizes)
            self.data["size_percentiles_history"].append({
                "ts": datetime.now().isoformat(),
                "root": str(options.root),
                "p50": sizes[int(n*0.50)],
                "p90": sizes[int(n*0.90)],
                "p99": sizes[int(n*0.99)],
                "count": n,
            })
            self.data["size_percentiles_history"] = self.data["size_percentiles_history"][-20:]
        total = sum(r.size for r in files)
        waste = sum(sum(r.size for r in g[1:]) for g in duplicates.values())
        if total > 0:
            self.data["duplicate_ratio_history"].append(waste / total)
            self.data["duplicate_ratio_history"] = self.data["duplicate_ratio_history"][-20:]
        self.data["scan_count"] += 1
        recent = self.data["size_percentiles_history"][-5:]
        if recent:
            avg_p90 = sum(e["p90"] for e in recent) / len(recent)
            old_base = self.data["global_large_baseline"]
            self.data["global_large_baseline"] = int(old_base * 0.7 + avg_p90 * 0.3)
        self.save()

    def record_feedback(self, path_str, action, reason=""):
        ext = Path(path_str).suffix.lower()
        self.data["feedback_log"].append({
            "ts": datetime.now().isoformat(),
            "path": path_str, "action": action, "reason": reason,
        })
        self.data["feedback_log"] = self.data["feedback_log"][-500:]
        if action == "kept":
            self.data["confirmed_safe_extensions"][ext] = self.data["confirmed_safe_extensions"].get(ext, 0) + 1
        elif action == "deleted":
            self.data["confirmed_waste_extensions"][ext] = self.data["confirmed_waste_extensions"].get(ext, 0) + 1
        for part in Path(path_str).parts:
            key = part.lower()
            delta = -1 if action == "kept" else 1 if action == "deleted" else 0
            self.data["path_risk_patterns"][key] = self.data["path_risk_patterns"].get(key, 0) + delta
        self.save()

    def extension_bias(self, ext):
        safe  = self.data["confirmed_safe_extensions"].get(ext, 0)
        waste = self.data["confirmed_waste_extensions"].get(ext, 0)
        total = safe + waste
        if total < 3:
            return 0
        return int(((safe - waste) / total) * 20)

    def path_bias(self, path):
        delta = 0
        for part in path.parts:
            raw = self.data["path_risk_patterns"].get(part.lower(), 0)
            delta += max(-15, min(15, raw * 2))
        return delta

    def weights(self):
        return self.data.get("pattern_weights", {})

    def smart_large_threshold(self, configured):
        baseline = self.data.get("global_large_baseline", DEFAULT_LARGE_MB * 1024 * 1024)
        return max(configured, int(baseline * 0.8))

    def avg_duplicate_ratio(self):
        h = self.data.get("duplicate_ratio_history", [])
        return sum(h) / len(h) if h else 0.0

    def summary_lines(self):
        scans    = self.data.get("scan_count", 0)
        feedback = len(self.data.get("feedback_log", []))
        safe_e   = sorted(self.data["confirmed_safe_extensions"].items(), key=lambda x: -x[1])[:5]
        waste_e  = sorted(self.data["confirmed_waste_extensions"].items(), key=lambda x: -x[1])[:5]
        avg_dup  = self.avg_duplicate_ratio() * 100
        return [
            f"Scans processed: {scans}   Feedback events: {feedback}",
            f"Avg duplicate waste ratio: {avg_dup:.1f}%",
            f"Safe extensions: {', '.join(e for e,_ in safe_e) or 'none yet'}",
            f"Risky extensions: {', '.join(e for e,_ in waste_e) or 'none yet'}",
            f"Learned large baseline: {human_size(self.data.get('global_large_baseline', DEFAULT_LARGE_MB*1024*1024))}",
        ]

AI_MODEL = AILearningModel()

# ════════════════════════════════════════════════════════════════════════════
#  DATA CLASSES
# ════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class FileRecord:
    path: Path
    size: int
    modified: float
    accessed: float
    suffix: str

@dataclass(frozen=True)
class ScanOptions:
    root: Path
    large_bytes: int
    old_days: int
    workers: int
    admin: bool
    autopilot: bool = False
    skip_preset: str = "none"
    exclude_patterns: tuple = ()

@dataclass(frozen=True)
class ScanResult:
    files: list
    duplicates: dict
    denied: int
    empty_dirs: int
    skipped_dirs: int
    report_path: Path
    elapsed: float
    options: ScanOptions

@dataclass
class CleanupCandidate:
    risk: str
    confidence: int
    reclaimable: int
    reason: str
    path: Path
    category: str = ""
    ai_score: int = 0
    learned_bias: int = 0

# ════════════════════════════════════════════════════════════════════════════
#  TERMINAL COLORS
# ════════════════════════════════════════════════════════════════════════════

class C:
    RST="\033[0m"; BOLD="\033[1m"; DIM="\033[2m"; ITAL="\033[3m"
    RED="\033[31m"; GRN="\033[32m"; YLW="\033[33m"; BLU="\033[34m"
    MAG="\033[35m"; CYN="\033[36m"; WHT="\033[37m"; GRAY="\033[90m"
    BRED="\033[91m"; BGRN="\033[92m"; BYLW="\033[93m"; BBLU="\033[94m"
    BMAG="\033[95m"; BCYN="\033[96m"; BWHT="\033[97m"
    BG_BLK="\033[40m"; BG_RED="\033[41m"; BG_GRN="\033[42m"
    BG_YLW="\033[43m"; BG_BLU="\033[44m"; BG_MAG="\033[45m"
    BG_CYN="\033[46m"; BG_DGRAY="\033[100m"; BG_WHT="\033[107m"

def p(text, *colors):
    return "".join(colors) + str(text) + C.RST

# ════════════════════════════════════════════════════════════════════════════
#  UTILITIES
# ════════════════════════════════════════════════════════════════════════════

def is_windows():
    return os.name == "nt"

def enable_ansi():
    if not is_windows(): return
    try:
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        m = ctypes.c_uint32()
        if k.GetConsoleMode(h, ctypes.byref(m)):
            k.SetConsoleMode(h, m.value | 0x0004)
    except Exception:
        pass

def is_admin():
    if not is_windows():
        return os.geteuid() == 0
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False

def relaunch_as_admin():
    if not is_windows():
        print("Run with sudo/root on Linux/Mac.")
        sys.exit(1)
    args = " ".join(f'"{a}"' for a in sys.argv)
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, args, None, 1)
    if rc <= 32:
        print("Could not open UAC prompt.")
        sys.exit(1)
    sys.exit(0)

def clear():
    os.system("cls" if is_windows() else "clear")

def human_size(size):
    units = ["B","KB","MB","GB","TB","PB"]
    v = float(size)
    for u in units:
        if v < 1024 or u == units[-1]:
            return f"{v:.1f} {u}" if u != "B" else f"{int(v)} B"
        v /= 1024
    return f"{size} B"

def compact_path(path, width=60):
    text = str(path)
    if len(text) <= width:
        return text
    parts = Path(text).parts
    if len(parts) >= 4:
        head = parts[0]
        tail = os.sep.join(parts[-2:])
        cand = f"{head}{os.sep}...{os.sep}{tail}"
        if len(cand) <= width:
            return cand
    half = max(6, width // 2 - 3)
    return text[:half] + "..." + text[-(width-half-3):]

def percentile(values, pct):
    if not values: return 0
    s = sorted(values)
    i = int((len(s)-1)*pct)
    return s[max(0, min(i, len(s)-1))]

def format_dt(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")

def sparkline(values, width=20):
    if not values: return "─"*width
    blocks = "▁▂▃▄▅▆▇█"
    mn, mx = min(values), max(values)
    rng = mx - mn or 1
    step = max(1, len(values)//width)
    sampled = values[::step][:width]
    return "".join(blocks[int((v-mn)/rng*(len(blocks)-1))] for v in sampled).ljust(width,"▁")

def drive_roots():
    if not is_windows(): return [Path("/")]
    drives = []
    try:
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        for i in range(26):
            if mask & (1<<i):
                drives.append(Path(f"{chr(65+i)}:\\"))
    except Exception:
        drives = [Path(f"{l}:\\") for l in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if Path(f"{l}:\\").exists()]
    return drives

def disk_usage_for(path):
    try:
        u = shutil.disk_usage(path)
        used = u.total - u.free
        return u.total, used, u.free, (used/u.total*100) if u.total else 0
    except OSError:
        return None

def wait_key(msg="  ↵ Enter to continue"):
    print()
    input(p(msg + "...", C.GRAY + C.ITAL))

# ════════════════════════════════════════════════════════════════════════════
#  UI PRIMITIVES
# ════════════════════════════════════════════════════════════════════════════

def hr(char="─", color=C.GRAY, width=UI_WIDTH):
    print(p(char*width, color))

def section_title(title, icon="◈", color=C.BCYN):
    pad = max(2, UI_WIDTH - len(title) - 6)
    print()
    print(p(f"  {icon} {title} ", color+C.BOLD) + p("─"*pad, color+C.DIM))

def bar_chart(val, total, width=24, fg=C.BGRN, empty=C.GRAY):
    if total <= 0: return p("░"*width, empty)
    filled = min(width, int(width*val/total))
    return p("█"*filled, fg) + p("░"*(width-filled), empty)

def risk_color(risk):
    return {
        "review": C.BRED+C.BOLD,
        "inspect": C.BYLW+C.BOLD,
        "low": C.BGRN,
    }.get(risk, C.GRAY)

def risk_icon(risk):
    return {"review":"◉","inspect":"◎","low":"○"}.get(risk,"·")

def conf_color(conf):
    if conf >= 80: return C.BRED
    if conf >= 60: return C.BYLW
    return C.BGRN

def live_line(text):
    sys.stdout.write("\r" + text[:UI_WIDTH-1].ljust(UI_WIDTH-1))
    sys.stdout.flush()

# ════════════════════════════════════════════════════════════════════════════
#  STAT GRID  (fixed layout)
# ════════════════════════════════════════════════════════════════════════════

def stat_grid(stats):
    """
    stats = list of (value_str, label_str)
    Renders a clean 2-row grid that works at any terminal width.
    """
    n = len(stats)
    # Each cell: "  VALUE  \n  LABEL  " with a separator
    cells = []
    for val, lbl in stats:
        w = max(len(val), len(lbl)) + 2
        cells.append((val, lbl, w))

    # Top border
    top = "  " + "  ".join(p("┌" + "─"*c[2] + "┐", C.GRAY) for c in cells)
    print(top)

    # Value row
    val_row = "  " + "  ".join(
        p("│", C.GRAY) + p((" " + v).ljust(w), C.BWHT+C.BOLD) + p("│", C.GRAY)
        for v, l, w in cells
    )
    print(val_row)

    # Label row
    lbl_row = "  " + "  ".join(
        p("│", C.GRAY) + p((" " + l).ljust(w), C.GRAY) + p("│", C.GRAY)
        for v, l, w in cells
    )
    print(lbl_row)

    # Bottom border
    bot = "  " + "  ".join(p("└" + "─"*c[2] + "┘", C.GRAY) for c in cells)
    print(bot)

# ════════════════════════════════════════════════════════════════════════════
#  BANNER
# ════════════════════════════════════════════════════════════════════════════

def banner():
    scans = AI_MODEL.data.get("scan_count", 0)
    admin_badge   = p("  ◉ ADMIN  ",    C.BG_GRN  + C.BOLD + "\033[30m") if is_admin() else p("  ○ STANDARD  ", C.BG_DGRAY + "\033[37m")
    learn_badge   = p(f"  ⬡ AI:{scans} scans  ", C.BG_BLU + C.BOLD + C.BWHT)
    version_badge = p(f"  v{APP_VERSION}  ",     C.BG_DGRAY + "\033[37m")
    print()
    print(p("  " + "═"*(UI_WIDTH-4), C.BBLU))
    print(p(f"  {APP_NAME}", C.BWHT+C.BOLD) + "  " + p("Adaptive AI · Self-Learning · Autonomous Cleanup", C.BCYN+C.ITAL))
    print(f"  {admin_badge}  {learn_badge}  {version_badge}")
    print(p("  " + "═"*(UI_WIDTH-4), C.BBLU))

# ════════════════════════════════════════════════════════════════════════════
#  DISK OVERVIEW & HISTORY
# ════════════════════════════════════════════════════════════════════════════

def show_disk_overview():
    section_title("DISK OVERVIEW", "◈", C.BCYN)
    shown = 0
    for drive in drive_roots():
        u = disk_usage_for(drive)
        if not u: continue
        total, used, free, pct = u
        b = bar_chart(int(pct), 100, 28, C.BRED if pct>=90 else C.BYLW if pct>=70 else C.BGRN)
        pct_col = p(f"{pct:5.1f}%", C.BRED+C.BOLD if pct>=90 else C.BYLW if pct>=70 else C.BGRN)
        print(f"  {p(str(drive), C.BWHT+C.BOLD):<8}{b} {pct_col}  free {p(human_size(free),C.BGRN):>11}  total {p(human_size(total),C.GRAY)}")
        shown += 1
    if not shown:
        print(p("  No disk info available.", C.GRAY))

def load_history():
    try:
        if HISTORY_PATH.exists():
            d = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
            if isinstance(d, list): return d
    except (OSError, json.JSONDecodeError):
        pass
    return []

def save_history_entry(result):
    entry = scan_summary_dict(result)
    h = load_history()
    h.insert(0, entry)
    try:
        HISTORY_PATH.write_text(json.dumps(h[:20], indent=2), encoding="utf-8")
    except OSError:
        pass

def show_history_summary():
    h = load_history()
    if not h: return
    section_title("RECENT SCANS", "◷", C.BMAG)
    for e in h[:3]:
        date  = e.get("generated","?")[:16]
        root  = compact_path(e.get("root","?"), 38)
        files = int(e.get("files",0))
        size  = e.get("total_size_text","0 B")
        waste = e.get("duplicate_waste_text","0 B")
        print(f"  {p(date,C.GRAY)}  {p(root,C.BWHT):<40}  {p(f'{files:,} files',C.BCYN):<16}  {p(size,C.BYLW)}  dup waste {p(waste,C.BRED)}")

# ════════════════════════════════════════════════════════════════════════════
#  FILE COLLECTION
# ════════════════════════════════════════════════════════════════════════════

def safe_stat(path):
    try: return path.stat()
    except (OSError, PermissionError): return None

def should_skip_dir(path, root, patterns):
    if not patterns: return False, ""
    full = str(path).replace("/","\\").lower()
    name = path.name.lower()
    try:
        rel = str(path.relative_to(root)).replace("/","\\").lower()
    except ValueError:
        rel = full
    for pat in patterns:
        clean = pat.strip().strip("\\/").lower().replace("/","\\")
        if not clean: continue
        if (fnmatch.fnmatch(name,clean) or fnmatch.fnmatch(rel,clean)
                or fnmatch.fnmatch(full,f"*\\{clean}")
                or fnmatch.fnmatch(full,f"*\\{clean}\\*")):
            return True, pat
    return False, ""

def effective_skip_patterns(options):
    return tuple(SKIP_PRESETS.get(options.skip_preset,[])) + tuple(options.exclude_patterns)

def producer(root, out, counters, stop, patterns):
    stack = [root]
    while stack and not stop.is_set():
        cur = stack.pop()
        skip, pat = should_skip_dir(cur, root, patterns)
        if skip:
            counters["skipped"] += 1
            counters["current"] = f"skip:{cur.name}"
            continue
        counters["current"] = str(cur)
        try:
            with os.scandir(cur) as entries:
                has_child = False
                for e in entries:
                    has_child = True
                    try:
                        if e.is_dir(follow_symlinks=False):
                            child = Path(e.path)
                            sk, _ = should_skip_dir(child, root, patterns)
                            if sk:
                                counters["skipped"] += 1
                                continue
                            stack.append(child)
                            counters["dirs"] += 1
                        elif e.is_file(follow_symlinks=False):
                            out.put(Path(e.path))
                            counters["queued"] += 1
                    except (OSError, PermissionError):
                        counters["denied"] += 1
                if not has_child:
                    counters["empty_dirs"] += 1
        except (OSError, PermissionError):
            counters["denied"] += 1
    out.put(None)

def collect_files(options, live=False):
    files   = []
    root    = options.root
    patterns = effective_skip_patterns(options)
    paths   = queue.Queue(maxsize=options.workers*1000)
    stop    = threading.Event()
    counters = {"dirs":1,"denied":0,"empty_dirs":0,"queued":0,"stat_done":0,"skipped":0,"current":str(root)}
    t = threading.Thread(target=producer, args=(root,paths,counters,stop,patterns), daemon=True)
    t.start()

    def consume():
        local = []
        while True:
            path = paths.get()
            if path is None:
                paths.put(None); break
            stat = safe_stat(path)
            if stat is None:
                counters["denied"] += 1; continue
            counters["stat_done"] += 1
            local.append(FileRecord(path=path, size=stat.st_size,
                                    modified=stat.st_mtime, accessed=stat.st_atime,
                                    suffix=path.suffix.lower()))
        return local

    try:
        with ThreadPoolExecutor(max_workers=options.workers) as pool:
            futures = [pool.submit(consume) for _ in range(options.workers)]
            while True:
                done = sum(1 for f in futures if f.done())
                if live:
                    pct = min(100, int(counters["stat_done"]/max(counters["queued"],1)*100))
                    b = bar_chart(pct, 100, 14, C.BCYN)
                    live_line(f"  Stage 1/3  {b}  dirs {counters['dirs']:,}  files {counters['stat_done']:,}"
                              f"  denied {counters['denied']:,}  → {compact_path(counters['current'],28)}")
                if done == len(futures): break
                time.sleep(0.15)
            if live: print()
            for f in futures:
                files.extend(f.result())
    finally:
        stop.set(); t.join(timeout=2)
    return files, counters["denied"], counters["empty_dirs"], counters["skipped"]

def hash_file(path):
    d = hashlib.sha256()
    try:
        with path.open("rb") as f:
            while True:
                chunk = f.read(HASH_CHUNK_SIZE)
                if not chunk: break
                d.update(chunk)
        return d.hexdigest()
    except (OSError, PermissionError):
        return None

def find_duplicates(files, workers, live=False):
    by_size = defaultdict(list)
    for r in files:
        if r.size > 0:
            by_size[r.size].append(r)
    candidates = [g for g in by_size.values() if len(g)>1]
    hashes = defaultdict(list)

    def hash_record(record):
        return hash_file(record.path), record

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(hash_record, r) for g in candidates for r in g]
        total = len(futures); done = 0
        for future in as_completed(futures):
            done += 1
            if live:
                b = bar_chart(done, total, 14, C.BMAG)
                live_line(f"  Stage 2/3  {b}  {done:,}/{total:,} hashed")
            fh, rec = future.result()
            if fh: hashes[fh].append(rec)
        if live and total: print()
    return {h:g for h,g in hashes.items() if len(g)>1}

# ════════════════════════════════════════════════════════════════════════════
#  FILE CLASSIFICATION
# ════════════════════════════════════════════════════════════════════════════

CATEGORIES = {
    "Video":     {".mp4",".mkv",".mov",".avi",".wmv",".flv",".webm",".m4v",".ts",".vob"},
    "Audio":     {".mp3",".wav",".flac",".aac",".ogg",".m4a",".wma",".opus"},
    "Images":    {".jpg",".jpeg",".png",".gif",".bmp",".webp",".heic",".tiff",".svg",".raw"},
    "Documents": {".pdf",".doc",".docx",".xls",".xlsx",".ppt",".pptx",".txt",".md",".rtf",".csv",".odt"},
    "Archives":  {".zip",".rar",".7z",".tar",".gz",".bz2",".xz",".iso",".cab"},
    "Installers":{".exe",".msi",".msix",".appx",".apk",".dmg",".pkg",".deb",".rpm"},
    "Code":      {".py",".js",".ts",".jsx",".tsx",".java",".c",".cpp",".cs",".go",".rs",".php",".html",".css",".json",".xml",".yml",".yaml",".sh",".bat"},
    "VirtualDisk":{".vhd",".vhdx",".vmdk",".ova",".qcow2",".img"},
    "Databases": {".db",".sqlite",".sqlite3",".mdb",".accdb",".bak"},
    "Logs/Temp": {".log",".tmp",".temp",".dmp",".part",".crdownload",".old"},
    "Keys":      {".key",".pem",".pfx",".p12",".cer",".crt",".kdbx",".gpg",".pgp"},
}

def file_category(record):
    for name, exts in CATEGORIES.items():
        if record.suffix in exts: return name
    return "No Ext" if not record.suffix else "Other"

def category_summary(files):
    s = defaultdict(lambda:[0,0])
    for r in files:
        k = file_category(r); s[k][0]+=1; s[k][1]+=r.size
    return sorted(((k,c,sz) for k,(c,sz) in s.items()), key=lambda x:x[2], reverse=True)

def extension_summary(files):
    s = defaultdict(lambda:[0,0])
    for r in files:
        k = r.suffix or "[none]"; s[k][0]+=1; s[k][1]+=r.size
    return sorted(((k,c,sz) for k,(c,sz) in s.items()), key=lambda x:x[2], reverse=True)

def folder_summary(files, root):
    s = defaultdict(lambda:[0,0])
    for r in files:
        try:
            rel = r.path.relative_to(root)
            top = root/rel.parts[0] if len(rel.parts)>1 else root
        except ValueError:
            top = r.path.parent
        s[top][0]+=1; s[top][1]+=r.size
    return sorted(((fp,c,sz) for fp,(c,sz) in s.items()), key=lambda x:x[2], reverse=True)

# ════════════════════════════════════════════════════════════════════════════
#  AI SCORING & ANALYSIS
# ════════════════════════════════════════════════════════════════════════════

def ai_brain_profile(files, duplicates, options):
    now  = datetime.now()
    sizes = [r.size for r in files if r.size>0]
    ages  = [(now-datetime.fromtimestamp(r.modified)).days for r in files]
    accs  = [(now-datetime.fromtimestamp(r.accessed)).days for r in files]
    dup_waste = sum(sum(r.size for r in g[1:]) for g in duplicates.values())
    p90=percentile(sizes,0.90); p95=percentile(sizes,0.95); p99=percentile(sizes,0.99)
    smart = AI_MODEL.smart_large_threshold(options.large_bytes)
    return {
        "p90":p90,"p95":p95,"p99":p99,
        "high_large":   max(smart,p95),
        "extreme_large":max(smart*2,p99),
        "normal_old":   max(options.old_days,percentile(ages,0.75)),
        "very_old":     max(options.old_days*2,percentile(ages,0.90)),
        "stale_access": max(options.old_days,percentile(accs,0.80)),
        "dup_waste":    dup_waste,
        "avg_dup_ratio":AI_MODEL.avg_duplicate_ratio(),
    }

WASTE_EXTS = {".tmp",".temp",".bak",".old",".log",".dmp",".crdownload",".part",".cache"}
KEEP_EXTS  = {".docx",".pdf",".xlsx",".pptx",".jpg",".jpeg",".png",".mp4",".mp3",
              ".key",".pem",".kdbx",".pfx",".gpg"}
SYSTEM_DIRS = {"windows","program files","program files (x86)","programdata"}

def relevance_score(record, now, profile=None):
    score = 50
    reasons = []
    w = AI_MODEL.weights()
    age_days    = (now-datetime.fromtimestamp(record.modified)).days
    access_days = (now-datetime.fromtimestamp(record.accessed)).days

    if record.size == 0:
        score -= int(35*w.get("size_weight",1.0)); reasons.append("empty file")

    age_w = w.get("age_weight",1.0)
    if age_days>730:   score-=int(22*age_w); reasons.append("2+ years no modification")
    elif age_days>365: score-=int(12*age_w); reasons.append(f"old ({age_days}d)")

    acc_w = w.get("access_weight",1.0)
    if access_days>730:   score-=int(20*acc_w); reasons.append("2+ years no access")
    elif access_days>365: score-=int(12*acc_w); reasons.append("1+ year no access")

    ext_w = w.get("extension_weight",1.0)
    if record.suffix in WASTE_EXTS:
        score-=int(28*ext_w); reasons.append(f"temp/waste ext ({record.suffix})")
    elif record.suffix in KEEP_EXTS:
        score+=int(12*ext_w); reasons.append("personal/media/key type")

    bias = AI_MODEL.extension_bias(record.suffix)
    if bias!=0:
        score+=bias
        reasons.append(f"learned ext bias ({bias:+d})")

    path_w = w.get("path_weight",1.0)
    parts_l = [pt.lower() for pt in record.path.parts]
    if any(d in parts_l for d in SYSTEM_DIRS):
        score+=int(32*path_w); reasons.append("system/app path")

    pbias = AI_MODEL.path_bias(record.path)
    if pbias!=0:
        score=max(0,min(100,score+pbias)); reasons.append(f"learned path bias ({pbias:+d})")

    if profile:
        if record.size>=profile.get("extreme_large",float("inf")): reasons.append("extreme size")
        elif record.size>=profile.get("high_large",float("inf")):  reasons.append("high-large file")

    return max(0,min(100,score)), reasons

def encrypted_indicator(record):
    if record.suffix in {".gpg",".pgp",".enc",".aes",".crypt",".vault"}: return "encrypted-looking"
    if record.suffix in {".zip",".7z",".rar"}: return "archive"
    return ""

def duplicate_cleanup_candidates(duplicates):
    candidates = []
    for group in duplicates.values():
        sg = sorted(group, key=lambda r:(r.modified,str(r.path).lower()), reverse=True)
        for dup in sg[1:]:
            candidates.append(CleanupCandidate(
                risk="review", confidence=90, reclaimable=dup.size,
                reason="identical SHA-256 — older duplicate",
                path=dup.path, category="Duplicate",
                ai_score=20, learned_bias=0,
            ))
    return sorted(candidates, key=lambda x:x.reclaimable, reverse=True)

def autonomous_cleanup_plan(files, duplicates, options):
    now     = datetime.now()
    profile = ai_brain_profile(files, duplicates, options)
    plan    = duplicate_cleanup_candidates(duplicates)

    for record in files:
        score, reasons = relevance_score(record, now, profile)
        joined = "; ".join(reasons) if reasons else "low activity signal"
        sys_path = any(pt.lower() in SYSTEM_DIRS for pt in record.path.parts)
        bias = AI_MODEL.path_bias(record.path) + AI_MODEL.extension_bias(record.suffix)
        cat  = file_category(record)

        if record.size==0 and not sys_path:
            plan.append(CleanupCandidate("low", 72, 0, "empty non-system file",
                                         record.path, cat, score, bias))
        elif score<=22 and record.size>=512*1024 and not sys_path:
            plan.append(CleanupCandidate("review", max(55,int((100-score)*0.8)), record.size,
                                         joined, record.path, cat, score, bias))
        elif record.size>=options.large_bytes*4 and not sys_path and score<65:
            plan.append(CleanupCandidate("inspect", 48, record.size,
                                         "very large; low activity score",
                                         record.path, cat, score, bias))

    return sorted(plan, key=lambda x:(x.risk!="review",-x.reclaimable,-x.confidence))[:120]

def ai_brain_insights(files, duplicates, options):
    profile = ai_brain_profile(files, duplicates, options)
    total   = sum(r.size for r in files)
    cats    = category_summary(files)
    folders = folder_summary(files, options.root)
    plan    = autonomous_cleanup_plan(files, duplicates, options)
    now     = datetime.now()
    dup_pct = (profile["dup_waste"]/total*100) if total else 0
    extreme = sum(1 for r in files if r.size>=profile["extreme_large"])
    very_old= sum(1 for r in files if (now-datetime.fromtimestamp(r.modified)).days>=profile["very_old"])
    top_cat = cats[0] if cats else ("—",0,0)
    top_fld = folders[0] if folders else (options.root,0,0)

    if dup_pct>=10:
        priority="⚠ Duplicates are the primary opportunity"; plvl="HIGH"
    elif extreme:
        priority="⚠ Extreme large files dominate"; plvl="HIGH"
    elif very_old:
        priority="↻ Very old files warrant review"; plvl="MEDIUM"
    elif plan:
        priority="✓ Moderate cleanup candidates"; plvl="MEDIUM"
    else:
        priority="✓ No strong cleanup pressure"; plvl="LOW"

    return [
        f"Priority: {priority}  [{plvl}]",
        f"AI learned from {AI_MODEL.data.get('scan_count',0)} scans  |  Avg dup ratio: {profile['avg_dup_ratio']*100:.1f}%",
        f"Adaptive large:  high ≥ {human_size(profile['high_large'])}  |  extreme ≥ {human_size(profile['extreme_large'])}",
        f"Adaptive old:    old ≥ {profile['normal_old']}d  |  very old ≥ {profile['very_old']}d",
        f"Extreme files: {extreme:,}  |  Very old: {very_old:,}  |  AI candidates: {len(plan):,}",
        f"Top type:   {top_cat[0]} → {human_size(top_cat[2])} in {top_cat[1]:,} files",
        f"Top folder: {compact_path(top_fld[0],58)} → {human_size(top_fld[2])}",
        f"Duplicate waste: {human_size(profile['dup_waste'])} ({dup_pct:.1f}% of total)",
    ]

# ════════════════════════════════════════════════════════════════════════════
#  SCAN SUMMARY / REPORT
# ════════════════════════════════════════════════════════════════════════════

def scan_summary_dict(result):
    files = result.files
    dup_waste = sum(sum(r.size for r in g[1:]) for g in result.duplicates.values())
    total = sum(r.size for r in files)
    return {
        "generated":  datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "root":       str(result.options.root),
        "files":      len(files),
        "total_size": total, "total_size_text": human_size(total),
        "duplicate_groups": len(result.duplicates),
        "duplicate_waste":  dup_waste, "duplicate_waste_text": human_size(dup_waste),
        "denied": result.denied, "skipped_dirs": result.skipped_dirs,
        "empty_dirs": result.empty_dirs, "elapsed": result.elapsed,
        "report_path": str(result.report_path),
        "skip_preset": result.options.skip_preset,
        "categories": [{"name":n,"count":c,"size":s,"size_text":human_size(s)} for n,c,s in category_summary(files)[:12]],
        "folders":    [{"path":str(fp),"count":c,"size":s,"size_text":human_size(s)} for fp,c,s in folder_summary(files,result.options.root)[:12]],
    }

def build_report(files, duplicates, options, denied, empty_dirs, skipped_dirs):
    now = datetime.now()
    old_cutoff  = now-timedelta(days=options.old_days)
    large_files = sorted((f for f in files if f.size>=options.large_bytes), key=lambda x:x.size, reverse=True)
    empty_files = [f for f in files if f.size==0]
    old_files   = sorted((f for f in files if datetime.fromtimestamp(f.modified)<=old_cutoff), key=lambda x:x.modified)
    dup_waste   = sum(sum(r.size for r in g[1:]) for g in duplicates.values())
    total_size  = sum(r.size for r in files)
    plan        = autonomous_cleanup_plan(files, duplicates, options)

    L=[f"{APP_NAME} v{APP_VERSION} Report",f"Generated: {now.strftime('%Y-%m-%d %H:%M:%S')}",
       f"Root: {options.root}",f"Admin: {'yes' if options.admin else 'no'}","",
       "═"*80,"SUMMARY","═"*80,
       f"  Files:           {len(files):,}",
       f"  Total size:      {human_size(total_size)}",
       f"  Duplicates:      {len(duplicates):,} groups  ({human_size(dup_waste)} waste)",
       f"  AI candidates:   {len(plan):,}",
       f"  Denied/skipped:  {denied:,} / {skipped_dirs:,}","",
       "AI BRAIN","─"*80]
    for line in ai_brain_insights(files, duplicates, options): L.append(f"  {line}")
    L+=["","AI LEARNING MODEL","─"*80]
    for line in AI_MODEL.summary_lines(): L.append(f"  {line}")
    L+=["","AUTONOMOUS CLEANUP PLAN  (verify before deleting)","─"*80]
    for item in plan[:60]:
        L.append(f"  {item.risk.upper():>7}  {item.confidence:>3}%  {human_size(item.reclaimable):>10}  {item.path}")
        L.append(f"           {item.reason}")
    L+=["","FILE TYPE STORAGE","─"*80]
    for cat,cnt,sz in category_summary(files)[:20]:
        L.append(f"  {cat:<14} {cnt:>8,} files  {human_size(sz):>10}")
    L+=["","TOP LARGE FILES","─"*80]
    for r in large_files[:30]:
        L.append(f"  {human_size(r.size):>10}  {format_dt(r.modified)}  {r.path}")
    L+=["","DUPLICATE GROUPS","─"*80]
    for fh,group in sorted(duplicates.items(),key=lambda x:x[1][0].size*len(x[1]),reverse=True)[:30]:
        L.append(f"  SHA256:{fh[:20]}…  {len(group)} copies  each {human_size(group[0].size)}")
        for r in sorted(group,key=lambda x:x.modified,reverse=True):
            L.append(f"    {r.path}  mod:{format_dt(r.modified)}")
    L+=["","EXTENSION BREAKDOWN","─"*80]
    for ext,cnt,sz in extension_summary(files)[:25]:
        L.append(f"  {ext:>16}  {cnt:>8,} files  {human_size(sz):>10}")
    L+=["","TOP FOLDERS","─"*80]
    for fld,cnt,sz in folder_summary(files,options.root)[:25]:
        L.append(f"  {human_size(sz):>10}  {cnt:>7,} files  {fld}")
    return "\n".join(L)

# ════════════════════════════════════════════════════════════════════════════
#  DASHBOARD
# ════════════════════════════════════════════════════════════════════════════

def print_scan_dashboard(result):
    files      = result.files
    duplicates = result.duplicates
    dup_waste  = sum(sum(r.size for r in g[1:]) for g in duplicates.values())
    total_size = sum(r.size for r in files)
    large_cnt  = sum(1 for r in files if r.size>=result.options.large_bytes)
    old_cutoff = datetime.now()-timedelta(days=result.options.old_days)
    old_cnt    = sum(1 for r in files if datetime.fromtimestamp(r.modified)<=old_cutoff)
    plan       = autonomous_cleanup_plan(files, duplicates, result.options)
    du         = disk_usage_for(str(result.options.root.anchor or result.options.root))

    section_title("SCAN RESULTS", "◈", C.BCYN)
    print()

    if du:
        tot,used,free,pct = du
        col = C.BRED if pct>=90 else C.BYLW if pct>=70 else C.BGRN
        b   = bar_chart(int(pct),100,32,col)
        print(f"  {p('Drive usage:', C.GRAY)} {b} {p(f'{pct:.1f}%', col+C.BOLD)}  free {p(human_size(free),C.BGRN)}  total {human_size(tot)}")
        print()

    # Clean stat grid — 4 wide then 4 wide
    stat_grid([
        (f"{len(files):,}",        "FILES"),
        (human_size(total_size),   "TOTAL SIZE"),
        (f"{len(duplicates):,}",   "DUP GROUPS"),
        (human_size(dup_waste),    "DUP WASTE"),
    ])
    stat_grid([
        (f"{large_cnt:,}",         "LARGE FILES"),
        (f"{old_cnt:,}",           "OLD FILES"),
        (f"{len(plan):,}",         "AI CANDIDATES"),
        (f"{result.elapsed:.1f}s", "SCAN TIME"),
    ])
    print()

    # Category bar chart
    section_title("FILE TYPE BREAKDOWN", "▦", C.BMAG)
    cats = category_summary(files)
    max_sz = cats[0][2] if cats else 1
    for name,cnt,sz in cats[:8]:
        pct_str = f"{sz/total_size*100:4.1f}%" if total_size else "  0.0%"
        b = bar_chart(sz,max_sz,24, C.BMAG)
        print(f"  {p(name,C.BWHT):<13} {b}  {p(human_size(sz),C.BYLW):>10}  {p(pct_str,C.GRAY)}  {p(f'{cnt:,} files',C.GRAY)}")
    print()

    # AI brain summary
    section_title("AI BRAIN", "⬡", C.BYLW)
    for line in ai_brain_insights(files, duplicates, result.options):
        if "⚠" in line: icon=p("◉",C.BRED)
        elif "↻" in line: icon=p("◎",C.BYLW)
        else: icon=p("✓",C.BGRN)
        clean = line.replace("⚠","").replace("↻","").replace("✓","").strip()
        print(f"  {icon} {p(clean,C.BWHT)}")
    print()
    print(f"  {p('Report saved:', C.GRAY)} {p(str(result.report_path),C.BCYN+C.ITAL)}")

# ════════════════════════════════════════════════════════════════════════════
#  DELETE MANAGER
# ════════════════════════════════════════════════════════════════════════════

def delete_files_interactive(candidates, title="Delete Selected Files"):
    """Show a numbered list, let user select which to delete, confirm, then delete."""
    clear(); banner()
    section_title(f"DELETE MANAGER — {title}", "✗", C.BRED)
    print(p("  Files are moved to OS trash / renamed .daitrash (recoverable). Nothing is permanently erased here.\n", C.GRAY+C.ITAL))

    if not candidates:
        print(p("  No candidates to delete.", C.GRAY))
        wait_key(); return

    # Show numbered list
    hr("─",C.GRAY)
    print(f"  {'#':>4}  {'RISK':<8}  {'SIZE':>10}  {'SCORE':>5}  {'CAT':<12}  PATH")
    hr("─",C.GRAY)
    for i,c in enumerate(candidates[:80],1):
        rc = risk_color(c.risk)
        ri = risk_icon(c.risk)
        print(f"  {p(str(i),C.GRAY):>4}  {p(ri+' '+c.risk.upper(),rc):<8}  {p(human_size(c.reclaimable),C.BYLW):>10}"
              f"  {p(str(c.ai_score),conf_color(c.confidence)):>5}  {p(c.category,C.GRAY):<12}  {p(compact_path(c.path,48),C.BWHT)}")
        print(f"  {'':>4}  {p(c.reason,C.GRAY+C.ITAL)}")
    hr("─",C.GRAY)
    total_reclaimable = sum(c.reclaimable for c in candidates[:80])
    print(f"\n  {p('Total reclaimable:', C.GRAY)} {p(human_size(total_reclaimable), C.BRED+C.BOLD)}")

    print(p("\n  Select files to delete:", C.BWHT))
    print(p("  Examples:  all  |  1,3,5  |  1-10  |  1-5,8,12  |  cancel", C.GRAY))
    raw = input(p("  Selection → ", C.BCYN)).strip().lower()

    if not raw or raw == "cancel":
        print(p("  Cancelled.", C.GRAY)); wait_key(); return

    selected = parse_selection(raw, len(candidates[:80]))
    if not selected:
        print(p("  No valid selection.", C.BRED)); wait_key(); return

    chosen = [candidates[i-1] for i in selected if 1<=i<=len(candidates)]
    if not chosen:
        print(p("  No valid candidates selected.", C.BRED)); wait_key(); return

    # Final confirmation screen
    clear(); banner()
    section_title("FINAL CONFIRMATION", "⚠", C.BRED)
    print(p(f"\n  You are about to delete {len(chosen)} file(s):\n", C.BWHT+C.BOLD))

    keep_list = [c for c in candidates if c not in chosen]
    total_del = sum(c.reclaimable for c in chosen)

    print(p("  ── WILL BE DELETED ──────────────────────────────────────────────────────", C.BRED))
    for c in chosen:
        print(f"  {p('✗',C.BRED)} {p(human_size(c.reclaimable),C.BYLW):>10}  {p(c.category,C.GRAY):<12}  {p(compact_path(c.path,50),C.BWHT)}")
        print(f"  {p('  '+c.reason,C.GRAY+C.ITAL)}")

    print()
    print(p("  ── WILL BE KEPT ─────────────────────────────────────────────────────────", C.BGRN))
    for c in keep_list[:10]:
        print(f"  {p('✓',C.BGRN)} {p(human_size(c.reclaimable),C.BYLW):>10}  {p(c.category,C.GRAY):<12}  {p(compact_path(c.path,50),C.BWHT)}")
    if len(keep_list)>10:
        print(f"  {p('✓',C.BGRN)} … and {len(keep_list)-10} more kept files")

    print()
    print(p(f"  ┌─────────────────────────────────────────────┐", C.BYLW+C.BOLD))
    print(p(f"  │  Space to reclaim: {human_size(total_del):<27}│", C.BYLW+C.BOLD))
    print(p(f"  │  Method: OS trash / .daitrash rename        │", C.BYLW+C.BOLD))
    print(p(f"  │  Undo: restore from trash or rename back    │", C.BYLW+C.BOLD))
    print(p(f"  └─────────────────────────────────────────────┘", C.BYLW+C.BOLD))
    print()

    confirm = input(p("  Type YES to proceed or anything else to cancel: ", C.BRED+C.BOLD)).strip()
    if confirm != "YES":
        print(p("  Cancelled. No files were touched.", C.BGRN))
        wait_key(); return

    # Execute deletions
    print()
    ok_count = 0; fail_count = 0; reclaimed = 0
    for c in chosen:
        ok, msg = safe_delete(c.path)
        if ok:
            log_deletion(c.path, c.reason, msg, c.reclaimable)
            AI_MODEL.record_feedback(str(c.path), "deleted", c.reason)
            print(f"  {p('✓',C.BGRN)} {p(compact_path(c.path,60),C.GRAY)}  {p(msg,C.BGRN)}")
            ok_count+=1; reclaimed+=c.reclaimable
        else:
            print(f"  {p('✗',C.BRED)} {p(compact_path(c.path,60),C.GRAY)}  {p(msg,C.BRED)}")
            fail_count+=1

    print()
    print(p(f"  ✓ Deleted: {ok_count}  ✗ Failed: {fail_count}  Space reclaimed: {human_size(reclaimed)}", C.BWHT+C.BOLD))
    AI_MODEL.save()
    wait_key()

def parse_selection(raw, max_n):
    """Parse 'all', '1,3,5', '1-10', '1-5,8' into a set of 1-based ints."""
    if raw == "all":
        return set(range(1, max_n+1))
    result = set()
    for part in raw.split(","):
        part = part.strip()
        if "-" in part:
            try:
                a, b = part.split("-",1)
                result.update(range(int(a), int(b)+1))
            except ValueError:
                pass
        else:
            try:
                result.add(int(part))
            except ValueError:
                pass
    return result

# ════════════════════════════════════════════════════════════════════════════
#  AUTONOMOUS MODE
# ════════════════════════════════════════════════════════════════════════════

def run_autonomous_mode(result):
    """Full autonomous analysis: show plan, single confirmation, execute."""
    files      = result.files
    duplicates = result.duplicates
    options    = result.options
    now        = datetime.now()
    profile    = ai_brain_profile(files, duplicates, options)
    plan       = autonomous_cleanup_plan(files, duplicates, options)

    clear(); banner()
    section_title("AUTONOMOUS CLEANUP MODE", "⬡", C.BRED)
    print(p("\n  The AI has analysed your drive and prepared a cleanup plan.", C.BWHT))
    print(p("  Review the plan carefully. ONE confirmation executes everything.\n", C.GRAY+C.ITAL))

    if not plan:
        print(p("  ✓ AI found no cleanup candidates. Your drive looks clean.", C.BGRN))
        wait_key(); return

    # ── KEEP LIST ──
    all_files    = set(r.path for r in files)
    delete_paths = set(c.path for c in plan)
    keep_files   = [r for r in files if r.path not in delete_paths]

    # Group plan by reason type
    dup_items    = [c for c in plan if c.category=="Duplicate"]
    temp_items   = [c for c in plan if c.risk=="review" and c.category in {"Logs/Temp"}]
    old_items    = [c for c in plan if c.risk=="review" and "old" in c.reason.lower() and c.category!="Duplicate"]
    large_items  = [c for c in plan if c.risk=="inspect"]
    other_items  = [c for c in plan if c not in dup_items+temp_items+old_items+large_items]

    def print_group(title, items, color=C.BRED):
        if not items: return
        section_title(f"DELETE — {title} ({len(items)} files  {human_size(sum(c.reclaimable for c in items))})", "✗", color)
        for c in items[:20]:
            print(f"  {p('✗',color)} {p(human_size(c.reclaimable),C.BYLW):>10}  {p(c.category,C.GRAY):<12}  {p(compact_path(c.path,52),C.BWHT)}")
            print(f"       {p(c.reason,C.GRAY+C.ITAL)}")
        if len(items)>20:
            print(p(f"  … and {len(items)-20} more in this group", C.GRAY))

    print_group("DUPLICATES",           dup_items,  C.BRED)
    print_group("TEMP / LOG / CACHE",   temp_items, C.BYLW)
    print_group("OLD INACTIVE FILES",   old_items,  C.BYLW)
    print_group("LARGE LOW-USE FILES",  large_items, C.BMAG)
    print_group("OTHER AI CANDIDATES",  other_items, C.GRAY)

    # ── KEEP SUMMARY ──
    section_title("KEEPING — EVERYTHING ELSE", "✓", C.BGRN)
    keep_cats = defaultdict(lambda:[0,0])
    for r in keep_files:
        k=file_category(r); keep_cats[k][0]+=1; keep_cats[k][1]+=r.size
    for cat,(cnt,sz) in sorted(keep_cats.items(),key=lambda x:-x[1][1])[:10]:
        print(f"  {p('✓',C.BGRN)} {p(cat,C.BWHT):<13}  {cnt:>7,} files  {p(human_size(sz),C.BGRN)}")
    print(p(f"\n  Total kept: {len(keep_files):,} files  ({human_size(sum(r.size for r in keep_files))})", C.BWHT))

    # ── SUMMARY BOX ──
    total_del = sum(c.reclaimable for c in plan)
    total_files_del = len(plan)
    print()
    print(p("  ╔════════════════════════════════════════════════════════════╗", C.BYLW+C.BOLD))
    print(p(f"  ║  AUTONOMOUS PLAN SUMMARY                                   ║", C.BYLW+C.BOLD))
    print(p(f"  ║                                                            ║", C.BYLW+C.BOLD))
    print(p(f"  ║  Files to delete:    {total_files_del:<40}║", C.BYLW+C.BOLD))
    print(p(f"  ║  Space to reclaim:   {human_size(total_del):<40}║", C.BYLW+C.BOLD))
    print(p(f"  ║  Files to keep:      {len(keep_files):<40}║", C.BYLW+C.BOLD))
    print(p(f"  ║  Method:             OS trash / .daitrash rename           ║", C.BYLW+C.BOLD))
    print(p(f"  ║  Undo:               restore from trash or rename back     ║", C.BYLW+C.BOLD))
    print(p(f"  ║                                                            ║", C.BYLW+C.BOLD))
    print(p(f"  ║  AI confidence range: {min(c.confidence for c in plan)}% – {max(c.confidence for c in plan)}%{' '*30}║", C.BYLW+C.BOLD))
    print(p(f"  ╚════════════════════════════════════════════════════════════╝", C.BYLW+C.BOLD))
    print()

    print(p("  ─── OPTIONS ───────────────────────────────────────────────", C.GRAY))
    print(f"  {p('[1]',C.BGRN+C.BOLD)} Execute full plan  ({len(plan)} files  {human_size(total_del)})")
    print(f"  {p('[2]',C.BCYN+C.BOLD)} Execute duplicates only  ({len(dup_items)} files  {human_size(sum(c.reclaimable for c in dup_items))})")
    print(f"  {p('[3]',C.BYLW+C.BOLD)} Execute high-confidence only (≥80%)  ({sum(1 for c in plan if c.confidence>=80)} files)")
    print(f"  {p('[4]',C.BMAG+C.BOLD)} Custom selection — choose individual files")
    print(f"  {p('[C]',C.GRAY+C.BOLD)} Cancel — no changes")
    print()

    choice = input(p("  Choice → ", C.BCYN)).strip().upper()

    if choice == "C" or not choice:
        print(p("  Cancelled. No files were touched.", C.BGRN)); wait_key(); return

    if choice == "1":
        to_delete = plan
        tag = "FULL PLAN"
    elif choice == "2":
        to_delete = dup_items
        tag = "DUPLICATES ONLY"
    elif choice == "3":
        to_delete = [c for c in plan if c.confidence>=80]
        tag = "HIGH CONFIDENCE ONLY"
    elif choice == "4":
        delete_files_interactive(plan, "Custom Selection")
        return
    else:
        print(p("  Invalid choice.", C.BRED)); wait_key(); return

    if not to_delete:
        print(p("  Nothing to delete in this category.", C.GRAY)); wait_key(); return

    # Final hard confirmation
    print()
    print(p(f"  ⚠ FINAL CONFIRMATION — {tag}", C.BRED+C.BOLD))
    confirm = input(p(f"  Type YES to delete {len(to_delete)} files and reclaim {human_size(sum(c.reclaimable for c in to_delete))}: ", C.BRED+C.BOLD)).strip()

    if confirm != "YES":
        print(p("  Cancelled. No files were touched.", C.BGRN)); wait_key(); return

    # Execute
    print()
    ok_count=0; fail_count=0; reclaimed=0
    for c in to_delete:
        ok, msg = safe_delete(c.path)
        if ok:
            log_deletion(c.path, c.reason, msg, c.reclaimable)
            AI_MODEL.record_feedback(str(c.path), "deleted", c.reason)
            print(f"  {p('✓',C.BGRN)} {p(compact_path(c.path,64),C.GRAY)}  {p(msg,C.BGRN)}")
            ok_count+=1; reclaimed+=c.reclaimable
        else:
            print(f"  {p('✗',C.BRED)} {p(compact_path(c.path,64),C.GRAY)}  {p(msg,C.BRED)}")
            fail_count+=1

    AI_MODEL.save()
    print()
    print(p("  ╔══════════════════════════════════════════════╗", C.BGRN+C.BOLD))
    print(p(f"  ║  ✓ Complete!  Deleted: {ok_count:<5} Failed: {fail_count:<5}     ║", C.BGRN+C.BOLD))
    print(p(f"  ║  Space reclaimed: {human_size(reclaimed):<29}║", C.BGRN+C.BOLD))
    print(p("  ╚══════════════════════════════════════════════╝", C.BGRN+C.BOLD))
    wait_key()

# ════════════════════════════════════════════════════════════════════════════
#  VIEW SCREENS
# ════════════════════════════════════════════════════════════════════════════

def show_cleanup_plan(result):
    clear(); banner()
    plan = autonomous_cleanup_plan(result.files, result.duplicates, result.options)
    section_title("AI CLEANUP PLAN", "⬡", C.BYLW)

    if not plan:
        print(p("  ✓ No candidates found.", C.BGRN)); wait_key(); return

    total_r = sum(c.reclaimable for c in plan)
    print(f"  {p(f'{len(plan)} candidates',C.BWHT+C.BOLD)}  |  potential: {p(human_size(total_r),C.BYLW+C.BOLD)}\n")
    hr("─",C.GRAY)
    print(f"  {'#':>3}  {'RISK':<9}  {'CONF':>4}  {'SIZE':>10}  {'SCORE':>5}  {'CAT':<12}  PATH")
    hr("─",C.GRAY)
    for i,c in enumerate(plan[:60],1):
        rc = risk_color(c.risk)
        print(f"  {p(str(i),C.GRAY):>3}  {p(risk_icon(c.risk)+' '+c.risk.upper(),rc):<9}  "
              f"{p(str(c.confidence)+'%',conf_color(c.confidence)):>4}  "
              f"{p(human_size(c.reclaimable),C.BYLW):>10}  "
              f"{p(str(c.ai_score),C.BCYN):>5}  "
              f"{p(c.category,C.GRAY):<12}  {p(compact_path(c.path,46),C.BWHT)}")
        print(f"  {'':>3}  {p(c.reason,C.GRAY+C.ITAL)}")
    hr("─",C.GRAY)
    print()
    print(f"  {p('[D]',C.BRED+C.BOLD)} Delete selected files   {p('[F]',C.BCYN+C.BOLD)} Teach AI (feedback)   {p('[Enter]',C.GRAY)} Back")
    choice = input(p("  → ", C.BCYN)).strip().upper()
    if choice == "D":
        delete_files_interactive(plan, "AI Cleanup Candidates")
    elif choice == "F":
        feedback_prompt(plan)

def feedback_prompt(plan):
    print()
    idx = input(p(f"  Candidate number to give feedback on (1–{min(len(plan),60)}): ", C.BYLW)).strip()
    try:
        i = int(idx)-1
        if 0<=i<len(plan):
            item = plan[i]
            print(f"  File: {p(str(item.path),C.BWHT)}")
            print(f"  {p('[K]',C.BGRN)} Kept it (false alarm)   {p('[D]',C.BRED)} Deleted it   {p('[S]',C.GRAY)} Skip")
            act = input("  → ").strip().lower()
            reason = ""
            if act in ("k","d"):
                reason = input(p("  Why (optional): ",C.GRAY)).strip()
            if act == "k":
                AI_MODEL.record_feedback(str(item.path),"kept",reason)
                print(p("  ✓ Recorded. AI will down-weight this pattern.",C.BGRN))
            elif act == "d":
                AI_MODEL.record_feedback(str(item.path),"deleted",reason)
                print(p("  ✓ Recorded. AI will up-weight confidence for similar files.",C.BGRN))
    except ValueError:
        print(p("  Invalid input.",C.BRED))
    wait_key()

def show_duplicates(duplicates):
    clear(); banner()
    section_title("DUPLICATE CONTENT GROUPS", "◈", C.BRED)
    if not duplicates:
        print(p("  ✓ No duplicates found.",C.BGRN)); wait_key(); return
    total_waste = sum(sum(r.size for r in g[1:]) for g in duplicates.values())
    plan = [CleanupCandidate("review",90,r.size,"duplicate",r.path,"Duplicate",20,0)
            for g in duplicates.values()
            for r in sorted(g,key=lambda x:x.modified)[:-1]]
    print(f"  {p(f'{len(duplicates)} groups',C.BWHT+C.BOLD)}  |  recoverable: {p(human_size(total_waste),C.BRED+C.BOLD)}\n")
    for fh,group in sorted(duplicates.items(),key=lambda x:x[1][0].size*len(x[1]),reverse=True)[:25]:
        waste=group[0].size*(len(group)-1)
        print(p(f"  ╔══ {fh[:20]}…  {len(group)} copies  {human_size(group[0].size)} each  waste {human_size(waste)}",C.BYLW))
        for r in sorted(group,key=lambda x:x.modified,reverse=True):
            age=(datetime.now()-datetime.fromtimestamp(r.modified)).days
            print(f"  ║  {p(compact_path(r.path,76),C.BWHT)}  {p(f'{age}d',C.GRAY)}")
        print(p("  ╚"+"─"*78,C.GRAY))
    print()
    print(f"  {p('[D]',C.BRED+C.BOLD)} Delete duplicates (keep newest)   {p('[Enter]',C.GRAY)} Back")
    choice = input(p("  → ",C.BCYN)).strip().upper()
    if choice == "D":
        delete_files_interactive(plan, "Duplicate Files — Keep Newest Copy")

def show_large_files(result):
    clear(); banner()
    section_title("LARGEST FILES", "◈", C.BYLW)
    now = datetime.now()
    profile = ai_brain_profile(result.files, result.duplicates, result.options)
    records = sorted(result.files, key=lambda r:r.size, reverse=True)
    print(f"  {'#':>3}  {'SIZE':>10}  {'AGE':>7}  {'ACCESS':>7}  {'SCORE':>5}  {'TYPE':<12}  PATH")
    hr("─",C.GRAY)
    for i,r in enumerate(records[:60],1):
        age  = (now-datetime.fromtimestamp(r.modified)).days
        acc  = (now-datetime.fromtimestamp(r.accessed)).days
        sc,_ = relevance_score(r,now,profile)
        sc_c = C.BRED if sc<=30 else C.BYLW if sc<=55 else C.BGRN
        print(f"  {p(str(i),C.GRAY):>3}  {p(human_size(r.size),C.BYLW):>10}  {age:>6}d  {acc:>6}d  "
              f"{p(str(sc),sc_c):>5}  {p(file_category(r),C.GRAY):<12}  {p(compact_path(r.path,46),C.BWHT)}")
    print()
    plan = [CleanupCandidate("inspect",50,r.size,f"large file score={sc}",
                             r.path,file_category(r),sc,0)
            for r in records[:60]
            for sc,_ in [relevance_score(r,now,profile)]]
    print(f"  {p('[D]',C.BRED+C.BOLD)} Delete selected large files   {p('[Enter]',C.GRAY)} Back")
    choice = input(p("  → ",C.BCYN)).strip().upper()
    if choice == "D":
        delete_files_interactive(plan, "Large Files")

def show_folder_heatmap(result):
    clear(); banner()
    section_title("FOLDER SIZE HEATMAP", "▦", C.BCYN)
    folders = folder_summary(result.files, result.options.root)
    max_sz  = folders[0][2] if folders else 1
    total   = sum(r.size for r in result.files) or 1
    for fld,cnt,sz in folders[:30]:
        pct=sz/total*100
        b=bar_chart(sz,max_sz,24,C.BCYN)
        print(f"  {b}  {p(human_size(sz),C.BYLW):>10}  {pct:5.1f}%  {p(f'{cnt:,} files',C.GRAY):>14}  {p(compact_path(fld,44),C.BWHT)}")
    wait_key()

def show_filetype_heatmap(result):
    clear(); banner()
    section_title("FILE TYPE HEATMAP", "▦", C.BMAG)
    cats   = category_summary(result.files)
    max_sz = cats[0][2] if cats else 1
    total  = sum(r.size for r in result.files) or 1
    for name,cnt,sz in cats:
        pct=sz/total*100
        b=bar_chart(sz,max_sz,24,C.BMAG)
        print(f"  {p(name,C.BWHT):<13} {b}  {p(human_size(sz),C.BYLW):>10}  {pct:5.1f}%  {p(f'{cnt:,} files',C.GRAY)}")
    wait_key()

def show_extension_breakdown(result):
    clear(); banner()
    section_title("EXTENSION BREAKDOWN + AI FLAGS", "◈", C.BCYN)
    exts   = extension_summary(result.files)
    max_sz = exts[0][2] if exts else 1
    for ext,cnt,sz in exts[:35]:
        b    = bar_chart(sz,max_sz,18,C.BCYN)
        safe = AI_MODEL.data["confirmed_safe_extensions"].get(ext,0)
        wst  = AI_MODEL.data["confirmed_waste_extensions"].get(ext,0)
        flag = p(f" ✓{safe}",C.BGRN) if safe else p(f" ✗{wst}",C.BRED) if wst else ""
        print(f"  {p(ext,C.BWHT):>18}  {b}  {p(human_size(sz),C.BYLW):>10}  {p(f'{cnt:,}',C.GRAY):>8} files{flag}")
    wait_key()

def show_ai_brain_detail(result):
    clear(); banner()
    section_title("AI BRAIN DEEP ANALYSIS", "⬡", C.BYLW)
    for line in ai_brain_insights(result.files, result.duplicates, result.options):
        print(f"  {p('◈',C.BCYN)} {line}")

    profile = ai_brain_profile(result.files, result.duplicates, result.options)
    section_title("ADAPTIVE THRESHOLDS", "◎", C.BCYN)
    for label,val in [
        ("Configured large", human_size(result.options.large_bytes)),
        ("AI smart large",   human_size(AI_MODEL.smart_large_threshold(result.options.large_bytes))),
        ("High-large p95",   human_size(profile["high_large"])),
        ("Extreme-large p99",human_size(profile["extreme_large"])),
        ("Old threshold",    f"{result.options.old_days}d"),
        ("Drive-adaptive old",f"{profile['normal_old']}d"),
        ("Very old",         f"{profile['very_old']}d"),
        ("Stale access",     f"{profile['stale_access']}d"),
    ]:
        print(f"  {p(label+':',C.GRAY):<30} {p(val,C.BWHT+C.BOLD)}")

    section_title("LEARNING MODEL", "⬡", C.BMAG)
    for line in AI_MODEL.summary_lines():
        print(f"  {p('◈',C.BCYN)} {line}")
    hist = AI_MODEL.data.get("duplicate_ratio_history",[])
    if len(hist)>=3:
        spark = sparkline([int(v*100) for v in hist],30)
        print(f"\n  Dup ratio trend: {p(spark,C.BYLW)}")
    wait_key()

def show_ai_learning_detail():
    clear(); banner()
    section_title("AI LEARNING MODEL DETAIL", "⬡", C.BMAG)
    data = AI_MODEL.data
    print(f"\n  Scans: {p(str(data.get('scan_count',0)),C.BWHT+C.BOLD)}   Feedback events: {p(str(len(data.get('feedback_log',[]))),C.BWHT+C.BOLD)}")
    safe_e  = sorted(data["confirmed_safe_extensions"].items(),  key=lambda x:-x[1])
    waste_e = sorted(data["confirmed_waste_extensions"].items(), key=lambda x:-x[1])
    if safe_e:
        section_title("SAFE EXTENSIONS LEARNED", "✓", C.BGRN)
        for ext,cnt in safe_e[:12]:
            b=bar_chart(cnt, safe_e[0][1]+1, 12, C.BGRN)
            print(f"  {p(ext,C.BGRN):>18}  {b}  {cnt} events")
    if waste_e:
        section_title("RISKY EXTENSIONS LEARNED", "✗", C.BRED)
        for ext,cnt in waste_e[:12]:
            b=bar_chart(cnt, waste_e[0][1]+1, 12, C.BRED)
            print(f"  {p(ext,C.BRED):>18}  {b}  {cnt} events")
    pats = sorted(data.get("path_risk_patterns",{}).items(), key=lambda x:-abs(x[1]))
    if pats:
        section_title("PATH PATTERNS", "◈", C.BCYN)
        for pat,sc in pats[:12]:
            col = C.BGRN if sc<0 else C.BRED
            print(f"  {p(pat,col):>24}  {p(f'{sc:+d}',col)}")
    feedback = data.get("feedback_log",[])
    if feedback:
        section_title("RECENT FEEDBACK", "◷", C.BYLW)
        for ev in feedback[-8:]:
            col=C.BGRN if ev["action"]=="kept" else C.BRED if ev["action"]=="deleted" else C.GRAY
            print(f"  {ev.get('ts','')[:16]}  {p(ev['action'].upper(),col)}  {compact_path(ev.get('path',''),60)}")

    # Show deletion log
    try:
        if TRASH_LOG_PATH.exists():
            log = json.loads(TRASH_LOG_PATH.read_text(encoding="utf-8"))
            if log:
                section_title("DELETION LOG (recoverable)", "◷", C.GRAY)
                for ev in log[:8]:
                    print(f"  {ev.get('ts','')[:16]}  {p(human_size(ev.get('size',0)),C.BYLW):>10}  {compact_path(ev.get('path',''),56)}")
                    print(f"                       {p(ev.get('method',''),C.GRAY+C.ITAL)}")
    except (OSError, json.JSONDecodeError):
        pass
    wait_key()

def show_previous_scan_details():
    clear(); banner()
    section_title("SCAN HISTORY", "◷", C.BMAG)
    h = load_history()
    if not h:
        print(p("  No history yet.",C.GRAY)); wait_key(); return
    for i,e in enumerate(h[:10],1):
        files_str = f"{int(e.get('files',0)):,}"
        print(f"\n  {p(str(i)+'.',C.GRAY)} {p(e.get('generated','?')[:16],C.BCYN)}  {p(e.get('root','?'),C.BWHT+C.BOLD)}")
        print(f"     files {p(files_str,C.BWHT)}  size {p(e.get('total_size_text','0B'),C.BYLW)}  dup waste {p(e.get('duplicate_waste_text','0B'),C.BRED)}  denied {e.get('denied',0)}")
        cats=e.get("categories",[])
        if cats:
            print("     types: "+"  ".join(f"{c['name']} {c['size_text']}" for c in cats[:4]))
        print(f"     {p(e.get('report_path',''),C.GRAY+C.ITAL)}")
    wait_key()

# ════════════════════════════════════════════════════════════════════════════
#  RESULT BROWSER
# ════════════════════════════════════════════════════════════════════════════

def result_browser(result):
    while True:
        clear(); banner()
        print_scan_dashboard(result)
        hr("─",C.GRAY)
        menu = [
            ("1","⬡ AUTONOMOUS MODE — AI decides, one confirmation",  C.BRED),
            ("2","◉ AI Cleanup Plan — review & selective delete",       C.BYLW),
            ("3","◈ Duplicate Groups — view & delete duplicates",       C.BMAG),
            ("4","◈ Largest Files — view & delete by size",             C.BCYN),
            ("5","▦ Folder Heatmap",                                    C.BCYN),
            ("6","▦ File Type Heatmap",                                 C.BMAG),
            ("7","◈ Extension Breakdown + AI flags",                    C.GRAY),
            ("8","⬡ AI Brain Deep Analysis",                            C.BYLW),
            ("9","⬡ AI Learning Model Detail",                          C.BMAG),
            ("0","↩ Back to Main Menu",                                 C.GRAY),
        ]
        print()
        for key,label,col in menu:
            print(f"  {p('['+key+']',col+C.BOLD)}  {p(label,C.BWHT)}")
        print()
        choice = input(p("  Select → ",C.BCYN)).strip()

        if   choice=="1": run_autonomous_mode(result)
        elif choice=="2": show_cleanup_plan(result)
        elif choice=="3": show_duplicates(result.duplicates)
        elif choice=="4": show_large_files(result)
        elif choice=="5": show_folder_heatmap(result)
        elif choice=="6": show_filetype_heatmap(result)
        elif choice=="7": show_extension_breakdown(result)
        elif choice=="8": show_ai_brain_detail(result)
        elif choice=="9": show_ai_learning_detail()
        elif choice=="0": break
        else: print(p("  Invalid choice.",C.BRED)); time.sleep(0.7)

# ════════════════════════════════════════════════════════════════════════════
#  SCAN RUNNER
# ════════════════════════════════════════════════════════════════════════════

def run_scan(options):
    clear(); banner()
    if not options.root.exists():
        raise FileNotFoundError(f"Path does not exist: {options.root}")

    started = time.time()
    section_title("SCAN CONFIGURATION", "◈", C.BCYN)
    for label,val in [
        ("Target",          str(options.root)),
        ("Workers",         str(options.workers)),
        ("Large threshold", human_size(options.large_bytes)),
        ("Old threshold",   f"{options.old_days} days"),
        ("AI smart large",  human_size(AI_MODEL.smart_large_threshold(options.large_bytes))),
        ("Skip preset",     options.skip_preset),
        ("Extra skips",     ", ".join(options.exclude_patterns) or "none"),
        ("AI scans so far", str(AI_MODEL.data.get("scan_count",0))),
    ]:
        print(f"  {p(label+':',C.GRAY):<22} {p(val,C.BWHT)}")
    print()

    print(p("  Stage 1/3  Collecting file metadata…", C.BCYN))
    files, denied, empty_dirs, skipped_dirs = collect_files(options, live=True)

    print(p(f"  Stage 2/3  Hashing {len(files):,} files for duplicates…", C.BCYN))
    duplicates = find_duplicates(files, options.workers, live=True)

    print(p("  Stage 3/3  Building report and AI analysis…", C.BCYN))
    AI_MODEL.record_scan(files, duplicates, options)
    report = build_report(files, duplicates, options, denied, empty_dirs, skipped_dirs)
    safe_name = "".join(c if c.isalnum() else "_" for c in str(options.root))[:50].strip("_") or "scan"
    out = Path.cwd() / f"disk_ai_report_{safe_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    out.write_text(report, encoding="utf-8")

    elapsed = time.time()-started
    result  = ScanResult(files, duplicates, denied, empty_dirs, skipped_dirs, out, elapsed, options)
    save_history_entry(result)
    print()
    print_scan_dashboard(result)
    return result

# ════════════════════════════════════════════════════════════════════════════
#  INTERACTIVE MENU
# ════════════════════════════════════════════════════════════════════════════

def prompt_path():
    raw = input(p("  Scan path (e.g. C:\\ or /home/user): ",C.BCYN)).strip().strip('"')
    if not raw: raw = str(Path.home())
    return Path(raw).expanduser()

def prompt_int(label, default, minimum=1):
    raw = input(p(f"  {label} [{default}]: ",C.GRAY)).strip()
    if not raw: return default
    try: return max(minimum,int(raw))
    except ValueError: print(p("  Invalid, using default.",C.BRED)); return default

def prompt_skip_preset(default="none"):
    print(p("  Presets: none | safe (skip cache/temp) | aggressive (skip most system)", C.GRAY))
    raw = input(p(f"  Skip preset [{default}]: ",C.GRAY)).strip().lower()
    if not raw: return default
    if raw not in SKIP_PRESETS: print(p("  Unknown, using safe.",C.BRED)); return "safe"
    return raw

def prompt_excludes():
    raw = input(p("  Extra folder skips, comma-separated [none]: ",C.GRAY)).strip()
    if not raw: return ()
    return tuple(x.strip() for x in raw.split(",") if x.strip())

def autopilot_options(root):
    cpu = os.cpu_count() or 4
    return ScanOptions(root=root, large_bytes=DEFAULT_LARGE_MB*1024*1024,
                       old_days=DEFAULT_OLD_DAYS, workers=max(4,min(24,cpu*2)),
                       admin=is_admin(), autopilot=True, skip_preset="none")

def interactive_menu():
    last_result = None
    while True:
        clear(); banner()
        show_disk_overview()
        show_history_summary()

        section_title("MISSION CONTROL", "◈", C.BCYN)
        print()
        menu = [
            ("1","Autopilot scan — smart defaults, scan everything",    C.BGRN),
            ("2","Custom deep scan — configure all parameters",          C.BCYN),
            ("3","Browse last scan results + delete tools",              C.BYLW),
            ("4","View AI Learning Model",                               C.BMAG),
            ("5","View scan history",                                    C.GRAY),
            ("6","Relaunch with admin privileges",                       C.GRAY),
            ("7","Exit",                                                  C.GRAY),
        ]
        for key,label,col in menu:
            print(f"  {p('['+key+']',col+C.BOLD)}  {p(label,C.BWHT)}")
        print()
        choice = input(p("  Select → ",C.BCYN)).strip()

        if choice=="1":
            root = prompt_path()
            opts = autopilot_options(root)
            try:
                last_result = run_scan(opts)
                result_browser(last_result)
            except Exception as exc:
                print(p(f"\n  Scan failed: {exc}",C.BRED)); wait_key()

        elif choice=="2":
            root     = prompt_path()
            large_mb = prompt_int("Large file threshold in MB", DEFAULT_LARGE_MB)
            old_days = prompt_int("Old file threshold in days", DEFAULT_OLD_DAYS)
            workers  = prompt_int("Worker threads", max(4, os.cpu_count() or 4))
            preset   = prompt_skip_preset()
            excludes = prompt_excludes()
            opts = ScanOptions(root=root, large_bytes=large_mb*1024*1024,
                               old_days=old_days, workers=workers, admin=is_admin(),
                               skip_preset=preset, exclude_patterns=excludes)
            try:
                last_result = run_scan(opts)
                result_browser(last_result)
            except Exception as exc:
                print(p(f"\n  Scan failed: {exc}",C.BRED)); wait_key()

        elif choice=="3":
            if last_result: result_browser(last_result)
            else: print(p("\n  No scan in memory yet.",C.GRAY)); wait_key()

        elif choice=="4":
            show_ai_learning_detail()

        elif choice=="5":
            show_previous_scan_details()

        elif choice=="6":
            if is_admin(): print(p("\n  Already admin.",C.BGRN)); wait_key()
            else: relaunch_as_admin()

        elif choice=="7":
            break
        else:
            print(p("  Invalid choice.",C.BRED)); time.sleep(0.7)

# ════════════════════════════════════════════════════════════════════════════
#  CLI ENTRY POINT
# ════════════════════════════════════════════════════════════════════════════

def parse_args():
    parser = argparse.ArgumentParser(description=f"{APP_NAME} v{APP_VERSION}")
    parser.add_argument("--path")
    parser.add_argument("--admin",       action="store_true")
    parser.add_argument("--large-mb",    type=int, default=DEFAULT_LARGE_MB)
    parser.add_argument("--old-days",    type=int, default=DEFAULT_OLD_DAYS)
    parser.add_argument("--workers",     type=int, default=max(4,os.cpu_count() or 4))
    parser.add_argument("--autopilot",   action="store_true")
    parser.add_argument("--skip-preset", choices=sorted(SKIP_PRESETS), default="none")
    parser.add_argument("--exclude",     action="append", default=[])
    return parser.parse_args()

def main():
    enable_ansi()
    args = parse_args()
    if args.admin and not is_admin():
        relaunch_as_admin()
    if args.path:
        root = Path(args.path).expanduser()
        if args.autopilot:
            opts = autopilot_options(root)
            opts = ScanOptions(root=opts.root, large_bytes=opts.large_bytes, old_days=opts.old_days,
                               workers=opts.workers, admin=opts.admin, autopilot=True,
                               skip_preset=args.skip_preset, exclude_patterns=tuple(args.exclude))
        else:
            opts = ScanOptions(root=root, large_bytes=max(1,args.large_mb)*1024*1024,
                               old_days=max(1,args.old_days), workers=max(1,args.workers),
                               admin=is_admin(), skip_preset=args.skip_preset,
                               exclude_patterns=tuple(args.exclude))
        result = run_scan(opts)
        print(f"\n  Report: {result.report_path}")
    else:
        interactive_menu()

if __name__=="__main__":
    main()
