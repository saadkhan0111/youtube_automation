"""AI Information Video Studio: foundation tests (state machine, worker, costs, API). No real API calls."""
import json

import pytest
from fastapi.testclient import TestClient

from app import db
from app.config import settings
from app.studio import costs, stages, worker
from app.studio import settings as ss
from app.studio.models import StudioProject

H = {"X-Requested-With": "ytshorts"}
BASE = {"topic": "Hidden details in the GTA 6 trailers", "category": "gaming", "video_format": "short",
        "target_duration": 60, "editing_style": "viral", "narration_style": "energetic"}


@pytest.fixture()
def client(env, monkeypatch):
    from app import jobs, main

    monkeypatch.setattr(jobs, "trigger_queue", lambda: None)
    monkeypatch.setattr(worker, "synchronous", True)
    monkeypatch.setattr(ss.clip_settings, "openai_available", lambda: True)
    monkeypatch.setattr(worker, "HANDLERS", {})
    with TestClient(main.create_app()) as c:
        yield c


def _create(client, **over):
    r = client.post("/api/studio/projects", json={**BASE, **over}, headers=H)
    assert r.status_code == 200, r.text
    return r.json()


def _status(p, key):
    return next(s["status"] for s in p["stages"] if s["key"] == key)


# ------------------------------------------------------------------ project creation
def test_options_list_everything_the_form_needs(client):
    o = client.get("/api/studio/options").json()
    assert set(o["editing_styles"]) == {"viral", "documentary", "explainer", "countdown"}
    assert len(o["categories"]) == 6 and o["formats"]["short"] and o["voices"][0] == "cedar"
    assert [s["key"] for s in o["stages"]] == ss.STAGES
    assert set(o["models"]) >= {"research", "script", "storyboard", "image", "tts", "transcribe"}


def test_create_project_and_initial_stages(client):
    p = _create(client, reference_urls=["https://www.rockstargames.com/VI", "  "], audience="GTA fans")
    assert p["reference_urls"] == ["https://www.rockstargames.com/VI"]
    assert p["voice"] == ss.DEFAULT_VOICE and p["stage"] == "research"
    assert _status(p, "research") == "ready" and all(_status(p, s) == "locked" for s in ss.STAGES[1:])
    assert (settings.data_dir / "studio" / str(p["id"])).is_dir()
    assert [x["id"] for x in client.get("/api/studio/projects").json()] == [p["id"]]


@pytest.mark.parametrize("over,msg", [
    ({"category": "nope"}, "category"),
    ({"editing_style": "nope"}, "editing style"),
    ({"video_format": "short", "target_duration": 600}, "15-180"),
    ({"video_format": "long", "target_duration": 60}, "120-1800"),
    ({"voice": "robot"}, "voice"),
    ({"reference_urls": ["ftp://x.com/a"]}, "valid web address"),
    ({"topic": "x"}, ""),
])
def test_create_project_validation(client, over, msg):
    r = client.post("/api/studio/projects", json={**BASE, **over}, headers=H)
    assert r.status_code in (400, 422)
    assert msg.lower() in r.text.lower()


def test_csrf_required(client):
    assert client.post("/api/studio/projects", json=BASE).status_code == 403


# ------------------------------------------------------------------ workflow
def test_unbuilt_stage_is_reported_not_faked(client):
    p = _create(client)
    r = client.post(f"/api/studio/projects/{p['id']}/stages/research/run", headers=H)
    assert r.status_code == 501 and "not built yet" in r.json()["detail"]
    assert _status(client.get(f"/api/studio/projects/{p['id']}").json(), "research") == "ready"


def test_stage_flow_review_approve_and_order(client):
    calls = []

    def research(pid, report):
        report(0.5, "Searching")
        calls.append(("research", pid))

    worker.HANDLERS["research"] = research
    worker.HANDLERS["script"] = lambda pid, report: calls.append(("script", pid))
    p = _create(client)
    pid = p["id"]
    # later stages can't run before earlier ones are approved
    assert client.post(f"/api/studio/projects/{pid}/stages/script/run", headers=H).status_code == 400
    p = client.post(f"/api/studio/projects/{pid}/stages/research/run", headers=H).json()
    assert calls == [("research", pid)] and _status(p, "research") == "review"    # waits for the user
    assert _status(p, "script") == "locked"                                        # nothing continues automatically
    p = client.post(f"/api/studio/projects/{pid}/stages/research/approve", headers=H).json()
    assert _status(p, "research") == "approved" and _status(p, "script") == "ready" and p["stage"] == "script"
    assert client.post(f"/api/studio/projects/{pid}/stages/research/approve", headers=H).status_code == 400
    p = client.post(f"/api/studio/projects/{pid}/stages/script/run", headers=H).json()
    assert _status(p, "script") == "review"


def test_failure_is_recorded_and_only_that_stage_retried(client):
    attempts = {"n": 0}

    def flaky(pid, report):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("search provider timed out")

    worker.HANDLERS["research"] = flaky
    pid = _create(client)["id"]
    p = client.post(f"/api/studio/projects/{pid}/stages/research/run", headers=H).json()
    research = next(s for s in p["stages"] if s["key"] == "research")
    assert research["status"] == "failed" and "timed out" in research["error"]
    p = client.post(f"/api/studio/projects/{pid}/stages/research/run", headers=H).json()
    assert _status(p, "research") == "review" and attempts["n"] == 2


def test_rerunning_or_editing_an_earlier_stage_locks_later_ones(client):
    for s in ("research", "script"):
        worker.HANDLERS[s] = lambda pid, report: None
    pid = _create(client)["id"]
    client.post(f"/api/studio/projects/{pid}/stages/research/run", headers=H)
    client.post(f"/api/studio/projects/{pid}/stages/research/approve", headers=H)
    client.post(f"/api/studio/projects/{pid}/stages/script/run", headers=H)
    # changing the topic re-opens research and locks the script built on the old research
    p = client.patch(f"/api/studio/projects/{pid}", json={"topic": "GTA 6 map size explained"}, headers=H).json()
    assert p["topic"] == "GTA 6 map size explained" and p["stage"] == "research"
    assert _status(p, "research") == "review" and _status(p, "script") == "locked"
    # settings that don't affect research don't reopen anything
    p = client.patch(f"/api/studio/projects/{pid}", json={"voice": "marin", "use_avatar": True}, headers=H).json()
    assert p["voice"] == "marin" and p["use_avatar"] is True and _status(p, "research") == "review"
    assert client.patch(f"/api/studio/projects/{pid}", json={"voice": "robot"}, headers=H).status_code == 400


def test_restart_recovery_marks_running_stage_failed(env):
    with db.session_scope() as s:
        p = StudioProject(**{k: v for k, v in BASE.items()}, language="en", stage="research")
        p.stages = stages.initial_stages()
        s.add(p)
        s.flush()
        stages.mark_running(p, "research")
        pid = p.id
    worker.recover_on_startup()
    with db.session_scope() as s:
        p = s.get(StudioProject, pid)
        assert stages.status(p, "research") == "failed" and "restart" in p.stages["research"]["error"]


def test_delete_project_removes_files(client):
    p = _create(client)
    d = settings.data_dir / "studio" / str(p["id"])
    (d / "x.txt").write_text("x")
    assert client.delete(f"/api/studio/projects/{p['id']}", headers=H).status_code == 200
    assert not d.exists() and client.get(f"/api/studio/projects/{p['id']}").status_code == 404


