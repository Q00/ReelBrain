import json

import pytest

from reelbrain.memory import DeletionFenceRegistry, PreferenceScope, PreferenceStore


SHORT_TECH = PreferenceScope(output_mode="short", content_kind="technical", language="en")


def record_examples(store: PreferenceStore, count: int = 2):
    for index in range(count):
        store.record_feedback(
            creator_id="creator-1",
            project_id=f"project-{index}",
            category="caption_style",
            value="yellow-keyword-emphasis",
            scope=SHORT_TECH,
        )


def test_episode_feedback_does_not_become_durable_without_confirmation():
    store = PreferenceStore()
    record_examples(store, 2)

    assert store.inspect("creator-1") == ()
    proposal = store.propose(
        creator_id="creator-1", category="caption_style", scope=SHORT_TECH
    )
    assert proposal is not None
    assert proposal.value == "yellow-keyword-emphasis"


def test_two_confirmed_examples_transfer_to_next_relevant_edit():
    store = PreferenceStore()
    record_examples(store, 2)
    preference = store.confirm(
        store.propose(creator_id="creator-1", category="caption_style", scope=SHORT_TECH)
    )

    result = store.resolve(
        creator_id="creator-1",
        category="caption_style",
        context=SHORT_TECH,
        default="clean-white",
    )

    assert result.value == "yellow-keyword-emphasis"
    assert result.preference_id == preference.preference_id
    assert result.source == "explicit_preference"


def test_irrelevant_context_abstains_instead_of_misapplying_memory():
    store = PreferenceStore()
    record_examples(store, 3)
    store.confirm(store.propose(creator_id="creator-1", category="caption_style", scope=SHORT_TECH))

    result = store.resolve(
        creator_id="creator-1",
        category="caption_style",
        context=PreferenceScope(output_mode="long", content_kind="technical", language="en"),
    )

    assert result.source == "abstain"
    assert result.reason == "no_relevant_confirmed_preference"


def test_precedence_is_steering_then_override_then_specific_preference():
    store = PreferenceStore()
    broad_event = store.record_feedback(
        creator_id="creator-1",
        project_id="project-broad",
        category="pacing",
        value="natural",
        scope=PreferenceScope(output_mode="short"),
        remember=True,
    )
    store.record_feedback(
        creator_id="creator-1",
        project_id="project-specific",
        category="pacing",
        value="tight-technical",
        scope=SHORT_TECH,
        remember=True,
    )

    specific = store.resolve(
        creator_id="creator-1", category="pacing", context=SHORT_TECH
    )
    overridden = store.resolve(
        creator_id="creator-1",
        category="pacing",
        context=SHORT_TECH,
        edit_override="slow-this-edit",
    )
    steered = store.resolve(
        creator_id="creator-1",
        category="pacing",
        context=SHORT_TECH,
        edit_override="slow-this-edit",
        current_steering="keep-full-pauses",
    )

    assert broad_event.kind == "remember"
    assert specific.value == "tight-technical"
    assert overridden.value == "slow-this-edit"
    assert overridden.source == "edit_override"
    assert steered.value == "keep-full-pauses"
    assert steered.source == "current_steering"


def test_creator_can_inspect_edit_disable_and_reenable_memory():
    store = PreferenceStore()
    event = store.record_feedback(
        creator_id="creator-1",
        project_id="project-1",
        category="hook",
        value="start-with-thesis",
        scope=SHORT_TECH,
        remember=True,
    )
    preference = store.inspect("creator-1")[0]

    edited = store.edit(preference.preference_id, value="start-with-surprising-result")
    disabled = store.set_enabled(preference.preference_id, False)
    defaulted = store.resolve(
        creator_id="creator-1", category="hook", context=SHORT_TECH, default="thesis"
    )
    enabled = store.set_enabled(preference.preference_id, True)

    assert event.event_id in edited.provenance_event_ids
    assert edited.version == 2
    assert disabled.status == "disabled"
    assert defaulted.value == "thesis"
    assert enabled.status == "active"
    assert enabled.version == 4


