"""A wrong address should look like a wrong address, not like the app fell over.

BaseHTTPRequestHandler ships its own error page: a white Times-New-Roman document. Served
after a dark studio it reads as a crash, and it offers no way back. The handler now carries a
template of its own; this checks it is really wired in, that the %-substitution the base class
performs against it does not blow up, and that the escaping the base class relies on survives.
"""

import html
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _template():
    """Read the template out of the source; importing app.py starts far too much."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "app.py"), encoding="utf-8") as fh:
        src = fh.read()
    start = src.index("error_message_format = (")
    end = src.index("\n    )", start)
    return eval(src[start + len("error_message_format = "):end + len("\n    )")].strip())


TPL = _template()


class TheErrorPageBelongsToTheApp(unittest.TestCase):
    def test_the_handler_declares_one_at_all(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "app.py"), encoding="utf-8") as fh:
            src = fh.read()
        body = src[src.index("class Handler(BaseHTTPRequestHandler):"):][:2400]
        self.assertIn("error_message_format", body)
        self.assertIn('error_content_type = "text/html; charset=utf-8"', body)

    def test_it_substitutes_the_way_the_base_class_does_it(self):
        """send_error interpolates a dict of code/message/explain. A stray % would raise here."""
        page = TPL % {"code": 404, "message": "Not Found.",
                      "explain": "Nothing matches the given URI."}
        self.assertIn("404", page)
        self.assertIn("Not Found.", page)
        self.assertIn("Nothing matches the given URI.", page)

    def test_the_literal_percent_survived(self):
        """height:100% is written %% in the source; if it were not, the line above would raise."""
        page = TPL % {"code": 500, "message": "x", "explain": "y"}
        self.assertIn("height:100%;", page)
        self.assertNotIn("100%%", page)

    def test_it_is_dark_and_offers_the_way_back(self):
        page = TPL % {"code": 404, "message": "m", "explain": "e"}
        self.assertIn("#0e1015", page)                       # the studio ground
        self.assertIn("href='/'", page)
        self.assertNotIn("Times", page)

    def test_a_hostile_message_stays_text(self):
        """The base class escapes message and explain before it gets here; the template must not
        undo that by putting them anywhere but in element content."""
        page = TPL % {"code": 400, "message": html.escape("<script>x</script>", quote=False),
                      "explain": html.escape("a&b", quote=False)}
        self.assertNotIn("<script>x", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("a&amp;b", page)

    def test_it_is_small_enough_to_be_one_response(self):
        page = TPL % {"code": 404, "message": "m", "explain": "e"}
        self.assertLess(len(page), 4096)


if __name__ == "__main__":
    unittest.main()
