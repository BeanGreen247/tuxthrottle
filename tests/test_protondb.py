"""tuxthrottle_protondb.py — disk-cache logic and label formatting.
No real network calls: urllib.request.urlopen is monkeypatched.
"""
import io
import json
import urllib.error

import tuxthrottle_protondb as pdb


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(pdb, "_cache_dir", lambda: tmp_path)


def test_label_formats_known_tier():
    assert pdb.label({"tier": "gold"}) == "ProtonDB: Gold"
    assert pdb.label({"tier": "platinum"}) == "ProtonDB: Platinum"


def test_label_empty_for_no_data():
    assert pdb.label(None) == ""
    assert pdb.label({"tier": None}) == ""


def test_lookup_rejects_non_numeric_appid(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    assert pdb.lookup("not-an-id") is None


def test_lookup_uses_cache_without_network(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    pdb._write_cache("730", {"tier": "gold"})

    def boom(*a, **k):
        raise AssertionError("should not hit the network when cache is fresh")
    monkeypatch.setattr(pdb.urllib.request, "urlopen", boom)

    data = pdb.lookup("730")
    assert data["tier"] == "gold"


def test_lookup_fetches_and_caches_on_miss(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)

    class FakeResp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    payload = json.dumps({"tier": "silver"}).encode()
    monkeypatch.setattr(pdb.urllib.request, "urlopen", lambda *a, **k: FakeResp(payload))

    data = pdb.lookup("440")
    assert data["tier"] == "silver"
    cached = pdb._read_cache("440", pdb.CACHE_TTL_S)
    assert cached["tier"] == "silver"


def test_lookup_negative_caches_on_network_error(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)

    def boom(*a, **k):
        raise urllib.error.URLError("offline")
    monkeypatch.setattr(pdb.urllib.request, "urlopen", boom)

    assert pdb.lookup("1234") is None
    cached = pdb._read_cache("1234", pdb.CACHE_TTL_S)
    assert cached == {"tier": None, "_fetched": cached["_fetched"]}