def test_delete_removes_content_and_tombstone_prevents_resurrection():
    store = PreferenceStore()
    store.record_feedback(
        creator_id="creator-1",
        project_id="project-1",
        category="music",
        value="ambient-low",
        scope=SHORT_TECH,
        remember=True,
    )
    preference = store.inspect("creator-1")[0]
    exported = store.export_json("creator-1")

    tombstone = store.delete(preference.preference_id)

    assert store.inspect("creator-1") == ()
    assert tombstone.preference_id == preference.preference_id
    assert "ambient-low" not in json.dumps([t.__dict__ for t in store.tombstones])
    with pytest.raises(ValueError, match="deleted_preference_resurrection_denied"):
        store.import_json("creator-1", exported)


def test_preferences_are_portable_but_never_cross_creator():
    source = PreferenceStore()
    source.record_feedback(
        creator_id="creator-1",
        project_id="project-1",
        category="reframing",
        value="preserve-code",
        scope=SHORT_TECH,
        remember=True,
    )
    payload = source.export_json("creator-1")

    destination = PreferenceStore()
    imported = destination.import_json("creator-1", payload)

    assert imported[0].value == "preserve-code"
    with pytest.raises(ValueError, match="cross_creator_preference_import_denied"):
        PreferenceStore().import_json("creator-2", payload)


def test_inconsistent_examples_do_not_create_a_high_confidence_proposal():
    store = PreferenceStore()
    for index, value in enumerate(("fast", "slow", "natural")):
        store.record_feedback(
            creator_id="creator-1",
            project_id=f"project-{index}",
            category="pacing",
            value=value,
            scope=SHORT_TECH,
        )

    assert store.propose(
        creator_id="creator-1", category="pacing", scope=SHORT_TECH
    ) is None


def test_writes_all_declared_memory_artifacts_and_transfer_report(tmp_path):
    store = PreferenceStore()
    record_examples(store, 2)
    store.confirm(
        store.propose(creator_id="creator-1", category="caption_style", scope=SHORT_TECH)
    )

    artifacts = store.write_artifacts(
        tmp_path,
        creator_id="creator-1",
        evaluation_category="caption_style",
        evaluation_context=SHORT_TECH,
        frozen_baseline_value="clean-white",
    )

    assert set(artifacts) == {
        "preference_ledger",
        "feedback_events",
        "preference_snapshots",
        "deletion_tombstones",
        "personalized_vs_baseline_evaluation",
    }
    assert all(path.is_file() for path in artifacts.values())
    report = json.loads(artifacts["personalized_vs_baseline_evaluation"].read_text())
    assert report["frozen_baseline"] == "clean-white"
    assert report["personalized_value"] == "yellow-keyword-emphasis"
    assert report["preference_applied"] is True


def test_old_pre_delete_backup_cannot_resurrect_in_a_fresh_store():
    shared_fences = DeletionFenceRegistry()
    source = PreferenceStore(deletion_fences=shared_fences)
    source.record_feedback(
        creator_id="creator-1",
        project_id="project-1",
        category="pacing",
        value="natural",
        scope=SHORT_TECH,
        remember=True,
    )
    old_backup = source.export_json("creator-1")
    preference_id = source.inspect("creator-1")[0].preference_id
    source.delete(preference_id)

    restored_store = PreferenceStore(deletion_fences=shared_fences)
    with pytest.raises(ValueError, match="deleted_preference_resurrection_denied"):
        restored_store.import_json("creator-1", old_backup)


def test_deletion_tombstones_are_portable_without_content_values():
    fences = DeletionFenceRegistry()
    source = PreferenceStore(deletion_fences=fences)
    source.record_feedback(
        creator_id="creator-1",
        project_id="project-1",
        category="music",
        value="ambient-low",
        scope=SHORT_TECH,
        remember=True,
    )
    source.delete(source.inspect("creator-1")[0].preference_id)
    payload = source.export_json("creator-1")

    document = json.loads(payload)

    assert document["preferences"] == []
    assert len(document["deletion_tombstones"]) == 1
    assert "ambient-low" not in payload
