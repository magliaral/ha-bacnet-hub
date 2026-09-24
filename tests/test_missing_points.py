"""Tests for per-point availability and bacnet_hub.remove_missing_points."""

from __future__ import annotations

from types import SimpleNamespace

import custom_components.bacnet_hub as bacnet_hub_init
import custom_components.bacnet_hub.client_point_entities as entities_module
from custom_components.bacnet_hub.client_point_entities import BacnetClientPointEntityBase
from custom_components.bacnet_hub.client_runtime import _client_points_get
from custom_components.bacnet_hub.const import DOMAIN, POINT_MISSING_KEY


def _hass_with_points() -> SimpleNamespace:
    return SimpleNamespace(data={DOMAIN: {"client_point_cache": {"entry1": {"client_5": {
        "ai_1": {"point_key": "ai_1", "type_slug": "ai", "object_type": "analog-input", "object_instance": 1},
        "ai_2": {"point_key": "ai_2", "type_slug": "ai", "object_type": "analog-input", "object_instance": 2},
    }}}}})


def test_unavailable_is_per_point_by_default(monkeypatch) -> None:
    hass = _hass_with_points()
    monkeypatch.setattr(entities_module, "async_dispatcher_send", lambda *a, **k: None)
    entity = BacnetClientPointEntityBase(hass, "entry1", "client_5", 5, "ai_1", entity_domain="sensor")

    entity._set_client_points_unavailable(True, reason="cov_register_failed")

    cache = _client_points_get(hass, "entry1", "client_5")
    assert cache["ai_1"]["_cov_unavailable"] is True
    assert cache["ai_1"]["_cov_unavailable_reason"] == "cov_register_failed"
    assert "_cov_unavailable" not in cache["ai_2"]

    entity._set_client_points_unavailable(True, reason="bacnet_app_unavailable", all_points=True)
    assert cache["ai_2"]["_cov_unavailable"] is True

    entity._set_client_points_unavailable(False)
    assert cache["ai_1"]["_cov_unavailable"] is False
    assert cache["ai_2"]["_cov_unavailable"] is True  # untouched: per point


def test_remove_missing_points_removes_flagged_entities_only(monkeypatch) -> None:
    hass = _hass_with_points()
    cache = _client_points_get(hass, "entry1", "client_5")
    cache["ai_2"][POINT_MISSING_KEY] = True
    cache["bi_9"] = {"point_key": "bi_9", "type_slug": "bi", "object_instance": 9, POINT_MISSING_KEY: True}
    loaded = SimpleNamespace(_entry_id="entry1", _client_id="client_5", _point_key="ai_2")
    hass.data[DOMAIN]["client_point_entities"] = {"sensor.bacnet_doi_5_ai_2": loaded}

    removed: list[str] = []
    registry = SimpleNamespace(async_remove=removed.append)
    monkeypatch.setattr(bacnet_hub_init.er, "async_get", lambda hass: registry)
    monkeypatch.setattr(
        bacnet_hub_init.er,
        "async_entries_for_config_entry",
        lambda reg, entry_id: [
            SimpleNamespace(entity_id="binary_sensor.bacnet_doi_5_bi_9", unique_id="entry1-client_5-point-bi-9", domain="binary_sensor"),
            # orphan: object vanished before a restart rebuilt the cache
            SimpleNamespace(entity_id="switch.bacnet_doi_5_bo_30", unique_id="entry1-client_5-point-bo-30", domain="switch"),
            # another client without cache: must be left alone
            SimpleNamespace(entity_id="switch.bacnet_doi_7_bo_1", unique_id="entry1-client_7-point-bo-1", domain="switch"),
            # cached, present point and a non-point entry: left alone
            SimpleNamespace(entity_id="sensor.bacnet_doi_5_ai_1", unique_id="entry1-client_5-point-ai-1", domain="sensor"),
            # same object imported earlier on another platform: stale twin
            SimpleNamespace(entity_id="number.bacnet_doi_5_ai_1", unique_id="entry1-client_5-point-ai-1", domain="number"),
            SimpleNamespace(entity_id="sensor.bacnet_doi_5_object_name", unique_id="entry1-client_5-diag-object_name", domain="sensor"),
        ],
    )

    result = bacnet_hub_init._remove_missing_points(hass)

    assert sorted(result) == [
        "binary_sensor.bacnet_doi_5_bi_9",
        "number.bacnet_doi_5_ai_1",
        "sensor.bacnet_doi_5_ai_2",
        "switch.bacnet_doi_5_bo_30",
    ]
    assert sorted(removed) == sorted(result)
    assert set(cache) == {"ai_1"}
