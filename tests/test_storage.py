import app.storage as storage


def test_cache_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", str(tmp_path / "resolver.sqlite3"))
    storage.init_storage()
    storage.put_cached("https://short.test/a", "https://example.org/final", 3)
    assert storage.get_cached("https://short.test/a") == ("https://example.org/final", 3)


def test_rate_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DB_PATH", str(tmp_path / "resolver.sqlite3"))
    monkeypatch.setattr(storage, "RATE_LIMIT_COUNT", 2)
    monkeypatch.setattr(storage, "RATE_LIMIT_WINDOW", 3600)
    storage.init_storage()
    assert storage.allow_request(12345) == (True, 0)
    assert storage.allow_request(12345) == (True, 0)
    allowed, retry_after = storage.allow_request(12345)
    assert allowed is False
    assert retry_after > 0
