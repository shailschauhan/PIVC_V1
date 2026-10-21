"""Audit log, rules and versions, engine health, and report export."""
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from app.config import DEFAULT_RULES, settings
from app.decision import decide
from app.main import app
from app.db import T, open_db
from tests.test_api import (PASSWORD, as_user, client, detection, facedet, raw_client, run, sql_rows, sql_set, vid, wait_done)  # noqa: F401 - fixtures


def seed_measured(ref, fake, *, final=None, ago_h=1.0, filename=None, face=None):
    """A finished case with real stored measurements, so a rule change can be replayed against it."""
    run(detection(fake), face=face)
    params = run.params
    for v in params.values():
        v["engine"] = "x"
    decision, conf, exc = decide(params, DEFAULT_RULES)
    created = datetime.now(timezone.utc) - timedelta(hours=ago_h)
    with open_db() as db:
        c = db.insert("cases", case_ref=ref, proposal_no="-", customer_id="-", customer_name=f"{ref}.mp4", source="upload", declared={},
                      media={"original_name": filename or f"{ref}.mp4"}, created_at=created, status="ANALYZED", decision=decision, confidence=conf,
                      analyzed_at=created, processing_ms=1000,
                      review_status="COMPLETED" if final else ("PENDING" if decision == "Needs Review" else "NOT_REQUIRED"),
                      final_decision=final or (None if decision == "Needs Review" else decision), reviewer="alice" if final else None,
                      report={"parameters": params, "exception_details": exc, "labels": {}, "rules_applied": DEFAULT_RULES, "overall_decision": decision})
        for k, v in params.items():
            db.insert("case_params", case_id=c.id, parameter=k, status=v["status"], confidence=v["confidence"], score=None, engine="x")
        db.commit()
        return c.id


def audit(client, **q):
    return client.get("/api/audit?" + urlencode(q, doseq=True)).json()


def test_audit_log_records_who_did_what_and_survives_deletion(client):
    client.post("/api/cases/upload", files=[vid("real.mp4"), vid("bad.mp4", b"BAD")])
    wait_done(client, 2)
    bad = next(c for c in client.get("/api/cases").json()["items"] if c["filename"] == "bad.mp4")
    client.post(f"/api/cases/{bad['id']}/review", json={"decision": "Accept", "notes": "looked fine"})
    client.post("/api/cases/bulk", json={"action": "delete", "ids": [bad["id"]]})
    acts = {(e["action"], e["actor"]) for e in audit(client, page_size=200)["items"]}
    assert ("uploaded", "admin") in acts and ("reviewed", "admin") in acts and ("deleted", "admin") in acts
    assert ("analyzed", "system") in acts
    deleted = audit(client, action="deleted")["items"][0]
    assert deleted["target"] == bad["case_ref"] and "bad.mp4" in deleted["summary"]          # the video is gone, its record is not
    assert audit(client, q="looked fine")["total"] == 1                                       # searches the details too
    assert audit(client, category="review")["total"] == 1 and audit(client, actor="nobody")["total"] == 0
    assert audit(client, date_from="2999-01-01T00:00:00Z")["total"] == 0
    assert client.get("/api/audit?date_from=garbage").status_code == 422


def test_audit_log_covers_sign_in_users_and_settings_without_leaking_secrets(client):
    other = TestClient(app)
    other.post("/api/auth/login", json={"username": "admin", "password": "wrong-password-1"})
    other.post("/api/auth/login", json={"username": "ghost", "password": "wrong-password-1"})
    assert other.post("/api/auth/login", json={"username": "admin", "password": PASSWORD}).status_code == 200
    as_user(client, "rita", "reviewer")
    uid = next(u["id"] for u in client.get("/api/users").json() if u["username"] == "rita")
    client.put(f"/api/users/{uid}", json={"role": "viewer"})
    c = client.post("/api/settings/connections", json={"type": "azure_api", "name": "My API", "fields": {"endpoint": "https://x.openai.azure.com"},
                                                       "secrets": {"api_key": "SUPER-SECRET-KEY-123"}})
    assert c.status_code == 200, c.text
    client.put(f"/api/settings/connections/{c.json()['id']}", json={"secrets": {"api_key": "ANOTHER-SECRET-456"}})
    log = audit(client, page_size=200)["items"]
    actions = {e["action"] for e in log}
    assert {"sign-in failed", "signed in", "user created", "user changed", "connection created", "connection updated"} <= actions
    assert any(e["target"] == "ghost" for e in log if e["action"] == "sign-in failed")
    blob = str(log)
    assert "SUPER-SECRET" not in blob and "ANOTHER-SECRET" not in blob and "wrong-password" not in blob and PASSWORD not in blob
    upd = next(e for e in log if e["action"] == "connection updated")
    assert "api_key (secret)" in upd["detail"]["changed"]


def test_audit_log_access_and_export(client):
    rev, view = as_user(client, "rev", "reviewer"), as_user(client, "vee", "viewer")
    assert rev.get("/api/audit").status_code == 200 and rev.get("/api/audit/facets").status_code == 200
    assert view.get("/api/audit").status_code == 403 and view.get("/api/audit/export.csv").status_code == 403
    csv_text = rev.get("/api/audit/export.csv?category=users").text
    assert csv_text.startswith("﻿Time (UTC)") and "Created user rev" in csv_text
    assert audit(client, action="audit exported")["total"] == 1                                # exporting the log is itself logged
    assert "users" in {c["key"] for c in client.get("/api/audit/facets").json()["categories"]}


