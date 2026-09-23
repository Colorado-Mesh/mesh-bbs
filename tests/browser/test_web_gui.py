"""Standalone web flows against real HTTP and SQLite, with isolated Chromium contexts.

Protocol-labelled posts enter through CommandService, not simulated radio
drivers. Radio wire interoperability is covered separately by integration tests.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import pytest

from mesh_bbs.commands import CommandService
from mesh_bbs.store import Post, Store
from mesh_bbs.views import Views
from mesh_bbs.web import ReadOnlyWebServer
from mesh_bbs.web_access import WebAccess

playwright = pytest.importorskip("playwright.sync_api")
expect = playwright.expect


@dataclass
class BBS:
    base_url: str
    store: Store
    service: CommandService
    access: WebAccess
    editor_key: str = field(repr=False)
    contributor_key: str = field(repr=False)
    seeds: dict[str, Post]


@pytest.fixture(scope="module")
def browser():
    """Close Playwright's sync event loop before later pytest-asyncio modules run."""
    with playwright.sync_playwright() as engine:
        browser = engine.chromium.launch(headless=True)
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture
def page(browser):
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    page.set_default_timeout(5000)
    page.set_default_navigation_timeout(10000)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        yield page
    finally:
        context.close()
        assert not errors, errors


@pytest.fixture
def bbs(tmp_path):
    store = Store(tmp_path / "bbs.sqlite3", "colorado-mesh")
    server = None
    try:
        service = CommandService(store)
        access = WebAccess(store)
        editor_key = access.create("alice", editor=True)
        contributor_key = access.create("sam")
        seeds = {}
        for protocol, actor, title, body in (
            ("meshcore", "meshcore:" + "a1" * 32, "MeshCore field day", "Bring the portable mast."),
            (
                "meshtastic",
                "meshtastic:11223344",
                "Meshtastic trail report",
                "Trail coverage improved.",
            ),
            (
                "lxmf",
                "reticulum:" + "b2" * 16,
                "LXMF workshop",
                "Build an offline reading station.",
            ),
        ):
            response = service.handle(actor, f"@seed post general {title} | {body}")
            assert response.startswith("Saved locally as "), response
            seeds[protocol] = store.get_post(response.split()[3].rstrip("."))
        reply = service.handle(
            "meshtastic:11223344",
            f"@reply reply {seeds['meshcore'].post_id} I can bring a battery.",
        )
        draft = reply.split()[2]
        saved = service.handle("meshtastic:11223344", f"publish {draft}")
        seeds["reply"] = store.get_post(saved.split()[3].rstrip("."))
        seeds["issue"] = store.import_article(
            "colorado", "issue", "news", "September issue", "Network news."
        )
        views = Views(store, "Colorado Mesh browser pilot")
        server = ReadOnlyWebServer(views, port=0, access=access)
        server.start()
        host, port = server.address
        yield BBS(
            f"http://{host}:{port}", store, service, access, editor_key, contributor_key, seeds
        )
    finally:
        if server is not None:
            server.stop()
        store.close()


def sign_in(page, bbs, *, editor=False):
    page.get_by_role("button", name="Sign in to post", exact=True).click()
    page.get_by_label("Access key", exact=True).fill(
        bbs.editor_key if editor else bbs.contributor_key
    )
    page.get_by_role("button", name="Sign in", exact=True).click()
    expect(page.locator("#account-name")).to_have_text("web:alice" if editor else "web:sam")
    expect(page.locator("#signin-dialog")).not_to_be_visible()


def saved_post(page, bbs):
    page.wait_for_url(re.compile(r"/posts/[0-9a-f]{64}$"))
    return bbs.store.get_post(page.url.rsplit("/", 1)[1])


