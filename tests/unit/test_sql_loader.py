from pathlib import Path

import pytest

import db
import db.sql
from db.sql import render


def test_a_psql_quoted_variable_becomes_a_psycopg_named_parameter(tmp_path, monkeypatch):
    # written under tmp_path rather than added to db/queries/: that directory is frozen at exactly
    # ten committed files and a fixture file there would break the corpus criteria as well
    monkeypatch.setattr(db.sql, "QUERIES_DIR", tmp_path)
    (tmp_path / "q.sql").write_text("SELECT 1 FROM bars WHERE symbol = :'symbol'\n")
    rendered = render("q.sql")
    assert "%(symbol)s" in rendered
    assert ":'" not in rendered


def test_a_literal_percent_is_doubled_before_the_placeholder_rewrite(tmp_path, monkeypatch):
    monkeypatch.setattr(db.sql, "QUERIES_DIR", tmp_path)
    (tmp_path / "q.sql").write_text("-- 60% of the day\nSELECT :'symbol'\n")
    rendered = render("q.sql")
    # both halves in one test: removing the doubling breaks the first assertion and reordering the
    # two operations breaks only the second, so a test asserting either alone covers neither of
    # the other's mutations
    assert "60%% of the day" in rendered
    assert "%(symbol)s" in rendered
    assert "%%(symbol)s" not in rendered


def test_a_placeholder_that_survives_the_render_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(db.sql, "QUERIES_DIR", tmp_path)
    # uppercase is outside the committed r":'([a-z_]+)'" pattern, so it survives the substitution
    # and would reach psycopg as a syntax error naming neither the file nor the cause
    (tmp_path / "loud.sql").write_text("SELECT :'SYMBOL'\n")
    # ValueError and never Exception: Exception also catches the AssertionError an assert-shaped
    # guard raises, and python -O deletes that guard entirely
    with pytest.raises(ValueError) as excinfo:
        render("loud.sql")
    assert "loud.sql" in str(excinfo.value)


def test_an_empty_query_file_is_refused_by_name(tmp_path, monkeypatch):
    monkeypatch.setattr(db.sql, "QUERIES_DIR", tmp_path)
    (tmp_path / "blank.sql").write_text("   \n")
    with pytest.raises(ValueError) as excinfo:
        render("blank.sql")
    assert "blank.sql" in str(excinfo.value)


def test_the_loader_resolves_queries_relative_to_the_package_and_not_the_working_directory(
    tmp_path, monkeypatch
):
    # deliberately does NOT redirect QUERIES_DIR -- the real package directory is what is under
    # test -- and the chdir is what makes it discriminate: pytest already runs from the repo root,
    # where Path("db/queries") and the package path name the same directory, so a repo-root-relative
    # QUERIES_DIR passes from the cwd the suite runs in
    monkeypatch.chdir(tmp_path)
    assert db.sql.QUERIES_DIR == Path(db.__file__).resolve().parent / "queries"
    assert render("06_daily_rollup.sql")