def test_rules_are_validated_versioned_and_restorable(client):
    base = client.get("/api/rules").json()
    assert base["version"] == 0 and base["rules"]["fake_score_flag"] == DEFAULT_RULES["fake_score_flag"]
    bad = lambda ch: client.put("/api/rules", json={"changes": ch}).status_code  # noqa: E731
    assert bad({"bogus": 1}) == 422 and bad({"fake_score_flag": "high"}) == 422 and bad({"fake_score_flag": 1.5}) == 422
    assert bad({"fake_score_review": 0.5}) == 422                  # the review band would sit above the flag line
    assert bad({"hr_normal_low": 120}) == 422 and bad({"face_match_threshold": -0.1}) == 422
    assert bad({"fake_score_flag": DEFAULT_RULES["fake_score_flag"]}) == 409   # no change
    r = client.put("/api/rules", json={"changes": {"fake_score_flag": 0.4}, "note": "fewer false flags"}).json()
    assert r["version"] == 1 and r["rules"]["fake_score_flag"] == 0.4 and r["updated_by"] == "admin" and r["note"] == "fewer false flags"
    client.put("/api/rules", json={"changes": {"confidence_threshold": 0.8, "age_tolerance_years": 9}})
    h = client.get("/api/rules/history").json()["versions"]
    assert [v["version"] for v in h] == [2, 1] and h[0]["current"] and not h[1]["current"]
    assert {c["key"] for c in h[0]["changes"]} == {"confidence_threshold", "age_tolerance_years"}
    assert h[1]["changes"] == [{"key": "fake_score_flag", "from": DEFAULT_RULES["fake_score_flag"], "to": 0.4}]
    r = client.post("/api/rules/1/restore").json()                  # back to the state after version 1
    assert r["version"] == 3 and r["rules"]["confidence_threshold"] == DEFAULT_RULES["confidence_threshold"] and r["rules"]["fake_score_flag"] == 0.4
    assert client.post("/api/rules/99/restore").status_code == 404
    assert client.get("/api/config").json()["fake_score_flag"] == 0.4                    # the analysis reads the same rules
    ev = audit(client, category="rules")["items"]
    assert [e["action"] for e in ev] == ["rules restored", "rules changed", "rules changed"] and "fewer false flags" in str(ev[-1]["detail"])


def test_rules_are_admin_only(client):
    rev = as_user(client, "rev", "reviewer")
    for call in (lambda: rev.get("/api/rules"), lambda: rev.put("/api/rules", json={"changes": {"fake_score_flag": 0.4}}),
                 lambda: rev.post("/api/rules/preview", json={}), lambda: rev.get("/api/rules/history"),
                 lambda: rev.put("/api/config", json={"confidence_threshold": 0.9})):
        assert call().status_code == 403


def test_preview_counts_the_videos_a_change_would_flip_and_changes_nothing(client):
    first = seed_measured("A", 0.02, final="Accept")      # clean pass
    seed_measured("B", 0.20, final="Accept")      # grey zone: a reviewer accepted it
    seed_measured("C", 0.25, final="Reject")      # grey zone: a reviewer rejected it
    seed_measured("D", 0.90)                      # clear fake
    seed_measured("E", 0.20, ago_h=24 * 40)       # outside a 30 day window
    p = client.post("/api/rules/preview?days=30", json={"fake_score_review": 0.30, "fake_score_flag": 0.40}).json()
    assert p["examined"] == 4 and p["in_window"] == 4 and p["skipped"] == 0 and not p["sampled"]
    assert p["decisions"]["current"] == {"Accept": 1, "Needs Review": 2, "Reject": 1}
    assert p["decisions"]["proposed"] == {"Accept": 3, "Needs Review": 0, "Reject": 1}          # B and C now pass on their own
    assert p["flips"]["total"] == 2 and p["flips"]["matrix"] == [{"from": "Needs Review", "to": "Accept", "count": 2}]
    assert {e["case_ref"] for e in p["examples"]} == {"B", "C"}
    assert p["reviewers"]["reviewed"] == 3
    assert p["reviewers"]["current"] == {"agree": 1, "disagree": 0, "to_person": 2}
    assert p["reviewers"]["proposed"] == {"agree": 2, "disagree": 1, "to_person": 0}      # C would now be auto-accepted although a reviewer rejected it
    assert [t["key"] for t in p["tests"]] == ["video_authenticity"]
    assert p["replay_matches_stored"] == 1.0
    assert [c["key"] for c in p["changes"]] == ["fake_score_review", "fake_score_flag"]
    assert client.get("/api/rules").json()["version"] == 0                                    # a preview saves nothing
    assert client.get(f"/api/cases/{first}").json()["decision"] == "Accept"
    assert client.post("/api/rules/preview", json={"fake_score_review": 0.9}).status_code == 422
    same = client.post("/api/rules/preview", json={}).json()
    assert same["flips"]["total"] == 0 and same["decisions"]["current"] == same["decisions"]["proposed"]
    wider = client.post("/api/rules/preview?days=0", json={"fake_score_review": 0.3, "fake_score_flag": 0.4}).json()
    assert wider["examined"] == 5


def test_preview_replays_the_other_tests_too(client):
    seed_measured("F", 0.02, face=facedet("mismatch", 0.1))
    p = client.post("/api/rules/preview", json={"face_mismatch_requires_review": False}).json()
    assert p["decisions"]["current"]["Needs Review"] == 1 and p["decisions"]["proposed"]["Accept"] == 1 and p["flips"]["total"] == 1