def test_public_board_navigation_shows_shared_protocol_threads(page, bbs):
    page.goto(bbs.base_url)
    expect(page.locator("#account-name")).to_have_text("Public reading")
    page.locator('nav[aria-label="Boards"] a[href="/boards/general"]').click()
    for protocol in ("meshcore", "meshtastic", "lxmf"):
        expect(page.get_by_role("link", name=bbs.seeds[protocol].title, exact=True)).to_be_visible()
    page.get_by_role("link", name="MeshCore field day", exact=True).click()
    root = page.locator(f"#post-{bbs.seeds['meshcore'].post_id}")
    expect(root.locator(".post-body")).to_contain_text("Bring the portable mast.")
    root.get_by_role("link", name="Permalink", exact=True).click()
    page.get_by_role("link", name="Thread", exact=True).click()
    expect(page.locator(f"#post-{bbs.seeds['meshcore'].post_id}")).to_be_visible()
    expect(page.locator(f"#post-{bbs.seeds['reply'].post_id}")).to_contain_text(
        "I can bring a battery."
    )
    expect(page.locator(".post-meta")).to_contain_text(["MeshCore", "Meshtastic"])
    assert page.context.cookies() == []


def test_editor_preview_draft_refresh_and_community_publication(page, bbs):
    page.goto(bbs.base_url + "/connect")
    sign_in(page, bbs, editor=True)
    page.goto(bbs.base_url + "/boards/general")
    page.get_by_role("link", name="New post", exact=True).click()
    expect(page.get_by_role("button", name="Publish post", exact=True)).to_be_enabled()
    title = "October newsletter"
    body = '<img src=x onerror="window.injected=true">\n\nField day: café | あ and radios.'
    page.get_by_label("Title", exact=True).fill(title)
    page.get_by_label("Message", exact=True).fill(body)
    page.get_by_role("button", name="Preview", exact=True).click()
    expect(page.locator("#post-preview div")).to_have_text(body)
    assert page.locator("#post-preview img").count() == 0
    assert page.evaluate("window.injected") is None
    page.reload()
    expect(page.get_by_label("Title", exact=True)).to_have_value(title)
    expect(page.get_by_label("Message", exact=True)).to_have_value(body)
    expect(page.locator("#account-name")).to_have_text("web:alice")
    page.get_by_role("button", name="Publish post", exact=True).click()
    post = saved_post(page, bbs)
    assert (post.author, post.board, post.title, post.body) == ("web:alice", "general", title, body)
    assert post.thread_id == post.post_id and not post.parent_id
    expect(page.locator("article .post-body")).to_contain_text("<img src=x onerror=")
    assert page.locator("article .post-body img").count() == 0
    assert page.evaluate("window.injected") is None
    assert page.evaluate("localStorage.getItem('mesh-bbs-draft:general:')") is None
    assert bbs.service.handle("reticulum:" + "b2" * 16, f"read {post.post_id}", max_bytes=4096) == (
        f"{post.post_id[:12]} {title}\n{body}"
    )


def test_contributor_reply_targets_selected_message_and_cannot_start_news(page, bbs):
    page.goto(bbs.base_url + "/new/news")
    sign_in(page, bbs)
    expect(page.locator("main")).to_contain_text("News is read-only")
    expect(page.get_by_role("button", name="Publish post", exact=True)).to_have_count(0)
    selected = bbs.seeds["reply"]
    page.goto(bbs.base_url + "/posts/" + selected.post_id)
    page.get_by_role("link", name="Reply", exact=True).click()
    expect(page.locator("blockquote")).to_have_text(selected.body)
    expect(page.locator("#compose-form")).to_have_attribute("data-parent", selected.post_id)
    page.get_by_label("Message", exact=True).fill("Thanks, that is the battery we need.")
    page.get_by_role("button", name="Publish post", exact=True).click()
    post = saved_post(page, bbs)
    assert post.parent_id == selected.post_id
    assert post.thread_id == bbs.seeds["meshcore"].post_id
    assert post.author == "web:sam"
    response = bbs.service.handle("meshtastic:11223344", f"read {post.post_id}")
    assert "Thanks, that is the battery we need." in response
    page.get_by_role("link", name="Thread", exact=True).click()
    expect(page.locator(f"#post-{post.post_id}")).to_be_visible()


