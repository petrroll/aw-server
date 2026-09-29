"""Validation and compatibility projections for the revisioned rules_v2 setting.

This module intentionally has no dependency on the Web UI.  The JSON document is
shared by all clients, so the server validates the persisted boundary itself.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Sequence, Tuple
from urllib.parse import quote

RULES_KEY = "rules_v2"
PROFILE_KEY = "activity_profiles_v2"
SET_KEY = "category_sets_v2"
LEGACY_RULE_KEYS = {
    PROFILE_KEY,
    SET_KEY,
    "classes",
    "always_active_pattern",
    "category_sets",
    "active_set_ids",
}
MAX_EXPRESSION_DEPTH = 32
MAX_EXPRESSION_NODES = 4096
MAX_REGEX_LENGTH = 4096
MAX_CATEGORY_RULES = 1000
MAX_RULE_SOURCES = 128
MIN_RANKING_INTEGER = -1_000_000
MAX_RANKING_INTEGER = 1_000_000
MAX_SAFE_REVISION = 9_007_199_254_740_991


class RulesValidationError(ValueError):
    pass


class RulesConflictError(RuntimeError):
    pass


def _error(path: str, message: str) -> RulesValidationError:
    return RulesValidationError(f"{path} {message}")


def _object(value: Any, path: str) -> MutableMapping[str, Any]:
    if not isinstance(value, dict):
        raise _error(path, "must be an object")
    return value


def _array(value: Any, path: str) -> List[Any]:
    if not isinstance(value, list):
        raise _error(path, "must be an array")
    return value


def _string(value: Any, path: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        raise _error(path, "must be a non-empty string" if nonempty else "must be a string")
    return value


def _integer(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _error(path, "must be an integer")
    return value


def _ranking_integer(value: Any, path: str) -> int:
    result = _integer(value, path)
    if not MIN_RANKING_INTEGER <= result <= MAX_RANKING_INTEGER:
        raise _error(
            path,
            f"must be between {MIN_RANKING_INTEGER} and {MAX_RANKING_INTEGER}",
        )
    return result


def _optional_bool(obj: Mapping[str, Any], key: str, path: str) -> None:
    if key in obj and not isinstance(obj[key], bool):
        raise _error(f"{path}.{key}", "must be boolean")


def _string_array(value: Any, path: str, *, nonempty: bool = False) -> List[str]:
    values = _array(value, path)
    if nonempty and not values:
        raise _error(path, "must not be empty")
    for index, item in enumerate(values):
        _string(item, f"{path}[{index}]")
    return values


def _validate_expression(
    value: Any,
    path: str,
    state: MutableMapping[str, Any],
    depth: int = 1,
    *,
    allow_none_children: bool = False,
    require_source: bool = False,
) -> None:
    if depth > MAX_EXPRESSION_DEPTH:
        raise _error(path, f"exceeds maximum depth of {MAX_EXPRESSION_DEPTH}")
    expression = _object(value, path)
    state["nodes"] += 1
    if state["nodes"] > MAX_EXPRESSION_NODES:
        raise _error(path, f"exceeds maximum node count of {MAX_EXPRESSION_NODES}")
    expression_type = expression.get("type")
    if expression_type == "none":
        return
    if expression_type == "regex":
        pattern = _string(expression.get("regex"), f"{path}.regex")
        if len(pattern) > MAX_REGEX_LENGTH:
            raise _error(f"{path}.regex", f"exceeds maximum length of {MAX_REGEX_LENGTH}")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise _error(f"{path}.regex", f"is invalid: {exc}")
        if "weight" in expression:
            _ranking_integer(expression["weight"], f"{path}.weight")
        if "field" in expression and "fields" in expression:
            raise _error(path, "cannot contain both field and fields")
        if "field" in expression:
            _string(expression["field"], f"{path}.field")
        if "fields" in expression:
            _string_array(expression["fields"], f"{path}.fields", nonempty=True)
        for key in ("source", "host"):
            if key in expression:
                _string(expression[key], f"{path}.{key}")
        if require_source and not expression.get("source"):
            raise _error(f"{path}.source", "must be non-empty")
        if expression.get("source"):
            state["sources"].add(expression["source"])
            if len(state["sources"]) > MAX_RULE_SOURCES:
                raise _error(path, f"exceeds maximum source count of {MAX_RULE_SOURCES}")
        if expression.get("value_mode", "string") not in ("string", "scalar"):
            raise _error(f"{path}.value_mode", "must be string or scalar")
        for key in ("ignore_case", "negate"):
            _optional_bool(expression, key, path)
        return
    if expression_type in ("all", "any"):
        rules = _array(expression.get("rules"), f"{path}.rules")
        if not rules:
            raise _error(f"{path}.rules", "must contain at least one rule")
        for index, rule in enumerate(rules):
            child_path = f"{path}.rules[{index}]"
            if isinstance(rule, dict) and rule.get("type") == "none" and not allow_none_children:
                raise _error(child_path, "must be configured")
            _validate_expression(
                rule,
                child_path,
                state,
                depth + 1,
                allow_none_children=allow_none_children,
                require_source=require_source,
            )
        return
    raise _error(f"{path}.type", "is unsupported")


def _legacy_compatible_category(category: Mapping[str, Any]) -> bool:
    rule = category.get("rule")
    if not isinstance(rule, dict):
        return False
    if (
        category.get("priority", 0) != 0
        or category.get("set_priority", 0) != 0
        or category.get("requires", [])
    ):
        return False
    if rule.get("type") == "none":
        return True
    return (
        rule.get("type") == "regex"
        and not rule.get("source")
        and not rule.get("host")
        and not rule.get("negate", False)
        and rule.get("weight", 0) == 0
        and rule.get("value_mode", "string") == "string"
    )


def _validate_category_set(value: Any, path: str) -> None:
    category_set = _object(value, path)
    if category_set.get("schema_version") != 2:
        raise _error(f"{path}.schema_version", "must be 2")
    _string(category_set.get("id"), f"{path}.id")
    if "priority" in category_set:
        _ranking_integer(category_set["priority"], f"{path}.priority")
    categories = _array(category_set.get("categories"), f"{path}.categories")
    if len(categories) > MAX_CATEGORY_RULES:
        raise _error(f"{path}.categories", f"exceeds maximum count of {MAX_CATEGORY_RULES}")
    ids: set[str] = set()
    by_id: Dict[str, Mapping[str, Any]] = {}
    state: MutableMapping[str, Any] = {"nodes": 0, "sources": set()}
    for index, raw_category in enumerate(categories):
        category_path = f"{path}.categories[{index}]"
        category = _object(raw_category, category_path)
        category_id = _string(category.get("id"), f"{category_path}.id")
        if category_id in ids:
            raise _error(f"{category_path}.id", "is duplicated")
        ids.add(category_id)
        by_id[category_id] = category
        _string_array(category.get("name"), f"{category_path}.name", nonempty=True)
        for ranking_key in ("priority", "set_priority"):
            if ranking_key in category:
                _ranking_integer(
                    category[ranking_key], f"{category_path}.{ranking_key}"
                )
        if "requires" in category:
            _string_array(category["requires"], f"{category_path}.requires")
        if "data" in category and not isinstance(category["data"], dict):
            raise _error(f"{category_path}.data", "must be an object")
        _optional_bool(category, "simple_ui", category_path)
        _validate_expression(category.get("rule"), f"{category_path}.rule", state)
        if category.get("simple_ui") is True and not _legacy_compatible_category(category):
            raise _error(category_path, "has simple_ui=true but is not simple-compatible")
    for category_id, category in by_id.items():
        for required in category.get("requires", []):
            if required not in ids:
                raise _error(f"{path}.categories[{category_id}].requires", f"references unknown id {required}")
            if by_id[required].get("rule", {}).get("type") == "none":
                raise _error(
                    f"{path}.categories[{category_id}].requires",
                    f"references category without a matching rule {required}",
                )
    dependency_count = {
        category_id: len(category.get("requires", []))
        for category_id, category in by_id.items()
    }
    dependents: Dict[str, List[str]] = {category_id: [] for category_id in by_id}
    for category_id, category in by_id.items():
        for required in category.get("requires", []):
            dependents[required].append(category_id)
    ready = [
        category_id
        for category_id in by_id
        if dependency_count[category_id] == 0
    ]
    visited_count = 0
    while ready:
        category_id = ready.pop()
        visited_count += 1
        for dependent in dependents[category_id]:
            dependency_count[dependent] -= 1
            if dependency_count[dependent] == 0:
                ready.append(dependent)
    if visited_count != len(by_id):
        cycle_id = next(
            category_id
            for category_id in by_id
            if dependency_count[category_id] > 0
        )
        raise _error(path, f"contains a category requirement cycle at {cycle_id}")


def _validate_source(value: Any, path: str) -> None:
    source = _object(value, path)
    source_id = _string(source.get("id"), f"{path}.id")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", source_id):
        raise _error(f"{path}.id", "contains unsupported characters")
    _string(source.get("label"), f"{path}.label")
    bucket_ids = _string_array(source.get("bucket_ids"), f"{path}.bucket_ids")
    fields = _string_array(source.get("fields"), f"{path}.fields", nonempty=True)
    if len(set(bucket_ids)) != len(bucket_ids):
        raise _error(f"{path}.bucket_ids", "must be unique")
    if len(set(fields)) != len(fields):
        raise _error(f"{path}.fields", "must be unique")
    builtin = source.get("builtin")
    expected_builtin_ids = {
        "window": "builtin_window",
        "browser": "browser",
        "stopwatch": "stopwatch",
    }
    if "builtin" in source:
        if (
            not isinstance(builtin, str)
            or builtin not in expected_builtin_ids
            or source_id != expected_builtin_ids[builtin]
        ):
            raise _error(f"{path}.builtin", "is unsupported for this source id")
    elif not bucket_ids:
        raise _error(f"{path}.bucket_ids", "must be non-empty for a custom source")
    if source.get("interval_policy", "exact") not in ("exact", "heartbeat"):
        raise _error(f"{path}.interval_policy", "must be exact or heartbeat")
    if "field_types" in source:
        field_types = _object(source["field_types"], f"{path}.field_types")
        if not set(field_types).issubset(fields):
            raise _error(f"{path}.field_types", "may only describe configured fields")
        if any(value not in ("string", "scalar") for value in field_types.values()):
            raise _error(f"{path}.field_types", "values must be string or scalar")
    for key in ("creates_activity", "keeps_active", "auto_generated"):
        _optional_bool(source, key, path)
    if source.get("keeps_active") and not source.get("creates_activity"):
        raise _error(f"{path}.keeps_active", "requires creates_activity")
    if "host" in source:
        _string(source["host"], f"{path}.host")
    if "scope" in source and source["scope"] not in ("host", "global"):
        raise _error(f"{path}.scope", "must be host or global")
    bucket_hosts = None
    if "bucket_hosts" in source:
        bucket_hosts = _object(source["bucket_hosts"], f"{path}.bucket_hosts")
        for bucket_id, hostname in bucket_hosts.items():
            _string(bucket_id, f"{path}.bucket_hosts key")
            _string(hostname, f"{path}.bucket_hosts[{bucket_id}]")
    scope = source.get("scope")
    if "scope" not in source and ("host" in source or bucket_hosts is not None):
        scope = "host"
    if scope == "global" and ("host" in source or bucket_hosts is not None):
        raise _error(path, "global scope cannot define host ownership")
    if scope == "host" and "host" in source and bucket_hosts is not None:
        raise _error(path, "host scope must use either host or bucket_hosts")
    if builtin is not None and not bucket_ids:
        return
    if bucket_hosts is not None and set(bucket_hosts) != set(bucket_ids):
        raise _error(
            f"{path}.bucket_hosts",
            "must map every and only configured bucket id",
        )
    if scope not in ("host", "global"):
        raise _error(f"{path}.scope", "must be host or global")
    if scope == "host" and "host" not in source and bucket_hosts is None:
        raise _error(path, "host scope requires host ownership")


def _expression_source_ids(expression: Mapping[str, Any]) -> Iterable[str]:
    expression_type = expression.get("type")
    if expression_type == "regex":
        if expression.get("source"):
            yield expression["source"]
        return
    if expression_type not in ("all", "any"):
        return
    for child in expression["rules"]:
        yield from _expression_source_ids(child)


def _expression_has_unsourced_regex(expression: Mapping[str, Any]) -> bool:
    expression_type = expression.get("type")
    if expression_type == "regex":
        return not expression.get("source")
    if expression_type not in ("all", "any"):
        return False
    return any(
        _expression_has_unsourced_regex(child) for child in expression["rules"]
    )


def _validate_profile(value: Any, path: str, sets: Mapping[str, Mapping[str, Any]]) -> None:
    profile = _object(value, path)
    if profile.get("schema_version") != 2:
        raise _error(f"{path}.schema_version", "must be 2")
    _string(profile.get("id"), f"{path}.id")
    if "source_defaults_version" in profile and _integer(
        profile["source_defaults_version"], f"{path}.source_defaults_version"
    ) < 0:
        raise _error(f"{path}.source_defaults_version", "must be non-negative")
    selected_ids = _string_array(profile.get("category_set_ids"), f"{path}.category_set_ids", nonempty=True)
    if len(set(selected_ids)) != len(selected_ids):
        raise _error(f"{path}.category_set_ids", "must be unique")
    for set_id in selected_ids:
        if set_id not in sets:
            raise _error(f"{path}.category_set_ids", f"references unknown category set {set_id}")
    sources = _array(profile.get("sources"), f"{path}.sources")
    if len(sources) > MAX_RULE_SOURCES:
        raise _error(f"{path}.sources", f"exceeds maximum count of {MAX_RULE_SOURCES}")
    source_ids: set[str] = set()
    by_source: Dict[str, Mapping[str, Any]] = {}
    for index, source in enumerate(sources):
        _validate_source(source, f"{path}.sources[{index}]")
        source_id = source["id"]
        if source_id in source_ids:
            raise _error(f"{path}.sources[{index}].id", "is duplicated")
        source_ids.add(source_id)
        by_source[source_id] = source
    for key in ("app_title_source_id", "browser_focus_source_id"):
        if key in profile:
            source_id = _string(profile[key], f"{path}.{key}")
            if source_id not in source_ids:
                raise _error(f"{path}.{key}", f"references unknown source {source_id}")
    active_time = _object(profile.get("active_time"), f"{path}.active_time")
    active_type = active_time.get("type")
    if active_type == "legacy":
        for key in ("use_afk", "include_audible"):
            if not isinstance(active_time.get(key), bool):
                raise _error(f"{path}.active_time.{key}", "must be boolean")
        pattern = _string(
            active_time.get("always_active_pattern"),
            f"{path}.active_time.always_active_pattern",
            nonempty=False,
        )
        if pattern:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise _error(f"{path}.active_time.always_active_pattern", f"is invalid: {exc}")
            window = next(
                (source for source in sources if source.get("builtin") == "window"), None
            )
            if window is None:
                raise _error(
                    f"{path}.active_time.always_active_pattern",
                    "requires a configured App & window source",
                )
            if not any(field in ("app", "title") for field in window["fields"]):
                raise _error(
                    f"{path}.active_time.always_active_pattern",
                    "requires an App & window source exposing app or title",
                )
    elif active_type == "expression":
        state: MutableMapping[str, Any] = {"nodes": 0, "sources": set()}
        _validate_expression(
            active_time.get("rule"),
            f"{path}.active_time.rule",
            state,
            allow_none_children=True,
            require_source=True,
        )
        for source_id in _expression_source_ids(active_time["rule"]):
            if source_id not in source_ids:
                raise _error(f"{path}.active_time.rule", f"references unknown source {source_id}")
    else:
        raise _error(f"{path}.active_time.type", "must be legacy or expression")
    selected_category_count = sum(len(sets[set_id]["categories"]) for set_id in selected_ids)
    if selected_category_count > MAX_CATEGORY_RULES:
        raise _error(
            f"{path}.category_set_ids",
            f"select {selected_category_count} category rules; maximum is {MAX_CATEGORY_RULES}",
        )
    aggregate_state: MutableMapping[str, Any] = {"nodes": 0, "sources": set()}
    for set_id in selected_ids:
        category_set = sets[set_id]
        for category_index, category in enumerate(category_set["categories"]):
            _validate_expression(
                category["rule"],
                f"{path}.category_set_ids[{set_id}].categories[{category_index}].rule",
                aggregate_state,
            )
            if not aggregate_state["sources"].issubset(source_ids):
                missing_source = sorted(aggregate_state["sources"] - source_ids)[0]
                raise _error(
                    f"{path}.category_set_ids[{set_id}]",
                    f"references unknown source {missing_source}",
                )
            if _expression_has_unsourced_regex(category["rule"]) and not any(
                source.get("builtin") == "window" for source in sources
            ):
                raise _error(
                    f"{path}.category_set_ids[{set_id}]",
                    f"category {category['id']} has an unsourced rule but no App & window source",
                )
            for source_id in _expression_source_ids(category["rule"]):
                if source_id not in source_ids:
                    raise _error(
                        f"{path}.category_set_ids[{set_id}]",
                        f"category {category['id']} references unknown source {source_id}",
                    )


def validate_rules_envelope(value: Any) -> Dict[str, Any]:
    envelope = _object(value, RULES_KEY)
    unknown = set(envelope) - {"revision", PROFILE_KEY, SET_KEY}
    missing = {"revision", PROFILE_KEY, SET_KEY} - set(envelope)
    if unknown:
        raise _error(RULES_KEY, f"contains unsupported keys: {', '.join(sorted(unknown))}")
    if missing:
        raise _error(RULES_KEY, f"is missing keys: {', '.join(sorted(missing))}")
    revision = _integer(envelope["revision"], f"{RULES_KEY}.revision")
    if revision < 0 or revision > MAX_SAFE_REVISION:
        raise _error(
            f"{RULES_KEY}.revision",
            f"must be between 0 and {MAX_SAFE_REVISION}",
        )
    category_sets = _array(envelope[SET_KEY], f"{RULES_KEY}.{SET_KEY}")
    profiles = _array(envelope[PROFILE_KEY], f"{RULES_KEY}.{PROFILE_KEY}")
    if not category_sets:
        raise _error(f"{RULES_KEY}.{SET_KEY}", "must contain at least one set")
    if not profiles:
        raise _error(f"{RULES_KEY}.{PROFILE_KEY}", "must contain at least one profile")
    sets_by_id: Dict[str, Mapping[str, Any]] = {}
    for index, category_set in enumerate(category_sets):
        _validate_category_set(category_set, f"{RULES_KEY}.{SET_KEY}[{index}]")
        set_id = category_set["id"]
        if set_id in sets_by_id:
            raise _error(f"{RULES_KEY}.{SET_KEY}[{index}].id", "is duplicated")
        sets_by_id[set_id] = category_set
    profile_ids: set[str] = set()
    for index, profile in enumerate(profiles):
        _validate_profile(profile, f"{RULES_KEY}.{PROFILE_KEY}[{index}]", sets_by_id)
        profile_id = profile["id"]
        if profile_id in profile_ids:
            raise _error(f"{RULES_KEY}.{PROFILE_KEY}[{index}].id", "is duplicated")
        profile_ids.add(profile_id)
    return copy.deepcopy(envelope)


def _v2_rule_to_legacy(rule: Mapping[str, Any]) -> Dict[str, Any]:
    if rule.get("type") != "regex":
        return {"type": "none"}
    legacy: Dict[str, Any] = {"type": "regex", "regex": rule["regex"]}
    if "ignore_case" in rule:
        legacy["ignore_case"] = rule["ignore_case"]
    if "fields" in rule:
        legacy["select_keys"] = copy.deepcopy(rule["fields"])
    elif "field" in rule:
        legacy["select_keys"] = [rule["field"]]
    elif "select_keys" in rule:
        legacy["select_keys"] = copy.deepcopy(rule["select_keys"])
    return legacy


def _set_to_legacy(category_set: Mapping[str, Any]) -> List[Dict[str, Any]]:
    result = []
    for category in category_set["categories"]:
        projected: Dict[str, Any] = {
            "name": copy.deepcopy(category["name"]),
            "rule": _v2_rule_to_legacy(category["rule"])
            if _legacy_compatible_category(category)
            else {"type": "none"},
        }
        if "data" in category:
            projected["data"] = copy.deepcopy(category["data"])
        result.append(projected)
    return result


def compatibility_projection(envelope: Mapping[str, Any]) -> Dict[str, Any]:
    validate_rules_envelope(envelope)
    profile = envelope[PROFILE_KEY][0]
    sets_by_id = {category_set["id"]: category_set for category_set in envelope[SET_KEY]}
    active_ids = list(profile["category_set_ids"])
    classes: List[Dict[str, Any]] = []
    seen_names: set[Tuple[str, ...]] = set()
    for set_id in active_ids:
        for category in _set_to_legacy(sets_by_id[set_id]):
            name = tuple(category["name"])
            if name not in seen_names:
                seen_names.add(name)
                classes.append(category)
    active_time = profile["active_time"]
    return {
        PROFILE_KEY: copy.deepcopy(envelope[PROFILE_KEY]),
        SET_KEY: copy.deepcopy(envelope[SET_KEY]),
        "classes": classes,
        "always_active_pattern": active_time["always_active_pattern"]
        if active_time["type"] == "legacy"
        else "",
        "category_sets": [
            {"id": category_set["id"], "categories": _set_to_legacy(category_set)}
            for category_set in envelope[SET_KEY]
        ],
        "active_set_ids": active_ids,
    }


def _validate_legacy_rule(rule: Any, path: str) -> Dict[str, Any]:
    if rule is None:
        return {"type": "none"}
    rule = _object(rule, path)
    rule_type = rule.get("type")
    if rule_type in (None, "none"):
        return {"type": "none"}
    if rule_type != "regex":
        raise _error(f"{path}.type", "must be regex or none")
    regex = _string(rule.get("regex"), f"{path}.regex")
    if len(regex) > MAX_REGEX_LENGTH:
        raise _error(f"{path}.regex", f"exceeds maximum length of {MAX_REGEX_LENGTH}")
    try:
        re.compile(regex)
    except re.error as exc:
        raise _error(f"{path}.regex", f"is invalid: {exc}")
    result: Dict[str, Any] = {"type": "regex", "regex": regex, "weight": 0}
    if "ignore_case" in rule:
        if not isinstance(rule["ignore_case"], bool):
            raise _error(f"{path}.ignore_case", "must be boolean")
        if rule["ignore_case"]:
            result["ignore_case"] = True
    if "select_keys" in rule:
        select_keys = _string_array(rule["select_keys"], f"{path}.select_keys", nonempty=True)
        if len(select_keys) == 1:
            result["field"] = select_keys[0]
        else:
            result["fields"] = copy.deepcopy(select_keys)
    return result


def _stable_category_id(set_id: str, name: Sequence[str]) -> str:
    return f"{set_id}:" + "/".join(
        quote(segment, safe="-_.!~*'()") for segment in name
    )


def _legacy_categories_to_v2(value: Any, set_id: str, path: str) -> List[Dict[str, Any]]:
    categories = _array(value, path)
    if len(categories) > MAX_CATEGORY_RULES:
        raise _error(path, f"exceeds maximum count of {MAX_CATEGORY_RULES}")
    result: List[Dict[str, Any]] = []
    seen_names: set[Tuple[str, ...]] = set()
    for index, raw_category in enumerate(categories):
        category_path = f"{path}[{index}]"
        category = _object(raw_category, category_path)
        name = _string_array(category.get("name"), f"{category_path}.name", nonempty=True)
        name_key = tuple(name)
        if name_key in seen_names:
            raise _error(f"{category_path}.name", "is duplicated")
        seen_names.add(name_key)
        converted: Dict[str, Any] = {
            "id": _stable_category_id(set_id, name),
            "name": copy.deepcopy(name),
            "rule": _validate_legacy_rule(category.get("rule"), f"{category_path}.rule"),
            "simple_ui": True,
        }
        if "data" in category:
            if not isinstance(category["data"], dict):
                raise _error(f"{category_path}.data", "must be an object")
            converted["data"] = copy.deepcopy(category["data"])
        result.append(converted)
    return result


def _set_is_legacy_writable(category_set: Mapping[str, Any]) -> bool:
    return all(_legacy_compatible_category(category) for category in category_set["categories"])


def translate_legacy_write(envelope: Mapping[str, Any], key: str, value: Any) -> Dict[str, Any]:
    """Translate a safe old-client write without mutating *envelope*.

    The returned envelope retains the current revision; the storage layer performs
    the revision increment in the same critical section as the write.
    """

    if key in (PROFILE_KEY, SET_KEY):
        raise RulesConflictError(
            f"{key} is part of the atomic rules_v2 document; write /settings/rules_v2 instead"
        )
    updated = copy.deepcopy(envelope)
    profile = updated[PROFILE_KEY][0]
    sets_by_id = {category_set["id"]: category_set for category_set in updated[SET_KEY]}
    if key == "always_active_pattern":
        _string(value, key, nonempty=False)
        if profile["active_time"]["type"] != "legacy":
            raise RulesConflictError(
                "always_active_pattern cannot represent the canonical active-time expression"
            )
        profile["active_time"]["always_active_pattern"] = value
    elif key == "active_set_ids":
        active_ids = _string_array(value, key, nonempty=True)
        if len(set(active_ids)) != len(active_ids):
            raise RulesValidationError("active_set_ids must be unique")
        missing = [set_id for set_id in active_ids if set_id not in sets_by_id]
        if missing:
            raise RulesValidationError(f"active_set_ids references unknown sets: {', '.join(missing)}")
        profile["category_set_ids"] = copy.deepcopy(active_ids)
    elif key == "classes":
        active_ids = profile["category_set_ids"]
        if len(active_ids) != 1:
            raise RulesConflictError(
                "classes write is ambiguous while multiple canonical category sets are active"
            )
        active_set = sets_by_id[active_ids[0]]
        if not _set_is_legacy_writable(active_set):
            raise RulesConflictError("classes write would discard advanced canonical category rules")
        active_set["categories"] = _legacy_categories_to_v2(value, active_set["id"], "classes")
    elif key == "category_sets":
        incoming = _array(value, key)
        seen: set[str] = set()
        for index, raw_set in enumerate(incoming):
            set_path = f"category_sets[{index}]"
            legacy_set = _object(raw_set, set_path)
            set_id = _string(legacy_set.get("id"), f"{set_path}.id")
            if set_id in seen:
                raise _error(f"{set_path}.id", "is duplicated")
            seen.add(set_id)
            existing = sets_by_id.get(set_id)
            if existing is not None and not _set_is_legacy_writable(existing):
                raise RulesConflictError(
                    f"category_sets write would discard advanced canonical rules in set {set_id}"
                )
            replacement = copy.deepcopy(existing) if existing is not None else {
                "schema_version": 2,
                "id": set_id,
            }
            replacement["categories"] = _legacy_categories_to_v2(
                legacy_set.get("categories"), set_id, f"{set_path}.categories"
            )
            if existing is None:
                updated[SET_KEY].append(replacement)
            else:
                updated[SET_KEY][updated[SET_KEY].index(existing)] = replacement
            sets_by_id[set_id] = replacement
    else:
        raise RulesValidationError(f"{key} is not a legacy rules key")
    return validate_rules_envelope(updated)