def test_engine_calls_are_timed_and_summarised(client):
    from app import engines
    with engines.track("liveness"):
        pass
    with pytest.raises(RuntimeError):
        with engines.track("liveness"):
            raise RuntimeError("service exploded")
    with open_db() as db:
        s = engines.stats(db, "liveness")
        assert s["calls"] == 2 and s["errors"] == 1 and s["error_rate"] == 0.5 and s["p50_ms"] is not None
        assert "service exploded" in s["last_error"]["message"] and s["recent_errors"][0]["message"].startswith("RuntimeError")
        assert len(s["series"]) == 24 and sum(b["calls"] for b in s["series"]) == 2
        assert engines.stats(db, "face_match")["error_rate"] is None


def test_analysing_a_video_records_each_engine_run(client):
    client.post("/api/cases/upload", files=[vid("real.mp4")])
    wait_done(client, 1)
    e = {x["key"]: x for x in client.get("/api/engines").json()["engines"]}
    assert set(e) == {"video_authenticity", "health_indicators", "age_estimation", "face_match", "liveness"}
    assert e["video_authenticity"]["stats"]["calls"] == 1 and e["video_authenticity"]["stats"]["errors"] == 0
    assert e["video_authenticity"]["ready"] is True and e["liveness"]["kind"].startswith("Separate service")
    one = client.get("/api/engines/liveness?hours=48").json()
    assert one["key"] == "liveness" and one["stats"]["hours"] == 48 and client.get("/api/engines/nope").status_code == 404
    assert client.post("/api/engines/check").status_code == 200


def test_engine_health_is_admin_only(client):
    rev = as_user(client, "rev", "reviewer")
    assert rev.get("/api/engines").status_code == 403 and rev.post("/api/engines/check").status_code == 403


def test_report_filters_and_csv_columns(client):
    seed_measured("A", 0.02, final="Accept", ago_h=2)
    seed_measured("B", 0.20, ago_h=30)
    seed_measured("C", 0.90, ago_h=50, filename='=HYPERLINK("http://evil")')
    opts = client.get("/api/reports/options").json()
    assert opts["decisions"] == ["Accept", "Needs Review", "Reject"] and "video_authenticity" in {t["key"] for t in opts["tests"]}
    pv = lambda **q: client.get("/api/reports/preview", params=q).json()  # noqa: E731
    assert pv()["total"] == 3
    assert pv(decision=["Reject"])["total"] == 1 and pv(decision=["Accept", "Reject"])["total"] == 2
    assert pv(test=["video_authenticity:Review"])["total"] == 1
    assert pv(test=["video_authenticity:Pass", "video_authenticity:Review"])["total"] == 2         # any of one test's results
    assert pv(test=["video_authenticity:Pass", "liveness:Pass"])["total"] == 1                       # but every chosen test must match
    assert pv(review_status=["PENDING"])["total"] == 1
    since = (datetime.now(timezone.utc) - timedelta(hours=40)).isoformat()
    assert pv(date_from=since)["total"] == 2 and pv(date_to=since)["total"] == 1
    s = pv(decision=["Reject"])
    assert s["decisions"]["Reject"] == 1 and s["filters"] == ["Automatic decision: Reject"]
    assert next(t for t in s["tests"] if t["key"] == "video_authenticity")["results"][2] == {"status": "Suspected Manipulation", "tone": "bad", "count": 1}
    for bad in ({"decision": ["Maybe"]}, {"test": ["liveness:Nope"]}, {"test": ["x"]}, {"review_status": ["??"]}, {"date_from": "yesterday"}):
        assert client.get("/api/reports/preview", params=bad).status_code == 422
    assert client.get("/api/reports/preview", params={"date_from": "2030-01-02T00:00:00Z", "date_to": "2030-01-01T00:00:00Z"}).status_code == 422

    r = client.get("/api/reports/export", params={"format": "csv", "columns": ["case_ref", "decision", "fake_score", "bogus"], "decision": ["Reject", "Accept"]})
    lines = r.text.lstrip("﻿").splitlines()
    assert r.headers["content-type"].startswith("text/csv") and lines[0] == "Video ID,Automatic decision,Fake score" and len(lines) == 3
    assert any(line.startswith("C,Reject,0.9") for line in lines)
    assert "'=HYPERLINK" in client.get("/api/reports/export", params={"columns": ["filename"]}).text                  # no formula injection
    assert client.get("/api/reports/export", params={"format": "xls"}).status_code == 422
    assert client.get("/api/reports/export").text.splitlines()[0].count(",") == len(opts["default_columns"]) - 1


def test_report_pdf_and_it_is_logged(client):
    seed_measured("A", 0.02, final="Accept")
    seed_measured("B", 0.90)
    r = client.get("/api/reports/export", params={"format": "pdf", "decision": ["Accept", "Reject"]})
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf" and r.content[:5] == b"%PDF-" and len(r.content) > 2000
    empty = client.get("/api/reports/export", params={"format": "pdf", "date_from": "2999-01-01T00:00:00Z"})
    assert empty.content[:5] == b"%PDF-"
    ev = audit(client, action="report exported")["items"]
    assert len(ev) == 2 and ev[1]["detail"]["rows"] == 2 and ev[1]["detail"]["format"] == "pdf" and ev[1]["actor"] == "admin"
    assert ev[1]["detail"]["filters"] == ["Automatic decision: Accept, Reject"]


