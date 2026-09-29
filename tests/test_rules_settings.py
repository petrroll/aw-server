import copy
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from aw_server.server import AWFlask
from aw_server.settings import Settings


def canonical_document(revision=0, *, advanced=False, multiple_sets=True):
    sources = [
        {
            "id": "builtin_window",
            "label": "App & window",
            "bucket_ids": [],
            "builtin": "window",
            "fields": ["app", "title"],
            "creates_activity": True,
            "interval_policy": "heartbeat",
        }
    ]
    work_rule = (
        {
            "type": "all",
            "rules": [
                {
                    "type": "regex",
                    "regex": "Editor",
                    "source": "builtin_window",
                    "field": "app",
                }
            ],
        }
        if advanced
        else {"type": "regex", "regex": "Editor", "weight": 0}
    )
    sets = [
        {
            "schema_version": 2,
            "id": "work",
            "categories": [
                {
                    "id": "work:Work",
                    "name": ["Work"],
                    "rule": work_rule,
                    "simple_ui": not advanced,
                }
            ],
        }
    ]
    if multiple_sets:
        sets.append(
            {
                "schema_version": 2,
                "id": "private",
                "categories": [
                    {
                        "id": "private:Media",
                        "name": ["Media"],
                        "rule": {"type": "none"},
                        "simple_ui": True,
                    },
                    {
                        "id": "private:Work",
                        "name": ["Work"],
                        "rule": {"type": "none"},
                        "simple_ui": True,
                    },
                ],
            }
        )
    return {
        "revision": revision,
        "activity_profiles_v2": [
            {
                "schema_version": 2,
                "id": "default",
                "category_set_ids": [category_set["id"] for category_set in sets],
                "sources": sources,
                "app_title_source_id": "builtin_window",
                "browser_focus_source_id": "builtin_window",
                "active_time": {
                    "type": "legacy",
                    "use_afk": True,
                    "include_audible": True,
                    "always_active_pattern": "meeting",
                },
            }
        ],
        "category_sets_v2": sets,
    }


@pytest.fixture()
def rules_app(monkeypatch, tmp_path):
    monkeypatch.setattr("aw_server.settings.get_config_dir", lambda _name: tmp_path)
    return AWFlask("127.0.0.1", testing=True)


def test_rules_v2_first_save_get_and_derived_compatibility(rules_app):
    client = rules_app.test_client()
    assert client.get("/api/0/settings/rules_v2").status_code == 404

    assert client.post("/api/0/settings/theme", json="dark").status_code == 200
    response = client.post("/api/0/settings/rules_v2", json=canonical_document())
    assert response.status_code == 200
    assert response.json["revision"] == 1

    stored = client.get("/api/0/settings/rules_v2")
    assert stored.status_code == 200
    assert stored.json == response.json
    all_settings = client.get("/api/0/settings").json
    assert all_settings["theme"] == "dark"
    assert all_settings["rules_v2"] == response.json
    assert all_settings["active_set_ids"] == ["work", "private"]
    # Active sets flatten in profile order and duplicate names use first-set precedence.
    assert [category["name"] for category in all_settings["classes"]] == [
        ["Work"],
        ["Media"],
    ]
    assert all_settings["always_active_pattern"] == "meeting"
    assert len(all_settings["category_sets"]) == 2


def test_legacy_projection_preserves_select_keys_alias(rules_app):
    document = canonical_document(multiple_sets=False)
    rule = document["category_sets_v2"][0]["categories"][0]["rule"]
    rule["select_keys"] = ["title"]
    response = rules_app.test_client().post("/api/0/settings/rules_v2", json=document)
    assert response.status_code == 200

    classes = rules_app.test_client().get("/api/0/settings/classes").json
    assert classes[0]["rule"]["select_keys"] == ["title"]


