#!/usr/bin/env python3
"""Measures the Python edition against the C++ edition on this machine and writes docs/compare/{results.json,*.png,README.md}.
  cpp/scripts/compare-python-cpp.py [--runs N] [--idle SECONDS]
Needs a graphical session (both GUIs are started un-elevated, on the Dashboard, as the current user) and matplotlib for the charts."""
import argparse, json, os, re, statistics as st, subprocess, sys, time
ROOT = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", ".."))
BIN = os.environ.get("TT_BIN_DIR", "/usr/local/bin")
PY = ["python3", os.path.join(ROOT, "cpp", "scripts", "py_harness.py")]
OUT = os.path.join(ROOT, "docs", "compare")
ap = argparse.ArgumentParser(); ap.add_argument("--runs", type=int, default=7); ap.add_argument("--idle", type=int, default=30)
args = ap.parse_args()
os.makedirs(OUT, exist_ok=True)
env = dict(os.environ); env.setdefault("DISPLAY", ":0")

def tree(pid):
    kids = {}
    for p in os.listdir("/proc"):
        if p.isdigit():
            try: kids.setdefault(int(open(f"/proc/{p}/stat").read().rsplit(")", 1)[1].split()[1]), []).append(int(p))
            except OSError: pass
    out, todo = [], [pid]
    while todo:
        p = todo.pop(); out.append(p); todo += kids.get(p, [])
    return out

def smaps(pid):
    r = {"rss": 0, "pss": 0, "anon": 0}
    try:
        for l in open(f"/proc/{pid}/smaps_rollup"):
            k = l.split(":")[0]
            if k == "Rss": r["rss"] = int(l.split()[1])
            elif k == "Pss": r["pss"] = int(l.split()[1])
            elif k == "Anonymous": r["anon"] = int(l.split()[1])
    except OSError: pass
    return r

def cpu_s(pid):
    f = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
    t = os.sysconf("SC_CLK_TCK")
    return (int(f[11]) + int(f[12]) + int(f[13]) + int(f[14])) / t   # own + reaped children

def mapped_files(pids):
    seen = {}
    for p in pids:
        try:
            for l in open(f"/proc/{p}/maps"):
                f = l.split(None, 5)
                if len(f) == 6 and f[5].startswith("/") and not f[5].startswith("/dev") and f[5] not in seen:
                    try: seen[f[5]] = os.path.getsize(f[5])
                    except OSError: pass
        except OSError: pass
    return seen

