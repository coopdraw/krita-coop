"""krita-build/mac/moccache.py (the result cache for Qt's moc) against a stand-in for moc."""

import os
import re
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "krita-build", "mac"))
import moccache  # noqa: E402


class FakeMoc:
    """Stands in for the moc process: output = the text of the header and of everything it
    includes with "..." (looked up next to the including file), plus a dependency file."""

    def __init__(self):
        self.calls = 0
        self.suffix = ""

    def _read(self, path, seen):
        seen.append(path)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        parts = [text]
        for name in re.findall(r'#include "([^"]+)"', text):
            inc = os.path.join(os.path.dirname(path), name)
            if os.path.exists(inc) and inc not in seen:
                parts.append(self._read(inc, seen))
        return "".join(parts)

    def _moc(self, args):
        self.calls += 1
        out = moccache.option_value(args, "-o")
        header = args[-1]
        seen = []
        text = self._read(header, seen)
        if "FAIL" in text:
            return 1, "", "error: FAIL\n"
        if "NOOUT" in text:
            return 0, "", "Note: No relevant classes found. No output generated.\n"
        if out:
            with open(out, "w", encoding="utf-8") as f:
                f.write("// moc output\n" + text.upper() + self.suffix)
            if "--output-dep-file" in args:
                dep = moccache.option_value(args, "--dep-file-path") or out + ".d"
                with open(dep, "w", encoding="utf-8") as f:
                    f.write("%s: %s\n" % (out.replace(" ", "\\ "), " \\\n  ".join(p.replace(" ", "\\ ") for p in seen)))
        return 0, "", ("warning: W\n" if "WARN" in text else "")

    def run(self, cmd, stdout=None, stderr=None):
        code, o, e = self._moc(cmd[1:])
        return types.SimpleNamespace(returncode=code, stdout=o.encode(), stderr=e.encode())

    def call(self, cmd):
        return self._moc(cmd[1:])[0]

    PIPE = DEVNULL = None


class MocCacheTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="moc cache ")      # a space in every path
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.cache = os.path.join(self.dir, "cache")
        os.mkdir(self.cache)
        self.moc_file = self.write("moc", "the moc program")
        self.header = self.write("a.h", 'class A;\n#include "b.h"\n')
        self.write("b.h", "class B;\n")
        self.out = os.path.join(self.dir, "moc_a.cpp")
        self.fake = FakeMoc()
        self.env = {"MOCCACHE_DIR": self.cache, "MOCCACHE_SALT": "s1"}

    def write(self, name, text):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def read(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    def moc(self, *extra, dep=True, out=None):
        args = [self.moc_file, "-DX=1", *extra, "-o", out or self.out]
        if dep:
            args.append("--output-dep-file")
        args.append(self.header)
        with mock.patch.object(moccache, "subprocess", self.fake), \
                mock.patch.object(sys, "argv", ["moccache.py"] + args), \
                mock.patch.dict(os.environ, self.env):
            return moccache.main()

    def stats(self):
        path = os.path.join(self.cache, "stats.log")
        return self.read(path) if os.path.exists(path) else ""

    def test_second_call_comes_from_the_cache(self):
        self.assertEqual(self.moc(), 0)
        first, first_dep = self.read(self.out), self.read(self.out + ".d")
        os.remove(self.out)
        os.remove(self.out + ".d")
        self.assertEqual(self.moc(), 0)
        self.assertEqual(self.fake.calls, 1)
        self.assertEqual(self.read(self.out), first)
        self.assertEqual(self.read(self.out + ".d"), first_dep)
        self.assertIn("CLASS B", first)
        self.assertEqual(self.stats(), "mh")

    def test_changed_include_is_a_miss_and_both_results_are_kept(self):
        self.moc()
        self.write("b.h", "class B2;\n")
        self.moc()
        self.assertEqual(self.fake.calls, 2)
        self.assertIn("CLASS B2", self.read(self.out))
        self.write("b.h", "class B;\n")                       # back to the first content
        self.moc()
        self.assertEqual(self.fake.calls, 2)
        self.assertNotIn("CLASS B2", self.read(self.out))
        self.assertEqual(self.stats(), "mmh")

    def test_same_content_with_a_new_file_time_is_a_hit(self):
        self.moc()
        for name in ("a.h", "b.h"):
            os.utime(os.path.join(self.dir, name), (1, 1))
        self.moc()
        self.assertEqual(self.fake.calls, 1)

    def test_arguments_salt_and_moc_program_are_part_of_the_key(self):
        self.moc()
        self.moc("-DY=2")
        self.assertEqual(self.fake.calls, 2)
        self.env["MOCCACHE_SALT"] = "s2"
        self.moc()
        self.assertEqual(self.fake.calls, 3)
        self.env["MOCCACHE_SALT"] = "s1"
        self.write("moc", "another moc program")
        self.moc()
        self.assertEqual(self.fake.calls, 4)
        self.write("moc", "the moc program")
        self.moc()
        self.assertEqual(self.fake.calls, 4)

    def test_without_a_dependency_file_nothing_is_cached(self):
        self.moc(dep=False)
        self.moc(dep=False)
        self.assertEqual(self.fake.calls, 2)
        self.assertEqual(self.stats(), "uu")

    def test_without_a_cache_directory_moc_is_just_run(self):
        self.env = {"MOCCACHE_DIR": ""}
        self.moc()
        self.moc()
        self.assertEqual(self.fake.calls, 2)
        self.assertEqual(os.listdir(self.cache), [])

    def test_a_failing_moc_is_passed_on_and_not_stored(self):
        self.write("b.h", "FAIL\n")
        self.assertEqual(self.moc(), 1)
        self.assertEqual(self.moc(), 1)
        self.assertEqual(self.fake.calls, 2)
        self.assertFalse(os.path.exists(self.out))

    def test_no_output_generated_is_not_stored(self):
        self.write("b.h", "NOOUT\n")
        self.assertEqual(self.moc(), 0)
        self.assertEqual(self.moc(), 0)
        self.assertEqual(self.fake.calls, 2)
        self.assertEqual(self.stats(), "uu")

    def test_a_stale_output_does_not_survive_a_run_without_output(self):
        self.moc()
        self.write("b.h", "NOOUT\n")
        self.moc()
        self.assertFalse(os.path.exists(self.out))

    def test_at_most_three_results_per_key(self):
        for i in range(6):
            self.write("b.h", "class B%d;\n" % i)
            self.moc()
        entries = [n for _r, _d, files in os.walk(self.cache) for n in files if n.endswith(".z")]
        self.assertEqual(len(entries), moccache.MAX_VARIANTS)
        self.moc()                                            # the newest is still there
        self.assertEqual(self.fake.calls, 6)

    def test_verify_mode_catches_a_result_that_moc_would_not_produce(self):
        self.moc()
        self.env["MOCCACHE_VERIFY"] = "1"
        self.moc()                                            # agrees with moc: a hit
        self.assertEqual(self.stats(), "mh")
        self.fake.suffix = "// different now\n"
        self.moc()
        self.assertTrue(self.stats().startswith("mhX"))
        self.assertIn("different now", self.read(self.out))
        self.assertEqual([n for n in os.listdir(self.dir) if n.startswith("moccache_verify")], [])

    def test_warnings_are_repeated_on_a_hit(self):
        self.write("b.h", "class B; // WARN\n")
        self.moc()
        with mock.patch.object(sys, "stderr") as err:
            self.moc()
        self.assertEqual(self.fake.calls, 1)
        err.write.assert_any_call("warning: W\n")

    def test_dependency_file_parsing(self):
        deps = moccache.parse_depfile(b"out/moc_a.cpp: /src/a.h \\\n  /src/with\\ space/b.h \\\n  /x/c\\#d.h /y/e$$f.h\n")
        self.assertEqual(deps, ["/src/a.h", "/src/with space/b.h", "/x/c#d.h", "/y/e$f.h"])
        self.assertEqual(moccache.parse_depfile(b"D:/b/moc_a.cpp: D:/s/a.h D:/s/b.h\r\n"), ["D:/s/a.h", "D:/s/b.h"])
        self.assertIsNone(moccache.parse_depfile(b"nothing useful"))


if __name__ == "__main__":
    unittest.main()