# ------------------------------------------------------------------ costs
def test_costs_record_units_and_only_known_prices(client):
    pid = _create(client)["id"]
    costs.record(pid, "research", "openai", "chat", {"input_tokens": 2000, "output_tokens": 500}, model="mystery-model")
    costs.record(pid, "voice", "openai", "transcribe", {"minutes": 2.0}, model="whisper-1")
    s = client.get(f"/api/studio/projects/{pid}/costs").json()
    assert s["total_usd"] == pytest.approx(0.012)                       # whisper has a known price
    assert s["unpriced_calls"] == 1 and s["by_stage"]["research"]["units"]["input_tokens"] == 2000
    assert "pricing.json" in s["note"]
    # prices can be configured without code changes
    (settings.data_dir / "pricing.json").write_text(json.dumps({"mystery-model": {"input_per_million": 1.0, "output_per_million": 4.0}}))
    costs.record(pid, "research", "openai", "chat", {"input_tokens": 1_000_000, "output_tokens": 250_000}, model="mystery-model")
    s = client.get(f"/api/studio/projects/{pid}/costs").json()
    assert s["total_usd"] == pytest.approx(0.012 + 2.0)
    assert client.get(f"/api/studio/projects/{pid}").json()["costs"]["events"] == 3


# ------------------------------------------------------------------ research engine
from app.studio import fetch, llm, research
from app.studio.models import StudioSource

PAGES = ["https://www.rockstargames.com/VI/trailer-2", "https://www.miaminewtimes.com/gta-references",
         "https://gta-fan-wiki.example/easter-eggs"]


def _raw_report(extra_sources=(), facts=None):
    sources = [
        {"id": "S1", "url": PAGES[0] + "?utm_source=openai", "title": "Trailer 2", "publisher": "Rockstar Games",
         "source_type": "primary", "reliability": "high"},
        {"id": "S2", "url": PAGES[1], "title": "Real Miami references", "publisher": "Miami New Times",
         "source_type": "publication", "reliability": "high"},
        {"id": "S3", "url": PAGES[2], "title": "Easter eggs", "publisher": "Fan wiki",
         "source_type": "community", "reliability": "low"},
        *extra_sources,
    ]
    facts = facts or [
        {"id": "F1", "claim": "All Trailer 2 footage was captured on PS5.", "detail": "Rockstar says so.",
         "status": "verified", "source_ids": ["S1"], "why_interesting": "x"},
        {"id": "F2", "claim": "A scene mirrors a real 2017 Miami incident.", "detail": "d", "status": "verified",
         "source_ids": ["S2"], "why_interesting": "x"},
        {"id": "F3", "claim": "The map has named regions beyond Vice City.", "detail": "d", "status": "verified",
         "source_ids": ["S1", "S2"], "why_interesting": "x"},
        {"id": "F4", "claim": "A banner might parody nightlife ads.", "detail": "d", "status": "verified",
         "source_ids": ["S2"], "why_interesting": "x"},
        {"id": "F5", "claim": "A hidden dolphin appears in trailer 1.", "detail": "d", "status": "verified",
         "source_ids": ["S3"], "why_interesting": "x"},
        {"id": "F6", "claim": "The trailer was edited by a famous director.", "detail": "d", "status": "verified",
         "source_ids": ["S9"], "why_interesting": "x"},
    ]
    return {"summary": "s", "best_angle": "a", "sources": sources, "facts": facts,
            "disputed": [{"claim": "Every scene is gameplay.", "explanation": "unconfirmed", "source_ids": ["S1"]}],
            "gaps": ["Easter eggs not officially confirmed"]}


def test_validate_report_enforces_evidence_rules():
    allowed = {fetch.normalize_url(u) for u in PAGES}
    invented = {"id": "S9", "url": "https://made-up-source.example/fake", "title": "t", "publisher": "p",
                "source_type": "publication", "reliability": "high"}
    out = research.validate_report(_raw_report([invented]), allowed, set())
    assert [s["id"] for s in out["sources"]] == ["S1", "S2", "S3"]                    # invented URL dropped
    assert out["rejected_sources"][0]["url"].startswith("https://made-up-source")
    f = {x["id"]: x for x in out["facts"]}
    assert f["F1"]["status"] == "verified" and f["F1"]["include"]
    assert f["F4"]["status"] == "uncertain" and "hedged wording" in f["F4"]["flags"]   # "might" is not a fact
    assert f["F5"]["status"] == "uncertain" and "only community sources" in f["F5"]["flags"]
    assert f["F6"]["status"] == "uncertain" and f["F6"]["source_ids"] == [] and not f["F6"]["include"]


def test_validate_report_fails_without_enough_evidence():
    with pytest.raises(research.ResearchError, match="No usable sources"):
        research.validate_report(_raw_report(), set(), set())
    only_community = {fetch.normalize_url(PAGES[2])}
    with pytest.raises(research.ResearchError, match="could be verified"):
        research.validate_report(_raw_report(), only_community, set())
    with pytest.raises(research.ResearchError, match="invalid structure"):
        research.validate_report({"summary": "x"}, set(), set())


def test_user_references_count_as_allowed_sources():
    ref = fetch.normalize_url("https://example.org/official-press-release")
    extra = {"id": "S4", "url": "https://example.org/official-press-release/", "title": "Press", "publisher": "Org",
             "source_type": "primary", "reliability": "medium"}
    out = research.validate_report(_raw_report([extra]), {fetch.normalize_url(u) for u in PAGES} | {ref}, {ref})
    s4 = next(s for s in out["sources"] if s["id"] == "S4")
    assert s4["user_provided"] and s4["reliability"] == "high"


def test_normalize_url_and_html_extraction(monkeypatch):
    assert fetch.normalize_url("https://WWW.Example.com/a/?utm_source=openai&x=1#top") == "https://example.com/a?x=1"
    html = ("<html><head><title> GTA VI </title><script>var x=1</script></head><body><nav>menu</nav>"
            "<p>" + "Rockstar confirmed the trailer was captured on PS5. " * 10 + "</p></body></html>")
    monkeypatch.setattr(fetch, "http_get_hook", lambda url: (200, "text/html; charset=utf-8", html.encode()))
    page = fetch.fetch("https://example.com")
    assert page.ok and page.title == "GTA VI" and "captured on PS5" in page.text
    assert "var x" not in page.text and "menu" not in page.text
    monkeypatch.setattr(fetch, "http_get_hook", lambda url: (200, "text/html", b"<html><body>Loading...</body></html>"))
    assert not fetch.fetch("https://spa.example").ok


@pytest.fixture()
def research_stubs(monkeypatch):
    calls = {"web": 0, "structure": 0}

    def web(model, prompt):
        calls["web"] += 1
        calls["prompt"] = prompt
        return llm.WebResult("notes about the trailers", PAGES, search_calls=4, input_tokens=5000, output_tokens=800)

    def structure(model, system, user, schema):
        calls["structure"] += 1
        calls["structure_input"] = user
        return _raw_report()

    monkeypatch.setattr(llm, "web_research_hook", web)
    monkeypatch.setattr(llm, "chat_json_hook", structure)
    page = ("<p>" + "Official press release text about GTA VI. " * 20 + "</p>").encode()
    monkeypatch.setattr(fetch, "http_get_hook", lambda url: (200, "text/html", page))
    monkeypatch.setitem(worker.HANDLERS, "research", research.run)
    return calls