def start(kind):
    if kind == "python":
        return subprocess.Popen(PY + ["idle"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return subprocess.Popen([f"{BIN}/tuxthrottle", "--no-elevate", "--size", "1280x820"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

R = {"host": subprocess.run("uname -sr; grep -m1 'model name' /proc/cpuinfo | cut -d: -f2", shell=True, capture_output=True, text=True).stdout.split("\n")[:2]}

# ---- first frame
ff = {"python": [], "cpp": []}
for _ in range(args.runs):
    o = subprocess.run(PY + ["first"], env=env, capture_output=True, text=True, timeout=60).stdout
    m = re.search(r"first_frame_s ([\d.]+)", o); ff["python"].append(float(m.group(1)) if m else float("nan"))
    e = dict(env, TT_TIMING="1")
    o = subprocess.run([f"{BIN}/tuxthrottle", "--no-elevate", "--page", "Dashboard", "--shot", "/dev/null", "--size", "1280x820"], env=e, capture_output=True, text=True, timeout=60).stderr
    m = re.search(r"first_frame_s ([\d.]+)", o); ff["cpp"].append(float(m.group(1)) if m else float("nan"))
R["first_frame_s"] = {k: {"median": st.median(v), "min": min(v), "max": max(v), "runs": v} for k, v in ff.items()}

# ---- idle memory / cpu / threads (whole process tree)
R["idle"] = {}
for kind in ("python", "cpp"):
    p = start(kind); time.sleep(12)
    pids = tree(p.pid)
    mem = {k: sum(smaps(x)[k] for x in pids) for k in ("rss", "pss", "anon")}
    threads = sum(len(os.listdir(f"/proc/{x}/task")) for x in pids if os.path.isdir(f"/proc/{x}/task"))
    files = mapped_files(pids)
    c0 = sum(cpu_s(x) for x in pids); t0 = time.time(); time.sleep(args.idle)
    pids2 = tree(p.pid); c1 = sum(cpu_s(x) for x in pids2); t1 = time.time()
    R["idle"][kind] = {"rss_mb": mem["rss"] / 1024, "pss_mb": mem["pss"] / 1024, "anon_mb": mem["anon"] / 1024, "processes": len(pids), "threads": threads,
                       "cpu_pct": 100 * (c1 - c0) / (t1 - t0), "mapped_files": len(files), "mapped_mb": sum(files.values()) / 1048576}
    p.terminate()
    try: p.wait(10)
    except subprocess.TimeoutExpired: p.kill()
    time.sleep(2)

# ---- CLI
def timed(cmd, n):
    w, m = [], []
    for _ in range(n):
        r = subprocess.run(["/usr/bin/time", "-f", "%e %M"] + cmd, capture_output=True, text=True, env=env)
        a = r.stderr.strip().split("\n")[-1].split(); w.append(float(a[0])); m.append(int(a[1]) / 1024)
    return {"wall_s": st.median(w), "peak_rss_mb": st.median(m)}
n = max(5, args.runs)
R["cli"] = {
    "status --json": {"python": timed(["python3", f"{ROOT}/python/tuxthrottlectl.py", "status", "--json"], n), "cpp": timed([f"{BIN}/tuxthrottlectl", "status", "--json"], n)},
    "status report (146 checks)": {"python": timed(["python3", f"{ROOT}/python/tuxthrottle.py", "--report"], 3), "cpp": timed([f"{BIN}/tuxthrottlectl", "tweaks", "report"], 3)},
}

# ---- footprint
def du(paths):
    t = 0
    for p in paths:
        if os.path.isfile(p): t += os.path.getsize(p)
        else:
            for d, _, fs in os.walk(p):
                if "__pycache__" in d or "/tests" in d: continue
                for f in fs: t += os.path.getsize(os.path.join(d, f))
    return t
py_src = du([f for f in map(lambda x: f"{ROOT}/python/{x}", os.listdir(f"{ROOT}/python")) if f.endswith(".py")])
mods = subprocess.run(["python3", "-c", "import sys,os,tkinter,ttkbootstrap,PIL.Image,PIL.ImageTk;print(len(sys.modules));print(sum(os.path.getsize(m.__file__) for m in sys.modules.values() if getattr(m,'__file__',None) and os.path.exists(m.__file__)))"], capture_output=True, text=True).stdout.split()
lines = lambda g: sum(sum(1 for _ in open(f, errors="ignore")) for f in g)
import glob
cpp_files = glob.glob(f"{ROOT}/cpp/src/**/*.cpp", recursive=True) + glob.glob(f"{ROOT}/cpp/src/**/*.hpp", recursive=True)
R["footprint"] = {
    "python_source_mb": py_src / 1048576, "python_imported_modules": int(mods[0]), "python_imported_files_mb": int(mods[1]) / 1048576,
    "cpp_binaries_mb": {b: os.path.getsize(f"{BIN}/{b}") / 1048576 for b in ("tuxthrottle", "tuxthrottlectl", "tuxthrottled", "tuxthrottle-tray")},
    "python_loc": lines(glob.glob(f"{ROOT}/python/*.py")), "cpp_loc": lines(cpp_files),
    "python_tests": sum(open(f).read().count("def test_") for f in glob.glob(f"{ROOT}/python/tests/*.py")),
    "cpp_checks": None,
}
t = subprocess.run([f"{ROOT}/cpp/build/tt_tests", f"{ROOT}/config"], capture_output=True, text=True, env=dict(env, TT_TEST_TMP="/tmp")).stdout
m = re.search(r"(\d+) checks", t); R["footprint"]["cpp_checks"] = int(m.group(1)) if m else None
# ---- always-on helpers (tray, power daemon, G-key listener); C++ side measured on the live processes, Python side started for a few seconds
def pss_of(pid, root=False):
    cmd = ["sudo", "-n", "cat", f"/proc/{pid}/smaps_rollup"] if root else ["cat", f"/proc/{pid}/smaps_rollup"]
    t = subprocess.run(cmd, capture_output=True, text=True).stdout
    m = re.search(r"Pss:\s+(\d+)", t); return int(m.group(1)) / 1024 if m else None
def pids_of(pattern):
    return [int(x) for x in subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True).stdout.split() if int(x) != os.getpid()]
def py_run(cmd, wait=10):
    p = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); time.sleep(wait)
    v = pss_of(p.pid) if p.poll() is None else None
    p.terminate(); p.wait(); return v
res = {}
res["tray"] = {"python_pss_mb": None, "cpp_pss_mb": None}
old = subprocess.run(["pgrep", "-x", "tuxthrottle-tra"], capture_output=True, text=True).stdout.split()
exe = os.readlink(f"/proc/{old[0]}/exe") if old else f"{BIN}/tuxthrottle-tray"
for o in old: os.kill(int(o), 15)
time.sleep(2)
res["tray"]["python_pss_mb"] = py_run(["python3", f"{ROOT}/python/tray_monitor.py"], 12)
time.sleep(1)
res["tray"]["cpp_pss_mb"] = py_run([f"{BIN}/tuxthrottle-tray"], 12)
subprocess.Popen([exe], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
res["power daemon"] = {"python_pss_mb": py_run(["python3", f"{ROOT}/python/tuxthrottle_powerd.py", "run", "--user", os.environ.get("USER", "root")], 8), "cpp_pss_mb": None}
d = pids_of("^/usr/local/bin/tuxthrottled run")
res["power daemon"]["cpp_pss_mb"] = pss_of(d[0], True) if d else None
res["G-key listener"] = {"python_pss_mb": py_run(["python3", f"{ROOT}/python/hotkey_listener.py"], 8), "cpp_pss_mb": None}
h = pids_of("^/usr/local/bin/tuxthrottlectl hotkey")
res["G-key listener"]["cpp_pss_mb"] = pss_of(h[0]) if h else None
R["resident"] = {k: v for k, v in res.items() if v["python_pss_mb"] and v["cpp_pss_mb"]}
json.dump(R, open(f"{OUT}/results.json", "w"), indent=2)
print(json.dumps(R, indent=1))
