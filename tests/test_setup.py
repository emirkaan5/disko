"""The setup script's own lists, checked where they live (scripts/setup.sh).

The script refuses to run anywhere but an Apple Silicon Mac, so its venv list is checked as text
instead of by running it. That is also where it broke once: the list was a single string and the loop
read it unquoted, so the purposes' spaces split it and the word "app" became a venv name, which sent
uv looking for requirements/app.txt. Nothing else in the suite would have noticed, because no test
runs the script.
"""

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SETUP = REPO / "scripts" / "setup.sh"

# "live:.venv-live:the app" and the Spotify one appended the same way.
SPEC = re.compile(r'"([a-z]+):(\.venv-[a-z-]+):([^"]+)"')


class VenvListTest(unittest.TestCase):
    def setUp(self):
        self.text = SETUP.read_text()

    def test_every_spec_is_one_entry_whatever_its_purpose_says(self):
        said = 'for spec in "${VENVS[@]}";'
        self.assertIn(said, self.text)
        self.assertNotIn("for spec in $VENVS", self.text)  # unquoted: the spaces split the entries

    def test_each_venv_asks_for_a_requirements_file_that_exists(self):
        specs = SPEC.findall(self.text)
        self.assertGreaterEqual(len(specs), 3, "the venv list isn't being read at all")
        for name, venv, purpose in specs:
            self.assertTrue(venv.startswith(".venv-"), f"{name}'s venv is {venv}")
            self.assertTrue(purpose.strip(), f"{name} has no purpose to print")
            self.assertTrue((REPO / "requirements" / f"{name}.txt").exists(),
                            f"requirements/{name}.txt is missing but setup.sh installs it")

    def test_the_audio_helper_is_still_only_built_when_asked_for(self):
        self.assertIn('if [ "$SPOTIFY" = 1 ]; then', self.text)
        self.assertIn("--spotify-audio) SPOTIFY=1", self.text)
        self.assertIn("spotify.txt", self.text)  # and the file it needs


if __name__ == "__main__":
    unittest.main()
