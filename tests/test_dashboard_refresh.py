"""How hard the open page works, and how hard it makes the server work.

Two things were wasteful enough to be felt on a deployment rather than a
laptop. Four refresh timers fired on their own schedules whatever else was
happening: one ignored a hidden tab, none stopped when the server went away,
and none waited for the previous request, so a slow read could have several
answers in flight rewriting one table. And every page load re-downloaded
app.js, app.css and the vendored fonts, because the guard's blanket
``no-store`` overwrote even the week-long cache the font route asked for.

The scheduler is driven here under node, with the clock and the network
faked, so its behaviour is checked rather than read.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import sellbird.dashboard as dash

WEB = Path(__file__).resolve().parent.parent / "sellbird" / "web"
ORIGIN = "http://127.0.0.1:5555"


@pytest.fixture
def client(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(dash, "DB_PATH", tmp_path / "data" / "sellbird.db")
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")
    with TestClient(dash.app, base_url=ORIGIN) as c:
        yield c


# ── The page's assets are not re-sent every load ──


def test_a_stylesheet_comes_back_with_an_etag(client):
    r = client.get("/static/app.css")
    assert r.status_code == 200
    assert r.headers["etag"]
    # no-cache means "ask me first", not "don't keep it": editing app.css and
    # reloading still shows the edit.
    assert r.headers["cache-control"] == "no-cache"


def test_an_unchanged_asset_answers_304_with_no_body(client):
    etag = client.get("/static/app.js").headers["etag"]
    again = client.get("/static/app.js", headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert not again.content


def test_an_edited_asset_is_sent_again(client, monkeypatch, tmp_path):
    before = client.get("/static/app.css").headers["etag"]
    copy = tmp_path / "web"
    shutil.copytree(WEB, copy)
    (copy / "app.css").write_text("/* edited */", encoding="utf-8")
    monkeypatch.setattr(dash, "WEB_DIR", copy)
    after = client.get("/static/app.css")
    assert after.status_code == 200 and after.headers["etag"] != before


def test_the_guard_no_longer_overwrites_a_fonts_cache(client):
    """It used to replace this with no-store, so the vendored fonts were
    fetched again on every load of a page that works offline on purpose."""
    fonts = sorted(WEB.glob("fonts/*.woff2"))
    if not fonts:
        pytest.skip("no vendored fonts in this checkout")
    r = client.get(f"/static/fonts/{fonts[0].name}")
    assert r.status_code == 200
    assert "max-age=604800" in r.headers["cache-control"]


def test_data_is_still_never_stored(client):
    """The exception is for static files only. Anything carrying pipeline
    data keeps no-store."""
    r = client.get("/api/today")
    assert r.headers["cache-control"] == "no-store"
    assert client.get("/").headers["cache-control"] == "no-store"


def test_a_static_file_outside_the_folder_is_still_refused(client):
    # "../dashboard.py" is normalised by the client to /dashboard.py, which
    # is not a public path and needs a token; the rest reach the route and
    # are refused there. Either way no file outside sellbird/web is served.
    for attempt in ("../dashboard.py", "..%2Fdashboard.py", "fonts/../../dashboard.py"):
        r = client.get(f"/static/{attempt}")
        assert r.status_code in (400, 401, 404), attempt
        assert "def " not in r.text


# ── The refresh schedule ──

HARNESS = r"""
const fs = require('fs'), vm = require('vm');
// A clock and a page we control.
let now = 1000000, hidden = false, locked = false, tab = 'today';
const listeners = {};
global.document = {
  get hidden() { return hidden; },
  addEventListener: (name, fn) => { (listeners[name] = listeners[name] || []).push(fn); },
};
global.window = {addEventListener: (name, fn) => { (listeners[name] = listeners[name] || []).push(fn); }};
global.Date = {now: () => now};
let ticker = null;
global.setInterval = (fn, ms) => { ticker = fn; return 1; };

const src = fs.readFileSync(process.argv[1], 'utf8');
const block = src.slice(src.indexOf('const OFFLINE_WAIT_MS'), src.indexOf('// ── Your company'));
// The four jobs it registers call into the rest of the page; the harness
// replaces them with counters and a controllable outcome.
const calls = {status: 0, live: 0, counts: 0, tabData: 0};
let fail = false, slow = null;
global._locked = false;
global._transportFailures = 0;
global.currentTab = tab;
global.companyDrill = false;
global.loadSellbirdStatus = () => { calls.status++; return slow || (fail ? false : true); };
global.loadLiveStatus = () => { calls.live++; return slow || true; };
global.api = async () => { calls.counts++; if (slow) await slow;
  return fail ? null : {items: [], stats: {}}; };
