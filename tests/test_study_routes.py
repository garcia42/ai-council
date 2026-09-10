import dataclasses
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from council_tools.study_routes import StudyRouteError, resolve_study_route


@pytest.fixture
def account_home(tmp_path):
    home = tmp_path / "account"
    home.mkdir()
    with mock.patch(
        "council_tools.study_routes.pwd.getpwuid",
        return_value=SimpleNamespace(pw_dir=str(home)),
    ) as lookup:
        yield home, lookup


def test_routes_separate_evidence_but_share_existing_lock(account_home):
    home, lookup = account_home
    old = resolve_study_route("council-legacy")
    fresh = resolve_study_route("council-fresh-20260910")
    lookup.assert_called_with(os.getuid())
    assert old.collection_state == "closed"
    assert fresh.collection_state == "active"
    assert old.log == str(home / ".claude/knowledge/futures-panel-log.jsonl")
    assert old.artifact_root == (
        "/var/lib/ai-council-evidence/live-capture-20260907/artifacts"
    )
    fresh_knowledge = home / ".claude/knowledge/council-eval/studies/council-fresh-20260910"
    assert fresh.log == str(fresh_knowledge / "panel.jsonl")
    assert fresh.v1_events == str(fresh_knowledge / "predictions_resolved.jsonl")
    assert fresh.v2_events == str(fresh_knowledge / "capture_resolved.jsonl")
    for field in ("log", "v1_events", "v2_events", "artifact_root", "control_store"):
        assert getattr(old, field) != getattr(fresh, field)
    assert old.coordination_lock == fresh.coordination_lock == str(
        home / ".local/state/council-tools/evidence.lock"
    )
    assert list(home.iterdir()) == []
    with pytest.raises(dataclasses.FrozenInstanceError):
        fresh.collection_state = "closed"


def test_home_environment_cannot_redirect_study(account_home, monkeypatch):
    home, _ = account_home
    monkeypatch.setenv("HOME", "/untrusted/home")
    route = resolve_study_route("council-fresh-20260910")
    assert Path(route.log).is_relative_to(home)


@pytest.mark.parametrize("study_id", [None, "", "unknown", "council-legacy ", [], 1])
def test_unknown_or_missing_selector_refuses_before_account_lookup(account_home, study_id):
    _, lookup = account_home
    with pytest.raises(StudyRouteError, match="missing or unknown"):
        resolve_study_route(study_id)
    lookup.assert_not_called()


def test_explicit_selected_paths_are_accepted(account_home):
    route = resolve_study_route("council-fresh-20260910")
    paths = dataclasses.asdict(route)
    del paths["study_id"]
    del paths["collection_state"]
    assert resolve_study_route(route.study_id, paths) == route


@pytest.mark.parametrize(
    "field", ["log", "v1_events", "v2_events", "artifact_root", "control_store"]
)
def test_explicit_historical_default_cannot_cross_into_fresh_study(account_home, field):
    old = resolve_study_route("council-legacy")
    with pytest.raises(StudyRouteError, match="conflicts"):
        resolve_study_route("council-fresh-20260910", {field: getattr(old, field)})


def test_lock_override_refused(account_home):
    home, _ = account_home
    with pytest.raises(StudyRouteError, match="conflicts"):
        resolve_study_route("council-fresh-20260910", {"coordination_lock": str(home / "other.lock")})


@pytest.mark.parametrize("supplied", [{"unknown": "/tmp/value"}, [], "", False])
def test_unknown_fields_and_invalid_mapping_refused(account_home, supplied):
    with pytest.raises(StudyRouteError):
        resolve_study_route("council-fresh-20260910", supplied)


@pytest.mark.parametrize("value", [None, 7, "", "relative.jsonl", "/tmp/../file", "/tmp/./file", "/tmp//file", "/tmp/file/", "/tmp/\x00file"])
def test_noncanonical_explicit_paths_refused(account_home, value):
    with pytest.raises(StudyRouteError):
        resolve_study_route("council-fresh-20260910", {"log": value})


def test_outside_symlink_alias_of_correct_target_refused(account_home):
    home, _ = account_home
    route = resolve_study_route("council-fresh-20260910")
    alias = home / "alias.jsonl"
    alias.symlink_to(route.log)
    with pytest.raises(StudyRouteError, match="aliased"):
        resolve_study_route(route.study_id, {"log": str(alias)})


@pytest.mark.parametrize("redirect_parent", [True, False])
def test_selected_default_cannot_be_redirected_even_when_omitted(account_home, redirect_parent):
    home, _ = account_home
    route = resolve_study_route("council-fresh-20260910")
    target = home / ".claude" if redirect_parent else Path(route.log)
    target.parent.mkdir(parents=True, exist_ok=True)
    destination = home / "outside"
    target.symlink_to(destination)
    with pytest.raises(StudyRouteError, match="aliased"):
        resolve_study_route(route.study_id)
    assert not destination.exists()


def test_symlink_loop_refused(account_home):
    home, _ = account_home
    alias = home / ".claude"
    alias.symlink_to(alias)
    with pytest.raises(StudyRouteError, match="cannot resolve"):
        resolve_study_route("council-fresh-20260910")


def test_noncanonical_account_home_refused(account_home):
    with mock.patch(
        "council_tools.study_routes.pwd.getpwuid",
        return_value=SimpleNamespace(pw_dir="relative/home"),
    ):
        with pytest.raises(StudyRouteError, match="account_home"):
            resolve_study_route("council-fresh-20260910")
