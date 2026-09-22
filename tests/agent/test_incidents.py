from agent import incidents
from agent.store import Store


def test_record_then_find_exact_symptom(tmp_path):
    store = Store(str(tmp_path / "d.db"))
    incidents.record(
        store,
        component="hostctl",
        symptom="guest_exec to 104 returns a 500",
        cause="hostctl restarted mid-request",
        fix="retried after 5s, hostctl recovers on its own",
    )
    matches = incidents.find_similar(
        store, component="hostctl", symptom="guest_exec to 104 returns a 500"
    )
    assert len(matches) == 1
    assert matches[0].incident["cause"] == "hostctl restarted mid-request"


def test_similar_but_not_identical_symptom_still_matches(tmp_path):
    """The point of this whole module: exact string matching would miss
    this. Same fault, different words, same component."""
    store = Store(str(tmp_path / "d.db"))
    incidents.record(
        store,
        component="hostctl",
        symptom="guest_exec to 104 returns a 500 error",
        cause="hostctl restarted mid-request",
        fix="retried after 5s, hostctl recovers on its own",
    )
    matches = incidents.find_similar(
        store, component="hostctl", symptom="hostctl 500 when running guest_exec on 104"
    )
    assert len(matches) == 1
    assert matches[0].incident["fix"] == "retried after 5s, hostctl recovers on its own"


def test_unrelated_incident_does_not_match(tmp_path):
    store = Store(str(tmp_path / "d.db"))
    incidents.record(
        store,
        component="mail",
        symptom="SMTP login fails with authentication error",
        cause="MAIL_PASSWORD rotated and env file not reloaded",
        fix="restarted the agent after updating MAIL_PASSWORD",
    )
    matches = incidents.find_similar(
        store, component="zfs", symptom="pool tank reports DEGRADED state"
    )
    assert matches == []


def test_component_match_boosts_a_weak_word_overlap(tmp_path):
    store = Store(str(tmp_path / "d.db"))
    incidents.record(
        store,
        component="uptime_kuma",
        symptom="status page returns no monitors",
        cause="status page slug was wrong",
        fix="set UPTIME_KUMA_SLUG to the right slug",
    )
    same_component = incidents.find_similar(
        store, component="uptime_kuma", symptom="heartbeat list came back totally blank"
    )
    assert len(same_component) == 1


def test_find_similar_ranks_best_match_first(tmp_path):
    store = Store(str(tmp_path / "d.db"))
    incidents.record(
        store, component="jellyfin", symptom="library scan hangs at 50 percent",
        cause="a corrupt file", fix="deleted the file",
    )
    incidents.record(
        store, component="jellyfin", symptom="library scan hangs and never finishes",
        cause="ran out of disk space mid-scan", fix="freed space on tank",
    )
    matches = incidents.find_similar(
        store, component="jellyfin", symptom="library scan hangs and never completes"
    )
    assert len(matches) == 2
    assert matches[0].score >= matches[1].score
    assert matches[0].incident["fix"] == "freed space on tank"


def test_normalize_symptom_drops_stopwords_and_is_order_independent():
    a = incidents.normalize_symptom("the pool is in a degraded state")
    b = incidents.normalize_symptom("degraded state, pool")
    assert a == b
