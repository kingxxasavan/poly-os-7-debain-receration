import json
import struct
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from polyos import vara_tools
from polyos.core import ApiError, EventBus, Settings
from polyos.mock import MockBackend
from polyos.vara import match_app
from polyos.vara_skills import Memory, Skills
from polyos.vara_tools import READ, RUN, WRITE, ToolContext, ToolError


class FakeModel:
    """An OpenAI-compatible /chat/completions that answers from a script and records requests."""

    def __init__(self):
        self.replies: list = []
        self.requests: list[dict] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append(body)
                reply = fake.replies.pop(0) if fake.replies else {"content": "Done."}
                if callable(reply):
                    reply = reply(body)
                status, payload = (reply if isinstance(reply, tuple)
                                   else (200, {"choices": [{"message": {"role": "assistant", **reply}}]}))
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def call(tool_name, n=1, **args):
    return {"content": "", "tool_calls": [{"id": f"call{n}", "type": "function",
                                           "function": {"name": tool_name, "arguments": json.dumps(args)}}]}


class VaraTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.backend = MockBackend(Settings(root / "settings.json"), EventBus(), home=root / "home")
        self.home = self.backend.files.home
        self.vara = self.backend.vara
        # nothing listens on this port, so the model call fails fast
        self.vara.config.update("http://127.0.0.1:9", "test-model", "sk-secret")

    def tearDown(self):
        self.vara.stop()
        self.vara.wait(5)
        self.tmp.cleanup()

    def ask(self, text):
        self.vara.chat(self.backend, text)
        return self.vara.wait(20)

    def use_fake(self, *replies):
        fake = FakeModel()
        self.addCleanup(fake.close)
        fake.replies = list(replies)
        self.vara.config.update(fake.url, "test-model", "sk-secret")
        return fake

    def wait_for(self, test, timeout=10):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            state = self.vara.state()
            if test(state):
                return state
            time.sleep(0.02)
        self.fail(f"timed out; state: {self.vara.state()}")

    def test_local_actions(self):
        self.assertIn("40%", self.ask("set volume to 40")["history"][-1]["content"])
        self.assertEqual(self.backend.system_status()["volume"]["level"], 40)
        self.assertEqual(self.ask("mute")["history"][-1]["content"], "Muted.")
        self.assertTrue(self.backend.system_status()["volume"]["muted"])
        self.assertEqual(self.ask("open chrome")["history"][-1]["content"], "Opening Google Chrome.")
        self.assertTrue(any(w["appId"] == "google-chrome.desktop" for w in self.backend.windows()))
        self.assertIn("off", self.ask("turn wifi off")["history"][-1]["content"])
        self.assertFalse(self.backend.system_status()["network"]["wifiEnabled"])
        self.assertTrue(self.ask("what's the time?")["history"][-1]["content"].startswith("It's"))

    def test_pronouns_are_not_apps(self):
        self.assertIsNone(match_app(self.backend.apps(), "it"))
        self.assertEqual(match_app(self.backend.apps(), "writer")["name"], "LibreOffice Writer")
        state = self.ask("run it")  # goes to the model (unreachable here), doesn't open LibreOffice Writer
        self.assertTrue(state["history"][-1].get("error"))
        self.assertFalse(any("Writer" in w["title"] for w in self.backend.windows()))

    def test_model_unreachable_is_a_friendly_error(self):
        state = self.ask("write me a poem about Debian")
        self.assertFalse(state["busy"])
        self.assertTrue(state["history"][-1]["error"])
        self.assertIn("couldn't reach", state["history"][-1]["content"])

    def test_key_is_never_returned(self):
        public = self.vara.config.public()
        self.assertNotIn("apiKey", public)
        self.assertTrue(public["hasKey"])
        self.assertNotIn("sk-secret", json.dumps(public))
        self.assertEqual(public["approval"], "ask")
        self.assertEqual(self.vara.reset()["history"], [])

    def test_config_checks(self):
        with self.assertRaises(ApiError):
            self.vara.config.update(None, None, None, approval="yolo")
        with self.assertRaises(ApiError):
            self.vara.config.update(None, None, None, workspace="Projects")
        cfg = self.vara.config.update(None, None, None, workspace="~/robots", approval="workspace")
        self.assertEqual((cfg["workspace"], cfg["approval"]), ("~/robots", "workspace"))
        self.assertEqual(self.vara.workspace(), self.home / "robots")

    def test_agent_uses_tools_then_answers(self):
        (self.home / "Projects").mkdir(exist_ok=True)
        (self.home / "Projects" / "robot.py").write_text("print('beep')\n")
        fake = self.use_fake(call("list_files", path="."), {"content": "You have robot.py."})
        state = self.ask("what's in my projects folder?")
        self.assertEqual(state["history"][-1]["content"], "You have robot.py.")
        step = next(i for i in state["history"] if i["role"] == "step")
        self.assertEqual((step["tool"], step["status"]), ("list_files", "done"))
        self.assertIn("robot.py", step["output"])
        first, second = fake.requests
        names = {t["function"]["name"] for t in first["tools"]}
        self.assertTrue({"read_file", "write_file", "run_command", "remember", "load_skill"} <= names)
        system = first["messages"][0]["content"]
        self.assertIn("Workspace (default project folder)", system)
        self.assertIn("- openscad:", system)  # built-in skills are listed
        tool_msg = second["messages"][-1]
        self.assertEqual((tool_msg["role"], tool_msg["tool_call_id"]), ("tool", "call1"))
        self.assertIn("robot.py", tool_msg["content"])
        self.assertEqual(second["messages"][-2]["tool_calls"][0]["id"], "call1")

    def test_changes_wait_for_approval(self):
        fake = self.use_fake(call("write_file", path="hello.py", content="print('hi')\n"), {"content": "Wrote it."})
        self.vara.chat(self.backend, "make hello.py")
        state = self.wait_for(lambda s: s["pending"])
        self.assertEqual(state["pending"]["tool"], "write_file")
        self.assertIn("print('hi')", state["pending"]["detail"])
        target = self.home / "Projects" / "hello.py"
        self.assertFalse(target.exists())
        with self.assertRaises(ApiError):
            self.vara.approve("wrong-id", "allow")
        self.vara.approve(state["pending"]["id"], "allow")
        state = self.vara.wait(10)
        self.assertEqual(target.read_text(), "print('hi')\n")
        self.assertEqual(state["history"][-1]["content"], "Wrote it.")
        self.assertEqual(len(fake.requests), 2)

    def test_denied_actions_are_not_run(self):
        fake = self.use_fake(call("run_command", command="touch ~/boom"), {"content": "Okay, I won't."})
        self.vara.chat(self.backend, "run it")
        state = self.wait_for(lambda s: s["pending"])
        self.vara.approve(state["pending"]["id"], "deny")
        state = self.vara.wait(10)
        self.assertFalse((self.home / "boom").exists())
        step = next(i for i in state["history"] if i["role"] == "step")
        self.assertEqual(step["status"], "denied")
        self.assertIn("declined", fake.requests[1]["messages"][-1]["content"])

    def test_always_allow_lasts_for_the_chat(self):
        self.use_fake(call("write_file", 1, path="a.txt", content="a"), call("write_file", 2, path="b.txt", content="b"),
                      {"content": "Both written."})
        self.vara.chat(self.backend, "write two files")
        state = self.wait_for(lambda s: s["pending"])
        self.vara.approve(state["pending"]["id"], "always")
        state = self.vara.wait(10)
        self.assertEqual(state["history"][-1]["content"], "Both written.")
        self.assertTrue((self.home / "Projects" / "b.txt").exists())
        self.vara.reset()
        self.assertEqual(self.vara.allowed, set())

    def test_workspace_mode_edits_the_workspace_freely(self):
        self.vara.config.update(None, None, None, approval="workspace")
        self.use_fake(call("write_file", path="in.txt", content="x"), call("write_file", 2, path="~/outside.txt", content="y"),
                      {"content": "Done."})
        self.vara.chat(self.backend, "write files")
        state = self.wait_for(lambda s: s["pending"])  # the second one, outside the workspace, asks
        self.assertTrue((self.home / "Projects" / "in.txt").exists())
        self.assertIn("outside.txt", state["pending"]["title"])
        self.vara.stop()
        state = self.vara.wait(10)
        self.assertFalse(state["busy"])
        self.assertFalse((self.home / "outside.txt").exists())

    def test_stop_while_waiting(self):
        self.use_fake(call("run_command", command="echo hi"))
        self.vara.chat(self.backend, "go")
        self.wait_for(lambda s: s["pending"])
        with self.assertRaises(ApiError):  # one request at a time
            self.vara.chat(self.backend, "another")
        self.vara.stop()
        state = self.vara.wait(10)
        self.assertFalse(state["busy"])
        self.assertIsNone(state["pending"])

    def test_models_without_tools_still_chat(self):
        fake = self.use_fake(lambda body: (400, {"error": {"message": "test-model does not support tools"}}),
                             {"content": "Hello!"})
        state = self.ask("hi there")
        self.assertEqual(state["history"][-1]["content"], "Hello!")
        self.assertNotIn("tools", fake.requests[1])

    def test_reasoning_and_bad_tool_calls(self):
        self.use_fake({"content": "", "reasoning": "The user wants X.", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "no_such_tool", "arguments": "{}"}},
            {"id": "c2", "type": "function", "function": {"name": "read_file", "arguments": "{not json"}}]},
            {"content": "Sorted."})
        state = self.ask("do something")
        roles = [i["role"] for i in state["history"]]
        self.assertIn("thought", roles)
        steps = [i for i in state["history"] if i["role"] == "step"]
        self.assertEqual([s["status"] for s in steps], ["failed", "failed"])
        self.assertEqual(state["history"][-1]["content"], "Sorted.")

    def test_memory_and_skills_tools(self):
        self.use_fake(call("remember", note="Uses an Arduino Nano on /dev/ttyUSB0"),
                      call("load_skill", 2, name="arduino"), {"content": "Noted."})
        state = self.ask("remember my board")
        self.assertEqual(self.vara.memory.notes()[0]["note"], "Uses an Arduino Nano on /dev/ttyUSB0")
        skill_step = [i for i in state["history"] if i["role"] == "step"][1]
        self.assertIn("arduino-cli", skill_step["output"])


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        (self.home / "work").mkdir(parents=True)
        self.ctx = ToolContext(home=self.home, workspace=self.home / "work")

    def tearDown(self):
        self.tmp.cleanup()

    def run_tool(self, name, **args):
        return vara_tools.TOOLS[name].run(self.ctx, args)

    def test_private_files_are_refused(self):
        (self.home / ".ssh").mkdir()
        (self.home / ".ssh" / "id_ed25519").write_text("KEY")
        (self.home / ".config" / "polyos").mkdir(parents=True)
        (self.home / ".config" / "polyos" / "vara.json").write_text('{"apiKey": "sk"}')
        for path in ("~/.ssh/id_ed25519", "../.ssh/id_ed25519", "~/.config/polyos/vara.json"):
            with self.assertRaises(ToolError):
                self.run_tool("read_file", path=path.replace("~", str(self.home)))
        (self.home / "work" / "link").symlink_to(self.home / ".ssh")
        with self.assertRaises(ToolError):
            self.run_tool("read_file", path="link/id_ed25519")
        with self.assertRaises(ToolError):
            self.run_tool("write_file", path="/etc/polyos-test", content="x")
        self.assertNotIn("KEY", self.run_tool("search_files", query="KEY", path=str(self.home)))

    def test_files(self):
        self.assertIn("Created", self.run_tool("write_file", path="src/app.py", content="a = 1\nb = 2\na = 1\n"))
        self.assertIn("    2  b = 2", self.run_tool("read_file", path="src/app.py"))
        with self.assertRaises(ToolError):  # two matches
            self.run_tool("edit_file", path="src/app.py", old_text="a = 1", new_text="a = 3")
        self.run_tool("edit_file", path="src/app.py", old_text="b = 2", new_text="b = 5")
        self.assertIn("src/app.py:2: b = 5", self.run_tool("search_files", query=r"b = \d"))
        self.assertIn("src/", self.run_tool("list_files"))

    def test_commands(self):
        out = self.run_tool("run_command", command="echo hello && exit 3")
        self.assertTrue(out.startswith("exit code 3"))
        self.assertIn("hello", out)
        start = time.monotonic()
        out = self.run_tool("run_command", command="sleep 30", timeout=1)
        self.assertLess(time.monotonic() - start, 10)
        self.assertIn("stopped after 1 s", out)
        self.ctx.cancel.set()
        self.assertIn("stopped by the person", self.run_tool("run_command", command="sleep 30"))

    def test_risk_levels(self):
        tools = vara_tools.TOOLS
        self.assertEqual(tools["git"].risk_for({"args": "status"}), READ)
        self.assertEqual(tools["git"].risk_for({"args": "log --oneline -5"}), READ)
        self.assertEqual(tools["git"].risk_for({"args": "branch -a"}), READ)
        self.assertEqual(tools["git"].risk_for({"args": "branch -D main"}), RUN)
        self.assertEqual(tools["git"].risk_for({"args": "diff --output=/tmp/x"}), RUN)
        self.assertEqual(tools["git"].risk_for({"args": "push --force"}), RUN)
        self.assertEqual(tools["ros2"].risk_for({"args": "topic list"}), READ)
        self.assertEqual(tools["ros2"].risk_for({"args": "topic echo /odom --once"}), READ)
        self.assertEqual(tools["ros2"].risk_for({"args": "topic pub -1 /cmd_vel geometry_msgs/msg/Twist {}"}), RUN)
        self.assertEqual(tools["ros2"].risk_for({"args": "launch nav2_bringup nav.py"}), RUN)
        self.assertEqual(tools["arduino"].risk_for({"args": "board list"}), READ)
        self.assertEqual(tools["arduino"].risk_for({"args": "compile --fqbn arduino:avr:uno blink"}), WRITE)
        self.assertEqual(tools["arduino"].risk_for({"args": "compile -u -p /dev/ttyACM0 blink"}), RUN)
        self.assertEqual(tools["arduino"].risk_for({"args": "upload -p /dev/ttyACM0"}), RUN)
        self.assertEqual(tools["write_file"].risk_for({}), WRITE)
        self.assertEqual(tools["read_file"].risk_for({}), READ)

    def test_model_info(self):
        tri = [(0, 0, 0), (20, 0, 0), (0, 10, 5)]
        binary = self.home / "work" / "part.stl"
        facets = b"".join(struct.pack("<12fH", 0, 0, 1, *[c for v in tri for c in v], 0) for _ in range(2))
        binary.write_bytes(b"\0" * 80 + struct.pack("<I", 2) + facets)
        self.assertIn("20.0 × 10.0 × 5.0", self.run_tool("model_info", path="part.stl"))
        text = self.home / "work" / "cube.stl"
        text.write_text("solid c\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 4 0 0\nvertex 0 4 2\n"
                        "endloop\nendfacet\nendsolid c\n")
        self.assertEqual(vara_tools.mesh_stats(text)["size"], [4.0, 4.0, 2.0])
        with self.assertRaises(ToolError):
            self.run_tool("model_info", path="missing.stl")


class SkillMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.builtin = root / "builtin"
        (self.builtin / "blender").mkdir(parents=True)
        (self.builtin / "blender" / "SKILL.md").write_text("---\nname: blender\ndescription: Built-in\n---\nBuilt-in body")
        (self.builtin / "git.md").write_text("---\ndescription: Git things\n---\nUse git.")
        self.skills = Skills(self.builtin, root / "user")
        self.memory = Memory(root / "memory.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_skills(self):
        self.assertEqual([(s["name"], s["own"]) for s in self.skills.list()], [("blender", False), ("git", False)])
        self.assertEqual(self.skills.load("git"), "Use git.")
        self.skills.save("blender", "My way", "My steps")
        self.assertEqual(self.skills.load("blender").strip(), "My steps")
        self.assertTrue(next(s for s in self.skills.list() if s["name"] == "blender")["own"])
        for bad in ("../evil", "Has Spaces", ""):
            with self.assertRaises(ApiError):
                self.skills.save(bad, "d", "b")
        with self.assertRaises(ApiError):
            self.skills.load("nope")

    def test_shipped_skills_parse(self):
        from polyos import paths

        shipped = Skills(paths.ROOT / "data/vara/skills", Path(self.tmp.name) / "none")
        names = {s["name"]: s["description"] for s in shipped.list()}
        self.assertTrue({"blender", "openscad", "3d-printing", "ros2", "arduino", "git", "python-project", "freecad"} <= set(names))
        self.assertTrue(all(names.values()))

    def test_memory(self):
        self.memory.add("Prints with PLA on a Prusa MK4")
        self.memory.add("prints with pla on a prusa mk4")  # same note: kept once
        self.memory.add("Projects live in ~/robots")
        self.assertEqual(len(self.memory.notes()), 2)
        self.assertEqual(self.memory.forget(0)[0]["note"], "Projects live in ~/robots")
        self.assertEqual(self.memory.forget(), [])
        with self.assertRaises(ApiError):
            self.memory.add("   ")


if __name__ == "__main__":
    unittest.main()


class ClaudeTests(unittest.TestCase):
    """The Claude provider: conversation conversion, and a whole run against a fake Messages API."""

    def test_conversation_conversion(self):
        from polyos import vara_claude

        transcript = [
            {"role": "user", "content": "list my files"},
            {"role": "assistant", "content": "Looking.", "tool_calls": [
                {"id": "a", "type": "function", "function": {"name": "list_files", "arguments": "{}"}},
                {"id": "b", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "x"}'}}]},
            {"role": "tool", "tool_call_id": "a", "content": "x.py"},
            {"role": "tool", "tool_call_id": "b", "content": "Error: not found"},
            {"role": "assistant", "content": "", "_claude": [{"type": "thinking", "thinking": "t", "signature": "s"},
                                                            {"type": "text", "text": "Done."}]},
        ]
        msgs = vara_claude.to_messages(transcript)
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "user", "assistant"])
        self.assertEqual([b["type"] for b in msgs[1]["content"]], ["text", "tool_use", "tool_use"])
        self.assertEqual(msgs[1]["content"][2]["input"], {"path": "x"})
        results = msgs[2]["content"]  # both results in one user message
        self.assertEqual([(r["tool_use_id"], r.get("is_error", False)) for r in results], [("a", False), ("b", True)])
        self.assertEqual(msgs[3]["content"][0], {"type": "thinking", "thinking": "t", "signature": "s"})  # unchanged
        reply = vara_claude.from_blocks([{"type": "thinking", "thinking": "hmm", "signature": "s"},
                                         {"type": "text", "text": "Hi"},
                                         {"type": "tool_use", "id": "t1", "name": "remember", "input": {"note": "n"}}])
        self.assertEqual((reply["content"], reply["reasoning"]), ("Hi", "hmm"))
        self.assertEqual(json.loads(reply["tool_calls"][0]["function"]["arguments"]), {"note": "n"})
        tools = vara_claude.to_tools([vara_tools.TOOLS["read_file"].schema()])
        self.assertEqual(tools[0]["name"], "read_file")
        self.assertIn("path", tools[0]["input_schema"]["properties"])

    def test_a_run_against_a_fake_messages_api(self):
        try:
            import anthropic  # noqa: F401
        except ImportError:
            self.skipTest("Anthropic's library isn't installed here")
        requests, headers = [], []
        replies = [
            {"content": [{"type": "thinking", "thinking": "Check the workspace first.", "signature": "sig1"},
                         {"type": "tool_use", "id": "toolu_1", "name": "list_files", "input": {"path": "."}}],
             "stop_reason": "tool_use"},
            {"content": [{"type": "text", "text": "Your workspace has robot.py."}], "stop_reason": "end_turn"},
        ]

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                headers.append({k.lower(): v for k, v in self.headers.items()})
                reply = replies.pop(0)
                data = json.dumps({"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5",
                                   "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 5}, **reply}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = MockBackend(Settings(root / "settings.json"), EventBus(), home=root / "home")
            (backend.files.home / "Projects").mkdir(exist_ok=True)
            (backend.files.home / "Projects" / "robot.py").write_text("print(1)\n")
            backend.vara.config.update(f"http://127.0.0.1:{server.server_address[1]}", "claude-opus-5", "sk-ant-test",
                                       provider="claude")
            backend.vara.chat(backend, "what's in my workspace?")
            state = backend.vara.wait(30)
        self.assertEqual(state["history"][-1]["content"], "Your workspace has robot.py.")
        self.assertIn("thought", [i["role"] for i in state["history"]])
        first, second = requests
        self.assertEqual(first["model"], "claude-opus-5")
        self.assertEqual(first["fallbacks"], "default")
        self.assertEqual(first["thinking"], {"type": "adaptive", "display": "summarized"})
        self.assertIn("server-side-fallback-2026-07-01", headers[0].get("anthropic-beta", ""))
        self.assertEqual(headers[0].get("x-api-key"), "sk-ant-test")
        self.assertIn("Workspace (default project folder)", first["system"])
        self.assertTrue(any(t["name"] == "list_files" for t in first["tools"]))
        replayed = second["messages"][-2]["content"]  # the thinking block goes back unchanged
        self.assertEqual(replayed[0], {"type": "thinking", "thinking": "Check the workspace first.", "signature": "sig1"})
        result = second["messages"][-1]["content"][0]
        self.assertEqual((result["type"], result["tool_use_id"]), ("tool_result", "toolu_1"))
        self.assertIn("robot.py", result["content"])