def test_research_stage_end_to_end(client, research_stubs):
    p = _create(client, reference_urls=["https://www.rockstargames.com/VI"], instructions="no spoilers")
    p = client.post(f"/api/studio/projects/{p['id']}/stages/research/run", headers=H).json()
    assert _status(p, "research") == "review", p["stages"][0]
    assert "no spoilers" in research_stubs["prompt"] and "Official press release text" in research_stubs["prompt"]
    assert "rockstargames.com/VI/trailer-2" in research_stubs["structure_input"]       # only real URLs offered
    r = client.get(f"/api/studio/projects/{p['id']}/research").json()
    assert r["search"] == {"performed": True, "model": ss.RESEARCH_MODEL, "search_calls": 4, "pages_seen": 3, "error": None}
    assert r["references"][0]["ok"] and len(r["facts"]) == 6 and r["disputed"] and r["gaps"]
    with db.session_scope() as s:
        assert {x.url for x in s.query(StudioSource).filter_by(project_id=p["id"])} == {
            PAGES[0] + "?utm_source=openai", PAGES[1], PAGES[2]}
    c = client.get(f"/api/studio/projects/{p['id']}/costs").json()
    assert c["by_stage"]["research"]["units"]["calls"] == 4
    assert c["by_stage"]["research"]["units"]["input_tokens"] == 5000


def test_research_without_search_or_references_asks_for_material(client, research_stubs, monkeypatch):
    def broken(model, prompt):
        raise ConnectionError("search down")

    monkeypatch.setattr(llm, "web_research_hook", broken)
    p = _create(client)
    p = client.post(f"/api/studio/projects/{p['id']}/stages/research/run", headers=H).json()
    st = next(s for s in p["stages"] if s["key"] == "research")
    assert st["status"] == "failed" and "Add reference URLs" in st["error"]
    assert research_stubs["structure"] == 0                       # nothing was "researched" from thin air


def test_research_falls_back_to_references_when_search_fails(client, research_stubs, monkeypatch):
    def broken(model, prompt):
        raise ConnectionError("down")

    monkeypatch.setattr(llm, "web_research_hook", broken)
    three = [{"id": f"F{i}", "claim": f"Official fact number {i} about trailer two.", "detail": "d",
              "status": "verified", "source_ids": ["S1"], "why_interesting": "x"} for i in range(1, 4)]
    monkeypatch.setattr(llm, "chat_json_hook", lambda m, s, u, sc: _raw_report(facts=three))
    p = _create(client, reference_urls=[PAGES[0]])
    p = client.post(f"/api/studio/projects/{p['id']}/stages/research/run", headers=H).json()
    assert _status(p, "research") == "review"
    r = client.get(f"/api/studio/projects/{p['id']}/research").json()
    assert r["search"]["performed"] is False and "down" in r["search"]["error"]
    assert [s["url"] for s in r["sources"]] == [PAGES[0] + "?utm_source=openai"]    # only the user's reference survives


def test_research_edits(client, research_stubs):
    pid = _create(client)["id"]
    assert client.get(f"/api/studio/projects/{pid}/research").status_code == 404
    client.post(f"/api/studio/projects/{pid}/stages/research/run", headers=H)
    url = f"/api/studio/projects/{pid}/research"
    r = client.patch(url, json={"facts": [{"id": "F1", "status": "disputed"}]}, headers=H).json()
    f1 = next(f for f in r["facts"] if f["id"] == "F1")
    assert f1["status"] == "disputed" and f1["include"] is False and f1["edited"]       # status change resets inclusion
    r = client.patch(url, json={"facts": [{"id": "F1", "include": True, "claim": "Trailer 2 was captured on PS5."}],
                                "best_angle": "Real Florida inspirations"}, headers=H).json()
    assert next(f for f in r["facts"] if f["id"] == "F1")["include"] and r["best_angle"] == "Real Florida inspirations"
    assert client.patch(url, json={"facts": [{"id": "F6", "status": "verified"}]}, headers=H).status_code == 400
    assert client.patch(url, json={"facts": [{"id": "F2", "delete": True}]}, headers=H).status_code == 200
    assert "F2" not in [f["id"] for f in client.get(url).json()["facts"]]
    assert client.patch(url, json={"facts": [{"id": "F99", "include": False}]}, headers=H).status_code == 400
    # editing approved research re-opens it
    client.post(f"/api/studio/projects/{pid}/stages/research/approve", headers=H)
    client.patch(url, json={"facts": [{"id": "F3", "include": False}]}, headers=H)
    assert _status(client.get(f"/api/studio/projects/{pid}").json(), "research") == "review"


# ------------------------------------------------------------------ scriptwriter
from app.studio import script as scriptmod

RESEARCH = {
    "best_angle": "Real Florida inspirations",
    "facts": [
        {"id": "F1", "claim": "All Trailer 2 footage was captured on PS5.", "detail": "Released May 6, 2025.",
         "status": "verified", "source_ids": ["S1"], "include": True},
        {"id": "F2", "claim": "A scene mirrors a real 2017 Miami incident.", "detail": "MacArthur Causeway.",
         "status": "verified", "source_ids": ["S2"], "include": True},
        {"id": "F3", "claim": "Trailer 1 features Tom Petty's Love Is A Long Road.", "detail": "From 1989.",
         "status": "verified", "source_ids": ["S3"], "include": True},
        {"id": "F4", "claim": "A banner might parody nightlife ads.", "detail": "d", "status": "uncertain",
         "source_ids": ["S2"], "include": False},
    ],
    "disputed": [{"claim": "Every scene is gameplay.", "explanation": "unconfirmed", "source_ids": []}],
    "sources": [], "gaps": [], "summary": "s", "rejected_sources": [],
}


def _scene(purpose, narration, fact_ids=(), emphasis=()):
    return {"purpose": purpose, "narration": narration, "fact_ids": list(fact_ids), "emphasis": list(emphasis)}


def _good_script():
    return {"title": "GTA VI Trailer Details You Missed", "thumbnail": {"concept": "c", "text": "YOU MISSED THIS"},
            "description": "d", "scenes": [
                _scene("hook", "GTA 6 trailers hide real Florida chaos."),
                _scene("setup", "Trailer 2 was captured entirely on PS5, released May 6, 2025.", ["F1"], ["PS5"]),
                _scene("point", "One shot mirrors a real 2017 Miami incident on the MacArthur Causeway.", ["F2"]),
                _scene("payoff", "And the soundtrack? Tom Petty's Love Is A Long Road, straight from 1989.", ["F3"]),
                _scene("cta", "Which detail did you miss?"),
            ]}


def _proj(**over):
    base = dict(topic="10 hidden details in the GTA 6 trailers", category="gaming", video_format="short",
                target_duration=20, editing_style="viral", narration_style="energetic", language="en")
    base.update(over)
    return StudioProject(**base)


def test_script_checks_pass_on_a_clean_script():
    s = _good_script()
    w = scriptmod.check(s, _proj(), RESEARCH)
    assert w == [], w
    assert s["hook"] == "GTA 6 trailers hide real Florida chaos." and s["word_count"] > 40
    assert all(sc["seconds"] > 0 for sc in s["scenes"]) and "\n\n" in s["full_narration"]


