from verify_alignment import READMES, check_readmes


def write_readmes(root, body):
    for name in READMES:
        (root / name).write_text(body, encoding="utf-8")


def test_translations_allow_extra_paragraphs_and_preserve_examples(tmp_path):
    body = "# Title\n\n[English](README.md)\n\n```bash\npython3 orchestrator.py status\n```\n"
    write_readmes(tmp_path, body)
    (tmp_path / "README_ja.md").write_text(
        body + "\n追加の説明。\n\n", encoding="utf-8"
    )
    assert check_readmes(tmp_path) == []
    (tmp_path / "README_ja.md").write_text(
        body.replace("status", "reset"), encoding="utf-8"
    )
    assert any(
        "missing/changed bash example" in error for error in check_readmes(tmp_path)
    )


def test_readme_validation_rejects_broken_fences_and_local_links(tmp_path):
    write_readmes(tmp_path, "# Title\n\n[missing](absent.md)\n\n```bash\nrun\n")
    errors = check_readmes(tmp_path)
    assert any("Unclosed code fence" in error for error in errors)
    assert any("missing local link" in error for error in errors)
