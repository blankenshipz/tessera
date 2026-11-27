from __future__ import annotations

from test_research import _command, _project, _setup

from tessera_control.research import ResearchStore


def test_list_all_studies_pages_past_the_single_call_cap(tmp_path):
    _, store, owner = _setup(tmp_path)
    project = _project(store, owner)
    total = 230
    for n in range(total):
        _command(
            store.create_study,
            owner,
            project["id"],
            payload={"purpose": f"Synthetic study {n}", "protocol": "Synthetic"},
        )
    assert len(store.list_studies(owner, project["id"], 0, 200)) == 200
    assert len(store.list_studies(owner, project["id"])) == 100
    everything = store.list_all_studies(owner, project["id"])
    assert len(everything) == total
    assert len({s["id"] for s in everything}) == total
    # A page size that divides the total exactly still terminates (empty last page).
    assert len(store.list_all_studies(owner, project["id"], page_size=115)) == total


def test_list_all_studies_is_bounded(tmp_path, monkeypatch):
    _, store, owner = _setup(tmp_path)
    project = _project(store, owner)
    calls = []

    def endless(self, principal, project_id, offset=0, limit=100, campaign_id=None):
        calls.append(offset)
        return [{"id": str(offset)}] * limit

    monkeypatch.setattr(ResearchStore, "list_studies", endless)
    try:
        store.list_all_studies(owner, project["id"], page_size=5, max_pages=7)
    except Exception as error:
        assert "page bound" in str(error)
    else:
        raise AssertionError("expected bound error")
    assert len(calls) == 7


def test_a_campaign_scoped_listing_names_its_stage_studies(tmp_path):
    from salad_campaign_fixture import SaladWorld, salad_provider_reset  # noqa: F401

    world = SaladWorld(tmp_path)
    world.start()
    world.tick_until(
        lambda: world.row("SELECT study_id FROM campaign_salad_stages WHERE study_id IS NOT NULL")
        is not None
    )
    study_id = world.row("SELECT study_id FROM campaign_salad_stages")["study_id"]
    project_id = world.campaign["project_id"]
    store = ResearchStore(world.env.state)
    # An unrelated study of the same project is not part of the campaign's listing.
    _command(
        store.create_study,
        world.human,
        project_id,
        payload={"purpose": "Unrelated study", "protocol": "Synthetic"},
    )
    scoped = store.list_studies(world.human, project_id, campaign_id=world.campaign_id)
    assert [s["id"] for s in scoped] == [study_id]
    assert store.list_studies(world.human, project_id, campaign_id="campaign_other") == []
    assert len(store.list_studies(world.human, project_id)) == 2
