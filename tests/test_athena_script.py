from pathlib import Path

from scripts.run_athena_sql import split_statements

SQL = Path(__file__).resolve().parents[1] / "sql" / "athena_views.sql"


def test_split_drops_comments_and_finds_every_view():
    statements = split_statements(SQL.read_text())
    assert len(statements) == 11
    assert all(s.startswith("CREATE OR REPLACE VIEW minigrid.") for s in statements)
    assert not any("--" in s for s in statements)


def test_split_ignores_empty_chunks():
    assert split_statements("-- note\nSELECT 1;\n\n;\nSELECT 2;\n") == [
        "SELECT 1", "SELECT 2"]