def test_lost_publication_response_refresh_and_retry_creates_one_post(page, bbs):
    page.goto(bbs.base_url + "/new/general")
    sign_in(page, bbs)
    page.get_by_label("Title", exact=True).fill("Outage report")
    page.get_by_label("Message", exact=True).fill("Saved before the connection disappeared.")
    submissions = []

    def lose_confirmation(route):
        submissions.append(route.request.post_data_json)
        response = route.fetch()
        assert response.status == 201
        route.abort("connectionclosed")

    page.route("**/api/posts", lose_confirmation, times=1)
    before = bbs.store.db.execute("SELECT count(*) FROM events").fetchone()[0]
    page.get_by_role("button", name="Publish post", exact=True).click()
    expect(page.locator("#compose-message")).to_contain_text("Delivery is not confirmed")
    expect(page.get_by_role("button", name="Retry publication", exact=True)).to_be_enabled()
    assert bbs.store.db.execute("SELECT count(*) FROM events").fetchone()[0] == before + 1
    initial = json.loads(page.evaluate("localStorage.getItem('mesh-bbs-draft:general:')"))
    assert initial["pending"] and initial["operation"] == submissions[0]["operation"]
    page.reload()
    expect(page.get_by_label("Title", exact=True)).to_have_value("Outage report")
    expect(page.get_by_label("Message", exact=True)).to_have_attribute("readonly", "")
    expect(page.get_by_role("button", name="Retry publication", exact=True)).to_be_enabled()
    with page.expect_request("**/api/posts") as retry:
        page.get_by_role("button", name="Retry publication", exact=True).click()
    post = saved_post(page, bbs)
    assert retry.value.post_data_json == submissions[0]
    assert post.title == "Outage report"
    assert bbs.store.db.execute("SELECT count(*) FROM events").fetchone()[0] == before + 1
    assert (
        len([post for post in bbs.store.list_posts("general") if post.title == "Outage report"])
        == 1
    )


def test_stale_tab_cannot_publish_or_overwrite_another_tabs_draft(page, bbs):
    page.goto(bbs.base_url + "/new/general")
    sign_in(page, bbs)
    page.get_by_label("Title", exact=True).fill("Shared draft")
    page.get_by_label("Message", exact=True).fill("Original text.")
    other = page.context.new_page()
    try:
        other.goto(bbs.base_url + "/new/general")
        sign_in(other, bbs)
        expect(other.get_by_label("Message", exact=True)).to_have_value("Original text.")
        other.get_by_label("Message", exact=True).fill("Newer text from the other tab.")
        latest = other.evaluate("localStorage.getItem('mesh-bbs-draft:general:')")
        before = bbs.store.db.execute("SELECT count(*) FROM events").fetchone()[0]

        page.get_by_role("button", name="Publish post", exact=True).click()
        expect(page.locator("#compose-message")).to_contain_text("Another tab changed this draft")
        expect(page.get_by_role("button", name="Publish post", exact=True)).to_be_disabled()
        expect(page.get_by_role("button", name="Discard draft", exact=True)).to_be_disabled()
        page.get_by_label("Message", exact=True).fill("A stale edit that must not replace it.")
        assert page.evaluate("localStorage.getItem('mesh-bbs-draft:general:')") == latest
        assert bbs.store.db.execute("SELECT count(*) FROM events").fetchone()[0] == before

        page.reload()
        expect(page.get_by_label("Message", exact=True)).to_have_value(
            "Newer text from the other tab."
        )
        expect(page.get_by_role("button", name="Publish post", exact=True)).to_be_enabled()
        page.get_by_role("button", name="Publish post", exact=True).click()
        assert saved_post(page, bbs).body == "Newer text from the other tab."
        assert bbs.store.db.execute("SELECT count(*) FROM events").fetchone()[0] == before + 1
    finally:
        other.close()


