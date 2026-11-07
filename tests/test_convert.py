import pytest

from unity_docs_scraper.convert import convert_page, slugify
from unity_docs_scraper.source import SourceError, normalize_version

API = "Documentation/en/ScriptReference/Rigidbody.AddForce.html"
MANUAL = "Documentation/en/Manual/Intro.html"


def wrap(inner: str) -> str:
    return f'<html><head><title>Unity - Scripting API: X</title></head><body><div id="content-wrap" class="content-wrap"><div class="content-block"><div class="content"><div class="section">{inner}</div></div></div></div></body></html>'


def test_member_page_signature_params_and_example():
    html = wrap(
        '<div class="mb20 clear"><h1 class="heading inherit"><a href="Rigidbody.html">Rigidbody</a>.AddForce</h1>'
        '<div class="suggest">Suggest a change</div></div>'
        '<div class="subsection"><div class="signature"><div class="signature-CS sig-block">'
        '<span style="color:red;"></span><h2>Declaration</h2>public void <span class="sig-kw">AddForce</span>'
        '(<a href="Vector3.html">Vector3</a> <span class="sig-kw">force</span>);</div></div></div>'
        '<div class="subsection"><h3>Parameters</h3><table class="list"><thead><tr><th>Parameter</th><th>Description</th></tr></thead>'
        '<tr><td class="name lbl">force</td><td class="desc">Force | vector.</td></tr></table></div>'
        '<div class="subsection"><h3>Description</h3><p>Adds a force to the <a href="Rigidbody.html">Rigidbody</a>.</p></div>'
        '<div class="subsection"><pre class="codeExampleCS">int a = 1;<br/><br/>Foo(<a href="Bar.html">Bar</a>);\n</pre></div>'
    )
    page = convert_page(html, API, "6000.0")
    assert page.title == "Rigidbody.AddForce"
    assert page.parent == "Rigidbody"
    assert page.kind == "member"
    assert "Suggest a change" not in page.body
    assert "```csharp\npublic void AddForce(Vector3 force);\n```" in page.body
    assert "| force | Force \\| vector. |" in page.body
    assert "[Rigidbody](unity-link:ScriptReference/Rigidbody)" in page.body
    assert "```csharp\nint a = 1;\n\nFoo(Bar);\n```" in page.body
    # member headings start at level 4 so they nest under "### Rigidbody.AddForce"
    assert "#### Parameters" in page.body and "#### Description" in page.body
    assert page.summary == "Adds a force to the Rigidbody."


def test_class_header_and_heading_levels():
    html = wrap(
        '<div class="mb20 clear"><h1>Rigidbody</h1><p class="cl">class in UnityEngine</p><p class="cl">/</p>'
        '<p class="cl">Inherits from:<a href="Component.html">Component</a></p></div>'
        '<h3>Description</h3><p>Hello.</p><h3>Properties</h3>'
    )
    page = convert_page(html, "Documentation/en/ScriptReference/Rigidbody.html", "6000.0")
    assert (page.kind, page.namespace) == ("class", "UnityEngine")
    assert page.header == "class in UnityEngine · Inherits from: [Component](unity-link:ScriptReference/Component)"
    assert page.body.startswith("## Description")  # h3 re-levelled to sit under the H1


def test_enum_and_blank_h1_member():
    enum = convert_page(wrap('<div class="mb20"><h1>ForceMode</h1><p class="cl">enumeration</p></div><p>x</p>'),
                        "Documentation/en/ScriptReference/ForceMode.html", "6000.0")
    assert enum.kind == "enum"
    blank = convert_page(
        '<html><head><title>Unity - Scripting API: DrawCameraMode.BakedLighting</title></head><body>'
        '<div id="content-wrap"><div class="content"><div class="mb20"><h1></h1></div><h3>Description</h3><p>Baked.</p></div></div></body></html>',
        "Documentation/en/ScriptReference/DrawCameraMode.BakedLighting.html", "6000.0")
    assert blank.title == "DrawCameraMode.BakedLighting"
    assert blank.kind == "member"
    assert blank.body.startswith("#### Description")


def test_manual_links_lists_and_images():
    html = wrap(
        '<h1>Intro</h1><p>See <a href="Other.html#frag">other</a>, <a href="../ScriptReference/Mesh.html">Mesh</a>, '
        '<a href="#here">here</a>, <a href="../uploads/a.png">img</a> and <a href="https://x.dev/">ext</a>.</p>'
        '<ul><li>one<ul><li>nested</li></ul></li><li><strong>two</strong> <em>x</em></li></ul>'
        '<img src="a.png" alt="A diagram"/><img src="b.png"/>'
    )
    page = convert_page(html, MANUAL, "2022.3")
    b = page.body
    assert "[other](unity-link:Manual/Other)" in b          # fragment dropped, placeholder
    assert "[Mesh](unity-link:ScriptReference/Mesh)" in b
    assert "here" in b and "(#here)" not in b                # same-page anchors are not links
    assert "(https://docs.unity3d.com/2022.3/Documentation/uploads/a.png)" in b
    assert "[ext](https://x.dev/)" in b
    assert "- one\n  - nested\n- **two** *x*" in b
    assert "*[image: A diagram]*" in b and "b.png" not in b
    assert page.kind == "page"


def test_pages_without_content_are_skipped():
    assert convert_page("<html><body>hi</body></html>", MANUAL, "6000.0") is None
    assert convert_page(wrap("<h1>x</h1><p>y</p>"), "Documentation/en/Manual/docdata/toc.html", "6000.0") is None
    assert convert_page(wrap("<h1>x</h1><p>y</p>"), "Documentation/en/uploads/x.html", "6000.0") is None


def test_slugify_matches_github():
    assert slugify("Rigidbody.AddForce") == "rigidbodyaddforce"
    assert slugify("UnityAction<T0,T1>") == "unityactiont0t1"


@pytest.mark.parametrize("raw,expected", [
    ("2022.3", "2022.3"), ("2022.3.10f1", "2022.3"), ("6000.0.23f1", "6000.0"), (" 6000.1 ", "6000.1"),
])
def test_normalize_version(raw, expected):
    assert normalize_version(raw) == expected


@pytest.mark.parametrize("raw", ["", "2022", "abc", "6000", "v6000.0"])
def test_normalize_version_rejects(raw):
    with pytest.raises(SourceError):
        normalize_version(raw)