def test_script_checks_catch_problems():
    s = _good_script()
    s["scenes"][1]["narration"] = "Trailer 2 was captured on PS5, and 90 million people watched it in 24 hours."
    s["scenes"][2]["fact_ids"] = ["F4"]                                        # excluded fact
    s["scenes"].insert(3, _scene("point", "The map is enormous and full of secrets nobody expected."))  # no fact
    s["scenes"][0]["narration"] = "Welcome back to my channel, today we look at GTA 6 trailers in detail together."
    s["scenes"].append(_scene("point", "One shot mirrors a real 2017 Miami incident on the MacArthur Causeway!", ["F2"]))
    kinds = {}
    for w in scriptmod.check(s, _proj(), RESEARCH):
        kinds.setdefault(w["kind"], []).append(w["message"])
    assert any('"90"' in m for m in kinds["number"]) and any('"24"' in m for m in kinds["number"])
    assert not any('"2"' in m for m in kinds["number"])                       # "Trailer 2" is backed by F1
    assert not any('"6"' in m for m in kinds["number"])                       # "GTA 6" comes from the topic
    assert any("F4" in m for m in kinds["facts"]) and any("without citing" in m for m in kinds["facts"])
    assert any("Welcome back" in m for m in kinds["style"])
    assert kinds["repetition"] and kinds["structure"]                          # duplicate line, slow hook


def test_countdown_numbers_are_structure_not_statistics():
    s = _good_script()
    s["scenes"][2]["narration"] = "Number 3: a real 2017 Miami incident, recreated."
    assert not [w for w in scriptmod.check(s, _proj(editing_style="countdown"), RESEARCH) if w["kind"] == "number"]
    assert [w for w in scriptmod.check(s, _proj(editing_style="viral"), RESEARCH) if w["kind"] == "number"]


def test_length_warning_and_pacing():
    s = _good_script()
    w = scriptmod.check(s, _proj(target_duration=120), RESEARCH)
    assert any(x["kind"] == "length" for x in w)
    a, b = _good_script(), _good_script()
    scriptmod.check(a, _proj(narration_style="energetic"), RESEARCH)
    scriptmod.check(b, _proj(narration_style="dramatic"), RESEARCH)
    assert b["estimated_seconds"] > a["estimated_seconds"]                     # slower style = longer read


def test_generate_rewrites_once_when_the_first_draft_has_problems(monkeypatch):
    calls = []
    bad = _good_script()
    bad["scenes"][1]["narration"] = "Trailer 2 hit 90 million views on PS5."

    def fake(model, system, user, schema):
        calls.append(user)
        return bad if len(calls) == 1 else _good_script()

    monkeypatch.setattr(llm, "chat_json_hook", fake)
    script, warnings = scriptmod.generate(_proj(), RESEARCH)
    assert len(calls) == 2 and "Rewrite it and fix these problems" in calls[1] and '"90"' in calls[1]
    assert warnings == [] and [s["id"] for s in script["scenes"]] == ["S1", "S2", "S3", "S4", "S5"]
    calls.clear()
    monkeypatch.setattr(llm, "chat_json_hook", lambda m, s, u, sc: (calls.append(u), _good_script())[1])
    scriptmod.generate(_proj(), RESEARCH)
    assert len(calls) == 1                                                     # clean first draft: no second call


def test_prompt_contains_only_included_facts():
    user = scriptmod.user_prompt(_proj(instructions="no spoilers"), RESEARCH)
    assert "F1:" in user and "F3:" in user and "F4:" not in user              # excluded fact never reaches the writer
    assert "Unconfirmed" in user and "no spoilers" in user
    system = scriptmod.system_prompt(_proj(language="ur", editing_style="countdown"))
    assert "Urdu" in system and "N down to 1" in system and "welcome back" in system


@pytest.fixture()
def script_ready(client, monkeypatch):
    """A project with approved research and the script handler stubbed."""
    monkeypatch.setitem(worker.HANDLERS, "research", lambda pid, report: None)
    monkeypatch.setitem(worker.HANDLERS, "script", scriptmod.run)
    monkeypatch.setattr(llm, "chat_json_hook", lambda m, s, u, sc: _good_script())
    pid = _create(client, target_duration=20)["id"]
    client.post(f"/api/studio/projects/{pid}/stages/research/run", headers=H)
    with db.session_scope() as s:
        s.get(StudioProject, pid).research_json = json.dumps(RESEARCH)
    client.post(f"/api/studio/projects/{pid}/stages/research/approve", headers=H)
    return pid


def test_script_stage_end_to_end_and_editing(client, script_ready):
    pid = script_ready
    p = client.post(f"/api/studio/projects/{pid}/stages/script/run", headers=H).json()
    assert _status(p, "script") == "review"
    url = f"/api/studio/projects/{pid}/script"
    s = client.get(url).json()
    assert s["title"] and s["warnings"] == [] and s["model"] == ss.SCRIPT_MODEL and len(s["scenes"]) == 5
    # edit + reorder + add + delete in one save
    scenes = s["scenes"]
    new = [scenes[0], scenes[2], scenes[1], {"purpose": "point", "narration": "Rockstar loves Florida."}, scenes[4]]
    new[1] = {**new[1], "narration": new[1]["narration"] + " Wild."}
    r = client.patch(url, json={"scenes": new, "title": "Edited title"}, headers=H).json()
    assert [x["id"] for x in r["scenes"]] == ["S1", "S3", "S2", "N1", "S5"] and r["title"] == "Edited title"
    assert any(w["kind"] == "facts" and w["scene"] == 3 for w in r["warnings"])   # new point has no fact: flagged
    assert client.patch(url, json={"scenes": [new[0]]}, headers=H).status_code == 422  # min 2 scenes
    # approve, then edit -> back to review, later stages locked
    client.post(f"/api/studio/projects/{pid}/stages/script/approve", headers=H)
    client.patch(url, json={"title": "Another title"}, headers=H)
    p = client.get(f"/api/studio/projects/{pid}").json()
    assert _status(p, "script") == "review" and _status(p, "storyboard") == "locked"


def test_regenerate_one_scene(client, script_ready, monkeypatch):
    pid = script_ready
    client.post(f"/api/studio/projects/{pid}/stages/script/run", headers=H)
    seen = {}

    def one(model, system, user, schema):
        seen["user"] = user
        return {"scene": _scene("cta", "So which detail fooled you?", [], [])}

    monkeypatch.setattr(llm, "chat_json_hook", one)
    r = client.post(f"/api/studio/projects/{pid}/script/scenes/S5/regenerate", json={"instruction": "end with a question"},
                    headers=H).json()
    assert r["scenes"][4]["narration"] == "So which detail fooled you?" and r["scenes"][4]["id"] == "S5"
    assert r["scenes"][0]["narration"] == _good_script()["scenes"][0]["narration"]   # rest untouched
    assert "Rewrite ONLY scene 5" in seen["user"] and "end with a question" in seen["user"]
    assert client.post(f"/api/studio/projects/{pid}/script/scenes/S99/regenerate", json={}, headers=H).status_code == 404


def test_script_requires_research(client, monkeypatch):
    monkeypatch.setitem(worker.HANDLERS, "script", scriptmod.run)
    pid = _create(client)["id"]
    assert client.get(f"/api/studio/projects/{pid}/script").status_code == 404
    assert client.post(f"/api/studio/projects/{pid}/stages/script/run", headers=H).status_code == 400  # research not approved


# ------------------------------------------------------------------ storyboard
from app.studio import storyboard as sbmod
from app.studio.models import StudioScene