def test_reports_need_the_reviewer_role(client):
    view = as_user(client, "vee", "viewer")
    rev = as_user(client, "rev", "reviewer")
    assert view.get("/api/reports/options").status_code == 403 and view.get("/api/reports/export").status_code == 403
    assert rev.get("/api/reports/options").status_code == 200 and rev.get("/api/reports/export?format=pdf").status_code == 200


def test_old_columns_are_added_to_an_existing_database(client):
    from app import schema
    with open_db() as db:                                         # a table as an older version made it: two columns missing
        db.run(f"DROP TABLE {T.rule_config}")
        db.run(f"CREATE TABLE {T.rule_config} (id INT IDENTITY(1,1) PRIMARY KEY, rules NVARCHAR(MAX) NOT NULL, updated_at DATETIME2(6) NOT NULL)")
        db.run(f"INSERT INTO {T.rule_config} (rules, updated_at) VALUES ('{{}}', SYSUTCDATETIME())")
        db.commit()
        assert set(schema.ensure(db)) == {"rule_config.updated_by", "rule_config.note"}
        assert schema.ensure(db) == []                            # nothing left to add the second time
    assert client.get("/api/rules").json()["version"] == 1        # the old row is still there and now readable


def test_history_from_before_the_audit_log_is_carried_over(client):
    from app import activity
    cid = seed_measured("OLD", 0.02)
    with open_db() as db:
        db.insert("audit_log", case_id=cid, actor="legacy-user", action="uploaded", detail={"filename": "OLD.mp4"})
        db.delete("activity_log")
        db.commit()
        assert activity.backfill(db) == 1 and activity.backfill(db) == 0
    (a,) = sql_rows("activity_log")
    assert (a.actor, a.action, a.target, a.category) == ("legacy-user", "uploaded", "OLD", "video")


# ---------------------------------------------------------------- re-running single tests
def wait_idle():
    from app import jobs
    assert jobs.wait_idle(20)


def test_one_test_can_be_re_run_without_touching_the_others(client, monkeypatch):
    from app import services_client as sc
    from app.analyzers import providers
    client.post("/api/cases/upload", files=[vid("real.mp4")])
    wait_done(client, 1)
    cid = client.get("/api/cases").json()["items"][0]["id"]
    before = client.get(f"/api/cases/{cid}").json()
    assert before["decision"] == "Accept" and before["report"]["parameters"]["liveness"]["status"] == "Pass"

    calls = []
    real_get = providers.get_detector
    monkeypatch.setattr(providers, "get_detector", lambda: calls.append("detector") or real_get())
    monkeypatch.setattr(sc, "check_liveness", lambda video, rules: livedet_spoof())
    assert client.post(f"/api/cases/{cid}/analyze", json={"tests": ["liveness"]}).status_code == 200
    wait_idle()

    after = client.get(f"/api/cases/{cid}").json()
    p, q = before["report"]["parameters"], after["report"]["parameters"]
    assert q["liveness"]["status"] == "Review" and p["liveness"]["status"] == "Pass"
    assert calls == []                                                       # the other engines were not run
    for k in ("video_authenticity", "health_indicators", "age_estimation", "face_match"):
        assert q[k] == p[k]                                                  # byte-for-byte the stored results
    assert after["decision"] == "Needs Review" and after["review_status"] == "PENDING"      # the decision follows the merged results
    assert after["status"] == "ANALYZED" and after["processing_ms"] == before["processing_ms"]
    ev = [e for e in audit(client, page_size=50)["items"] if e["action"] in ("re-run requested", "re-analyzed")]
    assert {e["action"] for e in ev} == {"re-run requested", "re-analyzed"}
    done = next(e for e in ev if e["action"] == "re-analyzed")
    assert done["detail"]["before"] == {"liveness": "Pass"} and done["detail"]["after"] == {"liveness": "Review"} and "Needs Review" in done["summary"]
    # the dashboard rows moved with it
    d = client.get("/api/dashboard?days=7").json()
    lv = {o["status"]: o["count"] for o in next(t for t in d["tests"] if t["key"] == "liveness")["outcomes"]}
    assert lv["Review"] == 1 and lv["Pass"] == 0
    assert audit(client, action="re-analyzed")["total"] == 1


def livedet_spoof():
    from tests.test_api import livedet
    return livedet("review", 0.08, spoof_suspected=True)


def test_an_unchanged_decision_keeps_a_completed_review(client, monkeypatch):
    client.post("/api/cases/upload", files=[vid("bad.mp4", b"BAD")])
    wait_done(client, 1)
    cid = client.get("/api/cases").json()["items"][0]["id"]
    assert client.post(f"/api/cases/{cid}/review", json={"decision": "Reject", "notes": "unreadable"}).status_code == 200
    assert client.post(f"/api/cases/{cid}/analyze", json={"tests": ["health_indicators"]}).status_code == 200
    wait_idle()
    after = client.get(f"/api/cases/{cid}").json()
    assert after["decision"] == "Needs Review" and after["review_status"] == "COMPLETED" and after["final_decision"] == "Reject"
    assert after["reviewer"] == "admin"


