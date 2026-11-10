"""Parsers behind osu_lazer_diag.py (no live system needed)."""

import osu_lazer_diag as d

PW_TOP = """S   ID  QUANT   RATE    WAIT    BUSY   W/Q   B/Q  ERR FORMAT           NAME
R   53    256  48000  24,8us   7,8us  0,00  0,12    0    S32LE 2 48000 alsa_output.pci.HiFi__Headphones__sink
R   93    256  48000   9,0us  12,2us  0,00  0,00    3    F32LE 2 48000  + alsa_playback.osu!
S   30      0      0    ---     ---   ---   ---     0                  Dummy-Driver
"""


def test_parse_pw_top():
    rows = d.parse_pw_top(PW_TOP)
    assert rows["alsa_playback.osu!"]["err"] == 3
    assert rows["alsa_playback.osu!"]["follower"] is True
    sink = rows["alsa_output.pci.HiFi__Headphones__sink"]
    assert sink["quant"] == 256 and sink["follower"] is False and sink["busy_ratio"] == 0.12


def test_parse_pipewire_alsa():
    v = "{ alsa.format=F32_LE alsa.channels=2 alsa.rate=48000 alsa.period-bytes=1024 alsa.buffer-bytes=2048 }"
    assert d.parse_pipewire_alsa(v) == {"channels": 2, "rate": 48000, "period-bytes": 1024, "buffer-bytes": 2048}


def test_parse_ini_strips_comments_and_quotes():
    ini = d.parse_ini('A = 1\nB="x y"   # note\n[sect]\n; c=3\n')
    assert ini == {"A": "1", "B": "x y"}


def test_audio_checks_flag_big_quantum():
    rows = d.parse_pw_top(PW_TOP.replace("256  48000   9,0us", "1024  48000   9,0us"))
    checks = d.check_audio(1234, {}, rows)
    live = next(c for c in checks if c.name == "Live quantum (osu! stream)")
    assert live.status == d.BAD
    assert any(c.name == "xruns (osu! stream)" and c.status == d.WARN for c in checks)
    assert any(c.name.startswith("pipewire-alsa buffer") and c.status == d.WARN for c in checks)


def test_report_renders():
    out = d.text_report([d.Check("S", "n", "v", d.BAD, "fix it")])
    assert "✗ n" in out and "→ fix it" in out and "1 problems" in out