def _shot(vt="stock_video", desc="Aerial of the Miami coastline at golden hour", kw=("miami aerial", "florida coast"),
          share=0.5, gfx=("none", "", ""), camera="slow_push_in"):
    return {"visual_type": vt, "visual_description": desc, "search_keywords": list(kw), "camera_movement": camera,
            "effects": ["color_pop"], "share": share, "graphic": {"kind": gfx[0], "text": gfx[1], "subtext": gfx[2]}}


def _board_scene(sid, shots=None, **over):
    d = {"scene_id": sid, "shots": shots or [_shot(share=0.5), _shot("graphic", "Big text punch", (), 0.5, ("text_punch", "PS5", "Trailer 2"))],
         "on_screen_text": "", "animation": "pop in", "sound_effects": ["whoosh"], "music_mood": "energetic",
         "transition_out": "fast_cut"}
    d.update(over)
    return d


def _raw_board(script):
    return {"overall": {"music_mood": "energetic", "visual_style": "neon Miami digital illustration", "color_grade": "teal/magenta"},
            "scenes": [_board_scene(sc["id"]) for sc in script["scenes"]]}


def _script_with_ids():
    s = _good_script()
    for i, sc in enumerate(s["scenes"], 1):
        sc["id"] = f"S{i}"
        sc["seconds"] = 5.0
    return s


def test_storyboard_validate_copies_narration_and_computes_shots():
    s = _script_with_ids()
    b = sbmod.validate(_raw_board(s), _proj(), s, RESEARCH)
    assert [x["scene_id"] for x in b["scenes"]] == ["S1", "S2", "S3", "S4", "S5"]
    sc = b["scenes"][1]
    assert sc["narration"] == s["scenes"][1]["narration"] and sc["subtitle_text"] == sc["narration"]
    assert sc["caption_style"] == "dynamic" and [x["seconds"] for x in sc["shots"]] == [2.5, 2.5]
    assert b["shot_count"] == 10 and b["total_seconds"] == 25.0 and all(not x["warnings"] for x in b["scenes"])


def test_storyboard_rejects_missing_or_reordered_scenes():
    s = _script_with_ids()
    raw = _raw_board(s)
    raw["scenes"] = raw["scenes"][:-1]
    with pytest.raises(sbmod.StoryboardError, match="missing"):
        sbmod.validate(raw, _proj(), s, RESEARCH)
    raw = _raw_board(s)
    raw["scenes"][0], raw["scenes"][1] = raw["scenes"][1], raw["scenes"][0]
    with pytest.raises(sbmod.StoryboardError):
        sbmod.validate(raw, _proj(), s, RESEARCH)


def test_storyboard_checks():
    s = _script_with_ids()
    raw = _raw_board(s)
    raw["scenes"][0]["shots"] = [_shot(share=1.0)]                                                 # 5 s shot, viral = 1.5-3 s
    raw["scenes"][1]["shots"] = [_shot("graphic", "Card with nothing", (), 0.5, ("none", "", "")), _shot(kw=("x",), share=0.5)]
    raw["scenes"][2]["shots"] = [_shot("ai_image", "Real photo of the trailer set", (), 0.5), _shot(share=0.5)]
    raw["scenes"][3]["shots"] = [_shot("graphic", "Stat callout card", (), 0.5, ("stat_callout", "90 MILLION", "views")), _shot(share=0.5)]
    raw["scenes"][4]["on_screen_text"] = "Over 500 secrets"
    b = sbmod.validate(raw, _proj(), s, RESEARCH)
    w = {x["scene_id"]: " | ".join(x["warnings"]) for x in b["scenes"]}
    assert "changes visuals every" in w["S1"]
    assert "no graphic type/text" in w["S2"] and "2 search keywords" in w["S2"]
    assert "real photo" in w["S3"]
    assert '"90"' in w["S4"] and '"500"' in w["S5"]
    ok = _raw_board(s)
    ok["scenes"][2]["shots"][0] = _shot("ai_image", "ILLUSTRATION of a causeway dancer, stylized, not a real photo", (), 0.5)
    assert not sbmod.validate(ok, _proj(), s, RESEARCH)["scenes"][2]["warnings"]


def test_countdown_needs_ranking_cards():
    s = _script_with_ids()
    s["scenes"][2]["narration"] = "Number 3: a real 2017 Miami incident."
    b = sbmod.validate(_raw_board(s), _proj(editing_style="countdown"), s, RESEARCH)
    assert any("ranking card" in x for x in b["scenes"][2]["warnings"])
    raw = _raw_board(s)
    raw["scenes"][2]["shots"][1]["graphic"] = {"kind": "ranking_card", "text": "3", "subtext": "Miami incident"}
    assert not sbmod.validate(raw, _proj(editing_style="countdown"), s, RESEARCH)["scenes"][2]["warnings"]


def test_storyboard_prompt_reflects_style_and_assets(env):
    sys_viral = sbmod.system_prompt(_proj())
    sys_doc = sbmod.system_prompt(_proj(editing_style="documentary", video_format="long", target_duration=300))
    assert "1.5-3.0 seconds" in sys_viral and "vertical 9:16" in sys_viral
    assert "3.0-6.0 seconds" in sys_doc and "horizontal 16:9" in sys_doc
    assert "has no gameplay footage" in sys_viral                      # empty library in tests
    assert "Never describe an AI image as a real photo" in sys_viral


@pytest.fixture()
def board_ready(client, monkeypatch):
    """Project with approved research + script and the storyboard handler stubbed."""
    s = _script_with_ids()
    monkeypatch.setitem(worker.HANDLERS, "research", lambda pid, report: None)
    monkeypatch.setitem(worker.HANDLERS, "script", lambda pid, report: None)
    monkeypatch.setitem(worker.HANDLERS, "storyboard", sbmod.run)
    monkeypatch.setattr(llm, "chat_json_hook", lambda m, sy, u, sc: _raw_board(s))
    pid = _create(client, target_duration=25)["id"]
    for stg in ("research", "script"):
        client.post(f"/api/studio/projects/{pid}/stages/{stg}/run", headers=H)
        with db.session_scope() as ses:
            pr = ses.get(StudioProject, pid)
            pr.research_json = json.dumps(RESEARCH)
            pr.script_json = json.dumps(s)
        client.post(f"/api/studio/projects/{pid}/stages/{stg}/approve", headers=H)
    return pid


def test_storyboard_stage_end_to_end(client, board_ready):
    pid = board_ready
    p = client.post(f"/api/studio/projects/{pid}/stages/storyboard/run", headers=H).json()
    assert _status(p, "storyboard") == "review"
    b = client.get(f"/api/studio/projects/{pid}/storyboard").json()
    assert len(b["scenes"]) == 5 and b["model"] == ss.STORYBOARD_MODEL and b["overall"]["visual_style"]
    with db.session_scope() as ses:
        rows = ses.query(StudioScene).filter_by(project_id=pid).order_by(StudioScene.position).all()
        assert [json.loads(r.storyboard_json)["scene_id"] for r in rows] == ["S1", "S2", "S3", "S4", "S5"]
        assert rows[0].duration == 5.0 and rows[0].status == "storyboarded"


def test_storyboard_retries_once_then_fails(client, board_ready, monkeypatch):
    pid = board_ready
    calls = []

    def short(m, sy, u, sc):
        calls.append(u)
        return {"overall": _raw_board(_script_with_ids())["overall"], "scenes": [_board_scene("S1")]}

    monkeypatch.setattr(llm, "chat_json_hook", short)
    p = client.post(f"/api/studio/projects/{pid}/stages/storyboard/run", headers=H).json()
    st_ = next(x for x in p["stages"] if x["key"] == "storyboard")
    assert st_["status"] == "failed" and "don't match the script" in st_["error"]
    assert len(calls) == 2 and "was rejected" in calls[1]


