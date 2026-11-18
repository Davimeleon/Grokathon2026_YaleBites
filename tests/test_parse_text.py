from app.parse.text import collapse_ws, html_to_text


def test_entities_including_double_encoded_and_numeric():
    assert html_to_text("Mac &amp;amp; Cheese &#8212; &#x2014; caf&eacute;") == "Mac & Cheese — — café"


def test_nested_tags_and_block_breaks():
    raw = "<div><p>Free <b><i>pizza</i></b></p><p>at 6pm</p>line<br>break<br/>end</div>"
    assert html_to_text(raw) == "Free pizza at 6pm line break end"


def test_script_and_style_dropped():
    raw = "<style>p{color:red}</style><p>Snacks</p><script>alert('x')</script> provided"
    assert html_to_text(raw) == "Snacks provided"


def test_whitespace_collapse_crlf_nbsp_tabs():
    assert html_to_text("a\r\n\r\nb\t\tc&nbsp;&nbsp;d\xa0e  ") == "a b c d e"
    assert collapse_ws("  x \r\n y\xa0 ") == "x y"


def test_empty_and_none():
    assert html_to_text(None) is None
    assert html_to_text("   <p> </p> ") is None
    assert collapse_ws(None) is None
    assert collapse_ws(" \n ") is None


def test_plain_text_with_angle_brackets_survives():
    raw = "Use this link: < tinyurl.com/NHVgreenspace > to sign up"
    assert html_to_text(raw) == "Use this link: < tinyurl.com/NHVgreenspace > to sign up"