def test_rules_v2_cas_rejects_stale_and_invalid_without_partial_save(rules_app):
    client = rules_app.test_client()
    first = client.post("/api/0/settings/rules_v2", json=canonical_document()).json

    stale = canonical_document(revision=0)
    stale["activity_profiles_v2"][0]["id"] = "stale"
    response = client.post("/api/0/settings/rules_v2", json=stale)
    assert response.status_code == 409
    assert "current revision is 1" in response.json["message"]

    invalid = canonical_document(revision=1)
    invalid["category_sets_v2"][0]["categories"][0]["rule"] = {
        "type": "regex",
        "regex": "(",
    }
    response = client.post("/api/0/settings/rules_v2", json=invalid)
    assert response.status_code == 400
    assert client.get("/api/0/settings/rules_v2").json == first


def test_partial_v2_writes_rejected_and_legacy_originals_not_overwritten(rules_app):
    client = rules_app.test_client()
    old_profiles = [{"old": True}]
    assert client.post("/api/0/settings/activity_profiles_v2", json=old_profiles).status_code == 200
    stored = client.post("/api/0/settings/rules_v2", json=canonical_document()).json

    response = client.post(
        "/api/0/settings/activity_profiles_v2", json=stored["activity_profiles_v2"]
    )
    assert response.status_code == 409
    # Reads are projections, while the pre-migration value remains available on disk for recovery.
    assert client.get("/api/0/settings/activity_profiles_v2").json == stored["activity_profiles_v2"]
    raw_file = json.loads(rules_app.api.settings.config_file.read_text())
    assert raw_file["activity_profiles_v2"] == old_profiles


def test_safe_legacy_write_increments_revision_and_preserves_inactive_sets(rules_app):
    client = rules_app.test_client()
    document = canonical_document(multiple_sets=True)
    document["activity_profiles_v2"][0]["category_set_ids"] = ["work"]
    client.post("/api/0/settings/rules_v2", json=document)

    assert client.post("/api/0/settings/classes", json={"broken": True}).status_code == 400

    classes = [
        {
            "name": ["Changed"],
            "rule": {"type": "regex", "regex": "Terminal", "select_keys": ["app"]},
            "data": {"color": "#123"},
        }
    ]
    response = client.post(
        "/api/0/settings/classes",
        json=classes,
        headers={"If-Match": 'W/"1"'},
    )
    assert response.status_code == 200
    assert response.json == classes
    stored = client.get("/api/0/settings/rules_v2").json
    assert stored["revision"] == 2
    assert [category_set["id"] for category_set in stored["category_sets_v2"]] == [
        "work",
        "private",
    ]
    assert stored["category_sets_v2"][0]["categories"][0]["name"] == ["Changed"]

    stale = client.post("/api/0/settings/always_active_pattern?revision=1", json="call")
    assert stale.status_code == 409
    assert client.get("/api/0/settings/always_active_pattern").json == "meeting"


def test_lossy_legacy_writes_are_rejected(rules_app):
    client = rules_app.test_client()
    advanced = canonical_document(advanced=True, multiple_sets=False)
    client.post("/api/0/settings/rules_v2", json=advanced)
    assert client.post("/api/0/settings/classes", json=[]).status_code == 409

    current = client.get("/api/0/settings/rules_v2").json
    current["activity_profiles_v2"][0]["active_time"] = {
        "type": "expression",
        "rule": {
            "type": "regex",
            "regex": "yes",
            "source": "builtin_window",
            "field": "app",
        },
    }
    saved = client.post("/api/0/settings/rules_v2", json=current)
    assert saved.status_code == 200
    assert client.post("/api/0/settings/always_active_pattern", json="meeting").status_code == 409


def test_concurrent_cas_has_one_winner(rules_app):
    def save(profile_id):
        client = rules_app.test_client()
        document = canonical_document()
        document["activity_profiles_v2"][0]["id"] = profile_id
        return client.post("/api/0/settings/rules_v2", json=document).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = sorted(executor.map(save, ["one", "two"]))
    assert statuses == [200, 409]
    assert rules_app.test_client().get("/api/0/settings/rules_v2").json["revision"] == 1


