# Copyright 2026 Molence Bio
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import unittest
from unittest.mock import patch

from mol.config.parser import parse_configs, parse_sys_args
from mol.config.types import ListValue

CONFIGS = {
    "sets": ListValue(["a", "b"]),
    "path": "default",
    "steps": 1,
}


def parse_argv(*argv):
    with patch("sys.argv", ["prog", *argv]):
        return parse_configs(CONFIGS, parse_sys_args())


class ConfigParserTest(unittest.TestCase):
    def test_values_survive_the_command_line(self):
        configs = parse_argv("--sets", "", "--path", "/data/my run", "--steps", "3")
        self.assertEqual(configs.sets, [])
        self.assertEqual(configs.path, "/data/my run")
        self.assertEqual(configs.steps, 3)

    def test_lists_split_on_commas(self):
        self.assertEqual(parse_argv("--sets", "x,y,z").sets, ["x", "y", "z"])

    def test_unset_values_keep_defaults(self):
        configs = parse_argv()
        self.assertEqual(configs.sets, ["a", "b"])
        self.assertEqual(configs.path, "default")


if __name__ == "__main__":
    unittest.main()
