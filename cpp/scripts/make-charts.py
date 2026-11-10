#!/usr/bin/env python3
"""Draws docs/compare/*.png and docs/compare/README.md from results.json (written by compare-python-cpp.py)."""
import json, os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
ROOT = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", ".."))
D = os.path.join(ROOT, "docs", "compare")
R = json.load(open(f"{D}/results.json"))
PY, CPP = "#e0b341", "#4aa3ff"
plt.rcParams.update({"figure.facecolor": "#0f1722", "axes.facecolor": "#0f1722", "savefig.facecolor": "#0f1722", "text.color": "#d8e2ee",
                     "axes.labelcolor": "#d8e2ee", "xtick.color": "#d8e2ee", "ytick.color": "#d8e2ee", "axes.edgecolor": "#3a4a5e", "font.size": 11})

def bars(name, title, labels, py, cpp, unit, note=None, fmt="{:.1f}"):
    fig, ax = plt.subplots(figsize=(8.2, 3.6))
    n = len(labels); w = 0.38
    xs = range(n)
    b1 = ax.bar([x - w / 2 for x in xs], py, w, color=PY, label="Python")
    b2 = ax.bar([x + w / 2 for x in xs], cpp, w, color=CPP, label="C++")
    for b, v in list(zip(b1, py)) + list(zip(b2, cpp)):
        ax.text(b.get_x() + b.get_width() / 2, b.get_height(), fmt.format(v), ha="center", va="bottom", fontsize=9)
    ax.set_xticks(list(xs)); ax.set_xticklabels(labels); ax.set_ylabel(unit); ax.set_title(title, loc="left", fontsize=13)
    ax.spines[["top", "right"]].set_visible(False); ax.legend(frameon=False)
    ax.set_ylim(0, max(py + cpp) * 1.18)
    if note: fig.text(0.01, 0.01, note, fontsize=8, color="#8fa3b8")
    fig.tight_layout(rect=(0, 0.04, 1, 1)); fig.savefig(f"{D}/{name}.png", dpi=140); plt.close(fig)

ff, idle, cli, fp = R["first_frame_s"], R["idle"], R["cli"], R["footprint"]
bars("first-frame", "Time to first drawn frame (lower is better)", ["median"], [ff["python"]["median"]], [ff["cpp"]["median"]], "seconds",
     f"{len(ff['python']['runs'])} runs each, Dashboard, un-elevated, this laptop", "{:.2f}")
bars("memory", "Idle memory, Dashboard open (lower is better)", ["RSS", "PSS (fair share)", "private anon"],
     [idle["python"]["rss_mb"], idle["python"]["pss_mb"], idle["python"]["anon_mb"]], [idle["cpp"]["rss_mb"], idle["cpp"]["pss_mb"], idle["cpp"]["anon_mb"]], "MB",
     "RSS counts shared Mesa/LLVM pages in full; PSS divides them between the processes that use them")
bars("cpu-idle", "Idle CPU, Dashboard open (lower is better)", ["CPU %"], [idle["python"]["cpu_pct"]], [idle["cpp"]["cpu_pct"]], "percent of one core", None, "{:.2f}")
labels = list(cli.keys())
bars("cli", "Command-line tools, median wall time (lower is better)", labels, [cli[k]["python"]["wall_s"] for k in labels], [cli[k]["cpp"]["wall_s"] for k in labels], "seconds", None, "{:.2f}")
res = R.get("resident")
if res:
    names = list(res.keys())
    bars("resident", "Always-on helpers, proportional memory (lower is better)", names, [res[k]["python_pss_mb"] for k in names], [res[k]["cpp_pss_mb"] for k in names], "MB PSS",
         "what runs from login to logout: tray, power daemon, G-key listener")

# the one table that matters, as markdown
def row(a, b, c, d=""): return f"| {a} | {b} | {c} | {d} |"
md = ["# Python edition vs C++ edition", "", "Measured on this laptop by `cpp/scripts/compare-python-cpp.py` (raw numbers: `results.json`). Both GUIs run un-elevated on the Dashboard.", "",
      "| | Python | C++ | change |", "|---|---|---|---|"]
def pct(a, b): return f"{100 * (b - a) / a:+.0f}%" if a else ""
md += [row("First frame", f"{ff['python']['median']:.2f} s", f"{ff['cpp']['median']:.3f} s", f"{ff['python']['median'] / ff['cpp']['median']:.1f}x faster"),
       row("Idle CPU (GUI)", f"{idle['python']['cpu_pct']:.2f} %", f"{idle['cpp']['cpu_pct']:.2f} %", pct(idle['python']['cpu_pct'], idle['cpp']['cpu_pct'])),
       row("Idle PSS (GUI)", f"{idle['python']['pss_mb']:.0f} MB", f"{idle['cpp']['pss_mb']:.0f} MB", pct(idle['python']['pss_mb'], idle['cpp']['pss_mb'])),
       row("Idle private memory (GUI)", f"{idle['python']['anon_mb']:.0f} MB", f"{idle['cpp']['anon_mb']:.0f} MB", pct(idle['python']['anon_mb'], idle['cpp']['anon_mb'])),
       row("Idle RSS (GUI)", f"{idle['python']['rss_mb']:.0f} MB", f"{idle['cpp']['rss_mb']:.0f} MB", pct(idle['python']['rss_mb'], idle['cpp']['rss_mb']) + " (shared Mesa pages)"),
       row("Threads (GUI)", idle['python']['threads'], idle['cpp']['threads'], "")]
for k in labels: md.append(row(f"`{k}`", f"{cli[k]['python']['wall_s']:.2f} s / {cli[k]['python']['peak_rss_mb']:.0f} MB", f"{cli[k]['cpp']['wall_s']:.2f} s / {cli[k]['cpp']['peak_rss_mb']:.0f} MB", "wall / peak RSS"))
if res:
    for k, v in res.items(): md.append(row(f"Always-on: {k}", f"{v['python_pss_mb']:.1f} MB PSS", f"{v['cpp_pss_mb']:.1f} MB PSS", pct(v['python_pss_mb'], v['cpp_pss_mb'])))
    tp = sum(v["python_pss_mb"] for v in res.values()); tc = sum(v["cpp_pss_mb"] for v in res.values())
    md.append(row("Always-on total", f"{tp:.0f} MB PSS", f"{tc:.1f} MB PSS", pct(tp, tc)))
cb = fp["cpp_binaries_mb"]
md += [row("Runtime installed", f"python3 + Tk + ttkbootstrap + Pillow, {fp['python_imported_modules']} modules / {fp['python_imported_files_mb']:.1f} MB loaded at start, {fp['python_source_mb']:.1f} MB source", f"{sum(cb.values()):.1f} MB in 4 binaries", "no interpreter, no pip packages"),
       row("Code", f"{fp['python_loc']:,} lines", f"{fp['cpp_loc']:,} lines", ""), row("Automated tests", f"{fp['python_tests']} tests", f"{fp['cpp_checks']} checks + launch smoke test", "")]
md += ["", "![first frame](first-frame.png)", "![memory](memory.png)", "![cpu](cpu-idle.png)", "![cli](cli.png)"]
if res: md.append("![resident](resident.png)")
open(f"{D}/README.md", "w").write("\n".join(md) + "\n")
print("charts + README written")