def test_malformed_historical_document_remains_exportable_and_repairable(rules_app):
    settings = rules_app.api.settings
    settings.data = {
        "theme": "dark",
        "classes": [{"name": ["Legacy"], "rule": {"type": "none"}}],
        "rules_v2": {"revision": 7, "activity_profiles_v2": "broken"},
    }
    settings.save()
    client = rules_app.test_client()

    raw = client.get("/api/0/settings/rules_v2")
    assert raw.status_code == 200
    assert raw.json["revision"] == 7
    all_settings = client.get("/api/0/settings").json
    assert all_settings["theme"] == "dark"
    assert all_settings["classes"][0]["name"] == ["Legacy"]

    repaired = canonical_document(revision=7)
    response = client.post("/api/0/settings/rules_v2", json=repaired)
    assert response.status_code == 200
    assert response.json["revision"] == 8


@pytest.mark.parametrize("stored_revision", [None, -1, True, "7"])
def test_malformed_historical_revision_uses_zero_for_explicit_repair(
    rules_app, stored_revision
):
    malformed = {"activity_profiles_v2": "broken"}
    if stored_revision is not None:
        malformed["revision"] = stored_revision
    settings = rules_app.api.settings
    settings.data = {"rules_v2": malformed}
    settings.save()

    response = rules_app.test_client().post(
        "/api/0/settings/rules_v2", json=canonical_document(revision=0)
    )
    assert response.status_code == 200
    assert response.json["revision"] == 1


@pytest.mark.parametrize("malformed", [None, 42, []])
def test_present_malformed_canonical_document_blocks_legacy_writes_and_is_repairable(
    rules_app, malformed
):
    settings = rules_app.api.settings
    legacy = [{"name": ["Before"], "rule": {"type": "none"}}]
    settings.data = {"rules_v2": malformed, "classes": legacy}
    settings.save()
    client = rules_app.test_client()

    direct = client.get("/api/0/settings/rules_v2")
    assert direct.status_code == 200
    assert direct.json == malformed
    assert client.get("/api/0/settings").json["rules_v2"] == malformed
    blocked = client.post(
        "/api/0/settings/classes",
        json=[{"name": ["After"], "rule": {"type": "none"}}],
    )
    assert blocked.status_code == 409
    assert json.loads(settings.config_file.read_text())["classes"] == legacy

    repaired = client.post(
        "/api/0/settings/rules_v2", json=canonical_document(revision=0)
    )
    assert repaired.status_code == 200
    assert repaired.json["revision"] == 1


@pytest.mark.parametrize(
    "field",
    ["weight", "category_priority", "set_priority", "category_set_priority"],
)
@pytest.mark.parametrize("value", [-1_000_001, 1_000_001, True, 1.0])
def test_ranking_integers_use_the_shared_bounded_domain(rules_app, field, value):
    document = canonical_document(multiple_sets=False)
    category_set = document["category_sets_v2"][0]
    category = category_set["categories"][0]
    if field == "weight":
        category["rule"]["weight"] = value
    elif field == "category_priority":
        category["priority"] = value
    elif field == "set_priority":
        category["set_priority"] = value
    else:
        category_set["priority"] = value

    response = rules_app.test_client().post("/api/0/settings/rules_v2", json=document)
    assert response.status_code == 400
    assert "must be" in response.json["message"]


@pytest.mark.parametrize("field", ["builtin", "scope"])
@pytest.mark.parametrize("value", [None, 42, [], {}, True, "bogus"])
def test_present_source_enum_fields_require_valid_strings(rules_app, field, value):
    document = canonical_document(multiple_sets=False)
    document["activity_profiles_v2"][0]["sources"][0][field] = value

    response = rules_app.test_client().post("/api/0/settings/rules_v2", json=document)
    assert response.status_code == 400
    assert field in response.json["message"]