def test_a_failed_single_re_run_keeps_the_old_results(client, monkeypatch):
    from app import pipeline
    client.post("/api/cases/upload", files=[vid("real.mp4")])
    wait_done(client, 1)
    cid = client.get("/api/cases").json()["items"][0]["id"]
    before = client.get(f"/api/cases/{cid}").json()
    monkeypatch.setattr(pipeline, "build_report", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("worker crashed")))
    assert client.post(f"/api/cases/{cid}/analyze", json={"tests": ["age_estimation"]}).status_code == 200
    wait_idle()
    after = client.get(f"/api/cases/{cid}").json()
    assert after["status"] == "ANALYZED" and after["report"] == before["report"] and after["decision"] == before["decision"]
    assert audit(client, action="re-run failed")["items"][0]["detail"]["error"] == "worker crashed"


def test_single_test_re_run_validation(client, monkeypatch):
    from app import parameters
    client.post("/api/cases/upload", files=[vid("real.mp4")])
    wait_done(client, 1)
    cid = client.get("/api/cases").json()["items"][0]["id"]
    assert client.post(f"/api/cases/{cid}/analyze", json={"tests": ["nope"]}).status_code == 422
    seed = seed_measured("PEND", 0.02)
    sql_set("cases", seed, status="FAILED")
    assert client.post(f"/api/cases/{seed}/analyze", json={"tests": ["liveness"]}).status_code == 409     # nothing to merge into
    monkeypatch.setattr(parameters, "engine_status_fn", lambda spec: (lambda force=False: {"ready": spec["key"] != "liveness", "reason": "service down"}))
    r = client.post(f"/api/cases/{cid}/analyze", json={"tests": ["liveness"]})
    assert r.status_code == 503 and "service down" in r.json()["detail"]                                  # never replace a result with "Unavailable"
    assert client.post(f"/api/cases/{cid}/analyze", json={"tests": ["face_match"]}).status_code == 200
    wait_idle()
    assert client.post(f"/api/cases/{cid}/analyze").status_code == 200                                    # no body: the whole analysis, as before
    wait_idle()


def test_bulk_re_run_of_chosen_tests(client):
    client.post("/api/cases/upload", files=[vid("a.mp4"), vid("b.mp4", b"FAKE")])
    wait_done(client, 2)
    ids = [c["id"] for c in client.get("/api/cases").json()["items"]]
    r = client.post("/api/cases/bulk", json={"action": "rerun", "ids": ids, "tests": ["liveness", "face_match"]}).json()
    assert r["succeeded"] == 2 and r["failed"] == 0
    wait_idle()
    ev = audit(client, action="re-run requested")["items"]
    assert len(ev) == 2 and all(e["detail"]["tests"] == ["liveness", "face_match"] for e in ev)
    assert audit(client, action="re-analyzed")["total"] == 2
    assert client.post("/api/cases/bulk", json={"action": "rerun", "ids": ids, "tests": ["x"]}).status_code == 422
    everything = client.post("/api/cases/bulk", json={"action": "rerun", "ids": ids}).json()
    assert everything["succeeded"] == 2
    wait_idle()
    assert audit(client, action="re-run requested")["total"] == 4


# ---------------------------------------------------------------- history of test runs
def history(client, **q):
    return client.get("/api/history?" + urlencode(q)).json()


def test_every_analysis_is_recorded_in_the_history(client):
    client.post("/api/cases/upload", files=[vid("real.mp4"), vid("fake.mp4", b"FAKE")])
    wait_done(client, 2)
    h = history(client)
    assert h["total"] == 2 and h["active"] == {"queued": 0, "running": 0}
    first = h["items"][0]
    assert first["status"] == "DONE" and first["kind"] == "first" and first["tests"] == ["All tests"] and first["requested_by"] == "admin"
    assert first["started_at"] and first["finished_at"] and first["duration_ms"] is not None
    assert {r["test"] for r in first["results"]} == {"Video authenticity", "Health indicators", "Age estimation", "Face match", "Liveness"}
    assert all(r["from"] is None and r["to"] for r in first["results"]) and first["decision_after"] in ("Accept", "Reject")
    assert first["case_id"] and first["filename"] in ("real.mp4", "fake.mp4")

    cid = client.get("/api/cases").json()["items"][0]["id"]
    client.post(f"/api/cases/{cid}/analyze", json={"tests": ["age_estimation"]})
    wait_idle()
    item = history(client)["items"][0]
    assert item["kind"] == "tests" and item["tests"] == ["Age estimation"] and item["status"] == "DONE"
    assert [r["test"] for r in item["results"]] == ["Age estimation"] and item["results"][0]["from"] and item["results"][0]["to"]
    client.post(f"/api/cases/{cid}/analyze")
    wait_idle()
    again = history(client)["items"][0]
    assert again["kind"] == "all" and again["tests"] == ["All tests"] and len(again["results"]) == 5 and all(r["from"] for r in again["results"])
    assert history(client)["total"] == 4
    assert history(client, test="age_estimation")["total"] == 4          # the full runs include it too
    assert history(client, test="liveness")["total"] == 3                # the age-only run does not
    assert history(client, status="done")["total"] == 4 and history(client, status="failed")["total"] == 0
    assert client.get("/api/history?test=nope").status_code == 422 and client.get("/api/history?status=weird").status_code == 422


