"""Vara Voice's behaviour, with a stand-in recognizer that "hears" scripted phrases (Vosk and a
microphone aren't needed): wake words, requests, interrupting, answering approvals by voice."""

import json
import unittest

from polyos import vara_voice
from polyos.vara_voice import Assistant, after_wake, has_words, speakable


class FakeRecognizer:
    """Each audio chunk is b"text" (still speaking) or b"text|" (the phrase ended)."""

    def __init__(self, model, rate, grammar=None):
        self.grammar = json.loads(grammar) if grammar else None
        self.text = ""

    def AcceptWaveform(self, chunk):
        raw = chunk.decode()
        self.text = raw.rstrip("|")
        if self.grammar is not None:  # a grammar only hears its own words
            self.text = " ".join(w for w in self.text.split() if any(w in g.split() for g in self.grammar))
        return raw.endswith("|")

    def Result(self):
        text, self.text = self.text, ""
        return json.dumps({"text": text})

    FinalResult = Result

    def PartialResult(self):
        return json.dumps({"partial": self.text})

    def Reset(self):
        self.text = ""


class FakeShell:
    def __init__(self):
        self.calls = []

    def ask(self, text):
        self.calls.append(("ask", text))

    def stop(self):
        self.calls.append(("stop",))

    def approve(self, step, decision):
        self.calls.append(("approve", step, decision))

    def status(self, state, text=""):
        self.calls.append(("status", state))

    def said(self, after):
        return {"items": [], "last": after}


class FakeSpeaker:
    def __init__(self):
        self.said, self.current, self.stopped, self.chimes = [], "", 0, 0

    @property
    def speaking(self):
        return bool(self.current)

    def say(self, text):
        self.said.append(speakable(text))

    def stop(self):
        self.stopped += 1
        self.current = ""

    def play(self, raw, rate=22050):
        self.chimes += 1


class VoiceTests(unittest.TestCase):
    def setUp(self):
        self.shell, self.speaker = FakeShell(), FakeSpeaker()
        self.a = Assistant(self.shell, self.speaker, object(), FakeRecognizer)

    def hear(self, *chunks):
        for chunk in chunks:
            self.a.feed(chunk.encode())

    def asked(self):
        return [c[1] for c in self.shell.calls if c[0] == "ask"]

    def test_hey_vera_then_a_request(self):
        self.hear("hey vera|")
        self.assertEqual(self.a.state, "listening")
        self.assertEqual(self.speaker.chimes, 1)
        self.hear("open", "open firefox|")
        self.assertEqual(self.asked(), ["open firefox"])
        self.assertEqual(self.a.state, "waiting")
        self.a.announced({"kind": "reply", "text": "Opening **Firefox**."})
        self.assertEqual(self.speaker.said, ["Opening Firefox."])
        self.assertEqual(self.a.state, "idle")

    def test_other_talk_is_ignored(self):
        self.hear("what a nice day|", "open firefox|")
        self.assertEqual(self.a.state, "idle")
        self.assertEqual(self.asked(), [])

    def test_nothing_said_after_the_wake_word(self):
        self.hear("vera|", "|")
        self.assertEqual(self.a.state, "idle")
        self.assertEqual(self.asked(), [])

    def test_stop_interrupts(self):
        self.hear("hey vera|", "tell me a long story|")
        self.speaker.current = "once upon a time"
        self.hear("stop|")
        self.assertIn(("stop",), self.shell.calls)
        self.assertEqual(self.speaker.stopped, 2)  # once for the wake chime, once for stop
        self.assertEqual(self.a.state, "idle")

    def test_its_own_words_are_not_commands(self):
        self.speaker.current = "i'll stop the timer, vera out"  # the microphone hears Vara say this
        self.hear("stop|", "vera|")
        self.assertNotIn(("stop",), self.shell.calls)
        self.assertEqual(self.a.state, "idle")
        self.speaker.current = "i'll stop the timer"
        self.hear("vera stop|")  # but "Vera, stop" from the person always works
        self.assertIn(("stop",), self.shell.calls)

    def test_approval_by_voice(self):
        self.a.announced({"kind": "approval", "text": "Can I run a command: make? Say yes or no.", "step": "s1"})
        self.assertEqual(self.a.state, "confirming")
        self.hear("um|", "yes please|")
        self.assertIn(("approve", "s1", "allow"), self.shell.calls)
        self.a.announced({"kind": "approval", "text": "Can I write a file?", "step": "s2"})
        self.hear("no|")
        self.assertIn(("approve", "s2", "deny"), self.shell.calls)

    def test_push_to_talk(self):
        self.a.announced({"kind": "listen", "text": ""})
        self.assertEqual(self.a.state, "listening")

    def test_quiet_mode_shows_but_doesnt_speak(self):
        a = Assistant(self.shell, self.speaker, object(), FakeRecognizer, speak_replies=False)
        a.announced({"kind": "reply", "text": "Done."})
        a.announced({"kind": "reminder", "text": "Reminder: stretch"})
        self.assertEqual(self.speaker.said, [])

    def test_words(self):
        self.assertEqual(after_wake("hey vera open the browser"), "open the browser")
        self.assertTrue(has_words("okay vera stop", vara_voice.STOP))
        self.assertFalse(has_words("stopwatch", vara_voice.STOP))
        said = speakable("Here's the script:\n```python\nprint(1)\n```\nSee [the docs](https://x.org) or https://y.org.")
        self.assertEqual(said, "Here's the script: (The code is on screen.) See the docs or the link on screen")
        self.assertEqual(speakable("Saved *notes_2.txt* in __Projects__."), "Saved notes_2.txt in Projects.")
        long = speakable("One sentence here. " * 60)
        self.assertTrue(long.endswith("The rest is on screen."))
        self.assertLess(len(long), 660)


if __name__ == "__main__":
    unittest.main()