@pytest.mark.parametrize("value", [None, 42, [], True, "host", {"w": 42}])
def test_empty_builtin_validates_present_bucket_hosts(rules_app, value):
    client = rules_app.test_client()
    stored = client.post(
        "/api/0/settings/rules_v2",
        json=canonical_document(multiple_sets=False),
    ).json
    proposed = copy.deepcopy(stored)
    proposed["activity_profiles_v2"][0]["sources"][0]["bucket_hosts"] = value

    response = client.post("/api/0/settings/rules_v2", json=proposed)

    assert response.status_code == 400
    assert "bucket_hosts" in response.json["message"]
    assert client.get("/api/0/settings/rules_v2").json == stored


def test_empty_builtin_accepts_typed_empty_bucket_hosts(rules_app):
    document = canonical_document(multiple_sets=False)
    document["activity_profiles_v2"][0]["sources"][0]["bucket_hosts"] = {}

    response = rules_app.test_client().post(
        "/api/0/settings/rules_v2", json=document
    )

    assert response.status_code == 200


@pytest.mark.parametrize("metadata", [None, 42, True, [], {}, "ignored"])
def test_non_group_expression_ignores_irrelevant_rules_metadata(rules_app, metadata):
    document = canonical_document(multiple_sets=False)
    category_rule = document["category_sets_v2"][0]["categories"][0]["rule"]
    category_rule["rules"] = metadata
    active_time = document["activity_profiles_v2"][0]["active_time"]
    active_time.clear()
    active_time.update(
        {
            "type": "expression",
            "rule": {
                "type": "regex",
                "source": "builtin_window",
                "field": "app",
                "regex": "Editor",
                "rules": metadata,
            },
        }
    )

    client = rules_app.test_client()
    response = client.post("/api/0/settings/rules_v2", json=document)

    assert response.status_code == 200
    assert client.get("/api/0/settings").status_code == 200


def test_irrelevant_expression_metadata_is_safe_for_historical_reads_and_writes(
    rules_app,
):
    document = canonical_document(multiple_sets=False)
    document["category_sets_v2"][0]["categories"][0]["rule"]["rules"] = [
        {
            "type": "regex",
            "source": "missing",
            "regex": ".*",
        }
    ]
    settings = rules_app.api.settings
    settings.data = {"rules_v2": document}
    settings.save()
    client = rules_app.test_client()

    all_settings = client.get("/api/0/settings")
    assert all_settings.status_code == 200
    classes = all_settings.json["classes"]
    response = client.post("/api/0/settings/classes", json=classes)
    assert response.status_code == 200
    assert client.get("/api/0/settings/rules_v2").json["revision"] == 1


def test_ranking_integer_boundaries_are_accepted(rules_app):
    document = canonical_document(multiple_sets=False)
    category_set = document["category_sets_v2"][0]
    category_set["priority"] = -1_000_000
    category = category_set["categories"][0]
    category["simple_ui"] = False
    category["priority"] = 1_000_000
    category["set_priority"] = -1_000_000
    category["rule"]["weight"] = 1_000_000

    response = rules_app.test_client().post("/api/0/settings/rules_v2", json=document)
    assert response.status_code == 200


def test_revision_must_remain_a_safe_integer(rules_app):
    too_large = canonical_document(revision=9_007_199_254_740_992)
    assert rules_app.test_client().post(
        "/api/0/settings/rules_v2", json=too_large
    ).status_code == 400

    maximum = canonical_document(revision=9_007_199_254_740_991)
    response = rules_app.test_client().post("/api/0/settings/rules_v2", json=maximum)
    assert response.status_code == 400
    assert "cannot be incremented" in response.json["message"]

    settings = rules_app.api.settings
    settings.data = {"rules_v2": maximum}
    settings.save()
    legacy = rules_app.test_client().post(
        "/api/0/settings/always_active_pattern", json="call"
    )
    assert legacy.status_code == 400
    assert "cannot be incremented" in legacy.json["message"]