def test_the_history_names_no_engine_or_model(client):
    client.post("/api/cases/upload", files=[vid("real.mp4")])
    wait_done(client, 1)
    cid = client.get("/api/cases").json()["items"][0]["id"]
    client.post(f"/api/cases/{cid}/analyze", json={"tests": ["age_estimation"]})
    wait_idle()
    text = client.get("/api/history").text.lower()
    for word in ("dfspot", "mivolo", "rppg", "sface", "minifasnet", "timm", "engine", "model"):
        assert word not in text, word


def test_queued_and_running_runs_show_up_with_their_place_in_line(client):
    a, b, c = (seed_measured(r, 0.02) for r in ("A", "B", "C"))
    with open_db() as db:
        def run(case, status, tests=None, started=True):
            row = db.get("cases", case)
            return db.insert("runs", case_id=case, case_ref=row.case_ref, filename=row.media["original_name"], kind="tests" if tests else "all", tests=tests,
                             requested_by="admin", status=status, **({"started_at": datetime.now(timezone.utc)} if started else {}))
        run(a, "DONE", ["liveness"])
        run(b, "RUNNING", ["age_estimation"])
        run(c, "QUEUED", None, started=False)
        run(a, "QUEUED", ["face_match", "liveness"], started=False)
        db.commit()
    h = history(client)
    assert h["active"] == {"queued": 2, "running": 1}
    assert [i["status"] for i in h["items"]] == ["RUNNING", "QUEUED", "QUEUED", "DONE"]           # in progress first, queue in order
    assert [i["ahead"] for i in h["items"] if i["status"] == "QUEUED"] == [0, 1]
    assert h["items"][0]["tests"] == ["Age estimation"] and h["items"][2]["tests"] == ["Face match", "Liveness"]
    assert [i["status"] for i in history(client, status="active")["items"]] == ["RUNNING", "QUEUED", "QUEUED"]
    assert history(client, test="face_match")["total"] == 2                                       # the full run + the one naming it


def test_failed_runs_are_listed_and_details_are_for_admins(client, monkeypatch):
    from app import pipeline
    client.post("/api/cases/upload", files=[vid("real.mp4")])
    wait_done(client, 1)
    cid = client.get("/api/cases").json()["items"][0]["id"]
    monkeypatch.setattr(pipeline, "build_report", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("worker crashed on gpu 0")))
    client.post(f"/api/cases/{cid}/analyze", json={"tests": ["liveness"]})
    wait_idle()
    viewer = as_user(client, "vee", "viewer")
    mine, theirs = history(client, status="failed")["items"][0], history(viewer, status="failed")["items"][0]
    assert mine["status"] == "FAILED" and "worker crashed" in mine["error"]
    assert theirs["error"] == "The run could not be completed." and "worker" not in str(theirs)


def test_deleting_a_video_cancels_its_waiting_run_and_keeps_the_history(client):
    a = seed_measured("A", 0.02)
    with open_db() as db:
        row = db.get("cases", a)
        db.insert("runs", case_id=a, case_ref=row.case_ref, filename="A.mp4", kind="all", requested_by="admin", status="QUEUED")
        db.commit()
    assert client.post("/api/cases/bulk", json={"action": "delete", "ids": [a]}).status_code == 200
    item = history(client)["items"][0]
    assert item["status"] == "CANCELLED" and item["error"] == "The video was deleted" and item["case_id"] is None and item["filename"] == "A.mp4"


def test_history_needs_a_signed_in_user_but_any_role(client):
    assert history(as_user(client, "vee", "viewer"))["total"] == 0
    assert TestClient(app).get("/api/history").status_code == 401


# ---------------------------------------------------------------- OpenAI connection, interpretation, PDF reports
import json as _json

GOOD_ANSWER = {"headline": "The video looks consistent with a genuine recording.", "summary": "No test raised a concern. The face matched and the movie looked natural.",
               "findings": [{"test": "Video authenticity", "text": "Score 0.02 is well below the review line → no sign of manipulation.", "level": "ok"},
                            {"test": "Liveness", "text": "Not conclusive: “Low confidence”.", "level": "info"}],
               "concerns": [], "next_steps": ["Spot-check the first seconds."], "caveats": ["Scores are not calibrated probabilities."]}


class FakeOpenAI:
    """Stands in for httpx.post when the app talks to OpenAI / Azure OpenAI."""

    def __init__(self, answer=None, status=200):
        self.calls, self.answer, self.status = [], answer if answer is not None else GOOD_ANSWER, status

    def __call__(self, url, headers=None, params=None, json=None, timeout=None):
        import httpx
        self.calls.append({"url": url, "headers": headers, "params": params, "json": json})
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"message": "nope sk-abcdef123456"}})
        content = self.answer if isinstance(self.answer, str) else _json.dumps(self.answer)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def add_openai(client, monkeypatch, provider="openai", **fields):
    from app import connections
    monkeypatch.setattr(settings, "app_secret_key", "unit-test-key")
    monkeypatch.setattr(connections, "_check_public_https", lambda url: url)      # no DNS in the tests
    body = {"provider": provider, "model": "gpt-test", **({"endpoint": "https://res.openai.azure.com"} if provider == "azure" else {}), **fields}
    r = client.post("/api/settings/connections", json={"type": "openai", "name": "AI", "fields": body, "secrets": {"api_key": "sk-REALKEY-123"}})
    assert r.status_code == 200, r.text
    return r.json()


def uploaded_case(client):
    client.post("/api/cases/upload", files=[vid("secret-name.mp4")], data={"reference": "POL-999"})
    wait_done(client, 1)
    return client.get("/api/cases").json()["items"][0]["id"]


