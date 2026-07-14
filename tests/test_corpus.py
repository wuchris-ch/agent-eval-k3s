import shutil
from pathlib import Path

import pytest

from agent_eval.corpus import load_corpus, validate_corpus

REPO = Path(__file__).resolve().parents[1]
CORPUS = REPO / "benchmarks" / "reviewer-corpus" / "v1" / "corpus.yaml"


def test_checked_in_corpus_hashes_labels_and_reproducers_are_valid():
    result = validate_corpus(CORPUS)

    assert result.valid, result.errors
    assert result.version == "1.0.0"
    assert {item.case_id for item in result.reproducers} == {
        "auth-bypass",
        "clean-refactor",
    }
    assert all(item.passed for item in result.reproducers)


def test_corpus_detects_artifact_tampering(tmp_path):
    root = CORPUS.parent
    copied = tmp_path / "corpus"

    shutil.copytree(root, copied)
    target = copied / "cases" / "auth-bypass" / "head" / "auth.py"
    target.write_text(target.read_text() + "# modified\n")

    result = validate_corpus(copied / "corpus.yaml", execute=False)

    assert not result.valid
    assert any("artifact hash mismatch" in error for error in result.errors)


def test_corpus_detects_benchmark_gold_tampering(tmp_path):
    copied = tmp_path / "corpus"
    shutil.copytree(CORPUS.parent, copied)
    benchmark = copied / "benchmark.yaml"
    benchmark.write_text(
        benchmark.read_text(encoding="utf-8").replace(
            "severity: blocker", "severity: major"
        ),
        encoding="utf-8",
    )

    result = validate_corpus(copied / "corpus.yaml", execute=False)

    assert not result.valid
    assert "benchmark manifest hash mismatch" in result.errors


def test_corpus_rejects_unlisted_case_files(tmp_path):
    copied = tmp_path / "corpus"
    shutil.copytree(CORPUS.parent, copied)
    helper = copied / "cases" / "auth-bypass" / "head" / "helper.py"
    helper.write_text("UNDECLARED = True\n", encoding="utf-8")

    result = validate_corpus(copied / "corpus.yaml", execute=False)

    assert not result.valid
    assert any("unlisted artifact" in error and "helper.py" in error for error in result.errors)


def test_corpus_rejects_symlinks_in_case_subtree(tmp_path):
    copied = tmp_path / "corpus"
    shutil.copytree(CORPUS.parent, copied)
    link = copied / "cases" / "auth-bypass" / "head" / "auth-link.py"
    link.symlink_to("auth.py")

    result = validate_corpus(copied / "corpus.yaml", execute=False)

    assert not result.valid
    assert any("symlink is not allowed" in error for error in result.errors)


def test_faulty_case_requires_distinct_expected_exits(tmp_path):
    text = CORPUS.read_text(encoding="utf-8").replace(
        "expected_head_exit: 1", "expected_head_exit: 0", 1
    )
    manifest = tmp_path / "corpus.yaml"
    manifest.write_text(text, encoding="utf-8")

    with pytest.raises(ValueError, match="different expected base and head exits"):
        load_corpus(manifest)


def test_corpus_paths_cannot_escape_root(tmp_path):
    text = CORPUS.read_text().replace(
        "benchmark_manifest: benchmark.yaml",
        "benchmark_manifest: ../benchmark.yaml",
    )
    manifest = tmp_path / "corpus.yaml"
    manifest.write_text(text)

    with pytest.raises(ValueError, match="safe relative path"):
        load_corpus(manifest)
