"""T0 — the frozen marker contract (task-doc R1).

The artifact and its validated marker are one write pair; an unmarked file is
repo-shipped; a bare marker reads as legacy; an invalid record never reaches
the disk.
"""

from __future__ import annotations

import json

import pytest

from app.services import generated_marker as gm


def test_write_pair_writes_artifact_and_subclass_marker(tmp_path):
    path = tmp_path / "nginx.base.gt-abc.conf"
    marker = gm.write_marker_pair(
        path, "server {}\n", cls="base", description="nginx http base",
        job="gt-abc", generator="llm")
    assert path.read_text() == "server {}\n"
    assert marker == tmp_path / "nginx.base.gt-abc.conf.base.generated"
    record = json.loads(marker.read_text())
    assert record["schema"] == 1
    assert record["class"] == "base"
    assert record["description"] == "nginx http base"
    assert record["sha256"] == gm.sha256_text("server {}\n")
    assert record["job"] == "gt-abc"
    assert record["generator"] == "llm"
    gm.validate_record(record)


def test_invalid_record_is_refused_before_any_write(tmp_path):
    path = tmp_path / "x.conf"
    record = gm.build_record(cls="base", description="ok", content="a")
    record["description"] = "x" * 201
    with pytest.raises(gm.MarkerError):
        gm.write_pair(path, "a", record)
    assert not path.exists()


def test_description_and_guide_bounds():
    gm.validate_record(gm.build_record(cls="fix", description="d" * 200,
                                       guide="g" * 1000, content="a"))
    with pytest.raises(gm.MarkerError):
        gm.validate_record(gm.build_record(cls="fix", description="d" * 201,
                                           content="a"))
    with pytest.raises(gm.MarkerError):
        gm.validate_record(gm.build_record(cls="fix", guide="g" * 1001,
                                           content="a"))


def test_credential_field_is_refused():
    record = gm.build_record(cls="ins", content="a")
    record["fingerprint"] = {"api_key": "nope"}
    with pytest.raises(gm.MarkerError):
        gm.validate_record(record)


def test_unmarked_target_is_refused_and_untouched(tmp_path):
    path = tmp_path / "repo.conf"
    path.write_text("original\n")
    with pytest.raises(gm.UnmarkedTargetError):
        gm.write_marker_pair(path, "generated\n", cls="base")
    assert path.read_text() == "original\n"
    assert gm.find_marker(path) is None


def test_marked_target_regenerates_in_place(tmp_path):
    path = tmp_path / "x.conf"
    gm.write_marker_pair(path, "one\n", cls="base")
    marker = gm.write_marker_pair(path, "two\n", cls="base", description="again")
    assert path.read_text() == "two\n"
    assert json.loads(marker.read_text())["sha256"] == gm.sha256_text("two\n")


def test_bare_empty_marker_reads_legacy(tmp_path):
    path = tmp_path / "old.conf"
    path.write_text("x\n")
    (tmp_path / "old.conf.generated").write_text("")
    info = gm.read_marker(path)
    assert info.cls == "legacy" and info.legacy and info.record is None


def test_corrupt_record_reads_legacy(tmp_path):
    path = tmp_path / "bad.conf"
    path.write_text("x\n")
    (tmp_path / "bad.conf.base.generated").write_text("{not json")
    info = gm.read_marker(path)
    assert info.cls == "legacy" and info.record is None


def test_unmarked_reads_none(tmp_path):
    path = tmp_path / "repo.conf"
    path.write_text("x\n")
    info = gm.read_marker(path)
    assert info.cls is None and info.marker is None


def test_subclass_marker_wins_over_bare(tmp_path):
    path = tmp_path / "both.conf"
    gm.write_marker_pair(path, "x\n", cls="ins")
    (tmp_path / "both.conf.generated").write_text("")
    info = gm.read_marker(path)
    assert info.cls == "ins" and info.record is not None


def test_parse_marker_name_handles_dotted_artifacts():
    assert gm.parse_marker_name("a.b.c.d.ins.generated") == ("a.b.c.d", "ins")
    assert gm.parse_marker_name("a.b.generated") == ("a.b", None)
    assert gm.parse_marker_name("plain.conf") is None


def test_remove_pair_removes_both(tmp_path):
    path = tmp_path / "copy.yml"
    gm.write_marker_pair(path, "a\n", cls="ins")
    gm.remove_pair(path)
    assert not path.exists()
    assert gm.find_marker(path) is None


def test_finalizer_keeps_the_class_and_removes_a_stray_base_marker(tmp_path, monkeypatch):
    """R1.1 — a re-saved `.cls`/`.ins` artifact is not reclassified as base."""
    from app.config import settings
    from app.services.artifact_finalize import finalize_files

    monkeypatch.setattr(settings, "SOURCE_PROJECTS_DIR", tmp_path)
    recipe = tmp_path / "svc" / "recipes" / "main"
    recipe.mkdir(parents=True)
    (tmp_path / "svc" / ".provision-state.json").write_text(
        '{"file_sets": {}, "recipe_paths": ["recipes/main"], "recipe_origin": "user"}')

    finalize_files("svc", "recipes/main",
                   {"env.classification.7.yaml": "variables: {}\n"},
                   selection=None, persist_selection=False)
    assert (recipe / "env.classification.7.yaml.cls.generated").is_file()
    assert not (recipe / "env.classification.7.yaml.base.generated").exists()

    # An `.ins` artifact re-saved through the finalizer stays `ins`, and a
    # stray base marker from an older build is removed.
    (recipe / ".env.raw.alice.0").write_text("A=1\n")
    (recipe / ".env.raw.alice.0.base.generated").write_text("{}")
    (recipe / ".env.raw.alice.0.ins.generated").write_text("{}")
    finalize_files("svc", "recipes/main", {".env.raw.alice.0": "A=2\n"},
                   selection=None, persist_selection=False)
    assert (recipe / ".env.raw.alice.0.ins.generated").is_file()
    assert not (recipe / ".env.raw.alice.0.base.generated").exists()