def test_openai_connection_is_configurable_tested_and_never_leaks_the_key(client, monkeypatch):
    from app.analyzers import providers  # noqa: F401
    import httpx
    types = {t["type"]: t for t in client.get("/api/settings/connection-types").json()["types"]}
    assert types["openai"]["used"] and {f["key"] for f in types["openai"]["fields"]} >= {"provider", "model", "endpoint", "api_key"}
    assert client.get("/api/ai/status").json() == {"configured": False}
    monkeypatch.setattr(settings, "app_secret_key", "unit-test-key")
    bad = client.post("/api/settings/connections", json={"type": "openai", "name": "x", "fields": {"provider": "azure", "model": "m"}, "secrets": {"api_key": "k"}})
    assert bad.status_code == 422 and "endpoint" in bad.json()["detail"].lower()                      # Azure needs its endpoint
    assert client.post("/api/settings/connections", json={"type": "openai", "name": "x", "fields": {"model": "m", "base_url": "http://plain"}, "secrets": {"api_key": "k"}}).status_code == 422
    conn = add_openai(client, monkeypatch)
    assert client.get("/api/ai/status").json() == {"configured": True}
    assert "sk-REALKEY" not in client.get("/api/settings/connections").text and "sk-REALKEY" not in str(audit(client, page_size=200))
    assert "sk-REALKEY" not in sql_rows("connections")[0].secrets_enc                                  # encrypted at rest

    fake = FakeOpenAI()
    monkeypatch.setattr(httpx, "post", fake)
    t = client.post("/api/settings/connections/test", json={"id": conn["id"], "type": "openai"}).json()
    assert t["ok"] and "answered" in t["message"]
    assert fake.calls[0]["headers"]["Authorization"] == "Bearer sk-REALKEY-123" and fake.calls[0]["url"] == "https://api.openai.com/v1/chat/completions"
    assert fake.calls[0]["json"]["model"] == "gpt-test"
    monkeypatch.setattr(httpx, "post", FakeOpenAI(status=401))
    r = client.post("/api/settings/connections/test", json={"id": conn["id"], "type": "openai"}).json()
    assert not r["ok"] and "rejected the API key" in r["message"] and "sk-" not in r["message"]
    monkeypatch.setattr(httpx, "post", FakeOpenAI(status=429))
    assert "rate-limiting" in client.post("/api/settings/connections/test", json={"id": conn["id"], "type": "openai"}).json()["message"]


def test_azure_openai_uses_the_deployment_url_and_key_header(client, monkeypatch):
    import httpx
    conn = add_openai(client, monkeypatch, "azure", model="my-deployment", api_version="2024-06-01")
    fake = FakeOpenAI()
    monkeypatch.setattr(httpx, "post", fake)
    assert client.post("/api/settings/connections/test", json={"id": conn["id"], "type": "openai"}).json()["ok"]
    call = fake.calls[0]
    assert call["url"] == "https://res.openai.azure.com/openai/deployments/my-deployment/chat/completions"
    assert call["params"] == {"api-version": "2024-06-01"} and call["headers"] == {"api-key": "sk-REALKEY-123"} and "model" not in call["json"]


def test_private_addresses_are_refused_for_the_ai_service(client, monkeypatch):
    monkeypatch.setattr(settings, "app_secret_key", "unit-test-key")
    r = client.post("/api/settings/connections/test", json={"type": "openai", "fields": {"provider": "openai", "model": "m", "base_url": "https://127.0.0.1/v1"}, "secrets": {"api_key": "k"}}).json()
    assert not r["ok"] and "private" in r["message"]


def test_interpretation_is_written_stored_and_sees_no_personal_details(client, monkeypatch):
    import httpx
    cid = uploaded_case(client)
    assert client.post(f"/api/cases/{cid}/interpretation").status_code == 503                         # not set up yet
    add_openai(client, monkeypatch)
    fake = FakeOpenAI()
    monkeypatch.setattr(httpx, "post", fake)
    r = client.post(f"/api/cases/{cid}/interpretation")
    assert r.status_code == 200
    out = r.json()
    assert out["headline"] and out["findings"][0]["test"] == "Video authenticity" and out["findings"][0]["level"] == "ok" and out["stale"] is False
    sent = _json.dumps(fake.calls[0]["json"]).lower()
    for private in ("secret-name", "pol-999", "vid-", "dfspot", "timm", "mivolo", "rppg", "sface", "minifasnet"):
        assert private not in sent, private                                                            # no file name, reference, id or engine/model name
    assert "video authenticity" in sent and "thresholds_in_force" in sent
    assert "Bearer" in fake.calls[0]["headers"]["Authorization"] and fake.calls[0]["json"]["response_format"] == {"type": "json_object"}
    detail = client.get(f"/api/cases/{cid}").json()
    assert detail["interpretation"]["summary"] == out["summary"] and detail["interpretation"]["generated_at"]
    assert audit(client, action="interpretation generated")["total"] == 1

    # a re-run of a test changes the inputs: the old text is kept but marked as out of date
    from app import services_client as sc
    monkeypatch.setattr(sc, "check_liveness", lambda video, rules: __import__("tests.test_api", fromlist=["livedet"]).livedet("review", 0.08, spoof_suspected=True))
    monkeypatch.undo() if False else None
    client.post(f"/api/cases/{cid}/analyze", json={"tests": ["liveness"]})
    wait_idle()
    assert client.get(f"/api/cases/{cid}").json()["interpretation"]["stale"] is True


