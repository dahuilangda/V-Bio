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

"""A Kalign that does not run is an error, not a template hit to skip."""

import os
import pickle
import stat
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mol.data.template.template_featurizer import TemplateFeaturizer
from mol.data.template.template_utils import TemplateHitProcessor
from mol.data.tools.kalign import Kalign, KalignError

QUERY = "MKTAYIAKQRQISFVKSHFSRQ"


def put_on_path(directory: str, script: str) -> None:
    path = os.path.join(directory, "kalign")
    Path(path).write_text(script)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)


class KalignFailureTest(unittest.TestCase):
    def setUp(self):
        self.bin = tempfile.TemporaryDirectory()
        self.addCleanup(self.bin.cleanup)
        env = patch.dict(os.environ, {"PATH": self.bin.name})
        env.start()
        self.addCleanup(env.stop)

    def test_a_failing_run_reports_its_status_and_stderr(self):
        put_on_path(self.bin.name, "#!/bin/sh\necho 'cannot open input' >&2\nexit 3\n")
        with self.assertRaises(KalignError) as caught:
            Kalign().align([QUERY, QUERY[:-2]])
        self.assertIn("status 3", str(caught.exception))
        self.assertIn("cannot open input", str(caught.exception))

    def test_a_binary_that_cannot_execute_is_reported(self):
        put_on_path(self.bin.name, "not a program\n")
        with self.assertRaises(KalignError) as caught:
            Kalign().align([QUERY, QUERY[:-2]])
        self.assertIn("Could not run", str(caught.exception))

    def test_a_short_sequence_is_still_a_per_hit_rejection(self):
        put_on_path(self.bin.name, "#!/bin/sh\nexit 0\n")
        with self.assertRaises(ValueError):
            Kalign().align([QUERY, "MKT"])

    def test_kalign_never_reads_our_stdin(self):
        # An open pipe on stdin (ssh without -n, some schedulers) made every
        # template alignment hang; this kalign fails instead if it sees one.
        put_on_path(
            self.bin.name,
            "#!/bin/sh\n[ -p /dev/stdin ] && exit 9\n"
            'while [ $# -gt 0 ]; do case "$1" in -i) i=$2;; -o) o=$2;; esac; shift; done\n'
            '/bin/cp "$i" "$o"\n',
        )
        read_end, write_end = os.pipe()
        saved_stdin = os.dup(0)
        os.dup2(read_end, 0)
        try:
            aligned = Kalign().align([QUERY, QUERY])
        finally:
            os.dup2(saved_stdin, 0)
            for fd in (saved_stdin, read_end, write_end):
                os.close(fd)
        self.assertEqual(list(aligned), [QUERY, QUERY])

    def test_hit_processing_lets_a_kalign_failure_through(self):
        put_on_path(self.bin.name, "#!/bin/sh\nexit 1\n")
        with tempfile.TemporaryDirectory() as cache:
            parsed = SimpleNamespace(
                mmcif_object=SimpleNamespace(
                    header={"release_date": "2000-01-01"},
                    chain_to_seqres={"A": QUERY},
                )
            )
            with open(os.path.join(cache, "1abc_A.pkl"), "wb") as f:
                pickle.dump(parsed, f)
            processor = TemplateHitProcessor(mmcif_dir=cache, template_cache_dir=cache)
            with self.assertRaises(KalignError):
                processor.process(
                    QUERY,
                    SimpleNamespace(name="1abc_A"),
                    datetime(2100, 1, 1),
                    release_dates={},
                    obsolete_pdbs={},
                )

    def test_training_with_templates_needs_kalign_up_front(self):
        with self.assertRaises(KalignError):
            TemplateFeaturizer(
                enable_prot_template=True,
                release_dates_path="unused.json",
                obsolete_pdbs_path="unused.json",
            )


if __name__ == "__main__":
    unittest.main()
