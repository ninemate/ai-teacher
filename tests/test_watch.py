from pathlib import Path

import pytest
from pydantic import ValidationError
from teacher_common.watch import ScanReport, WatchConfig, WatchFolder, scan_watched_files

EXTENSIONS = [".pdf", ".txt", ".md"]


def make_tree(root: Path, files: list[str]) -> None:
    for name in files:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")


def scan(root: Path, config: WatchConfig, report: ScanReport | None = None) -> list[str]:
    return [p.relative_to(root).as_posix() for p in scan_watched_files(root, config, EXTENSIONS, report)]


def test_only_watched_folders_are_scanned(tmp_path):
    make_tree(tmp_path, ["books/a.pdf", "books/sub/b.txt", "other/c.pdf", "root.pdf"])
    config = WatchConfig(folders=[WatchFolder(path="books")])
    assert scan(tmp_path, config) == ["books/a.pdf", "books/sub/b.txt"]


def test_no_folders_scans_nothing(tmp_path):
    make_tree(tmp_path, ["a.pdf"])
    assert scan(tmp_path, WatchConfig()) == []


def test_dot_watches_whole_library(tmp_path):
    make_tree(tmp_path, ["a.pdf", "x/b.pdf"])
    assert scan(tmp_path, WatchConfig(folders=[WatchFolder(path=".")])) == ["a.pdf", "x/b.pdf"]


def test_disabled_folder_is_skipped(tmp_path):
    make_tree(tmp_path, ["a/x.pdf", "b/y.pdf"])
    config = WatchConfig(folders=[WatchFolder(path="a", enabled=False), WatchFolder(path="b")])
    assert scan(tmp_path, config) == ["b/y.pdf"]


def test_non_recursive(tmp_path):
    make_tree(tmp_path, ["a/top.pdf", "a/deep/low.pdf"])
    assert scan(tmp_path, WatchConfig(folders=[WatchFolder(path="a", recursive=False)])) == ["a/top.pdf"]


def test_extension_filter_and_per_folder_override(tmp_path):
    make_tree(tmp_path, ["a/x.pdf", "a/y.txt", "a/z.jpg", "b/x.pdf", "b/y.txt"])
    config = WatchConfig(folders=[WatchFolder(path="a"), WatchFolder(path="b", extensions=["txt"])])
    assert scan(tmp_path, config) == ["a/x.pdf", "a/y.txt", "b/y.txt"]


def test_exclude_prunes_directories_and_matches_files(tmp_path):
    make_tree(tmp_path, ["a/keep.pdf", "a/Drafts/no.pdf", "a/sub/Drafts/no2.pdf", "a/sub/ok.pdf", "a/tmp~.txt"])
    config = WatchConfig(folders=[WatchFolder(path="a", exclude=["drafts", "*~.txt"])])
    assert scan(tmp_path, config) == ["a/keep.pdf", "a/sub/ok.pdf"]


def test_global_exclude_applies_to_all_folders(tmp_path):
    make_tree(tmp_path, ["a/x.pdf", "a/#recycle/y.pdf", "b/#recycle/z.pdf", "b/w.pdf"])
    config = WatchConfig(folders=[WatchFolder(path="a"), WatchFolder(path="b")], exclude=["#recycle"])
    assert scan(tmp_path, config) == ["a/x.pdf", "b/w.pdf"]


def test_include_limits_files(tmp_path):
    make_tree(tmp_path, ["a/phys/one.pdf", "a/chem/two.pdf", "a/phys/three.txt"])
    config = WatchConfig(folders=[WatchFolder(path="a", include=["phys/*.pdf"])])
    assert scan(tmp_path, config) == ["a/phys/one.pdf"]


def test_overlapping_folders_do_not_yield_duplicates(tmp_path):
    make_tree(tmp_path, ["a/x.pdf", "a/sub/y.pdf"])
    config = WatchConfig(folders=[WatchFolder(path="a"), WatchFolder(path="a/sub")])
    assert sorted(scan(tmp_path, config)) == ["a/sub/y.pdf", "a/x.pdf"]


def test_missing_folder_is_reported_and_protects_its_documents(tmp_path):
    make_tree(tmp_path, ["a/x.pdf"])
    report = ScanReport()
    config = WatchConfig(folders=[WatchFolder(path="a"), WatchFolder(path="gone")])
    assert scan(tmp_path, config, report) == ["a/x.pdf"]
    assert report.missing_folders == [str(tmp_path / "gone")]
    assert report.is_unreliable(str(tmp_path / "gone" / "old.pdf"))
    assert not report.is_unreliable(str(tmp_path / "a" / "x.pdf"))
    assert not report.is_unreliable(str(tmp_path / "gone2" / "x.pdf"))
    assert report.scanned_per_folder == {"a": 1}


@pytest.mark.parametrize("bad", ["/abs", "../up", "a/../../up", "", "  "])
def test_paths_must_stay_inside_library(bad):
    with pytest.raises(ValidationError):
        WatchFolder(path=bad)


def test_path_is_normalized():
    assert WatchFolder(path="a//b/./c/").path == "a/b/c"
    assert WatchFolder(path="a\\b").path == "a/b"
