---
name: New hardware support
about: Request support for another gaming laptop model
title: "[hw] <vendor> <model> — "
labels: hardware
---

<!--
The toolkit currently only knows the Dell G15 5515 Ryzen Edition. Adding a
model needs its DMI strings, hwmon fan/pwm paths, evdev key codes, GPU layout
and (if it has RGB) the OpenRGB controller. The hardware bundle below captures
all of it.
-->

### Laptop
- Vendor / model:
- CPU / GPU(s):
- Distro + kernel:
- Keyboard backlight: none / single-colour / per-zone RGB / per-key RGB
- Dedicated "gaming"/performance key? which:

### What you want it to do
<!-- fan control, Game Mode / performance profile, RGB, the G-key binding, … -->

### Hardware bundle  (REQUIRED)
<!--
Toolkit → Report a Bug → "⇩ Collect hardware bundle (.tar.gz)"
   or a terminal:  sudo python3 /opt/tuxthrottle/tuxthrottle.py --collect
Skim it for private strings (hostname, serials in dmi.txt / lsusb.txt), then
DRAG THE .tar.gz ONTO THIS COMMENT to attach it.
-->
- [ ] hardware bundle attached

### Fn / media / vendor keys (if any don't work)
<!-- run `sudo evtest`, pick the keyboard / hotkey device, press the key,
     paste the event lines here -->

### Anything else
