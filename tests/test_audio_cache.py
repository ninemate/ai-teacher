import os

from teacher_common.audio_cache import AudioCache


def make(tmp_path, max_bytes=1000):
    return AudioCache(tmp_path / "cache", max_bytes)


def test_round_trip_and_voice_and_text_keep_entries_apart(tmp_path):
    cache = make(tmp_path)
    folder = cache.document_key(1, "hash")
    assert cache.get(folder, "imre", "szöveg") is None
    cache.put(folder, "imre", "szöveg", b"audio-1")
    assert cache.get(folder, "imre", "szöveg") == b"audio-1"
    assert cache.get(folder, "anna", "szöveg") is None
    assert cache.get(folder, "imre", "más") is None
    assert cache.get(cache.document_key(2, "hash"), "imre", "szöveg") is None


def test_client_supplied_names_never_reach_the_file_system(tmp_path):
    cache = make(tmp_path)
    folder = cache.document_key(1, "hash")
    cache.put(folder, "../../etc", "../x", b"a")
    names = [p.relative_to(cache.root).as_posix() for p in cache.root.rglob("*") if p.is_file()]
    assert len(names) == 1 and names[0].startswith(folder + "/") and ".." not in names[0]


def test_least_recently_used_files_are_evicted_to_stay_within_the_bound(tmp_path):
    cache = make(tmp_path, max_bytes=350)
    folder = cache.document_key(1, "h")
    for number in range(3):
        cache.put(folder, "v", f"t{number}", b"x" * 100)
        past = 1_000_000 + number
        os.utime(cache._path(folder, "v", f"t{number}"), (past, past))
    assert cache.get(folder, "v", "t0") is not None  # touched: now the most recent
    cache.put(folder, "v", "t3", b"x" * 100)
    assert cache.total_bytes() <= 350
    assert cache.get(folder, "v", "t0") is not None
    assert cache.get(folder, "v", "t1") is None  # the oldest untouched one went


def test_removing_a_document_removes_all_its_versions_and_only_those(tmp_path):
    cache = make(tmp_path)
    cache.put(cache.document_key(7, "old"), "v", "a", b"1")
    cache.put(cache.document_key(7, "new"), "v", "a", b"2")
    cache.put(cache.document_key(17, "h"), "v", "a", b"3")
    cache.remove_document(7)
    assert cache.get(cache.document_key(7, "old"), "v", "a") is None
    assert cache.get(cache.document_key(7, "new"), "v", "a") is None
    assert cache.get(cache.document_key(17, "h"), "v", "a") == b"3"  # 17 is not 7


def test_sweep_removes_orphans_but_keeps_live_documents(tmp_path):
    cache = make(tmp_path)
    live, stale = cache.document_key(1, "h"), cache.document_key(2, "h")
    cache.put(live, "v", "a", b"1")
    cache.put(stale, "v", "a", b"2")
    assert cache.sweep([live]) == 1
    assert cache.get(live, "v", "a") == b"1" and cache.get(stale, "v", "a") is None