def test_storyboard_editing(client, board_ready, monkeypatch):
    pid = board_ready
    client.post(f"/api/studio/projects/{pid}/stages/storyboard/run", headers=H)
    url = f"/api/studio/projects/{pid}/storyboard"
    r = client.patch(f"{url}/scenes/S2", json={"shots": [_shot(camera="static", share=1)], "transition_out": "crossfade",
                                                "narration": "hacked"}, headers=H).json()
    s2 = r["scenes"][1]
    assert s2["shots"][0]["camera_movement"] == "static" and s2["transition_out"] == "crossfade" and s2["edited"]
    assert s2["narration"] == _good_script()["scenes"][1]["narration"]          # narration can't be changed here
    assert r["shot_count"] == 9
    assert client.patch(f"{url}/scenes/S2", json={"transition_out": "teleport"}, headers=H).status_code == 400
    assert client.patch(f"{url}/scenes/S9", json={}, headers=H).status_code == 404
    r = client.patch(url, json={"music_mood": "suspense", "visual_style": "dark comic-book style"}, headers=H).json()
    assert r["overall"]["music_mood"] == "suspense"
    assert client.patch(url, json={"music_mood": "polka"}, headers=H).status_code == 400
    monkeypatch.setattr(llm, "chat_json_hook", lambda m, sy, u, sc: {"scene": _board_scene("WRONG", music_mood="dark")})
    r = client.post(f"{url}/scenes/S3/regenerate", json={"instruction": "darker"}, headers=H).json()
    assert r["scenes"][2]["scene_id"] == "S3" and r["scenes"][2]["music_mood"] == "dark"
    # approved storyboard edited -> back to review
    client.post(f"/api/studio/projects/{pid}/stages/storyboard/approve", headers=H)
    client.patch(f"{url}/scenes/S1", json={"on_screen_text": "NEW"}, headers=H)
    assert _status(client.get(f"/api/studio/projects/{pid}").json(), "storyboard") == "review"


# ================================================================== step 5: voiceover
import subprocess

from app.clipper import ai as clip_ai
from app.studio import narration


def test_voice_comes_before_assets():
    assert ss.STAGES.index("storyboard") < ss.STAGES.index("voice") < ss.STAGES.index("assets")


def test_spoken_text_applies_whole_word_pronunciations():
    d = narration.DEFAULT_PRONUNCIATIONS
    assert narration.spoken_text("GTA VI hits PS5 soon", d) == "G.T.A. six hits P.S. five soon"
    assert narration.spoken_text("AIM for the NPCs", d) == "AIM for the N.P.C.s"   # longest match first, no partials


def test_align_interpolates_words_whisper_heard_differently():
    heard = [{"word": "The", "start": 0.0, "end": 0.2}, {"word": "Leonida", "start": 0.2, "end": 0.6},
             {"word": "keys", "start": 1.0, "end": 1.3}, {"word": "shine.", "start": 1.3, "end": 1.6}]
    words, match = narration.align("The Leonida Keyz shine.", heard, 2.0)
    assert [w["word"] for w in words] == ["The", "Leonida", "Keyz", "shine"]
    assert match == 0.75
    assert words[2]["start"] == 0.6 and words[2]["end"] == 1.3          # filled between its neighbours
    sents = narration.sentences_with_times("The Leonida. Keyz shine.", words)
    assert [(s["text"], s["start"], s["end"]) for s in sents] == [("The Leonida.", 0.0, 0.6), ("Keyz shine.", 0.6, 1.6)]