global.navCount = () => {};
for (const name of ['loadToday', 'loadTrend', 'loadFunnel', 'loadRuns', 'loadTodayActivity',
                    'loadCompanies', 'loadProspects', 'loadCampaigns', 'loadConversations',
                    'loadActivity', 'loadInboxes', 'loadUsage', 'loadLogs']) {
  global[name] = () => { calls.tabData++; return slow || true; };
}
vm.runInThisContext(block);

const flush = () => new Promise(r => setImmediate(r));
async function ticks(n, ms = 1000) {
  for (let i = 0; i < n; i++) { now += ms; tickRefresh(); await flush(); }
}
const out = o => console.log(JSON.stringify(o));
"""


def run_js(body: str):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    script = HARNESS + body
    out = subprocess.run([node, "-e", script, str(WEB / "app.js")],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_nothing_runs_while_the_tab_is_hidden():
    data = run_js("""
      (async () => { hidden = true; await ticks(100); out(calls); })();
    """)
    assert data == {"status": 0, "live": 0, "counts": 0, "tabData": 0}


def test_nothing_runs_while_the_page_is_locked():
    data = run_js("""
      (async () => { _locked = true; await ticks(100); out(calls); })();
    """)
    assert data == {"status": 0, "live": 0, "counts": 0, "tabData": 0}


def test_each_job_keeps_its_own_interval():
    """Eighty seconds of ticks on a tab whose refresh is a single read:
    the header every 8s, the tab's data every 15s, nav counts every 20s."""
    data = run_js("""
      (async () => { currentTab = 'activity'; await ticks(80); out(calls); })();
    """)
    assert 9 <= data["status"] <= 11
    assert 4 <= data["tabData"] <= 6
    assert 3 <= data["counts"] <= 5
    assert data["live"] == 0          # the pipeline card belongs to Today


def test_todays_refresh_is_one_job_over_several_reads():
    """Today loads five things at once. They go together, so they count as
    one job and can't half-happen."""
    data = run_js("""
      (async () => { await ticks(80); out(calls); })();
    """)
    assert 7 <= data["live"] <= 9
    assert data["tabData"] % 5 == 0 and 4 <= data["tabData"] // 5 <= 6
    assert data["counts"] == 0        # Today keeps its own counts up to date


def test_the_nav_counts_job_runs_only_away_from_today():
    data = run_js("""
      (async () => { currentTab = 'outbox'; await ticks(60); out(calls); })();
    """)
    assert data["counts"] >= 2
    assert data["live"] == 0          # the pipeline card belongs to Today


def test_a_job_never_runs_twice_at_once():
    """The point of the whole thing: a slow read used to get a second and a
    third request sent under it, and the last answer to land won."""
    data = run_js("""
      (async () => {
        currentTab = 'activity';
        let release;
        slow = new Promise(r => { release = r; });
        await ticks(80);                       // every job starts, none finishes
        const during = {...calls};
        release(true);
        slow = null;
        await flush();
        out({during});
      })();
    """)
    # One run of each job, however many ticks went by while it was in flight.
    assert data["during"]["status"] == 1
    assert data["during"]["tabData"] == 1
    assert data["during"]["counts"] == 1


def test_everything_backs_off_while_the_server_is_unreachable():
    data = run_js("""
      (async () => {
        currentTab = 'activity';
        _transportFailures = 3;               // nothing is reaching the server
        await ticks(120);
        out(calls);
      })();
    """)
    # Two minutes of ticks, a minute between tries: a couple of attempts, not
    # the fifteen the header's 8-second timer would have made.
    assert data["status"] <= 3
    assert data["tabData"] <= 3
    assert data["counts"] <= 3


def test_a_job_that_fails_backs_off_on_its_own():
    data = run_js("""
      (async () => {
        fail = true;              // only loadSellbirdStatus answers false
        await ticks(60);
        out({failing: {...calls}});
      })();
    """)
    # loadSellbirdStatus returning false marks that job failed; the others
    # keep their own pace.
    assert data["failing"]["status"] <= 2       # backed off
    assert data["failing"]["live"] >= 4        # unaffected


def test_coming_back_to_the_tab_refreshes_at_once():
    data = run_js("""
      (async () => {
        currentTab = 'activity';
        hidden = true;
        await ticks(40);
        hidden = false;
        for (const fn of (listeners.visibilitychange || [])) fn();
        await flush();
        out(calls);
      })();
    """)
    assert data["status"] == 1 and data["tabData"] == 1 and data["counts"] == 1


def test_the_page_registers_the_scheduler_not_four_timers():
    """A regression guard: the old shape was four setIntervals, each with its
    own hidden/locked check (and one without)."""
    src = (WEB / "app.js").read_text(encoding="utf-8")
    tail = src[src.index("// ── Live refresh"):]
    tail = tail[:tail.index("// ── Your company")]
    assert len(re.findall(r"setInterval\(", tail)) == 1
    assert "tickRefresh" in tail