def test_selected_category_sets_must_fit_one_executable_budget(rules_app):
    document = canonical_document(multiple_sets=True)
    document["category_sets_v2"] = [
        {
            "schema_version": 2,
            "id": set_id,
            "categories": [
                {
                    "id": f"{set_id}-{index}",
                    "name": [set_id, str(index)],
                    "rule": {"type": "none"},
                }
                for index in range(501)
            ],
        }
        for set_id in ("one", "two")
    ]
    document["activity_profiles_v2"][0]["category_set_ids"] = ["one", "two"]

    response = rules_app.test_client().post("/api/0/settings/rules_v2", json=document)
    assert response.status_code == 400
    assert "select 1002 category rules" in response.json["message"]


@pytest.mark.parametrize("reverse", [False, True])
def test_maximum_category_requirement_chain_is_validated_iteratively(rules_app, reverse):
    document = canonical_document(multiple_sets=False)
    categories = [
        {
            "id": f"rule-{index}",
            "name": [f"Rule {index}"],
            "requires": [f"rule-{index - 1}"] if index else [],
            "rule": {"type": "regex", "regex": "match"},
        }
        for index in range(1000)
    ]
    if reverse:
        categories.reverse()
    document["category_sets_v2"][0]["categories"] = categories

    response = rules_app.test_client().post("/api/0/settings/rules_v2", json=document)
    assert response.status_code == 200


def test_category_requirement_graph_handles_branches_duplicates_and_cycles(rules_app):
    document = canonical_document(multiple_sets=False)
    categories = [
        {"id": "root", "name": ["Root"], "rule": {"type": "regex", "regex": "x"}},
        {
            "id": "branch-a",
            "name": ["A"],
            "requires": ["root", "root"],
            "rule": {"type": "regex", "regex": "x"},
        },
        {
            "id": "branch-b",
            "name": ["B"],
            "requires": ["root"],
            "rule": {"type": "regex", "regex": "x"},
        },
        {
            "id": "leaf",
            "name": ["Leaf"],
            "requires": ["branch-a", "branch-b"],
            "rule": {"type": "regex", "regex": "x"},
        },
    ]
    document["category_sets_v2"][0]["categories"] = categories
    assert rules_app.test_client().post(
        "/api/0/settings/rules_v2", json=document
    ).status_code == 200

    document["revision"] = 1
    categories[0]["requires"] = ["leaf"]
    response = rules_app.test_client().post("/api/0/settings/rules_v2", json=document)
    assert response.status_code == 400
    assert "cycle" in response.json["message"]


def test_selected_sets_share_the_executable_expression_budget(rules_app):
    document = canonical_document(multiple_sets=True)
    document["category_sets_v2"] = [
        {
            "schema_version": 2,
            "id": set_id,
            "categories": [
                {
                    "id": f"{set_id}-root",
                    "name": [set_id],
                    "rule": {
                        "type": "any",
                        "rules": [
                            {"type": "regex", "regex": "."} for _ in range(2048)
                        ],
                    },
                }
            ],
        }
        for set_id in ("one", "two")
    ]
    document["activity_profiles_v2"][0]["category_set_ids"] = ["one", "two"]

    response = rules_app.test_client().post("/api/0/settings/rules_v2", json=document)
    assert response.status_code == 400
    assert "maximum node count of 4096" in response.json["message"]


def test_atomic_file_replace_failure_leaves_previous_document(monkeypatch, tmp_path):
    monkeypatch.setattr("aw_server.settings.get_config_dir", lambda _name: tmp_path)
    settings = Settings(testing=True)
    first = settings.replace_rules(canonical_document())
    original = settings.config_file.read_bytes()

    proposed = canonical_document(revision=first["revision"])
    proposed["activity_profiles_v2"][0]["id"] = "replacement"
    monkeypatch.setattr("aw_server.settings.os.replace", lambda *_args: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(OSError, match="boom"):
        settings.replace_rules(proposed)
    assert settings.config_file.read_bytes() == original