@pytest.fixture()
def voice_ready(client, board_ready, monkeypatch):
    """Approved storyboard; TTS makes a 1.5s tone and Whisper 'hears' exactly what was spoken."""
    ff = ss.clip_settings.ffmpeg_path()
    spoken = []

    def tts(text, out):
        spoken.append(text)
        subprocess.run([ff, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=300:duration=1.5", str(out)], check=True)

    def transcribe(path):
        toks = spoken[-1].split()
        step = 1.4 / len(toks)
        return {"text": spoken[-1], "words": [{"word": t, "start": i * step, "end": (i + 1) * step} for i, t in enumerate(toks)]}

    monkeypatch.setattr(clip_ai, "tts_hook", tts)
    monkeypatch.setattr(clip_ai, "transcribe_hook", transcribe)
    monkeypatch.setitem(worker.HANDLERS, "voice", narration.run)
    pid = board_ready
    client.post(f"/api/studio/projects/{pid}/stages/storyboard/run", headers=H)
    client.post(f"/api/studio/projects/{pid}/stages/storyboard/approve", headers=H)
    return pid, spoken


def test_voice_stage_end_to_end(client, voice_ready):
    pid, spoken = voice_ready
    url = f"/api/studio/projects/{pid}/voice"
    assert client.get(url).json()["summary"] is None
    p = client.post(f"/api/studio/projects/{pid}/stages/voice/run", headers=H).json()
    assert _status(p, "voice") == "review" and len(spoken) == 5
    v = client.get(url).json()
    s = v["summary"]
    assert [x["scene_id"] for x in s["scenes"]] == ["S1", "S2", "S3", "S4", "S5"]
    assert all(x["match"] == 1.0 and not x["warnings"] and not x["cached"] for x in s["scenes"])
    assert s["scenes"][0]["duration"] == pytest.approx(1.5 / ss.clip_settings.VOICE_TEMPO, abs=0.08)
    assert s["total_seconds"] < 25 and s["warnings"]                     # 5 x ~1.4s is far below the 25s target
    with db.session_scope() as ses:
        rows = ses.query(StudioScene).filter_by(project_id=pid).order_by(StudioScene.position).all()
        assert all(r.status == "voiced" and r.audio_path and json.loads(r.timing_json)["words"] for r in rows)
    a = client.get(f"{url}/scenes/S1/audio")
    assert a.status_code == 200 and a.headers["content-type"] == "audio/wav" and len(a.content) > 1000
    assert client.get(f"{url}/scenes/S9/audio").status_code == 404
    with db.session_scope() as ses:
        from app.studio.models import CostEvent
        ev = ses.query(CostEvent).filter_by(project_id=pid, stage="voice").all()
        assert sorted(e.operation for e in ev) == ["transcribe"] * 5 + ["tts"] * 5
    # running again reuses every take for free
    client.post(f"/api/studio/projects/{pid}/stages/voice/run", headers=H)
    assert len(spoken) == 5 and all(x["cached"] for x in client.get(url).json()["summary"]["scenes"])


def test_voice_settings_and_single_scene_retake(client, voice_ready):
    pid, spoken = voice_ready
    url = f"/api/studio/projects/{pid}/voice"
    assert client.post(f"{url}/scenes/S2/regenerate", headers=H).status_code == 400       # not run yet
    client.post(f"/api/studio/projects/{pid}/stages/voice/run", headers=H)
    assert client.patch(f"{url}/settings", json={"speed": 2}, headers=H).status_code == 422
    # a pronunciation only re-records scenes whose spoken text changes
    prons = {**client.get(url).json()["pronunciations"], "Miami": "My-am-ee"}
    r = client.patch(f"{url}/settings", json={"pronunciations": prons}, headers=H)
    assert r.status_code == 200 and r.json()["pronunciations"]["Miami"] == "My-am-ee"
    n = len(spoken)
    client.post(f"/api/studio/projects/{pid}/stages/voice/run", headers=H)
    changed = len(spoken) - n
    assert changed == 1 and "My-am-ee" in spoken[-1]
    # speed changes every take
    client.patch(f"{url}/settings", json={"speed": 1.1}, headers=H)
    n = len(spoken)
    client.post(f"/api/studio/projects/{pid}/stages/voice/run", headers=H)
    s = client.get(url).json()["summary"]
    assert len(spoken) - n == 5 and s["speed"] == 1.1
    assert s["scenes"][0]["duration"] == pytest.approx(1.5 / (ss.clip_settings.VOICE_TEMPO * 1.1), abs=0.08)
    # one retake = one new recording, summary updated, approval re-opened
    client.post(f"/api/studio/projects/{pid}/stages/voice/approve", headers=H)
    n = len(spoken)
    r = client.post(f"{url}/scenes/S2/regenerate", headers=H)
    assert r.status_code == 200 and len(spoken) == n + 1
    assert [x["scene_id"] for x in r.json()["scenes"]] == ["S1", "S2", "S3", "S4", "S5"]
    assert not r.json()["scenes"][1]["cached"]
    assert _status(client.get(f"/api/studio/projects/{pid}").json(), "voice") == "review"
    assert client.post(f"{url}/scenes/S9/regenerate", headers=H).status_code == 404


def test_respelled_words_do_not_lower_the_match():
    heard = [{"word": w, "start": i * 0.3, "end": i * 0.3 + 0.3} for i, w in enumerate("G.T.A. six is here".split())]
    _, plain = narration.align("GTA 6 is here", heard, 1.5)
    words, match = narration.align("GTA 6 is here", heard, 1.5, {"gta", "6"})
    assert plain == 0.75 and match == 1.0 and [w["word"] for w in words] == ["GTA", "6", "is", "here"]


# ================================================================== step 6: assets
from app.studio import assets as amod
from pathlib import Path

from app.studio.models import StudioAsset


def _ff(*args):
    subprocess.run([ss.clip_settings.ffmpeg_path(), "-y", "-loglevel", "error", *args], check=True)


@pytest.fixture()
def assets_ready(client, voice_ready, monkeypatch):
    """Voice approved; storyboard rewritten with one shot of every type; Pexels / downloads / images stubbed."""
    pid, _ = voice_ready
    client.post(f"/api/studio/projects/{pid}/stages/voice/run", headers=H)
    client.post(f"/api/studio/projects/{pid}/stages/voice/approve", headers=H)
    with db.session_scope() as ses:
        pr = ses.get(StudioProject, pid)
        b = json.loads(pr.storyboard_json)
        b["scenes"][0]["shots"] = [dict(_shot("stock_video"), seconds=2.0), dict(_shot("stock_image", kw=("vice city skyline",)), seconds=2.0)]
        b["scenes"][1]["shots"] = [dict(_shot("ai_image", "Neon boulevard shaped like a map, illustration"), seconds=2.5),
                                   dict(_shot("graphic", "Big number", (), 0.5, ("number_reveal", "2025", "release")), seconds=2.5)]
        b["scenes"][2]["shots"] = [dict(_shot("gameplay", "Car chase on a highway", ("car chase",)), seconds=5.0)]
        pr.storyboard_json = json.dumps(b)
    calls = {"search": [], "download": [], "image": []}
    n = {"i": 0}

    def search(kind, query, orient):
        calls["search"].append((kind, query, orient))
        n["i"] += 1
        return [{"id": f"{kind[0]}{n['i']}{k}", "kind": kind, "url": f"https://x/{kind}{k}", "page": f"https://pexels.com/{k}",
                 "author": "Ana", "author_url": "https://pexels.com/@ana", "width": 1080, "height": 1920,
                 "duration": 1.0 if k == 0 else 8.0} for k in range(3)]

    def download(url, dest):
        calls["download"].append(url)
        if dest.suffix == ".mp4":
            _ff("-f", "lavfi", "-i", "testsrc=size=270x480:rate=25:duration=3", "-pix_fmt", "yuv420p", str(dest))
        else:
            _ff("-f", "lavfi", "-i", "color=c=blue:size=270x480", "-frames:v", "1", str(dest))

    def image(prompt, size):
        calls["image"].append((prompt, size))
        out = settings.data_dir / "tmp_ai.png"
        _ff("-f", "lavfi", "-i", "color=c=red:size=256x384", "-frames:v", "1", str(out))
        return out.read_bytes()

    monkeypatch.setattr(amod, "pexels_hook", search)
    monkeypatch.setattr(amod, "download_hook", download)
    monkeypatch.setattr(amod, "image_hook", image)
    monkeypatch.setattr(amod, "library_videos", lambda: [])
    monkeypatch.setitem(worker.HANDLERS, "assets", amod.run)
    return pid, calls


def _assets(client, pid):
    return client.get(f"/api/studio/projects/{pid}/assets").json()


def _shot_state(o, sid, i):
    return next(s for s in o["scenes"] if s["scene_id"] == sid)["shots"][i]


def test_assets_stage_end_to_end(client, assets_ready):
    pid, calls = assets_ready
    o = _assets(client, pid)
    assert o["plan"] == {"stock": 4, "ai_image": 1, "library": 1, "graphic": 3} and o["counts"]["pending"] == 9
    p = client.post(f"/api/studio/projects/{pid}/stages/assets/run", headers=H).json()
    assert _status(p, "assets") == "review"
    o = _assets(client, pid)
    v = _shot_state(o, "S1", 0)["asset"]
    # the 1s clip is too short for a 2s shot, so the 8s one is picked; credited to its author
    assert v["origin"] == "pexels" and v["kind"] == "video" and v["meta"]["duration"] == 8.0 and v["has_preview"]
    assert v["attribution"] == "Video by Ana on Pexels" and calls["search"][0] == ("video", "miami aerial", "portrait")
    assert _shot_state(o, "S1", 1)["asset"]["kind"] == "image"
    ai = _shot_state(o, "S2", 0)["asset"]
    assert ai["origin"] == "ai" and "not a photograph" in ai["prompt"] and "neon Miami" in ai["prompt"]
    assert calls["image"][0][1] == "1024x1536"
    assert _shot_state(o, "S2", 1)["asset"]["origin"] == "graphic" and not _shot_state(o, "S2", 1)["asset"]["has_file"]
    g = _shot_state(o, "S3", 0)["asset"]                                   # gameplay, empty library -> stock + a note
    assert g["origin"] == "pexels" and "No footage of your own" in g["note"]
    ids = [a["meta"]["pexels_id"] for sc in o["scenes"] for s in sc["shots"] if (a := s["asset"]) and a["origin"] == "pexels"]
    assert len(ids) == len(set(ids))                                       # never the same clip twice
    assert o["counts"] == {"ready": 9, "missing": 0, "failed": 0, "pending": 0}
    assert o["credits"] and o["credits"][0].startswith("Video by Ana on Pexels: https://pexels.com/")
    # files are served, previews too; costs recorded
    assert client.get(f"/api/studio/projects/{pid}/assets/{v['id']}/file").status_code == 200
    assert client.get(f"/api/studio/projects/{pid}/assets/{v['id']}/file?preview=1").status_code == 200
    with db.session_scope() as ses:
        from app.studio.models import CostEvent
        assert [e.operation for e in ses.query(CostEvent).filter_by(project_id=pid, stage="assets")] == ["image"]
    # re-run: nothing changed -> nothing fetched again
    n_dl, n_img = len(calls["download"]), len(calls["image"])
    client.post(f"/api/studio/projects/{pid}/stages/assets/run", headers=H)
    assert (len(calls["download"]), len(calls["image"])) == (n_dl, n_img)
    assert client.post(f"/api/studio/projects/{pid}/stages/assets/approve", headers=H).status_code == 200


def test_assets_without_pexels_key_are_missing_not_faked(client, assets_ready, monkeypatch):
    pid, calls = assets_ready
    monkeypatch.setattr(amod, "pexels_hook", None)
    monkeypatch.delenv(ss.PEXELS_API_KEY_ENV, raising=False)
    client.post(f"/api/studio/projects/{pid}/stages/assets/run", headers=H)
    o = _assets(client, pid)
    s = _shot_state(o, "S1", 0)
    assert s["state"] == "missing" and "PEXELS_API_KEY" in s["asset"]["note"]
    assert o["counts"]["missing"] == 5 and len(calls["image"]) == 1         # only the planned AI image was generated
    r = client.post(f"/api/studio/projects/{pid}/stages/assets/approve", headers=H)
    assert r.status_code == 400 and "no visual" in r.json()["detail"]
    # fill the gaps with AI illustrations (as a stage job), clearly labelled
    p = client.post(f"/api/studio/projects/{pid}/assets/fill-missing", headers=H).json()
    assert _status(p, "assets") == "review"
    o = _assets(client, pid)
    assert o["counts"]["missing"] == 0 and len(calls["image"]) == 1 + 5
    s = _shot_state(o, "S1", 0)["asset"]
    assert s["origin"] == "ai" and "instead of stock video" in s["note"]
    assert client.post(f"/api/studio/projects/{pid}/stages/assets/approve", headers=H).status_code == 200


def test_replace_next_ai_and_upload(client, assets_ready):
    pid, calls = assets_ready
    url = f"/api/studio/projects/{pid}/assets/shots"
    assert client.post(f"{url}/S1/0/replace", json={"action": "next"}, headers=H).status_code == 400   # not run yet
    client.post(f"/api/studio/projects/{pid}/stages/assets/run", headers=H)
    client.post(f"/api/studio/projects/{pid}/stages/assets/approve", headers=H)
    first = _shot_state(_assets(client, pid), "S1", 0)["asset"]
    o = client.post(f"{url}/S1/0/replace", json={"action": "next"}, headers=H).json()
    second = _shot_state(o, "S1", 0)["asset"]
    assert second["meta"]["pexels_id"] != first["meta"]["pexels_id"] and first["meta"]["pexels_id"] in second["meta"]["tried"]
    assert _status(client.get(f"/api/studio/projects/{pid}").json(), "assets") == "review"    # approval re-opened
    o = client.post(f"{url}/S1/0/replace", json={"action": "ai_image"}, headers=H).json()
    assert _shot_state(o, "S1", 0)["asset"]["origin"] == "ai"
    # upload: kept on re-run even though it differs from the plan
    png = settings.data_dir / "mine.png"
    _ff("-f", "lavfi", "-i", "color=c=green:size=200x300", "-frames:v", "1", str(png))
    with open(png, "rb") as f:
        r = client.post(f"{url}/S1/1/upload", files={"file": ("mine.png", f, "image/png")}, headers=H)
    assert r.status_code == 200 and _shot_state(r.json(), "S1", 1)["asset"]["origin"] == "upload"
    bad = client.post(f"{url}/S1/1/upload", files={"file": ("notes.txt", b"hello", "text/plain")}, headers=H)
    assert bad.status_code == 400
    client.post(f"/api/studio/projects/{pid}/stages/assets/run", headers=H)
    assert _shot_state(_assets(client, pid), "S1", 1)["asset"]["origin"] == "upload"
    assert client.post(f"{url}/S9/0/replace", json={"action": "ai_image"}, headers=H).status_code == 400
    # only one asset row per shot, replaced files are deleted
    with db.session_scope() as ses:
        rows = ses.query(StudioAsset).filter_by(project_id=pid, scene_id="S1", shot_index=0).all()
        assert len(rows) == 1
    files = list((ss.project_dir(pid) / "assets").glob("S1_0_*"))
    assert len([f for f in files if not f.name.endswith("_thumb.jpg")]) == 1


def test_changed_shot_is_refetched_and_library_footage_is_used(client, assets_ready, monkeypatch, tmp_path):
    pid, calls = assets_ready
    clip = tmp_path / "my_gta_car_chase.mp4"
    _ff("-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=30", "-pix_fmt", "yuv420p", str(clip))
    monkeypatch.setattr(amod, "library_videos", lambda: [{"path": str(clip), "text": "gta car chase highway"}])
    client.post(f"/api/studio/projects/{pid}/stages/assets/run", headers=H)
    g = _shot_state(_assets(client, pid), "S3", 0)["asset"]
    assert g["origin"] == "library" and 3 <= g["meta"]["start"] <= 22 and "Auto-picked" in g["note"]
    # the library file itself is served but never deleted when replaced
    assert client.get(f"/api/studio/projects/{pid}/assets/{g['id']}/file").status_code == 200
    o = client.post(f"/api/studio/projects/{pid}/assets/shots/S3/0/replace", json={"action": "next"}, headers=H).json()
    g2 = _shot_state(o, "S3", 0)["asset"]
    assert g2["origin"] == "library" and abs(g2["meta"]["start"] - g["meta"]["start"]) >= 5 and clip.exists()
    # editing a shot's description marks it stale; re-run refetches only that shot
    with db.session_scope() as ses:
        pr = ses.get(StudioProject, pid)
        b = json.loads(pr.storyboard_json)
        b["scenes"][0]["shots"][1]["visual_description"] = "Night skyline of Miami with neon reflections"
        pr.storyboard_json = json.dumps(b)
    o = _assets(client, pid)
    assert _shot_state(o, "S1", 1)["stale"] and o["counts"]["pending"] == 1
    assert client.post(f"/api/studio/projects/{pid}/stages/assets/approve", headers=H).status_code == 400
    n = len(calls["download"])
    client.post(f"/api/studio/projects/{pid}/stages/assets/run", headers=H)
    assert len(calls["download"]) == n + 1 and _assets(client, pid)["counts"]["pending"] == 0


def test_ai_images_never_portray_real_people_and_flag_text():
    board = {"overall": {"visual_style": "neon", "color_grade": "teal"}}
    prompt = amod.image_prompt({"visual_description": "Neon street at night"}, board)
    assert "not a photograph" in prompt and "never portray or resemble a real" in prompt
    assert sbmod.LIKENESS.search("a tattoo-faced mugshot-style character, a fictionalized nod to the Florida Joker")
    assert not sbmod.LIKENESS.search("neon boulevard with palm trees and traffic")
    assert amod.image_notes({"visual_description": "a roadside sign reading Kelly County"})
    assert amod.image_notes({"visual_description": "crowd faces in a neon street"}) is None
    s = _script_with_ids()
    raw = _raw_board(s)
    raw["scenes"][0]["shots"] = [_shot("ai_image", "ILLUSTRATION of a man who resembles the real suspect", share=1.0)]
    b = sbmod.validate(raw, _proj(), s, RESEARCH)
    assert any("real person" in w for w in b["scenes"][0]["warnings"])
