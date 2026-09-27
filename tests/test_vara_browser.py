"""Vara's browser, for real: Playwright drives Chromium through a small local site. Needs a Python
with Playwright (POLYOS_VARA_PYTHON) and a Chromium (POLYOS_BROWSER); skipped otherwise."""

import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from polyos import vara_tools
from polyos.vara_tools import ToolContext, ToolError

PAGES = {
    "/": """<html><head><title>Robot Club</title></head><body><main><h1>Robot Club</h1>
        <p>Sign up for the Saturday workshop.</p>
        <form action="/done" method="get"><label>Name <input name="name" placeholder="Your name"></label>
        <select name="level" aria-label="Level"><option>Beginner</option><option>Expert</option></select>
        <label><input type="checkbox" name="news"> Newsletter</label>
        <button type="submit">Sign up</button></form>
        <a href="/schedule" target="_blank">Schedule</a></main></body></html>""",
    "/schedule": "<html><head><title>Schedule</title></head><body><main><p>Saturday 10:00: servos and sensors.</p></main></body></html>",
}


class Site(BaseHTTPRequestHandler):
    def do_GET(self):
        path, _, query = self.path.partition("?")
        body = PAGES.get(path) or f"<html><head><title>Thanks</title></head><body><main><p>Signed up: {query}</p></main></body></html>"
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@unittest.skipUnless(os.environ.get("POLYOS_VARA_PYTHON") and os.environ.get("POLYOS_BROWSER"), "needs Playwright and Chromium")
class WebBrowserTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Site)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/"
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"HOME": self.tmp.name})
        self.env.start()
        self.ctx = ToolContext(home=Path(self.tmp.name), workspace=Path(self.tmp.name))

    def tearDown(self):
        vara_tools.WEB.close()
        self.env.stop()
        self.server.shutdown()
        self.tmp.cleanup()

    def web(self, **args):
        return vara_tools.TOOLS["web"].run(self.ctx, args)

    def test_fill_in_a_form_and_follow_a_link(self):
        self.assertTrue(vara_tools.web_available())
        page = self.web(action="open", url=self.url)
        self.assertIn("Page: Robot Club", page)
        self.assertIn("Sign up for the Saturday workshop.", page)
        lines = {line.split("] ", 1)[1].split(" “")[0] + " " + line.split("“")[1].split("”")[0]: line.split("]")[0][1:]
                 for line in page.splitlines() if line.startswith("[")}
        self.assertEqual(set(lines), {"textbox Name", "list Level", "checkbox Newsletter", "button Sign up", "link Schedule"})
        self.web(action="type", ref=int(lines["textbox Name"]), text="Savan")
        self.web(action="select", ref=int(lines["list Level"]), option="Expert")
        self.web(action="check", ref=int(lines["checkbox Newsletter"]), on=True)
        done = self.web(action="click", ref=int(lines["button Sign up"]))
        self.assertIn("Page: Thanks", done)
        self.assertIn("name=Savan&level=Expert&news=on", done)
        back = self.web(action="back")
        self.assertIn("Robot Club", back)
        ref = next(line.split("]")[0][1:] for line in back.splitlines() if "link “Schedule”" in line)
        schedule = self.web(action="click", ref=int(ref))  # opens in a new tab, which Vara follows
        self.assertIn("servos and sensors", schedule)
        self.assertIn("2 tabs", schedule)
        self.assertIn("(this one)", self.web(action="tabs"))
        shot = self.web(action="screenshot")
        self.assertTrue(Path(shot.split(": ", 1)[1]).is_file())

    def test_mistakes_are_explained(self):
        self.web(action="open", url=self.url)
        with self.assertRaises(ToolError) as caught:
            self.web(action="click", ref=99)
        self.assertIn("no [99]", str(caught.exception))
        with self.assertRaises(ToolError):
            self.web(action="click")

    def test_acting_needs_approval_but_reading_doesnt(self):
        tool = vara_tools.TOOLS["web"]
        self.assertEqual(tool.risk_for({"action": "open", "url": "x"}), "read")
        self.assertEqual(tool.risk_for({"action": "click", "ref": 1}), "write")


if __name__ == "__main__":
    unittest.main()