def test_delayed_old_account_failure_preserves_new_signin(page, bbs):
    page.goto(bbs.base_url + "/new/general")
    sign_in(page, bbs, editor=True)
    page.get_by_label("Title", exact=True).fill("Account switch")
    page.get_by_label("Message", exact=True).fill("Keep this draft with its original contributor.")
    held = []
    page.route("**/api/posts", lambda route: held.append(route), times=1)
    with page.expect_request("**/api/posts"):
        page.get_by_role("button", name="Publish post", exact=True).click()
    expect(page.get_by_role("button", name="Saving…", exact=True)).to_be_disabled()
    page.get_by_role("button", name="Sign out", exact=True).click()
    sign_in(page, bbs)
    assert len(held) == 1
    held[0].fulfill(status=401, json={"error": "The previous access key was revoked."})
    expect(page.locator("#compose-message")).to_contain_text("previous access key was revoked")
    expect(page.locator("#account-name")).to_have_text("web:sam")
    expect(page.locator("#compose-access")).to_contain_text("This draft belongs to web:alice")
    expect(page.get_by_role("button", name="Publish post", exact=True)).to_be_disabled()
    assert page.evaluate(
        "key => sessionStorage.getItem('mesh-bbs-access') === key", bbs.contributor_key
    )
    page.reload()
    expect(page.locator("#account-name")).to_have_text("web:sam")
    expect(page.get_by_label("Message", exact=True)).to_have_value(
        "Keep this draft with its original contributor."
    )


def test_mobile_reading_and_composer_have_no_horizontal_overflow(page, bbs):
    page.set_viewport_size({"width": 390, "height": 844})
    for path in (
        "/",
        "/boards/general",
        "/posts/" + bbs.seeds["meshcore"].post_id,
        "/threads/" + bbs.seeds["meshcore"].post_id,
        "/new/general",
        "/connect",
    ):
        page.goto(bbs.base_url + path)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), path
    sign_in(page, bbs)
    page.goto(bbs.base_url + "/new/general")
    page.get_by_label("Title", exact=True).fill("Mobile field report")
    page.get_by_label("Message", exact=True).fill("A" * 1000)
    page.get_by_role("button", name="Preview", exact=True).click()
    expect(page.locator("#post-preview")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_public_reading_and_protocol_help_work_without_javascript(browser, bbs):
    context = browser.new_context(java_script_enabled=False)
    try:
        page = context.new_page()
        page.set_default_timeout(5000)
        page.goto(bbs.base_url + "/boards/general")
        page.get_by_role("link", name="LXMF workshop", exact=True).click()
        expect(page.locator(".post-body")).to_contain_text("Build an offline reading station.")
        page.get_by_role("link", name="Permalink", exact=True).click()
        page.get_by_role("link", name="Thread", exact=True).click()
        expect(page.locator("article")).to_contain_text("LXMF workshop")
        page.get_by_role("link", name="Connect over a mesh", exact=True).click()
        expect(page.locator("main")).to_contain_text("From MeshCore or Meshtastic")
        expect(page.locator("main")).to_contain_text("DM help to see a numbered menu")
        expect(page.get_by_role("button", name="Sign in to post", exact=True)).not_to_be_visible()
    finally:
        context.close()


def test_contributor_creates_board_and_posts_while_news_stays_read_only(page, bbs):
    page.goto(bbs.base_url + "/new-board")
    sign_in(page, bbs)
    page.get_by_label("Board name", exact=True).fill("trail-reports")
    page.get_by_role("button", name="Create board", exact=True).click()
    page.wait_for_url("**/new/trail-reports")
    page.get_by_label("Title", exact=True).fill("Saturday hike")
    page.get_by_label("Message", exact=True).fill("Meet at nine.")
    page.get_by_role("button", name="Publish post", exact=True).click()
    post = saved_post(page, bbs)
    assert post.board == "trail-reports" and post.author == "web:sam"
    page.goto(bbs.base_url + "/boards/news")
    expect(page.get_by_role("link", name="New post", exact=True)).to_have_count(0)
    page.goto(bbs.base_url + "/posts/" + bbs.seeds["issue"].post_id)
    expect(page.get_by_role("link", name="Reply", exact=True)).to_have_count(0)
