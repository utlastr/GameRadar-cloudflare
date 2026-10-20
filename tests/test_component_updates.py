import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from radar import config, updater


class ComponentUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data = self.root / "radar_data"
        self.data.mkdir()
        self.config = patch.multiple(config, BASE_DIR=str(self.root), DATA_DIR=str(self.data),
                                     UPDATE_SCOPE="globe")
        self.config.start()
        self.original = {
            "version.txt": b"69\nRadar release",
            "README.md": b"Radar notes",
            "radar/__init__.py": b"",
            "radar/updater.py": b"# updater",
            "radar/web/index.html": b"<h1>Radar</h1>",
            "radar/globe/__init__.py": b"# Globe",
            "radar/globe/web/map.html": b"<h1>Globe 1</h1>",
            "radar/globe/version.txt": b"1\nGlobe release",
            "radar/globe/old.py": b"# obsolete",
        }
        for name, value in self.original.items():
            self.write(name, value)
        self.state = {"schema": 1, "files": {
            p: updater._blob_sha(b) for p, b in self.original.items()}, "components": {
                "core": {"version": 69, "commit": "old-core"}}}
        self.save_state()
        self.write("radar_data/settings.json", b'{"private":"local settings"}')
        self.write("radar_data/incgrid.bin", b"IG1\x00cached-data")

    def tearDown(self):
        self.config.stop()
        self.tmp.cleanup()

    def write(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def save_state(self):
        (self.data / "update_state.json").write_text(json.dumps(self.state), encoding="utf-8")

    def release(self, scope="globe"):
        new = {p: b for p, b in self.original.items() if updater._managed(p, scope)}
        if scope == "globe":
            new["radar/globe/web/map.html"] = b"<h1>Globe 2</h1>"
            new["radar/globe/version.txt"] = b"2\nNew map"
            new["radar/globe/new.py"] = b"# new feature"
            del new["radar/globe/old.py"]
        else:
            new["version.txt"] = b"70\nNew Radar"
            new["radar/web/index.html"] = b"<h1>Radar 70</h1>"
        self.blobs = {updater._blob_sha(b): b for b in new.values()}
        files = {p: updater._blob_sha(b) for p, b in new.items()}
        changes, conflicts = updater._plan(files, scope, self.state)
        return {"scope": scope, "version": 2 if scope == "globe" else 70,
                "local": 1 if scope == "globe" else 69, "note": "release",
                "commit": "reviewed-commit", "has_update": True,
                "changes": changes, "conflicts": conflicts,
                "_files": files, "_baseline": self.state}

    def test_globe_update_retains_local_radar_edits_and_user_data(self):
        self.write("radar/web/index.html", b"<h1>My unfinished Radar changes</h1>")
        protected = {p: (self.root / p).read_bytes() for p in (
            "radar/web/index.html", "README.md", "version.txt",
            "radar_data/settings.json", "radar_data/incgrid.bin")}
        release = self.release()
        with patch.object(updater, "_remote_blob", side_effect=self.blobs.__getitem__) as download:
            result = updater.apply("globe", release)
        for path, value in protected.items():
            self.assertEqual((self.root / path).read_bytes(), value)
        self.assertEqual((self.root / "radar/globe/web/map.html").read_bytes(), b"<h1>Globe 2</h1>")
        self.assertFalse((self.root / "radar/globe/old.py").exists())
        self.assertTrue((self.root / "radar/globe/new.py").exists())
        self.assertEqual(updater.local_version("core")[0], 69)
        self.assertEqual(updater.local_version("globe")[0], 2)
        self.assertTrue(all(p.startswith("radar/globe/") for p in result["updated"]))
        self.assertEqual(download.call_count, 3)
        self.assertEqual((Path(result["backup"]) / "radar/globe/web/map.html").read_bytes(),
                         self.original["radar/globe/web/map.html"])
        state = updater._baseline()
        self.assertEqual(state["components"]["core"]["commit"], "old-core")
        self.assertEqual(state["components"]["globe"]["commit"], "reviewed-commit")
        self.assertNotIn("radar/globe/old.py", state["files"])

    def test_core_update_retains_local_globe_edits(self):
        self.write("radar/globe/web/map.html", b"<h1>My Globe work</h1>")
        release = self.release("core")
        with patch.object(updater, "_remote_blob", side_effect=self.blobs.__getitem__):
            updater.apply("core", release)
        self.assertEqual((self.root / "radar/globe/web/map.html").read_bytes(), b"<h1>My Globe work</h1>")
        self.assertEqual(updater.local_version("globe")[0], 1)
        self.assertEqual(updater.local_version("core")[0], 70)

    def test_conflicting_component_edit_aborts_before_download(self):
        self.write("radar/globe/web/map.html", b"local map edit")
        release = self.release()
        with patch.object(updater, "_remote_blob") as download:
            with self.assertRaises(updater.LocalChangesError):
                updater.apply("globe", release)
            download.assert_not_called()
        self.assertEqual((self.root / "radar/globe/web/map.html").read_bytes(), b"local map edit")
        self.assertEqual(updater.local_version("globe")[0], 1)

    def test_edit_after_check_is_detected_after_download(self):
        release = self.release()
        def download(sha):
            self.write("radar/globe/web/map.html", b"changed while downloading")
            return self.blobs[sha]
        with patch.object(updater, "_remote_blob", side_effect=download):
            with self.assertRaises(updater.LocalChangesError):
                updater.apply("globe", release)
        self.assertEqual(updater.local_version("globe")[0], 1)
        self.assertEqual((self.root / "radar/globe/web/map.html").read_bytes(),
                         b"changed while downloading")

    def test_network_failure_does_not_replace_any_files(self):
        release = self.release()
        with patch.object(updater, "_remote_blob", side_effect=OSError("network interrupted")):
            with self.assertRaises(OSError):
                updater.apply("globe", release)
        for path, data in self.original.items():
            self.assertEqual((self.root / path).read_bytes(), data)

    def test_failed_install_restores_added_deleted_and_changed_files(self):
        release = self.release()
        atomic = updater._atomic
        failed = False
        def fail_version(path, content):
            nonlocal failed
            if path == self.root / "radar/globe/version.txt" and not failed:
                failed = True
                raise OSError("simulated locked version file")
            atomic(path, content)
        with patch.object(updater, "_remote_blob", side_effect=self.blobs.__getitem__), \
                patch.object(updater, "_atomic", side_effect=fail_version):
            with self.assertRaises(OSError):
                updater.apply("globe", release)
        for path, data in self.original.items():
            self.assertEqual((self.root / path).read_bytes(), data)
        self.assertFalse((self.root / "radar/globe/new.py").exists())
        self.assertEqual(updater._baseline(), self.state)

    def test_untracked_globe_files_and_unchanged_remote_local_edits_survive(self):
        self.write("radar/globe/scratch.txt", b"local notes")
        self.write("radar/globe/__init__.py", b"# local change to unchanged remote file")
        release = self.release()
        self.assertEqual(release["conflicts"], [])
        with patch.object(updater, "_remote_blob", side_effect=self.blobs.__getitem__):
            updater.apply("globe", release)
        self.assertEqual((self.root / "radar/globe/scratch.txt").read_bytes(), b"local notes")
        self.assertEqual((self.root / "radar/globe/__init__.py").read_bytes(),
                         b"# local change to unchanged remote file")

    def test_checksum_failure_is_rejected(self):
        with patch.object(updater, "_api", return_value={
                "encoding": "base64", "content": base64.b64encode(b"wrong bytes").decode()}):
            with self.assertRaisesRegex(RuntimeError, "校验失败"):
                updater._remote_blob(updater._blob_sha(b"expected"))

    def test_scope_rejects_other_component_and_path_aliases(self):
        for path in ("radar/web/index.html", "version.txt", "radar_data/settings.json",
                     "radar/globe/../web/index.html", "/radar/globe/x.py",
                     "radar\\globe\\x.py", "radar/globe/x.py:stream"):
            self.assertFalse(updater._managed(path, "globe"), path)
        for path in ("radar/globe/x.py", "radar/GLOBE/x.py", "radar/globe./x.py"):
            self.assertFalse(updater._managed(path, "core"), path)

    def test_scope_mismatch_rejected(self):
        with self.assertRaises(ValueError):
            updater.apply("core", self.release("globe"))

    def test_zip_installation_learns_only_verified_files(self):
        (self.data / "update_state.json").unlink()
        self.write("radar/web/index.html", b"private main changes")
        incoming = {p: updater._blob_sha(b) for p, b in self.original.items()}
        state = updater._remember_verified(incoming)
        self.assertNotIn("radar/web/index.html", state["files"])
        self.assertEqual(state["files"]["radar/globe/version.txt"],
                         updater._blob_sha(self.original["radar/globe/version.txt"]))

    def test_verified_release_advances_old_receipt_without_hiding_local_edits(self):
        self.write("radar/web/index.html", b"my main work")
        updated = b"2\nMap already installed by Git"
        self.write("radar/globe/version.txt", updated)
        incoming = {p: updater._blob_sha(b) for p, b in self.original.items()}
        incoming["radar/globe/version.txt"] = updater._blob_sha(updated)
        state = updater._remember_verified(incoming)
        self.assertEqual(state["files"]["radar/globe/version.txt"], updater._blob_sha(updated))
        self.assertEqual(state["files"]["radar/web/index.html"],
                         updater._blob_sha(self.original["radar/web/index.html"]))

    def test_latest_other_component_receipt_survives_cached_plan(self):
        release = self.release()
        self.state["components"]["core"] = {"version": 71, "commit": "new-core"}
        self.save_state()
        with patch.object(updater, "_remote_blob", side_effect=self.blobs.__getitem__):
            updater.apply("globe", release)
        self.assertEqual(updater._baseline()["components"]["core"]["commit"], "new-core")


if __name__ == "__main__":
    unittest.main()