def test_interpretation_failures_are_explained_and_roles_enforced(client, monkeypatch):
    import httpx
    cid = uploaded_case(client)
    add_openai(client, monkeypatch)
    for status, text in ((401, "rejected the API key"), (404, "model or deployment"), (429, "rate-limiting"), (500, "refused the request")):
        monkeypatch.setattr(httpx, "post", FakeOpenAI(status=status))
        r = client.post(f"/api/cases/{cid}/interpretation")
        assert r.status_code == 502 and text in r.json()["detail"] and "sk-" not in r.json()["detail"], status
    monkeypatch.setattr(httpx, "post", FakeOpenAI(answer="this is not json"))
    assert client.post(f"/api/cases/{cid}/interpretation").status_code == 502
    monkeypatch.setattr(httpx, "post", FakeOpenAI(answer="```json\n" + _json.dumps(GOOD_ANSWER) + "\n```"))
    assert client.post(f"/api/cases/{cid}/interpretation").status_code == 200                        # a fenced answer is still read
    assert as_user(client, "vee", "viewer").post(f"/api/cases/{cid}/interpretation").status_code == 403
    assert client.post("/api/cases/999/interpretation").status_code == 404
    seed = seed_measured("PEND", 0.02)
    sql_set("cases", seed, status="FAILED")
    assert client.post(f"/api/cases/{seed}/interpretation").status_code == 409


def test_model_output_is_cleaned_before_it_is_stored():
    from app import interpret
    out = interpret.normalize({"headline": "H" * 500, "summary": "S", "findings": [{"test": "T", "text": "x", "level": "weird"}, "junk", {"text": ""}],
                               "concerns": "not a list", "next_steps": ["a", "", None, "b"] + ["c"] * 20, "caveats": 5, "extra": "ignored"})
    assert len(out["headline"]) == 200 and out["findings"] == [{"test": "T", "text": "x", "level": "info"}]
    assert out["concerns"] == [] and out["next_steps"][:2] == ["a", "b"] and len(out["next_steps"]) == 6 and out["caveats"] == [] and "extra" not in out
    import pytest as _p
    with _p.raises(Exception):
        interpret.normalize({"findings": []})


def test_video_pdf_report(client, monkeypatch):
    import httpx
    cid = uploaded_case(client)
    plain = client.get(f"/api/cases/{cid}/report.pdf")
    assert plain.status_code == 200 and plain.headers["content-type"] == "application/pdf" and plain.content[:5] == b"%PDF-"
    assert plain.headers["content-disposition"].endswith(".pdf") and len(plain.content) > 4000
    add_openai(client, monkeypatch)
    monkeypatch.setattr(httpx, "post", FakeOpenAI())                                                   # answer contains arrows and curly quotes
    client.post(f"/api/cases/{cid}/interpretation")
    with_ai = client.get(f"/api/cases/{cid}/report.pdf")
    assert with_ai.status_code == 200 and with_ai.content[:5] == b"%PDF-" and len(with_ai.content) > len(plain.content)
    assert len(client.get(f"/api/cases/{cid}/report.pdf?include_ai=false").content) < len(with_ai.content)
    assert audit(client, action="video report downloaded")["total"] == 3
    assert client.get("/api/cases/999/report.pdf").status_code == 404
    assert as_user(client, "vee", "viewer").get(f"/api/cases/{cid}/report.pdf").status_code == 200   # read-only users may download a report
    seed = seed_measured("NOREP", 0.02)
    sql_set("cases", seed, report=None)
    assert client.get(f"/api/cases/{seed}/report.pdf").status_code == 409


def test_pdf_text_is_made_safe():
    from app import casepdf
    assert casepdf.clean("a → b “q” <b>&") == 'a -&gt; b "q" &lt;b&gt;&amp;' and casepdf.clean(None) == "" and casepdf.clean("café 中") == "café ?"


def test_report_pdf_can_include_an_ai_overview(client, monkeypatch):
    import httpx
    seed_measured("A", 0.02, final="Accept")
    seed_measured("B", 0.90)
    assert client.get("/api/reports/export", params={"format": "pdf", "ai": "true"}).status_code == 503   # AI not set up: say so, do not silently skip
    add_openai(client, monkeypatch)
    fake = FakeOpenAI(answer={"headline": "Two videos, one flagged.", "summary": "One was accepted; one looks manipulated.", "highlights": ["50% flagged"], "watch_points": ["Check B"]})
    monkeypatch.setattr(httpx, "post", fake)
    plain = client.get("/api/reports/export", params={"format": "pdf"})
    withai = client.get("/api/reports/export", params={"format": "pdf", "ai": "true"})
    assert withai.status_code == 200 and withai.content[:5] == b"%PDF-" and len(withai.content) > len(plain.content) and len(fake.calls) == 1
    sent = fake.calls[0]["json"]["messages"][1]["content"]
    assert '"videos": 2' in sent and "A.mp4" not in sent and "VID-" not in sent                       # counts only
    assert audit(client, action="report exported")["items"][0]["detail"]["ai_overview"] is True
    monkeypatch.setattr(httpx, "post", FakeOpenAI(status=429))
    assert client.get("/api/reports/export", params={"format": "pdf", "ai": "true"}).status_code == 502
    assert client.get("/api/reports/export", params={"format": "csv", "ai": "true"}).status_code == 200   # ai only applies to PDFs
