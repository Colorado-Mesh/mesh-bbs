from contextlib import closing
from html.parser import HTMLParser

import pytest

from mesh_bbs import micron
from mesh_bbs.commands import CommandService
from mesh_bbs.store import Store
from mesh_bbs.views import Views


class Document(HTMLParser):
    def __init__(self, value):
        super().__init__()
        self.elements = []
        self.feed(value)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


def test_plain_posts_keep_their_backticks_and_headings():
    value = ">A heading\n`!Not bold`!\n<script>literal</script>\n`="
    assert micron.plain(value) == value
    assert "<strong>" not in micron.html(value)
    assert "&lt;script&gt;" in micron.html(value)
    assert micron.nomad(value) == micron.literal(value)


def test_shared_formatting_colors_links_and_reset():
    value = micron.MARKER + (
        ">Trip report\n`c`!Ready`! and `*steady`* and `_go`_\n"
        "`Ff80Orange`f `BT112233Background`b\n"
        "`[Website`https://example.org/news]\n``Normal\n-\n"
        "`=\n>Literal `!text`!\n\\`=\n`=\nAfter"
    )
    rendered = micron.html(value)
    assert '<h3 class="micron-line micron-left">Trip report</h3>' in rendered
    assert "<strong>Ready</strong>" in rendered
    assert "<em>steady</em>" in rendered
    assert "<u>go</u>" in rendered
    assert "color:#f80" in micron.stylesheet(rendered)
    assert "background-color:#112233" in micron.stylesheet(rendered)
    assert 'href="https://example.org/news"' in rendered
    assert "micron-center" in rendered
    plain = micron.plain(value)
    assert "Website (https://example.org/news)" in plain
    assert ">Literal `!text`!\n`=\nAfter" in plain
    assert "`F" not in plain and "`*" not in plain
    wire = micron.nomad(value)
    assert "`Ff80Orange" in wire
    assert wire.endswith("<``\n")
    assert "\\`!text\\`!" in wire


@pytest.mark.parametrize(
    "source",
    [
        "<script>alert(1)</script>",
        "`[Click`javascript:alert(1)]",
        "`[Click`data:text/html,evil]",
        "`[Click`file:///etc/passwd]",
        "`[Send`:/page/post.mu`body=stolen]",
        "`<20|secret`password>",
        "`{aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:/page/index.mu`1}",
        "#!refresh=1",
        "`Ffoo;color:red;position:fixed",
        "`BTffffff;url(https://example.org/track)",
        "`[<img src=x onerror=alert(1)>`https://example.org]",
    ],
)
def test_untrusted_markup_has_no_active_controls(source):
    value = micron.MARKER + source
    elements = Document(micron.html(value)).elements
    assert all(tag in {"div", "span", "a"} for tag, _ in elements)
    for _, attrs in elements:
        assert not any(key.startswith("on") for key in attrs)
        if "href" in attrs:
            assert attrs["href"].startswith("https://example.org")
        if "style" in attrs:
            for declaration in attrs["style"].split(";"):
                prop, color = declaration.split(":")
                assert prop in {"color", "background-color"}
                assert color.startswith("#") and len(color) in {4, 7}
                int(color[1:], 16)
    wire = micron.nomad(value)
    assert not any(line.startswith(("#!", "`{", "`<", "`=")) for line in wire.splitlines())
    assert wire.endswith("<``\n")


def test_links_with_fields_or_ambiguous_delimiters_stay_literal():
    for url in [
        "https://example.org`x=y",
        "https://example.org/|x",
        "//example.org",
        "https://u:p@example.org",
    ]:
        value = micron.MARKER + "`[label`" + url + "]"
        assert "<a " not in micron.html(value)
        assert "\\`[" in micron.nomad(value)


def test_escaped_controls_and_formatting_across_lines():
    value = micron.MARKER + "\\>text\n\\#text\n\\-text\n\\`!literal\n`!bold\nstill bold\n``normal"
    assert micron.plain(value) == ">text\n#text\n-text\n`!literal\nbold\nstill bold\nnormal"
    assert "<strong>still bold</strong>" in micron.html(value)
    assert "<strong>normal</strong>" not in micron.html(value)


def test_formatting_is_bounded_and_cannot_swallow_host_footer():
    value = micron.MARKER + ("`!x" * 12000)
    assert len(micron.html(value)) < 200000
    assert len(micron.nomad(value).encode()) < 240 * 1024
    for suffix in ["`!", "`=", "`B000", "`c", ">heading"]:
        wire = micron.nomad(micron.MARKER + suffix)
        assert wire.endswith("<``\n")


@pytest.mark.parametrize("protocol", ["meshcore", "meshtastic", "lxmf", "packet"])
def test_protocol_reads_plain_text_while_signed_body_keeps_micron(tmp_path, protocol):
    store = Store(tmp_path / "bbs.db", "test")
    try:
        body = micron.MARKER + ">Meeting\n`!Bring a radio`!\n`[Details`https://example.org]"
        post = store.publish("web:alice", "formatted", "general", "Saturday", body)
        service = CommandService(store)
        response = service.handle(protocol + ":bob", "read " + post.post_id, max_bytes=4096)
        assert "Bring a radio" in response and "`!" not in response and "#!micron" not in response
        assert "https://example.org" in response
        assert store.get_post(post.post_id).body == body
        views = Views(store, "Test BBS")
        assert "<strong>Bring a radio</strong>" in views.html_post(post.post_id).decode()
        wire = views.page("/page/post.mu", {"var_id": post.post_id}).decode()
        assert "`!Bring a radio" in wire
        assert "<``\n`=\n Post ID:" in wire
    finally:
        store.close()


@pytest.mark.parametrize("protocol", ["meshcore", "meshtastic", "lxmf"])
def test_guided_writer_can_send_micron_marker_then_body(tmp_path, protocol):
    with closing(Store(tmp_path / "bbs.db", "test")) as store:
        service = CommandService(store)
        actor = protocol + ":author"
        for text in ["help", "3", "1", "Field notes", "#!micron", ">Trail\n`!Bring water`!"]:
            assert not service.handle(actor, text).startswith("Error:")
        preview = service.handle(actor, "done")
        assert "Bring water" in preview and "`!" not in preview
        assert "Posted to general" in service.handle(actor, "publish")
        post = store.list_posts("general")[0]
        assert post.body.startswith(micron.MARKER)
        assert "`!Bring water`!" in post.body
