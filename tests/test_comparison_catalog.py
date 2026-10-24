"""PRD-115/T1: the comparison dimensions and competitor list are a closed set.

The YAML files are the contract a later task fills in. These tests fail if a
dimension or competitor disappears, if a filled claim sneaks into T1, or if
the design note drifts away from the lists.
"""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DIM_PATH = ROOT / "docs" / "compare" / "dimensions.yaml"
COMP_PATH = ROOT / "docs" / "compare" / "competitors.yaml"
DOC_PATH = ROOT / "docs" / "plans" / "2026-10-21-comparison-dimensions.md"

DIMENSION_IDS = (
    "product",
    "who_runs_it",
    "source",
    "scale",
    "free_evidence",
    "local_endpoint",
    "protocols",
    "routing",
    "isolation",
    "discovery",
    "mcp",
    "evaluation",
    "analytics",
    "public_status",
    "install",
    "audience",
)
GROUPS = ("position", "access", "operations", "fit")
INCLUDED_IDS = ("omniroute", "9router", "litellm", "free-router")
EXCLUDED_IDS = ("rakazo", "openrouter", "new-api", "cloudflare-ai-gateway")


def _load(path: Path) -> dict:
    data = yaml.safe_load(path.read_text())
    assert isinstance(data, dict)
    return data


def test_dimensions_are_the_closed_set():
    doc = _load(DIM_PATH)
    assert doc["trend_issue"] == 574
    assert doc["format_reference"]["license"] == "Apache-2.0"
    assert doc["format_reference"]["not_a_competitor"] is True
    assert doc["page_shape"]["replaces_live_matrix"] is False
    assert doc["page_shape"]["slug_pattern"] == "vs-{competitor_id}"

    rules = doc["cell_rules"]
    assert rules["unknown_token"] == "unspecified"
    assert rules["unknown_display_zh"] == "资料未说明"
    assert rules["forbid_inferred_absence"] is True
    assert rules["require_source_for_filled_cells"] is True
    assert rules["numbers_require_snapshot_date"] is True
    assert rules["no_scores"] is True
    assert rules["no_winner_column"] is True
    assert rules["vs_card"]["bullets_per_side"] == 3
    assert rules["vs_card"]["must_compress_existing_rows"] is True
    assert rules["vs_card"]["written_in_this_task"] is False

    assert tuple(group["id"] for group in doc["groups"]) == GROUPS
    ids = tuple(row["id"] for row in doc["dimensions"])
    assert ids == DIMENSION_IDS
    assert len(set(ids)) == len(ids)
    for row in doc["dimensions"]:
        assert row["group"] in GROUPS
        assert row["topic_zh"].strip()
        assert row["question"].strip()
        assert "claims" not in row


def test_competitor_list_is_closed_and_unsourced_cells_stay_empty():
    doc = _load(COMP_PATH)
    assert doc["trend_issue"] == 574
    assert doc["ours"]["id"] == "open-free-router"
    assert doc["ours"]["id"] not in INCLUDED_IDS

    included = doc["included"]
    assert tuple(item["id"] for item in included) == INCLUDED_IDS
    for item in included:
        assert item["why_included"].strip()
        assert "claims" not in item
        hrefs = [source["href"] for source in item["sources"]]
        assert hrefs, item["id"]
        assert all(href.startswith("https://") for href in hrefs)
        assert all(source["label"].strip() for source in item["sources"])

    excluded = {item["id"]: item for item in doc["excluded"]}
    assert tuple(excluded) == EXCLUDED_IDS
    assert excluded["rakazo"]["reason"].strip()
    for item in doc["excluded"]:
        assert item["reason"].strip()
        assert "sources" not in item or not item["sources"]


def test_design_note_names_the_same_ids_and_the_trend_issue():
    text = DOC_PATH.read_text()
    assert "zhanglunet/trend#574" in text
    assert "Apache-2.0" in text
    for dim_id in DIMENSION_IDS:
        assert f"`{dim_id}`" in text
    for comp_id in INCLUDED_IDS:
        assert f"`{comp_id}`" in text
    for comp_id in EXCLUDED_IDS:
        assert f"`{comp_id}`" in text
    assert "site/compare/" in text
