#!/usr/bin/env python3
"""Runs the Python TuxThrottle GUI without elevation for measurements.
  py_harness.py first   print seconds from process start to the first drawn frame, then exit
  py_harness.py idle    keep the window open (killed by the caller)"""
import os, sys, time
HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "python")
sys.path.insert(0, HERE)
os.chdir(HERE)
import tkinter as tk
import tuxthrottle as tt

def started_at():   # seconds since this process was exec'd, from /proc (same clock for every program)
    ticks = os.sysconf("SC_CLK_TCK")
    st = int(open("/proc/self/stat").read().rsplit(")", 1)[1].split()[19])
    up = float(open("/proc/uptime").read().split()[0])
    return up - st / ticks

tt.self_elevate = lambda: None
mode = sys.argv[1] if len(sys.argv) > 1 else "idle"
orig = tk.Misc.mainloop
def mainloop(self, n=0):
    if mode == "first":
        def done():
            self.update_idletasks()
            print(f"first_frame_s {started_at():.3f}")
            sys.stdout.flush()
            os._exit(0)
        self.after_idle(done)
    orig(self, n)
tk.Misc.mainloop = mainloop
tt.main()
