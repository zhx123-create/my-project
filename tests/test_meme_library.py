from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from meme_library import MAX_FILE_BYTES, MemeLibrary, inspect_image_bytes


class MemeLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.directory = self.root / "library"
        self.library = MemeLibrary(self.directory)

    def image(self, name="image.png", color="red", image_format="PNG"):
        path = self.root / name
        with Image.new("RGB", (24, 18), color) as image:
            image.save(path, format=image_format)
        return path

    def test_empty_library_is_lazy_and_honest(self):
        self.assertFalse(self.directory.exists())
        self.assertEqual(self.library.list(), [])
        self.assertEqual(self.library.recommend("发疯", ["抽象"]), [])
        self.assertFalse(self.directory.exists())

    def test_import_copies_original_and_reloads_only_safe_metadata(self):
        source = self.image("沙雕企鹅.png")
        item = self.library.import_files([source], tags="抽象, 拒绝")[0]
        copied = self.library.get_path(item["id"])
        self.assertNotEqual(copied, source)
        self.assertEqual(copied.read_bytes(), source.read_bytes())
        self.assertEqual(copied.name, item["id"] + ".png")
        self.assertEqual(item["tags"], ["抽象", "拒绝", "搞怪"])
        self.assertEqual(item["width"], 24)
        self.assertEqual(item["height"], 18)
        self.assertFalse(item["animated"])
        self.assertNotIn("path", item)
        self.assertNotIn(str(source), (self.directory / "index.json").read_text())
        reloaded = MemeLibrary(self.directory)
        self.assertEqual(reloaded.list(), [item])
        self.assertEqual(reloaded.get_path(item["id"]), copied)

    def test_animated_gif_preserves_bytes_and_frames(self):
        source = self.root / "动图.gif"
        with Image.new("RGB", (20, 20), "red") as first, Image.new("RGB", (20, 20), "blue") as second:
            first.save(source, save_all=True, append_images=[second], duration=50, loop=0)
        item = self.library.import_files([source])[0]
        self.assertTrue(item["animated"])
        self.assertEqual(item["format"], "GIF")
        self.assertEqual(self.library.get_path(item["id"]).read_bytes(), source.read_bytes())
        with Image.open(self.library.get_path(item["id"])) as image:
            self.assertEqual(image.n_frames, 2)

    def test_shared_image_validator_has_no_filesystem_side_effect(self):
        source = self.image()
        details = inspect_image_bytes(source.read_bytes())
        self.assertEqual(details, {"width": 24, "height": 18, "format": "PNG", "animated": False, "extension": ".png"})
        self.assertFalse(self.directory.exists())
        for data in (b"", b"not an image", "not bytes", b"x" * (MAX_FILE_BYTES + 1)):
            with self.subTest(kind=type(data)), self.assertRaises(ValueError):
                inspect_image_bytes(data)

    def test_jpeg_and_webp_use_content_format_not_filename(self):
        jpeg = self.image("jpeg-image.dat", image_format="JPEG")
        webp = self.image("webp-image.dat", color="blue", image_format="WEBP")
        items = self.library.import_files([jpeg, webp])
        self.assertEqual([item["extension"] for item in items], [".jpg", ".webp"])
        self.assertEqual([item["format"] for item in items], ["JPEG", "WEBP"])

    def test_import_deduplicates_bytes_merges_tags_and_refreshes_recency(self):
        red = self.image("red.png")
        blue = self.image("blue.png", color="blue")
        original = self.library.import_files([red], tags="开心")[0]
        self.library.import_files([blue])
        repeated = self.library.import_files([red, red], tags="抽象")[0]
        self.assertEqual(repeated["id"], original["id"])
        self.assertEqual(repeated["tags"], ["开心", "抽象"])
        self.assertEqual(len(self.library.list()), 2)
        self.assertEqual(self.library.list()[0]["id"], original["id"])
        self.assertEqual(len(list(self.directory.glob("*.png"))), 2)

    def test_invalid_batch_is_rejected_before_creating_library(self):
        valid = self.image()
        invalid = self.root / "bad.png"
        invalid.write_bytes(b"not an image")
        with self.assertRaises(ValueError):
            self.library.import_files([valid, invalid])
        self.assertFalse(self.directory.exists())
        self.assertTrue(valid.exists())

    def test_import_limits_and_non_image_formats(self):
        source = self.image()
        for paths in ([], [source] * 21):
            with self.subTest(count=len(paths)), self.assertRaises(ValueError):
                self.library.import_files(paths)
        oversized = self.root / "oversized.png"
        with oversized.open("wb") as stream:
            stream.truncate(MAX_FILE_BYTES + 1)
        with self.assertRaises(ValueError):
            self.library.import_files([oversized])
        bmp = self.image("not-supported.bmp", image_format="BMP")
        with self.assertRaisesRegex(ValueError, "仅支持"):
            self.library.import_files([bmp])
        self.assertFalse(self.directory.exists())

    def test_image_dimensions_rejected_before_pixel_decode(self):
        payload = BytesIO()
        with Image.new("1", (10001, 1)) as image:
            image.save(payload, format="PNG")
        source = self.root / "wide.png"
        source.write_bytes(payload.getvalue())
        with self.assertRaisesRegex(ValueError, "尺寸"):
            self.library.import_files([source])
        self.assertFalse(self.directory.exists())

    def test_tags_names_and_returned_data_are_sanitized(self):
        source = self.image("line\nbreak.png")
        item = self.library.import_files([source], tags="无语，无语；抽象")[0]
        self.assertEqual(item["name"], "linebreak.png")
        self.assertEqual(item["tags"], ["无语", "抽象"])
        item["tags"].append("corrupted")
        listed = self.library.list()
        listed[0]["name"] = "changed"
        listed[0]["tags"].append("another")
        self.assertEqual(self.library.list()[0]["tags"], ["无语", "抽象"])
        self.assertEqual(self.library.list()[0]["name"], "linebreak.png")
        with self.assertRaises(ValueError):
            self.library.import_files([source], tags="a" * 17)
        with self.assertRaises(ValueError):
            self.library.import_files([source], tags="bad\x00tag")

    def test_recommendation_matches_custom_tags_and_builtin_aliases(self):
        happy = self.library.import_files([self.image("开心.png")])[0]
        no = self.library.import_files([self.image("second.png", color="blue")], tags="拒绝 摸鱼")[0]
        weird = self.library.import_files([self.image("third.png", color="green")], tags="抽象")[0]
        self.assertEqual(self.library.recommend("哈哈笑死", limit=1)[0]["id"], happy["id"])
        self.assertEqual(self.library.recommend("想摸鱼", limit=1)[0]["id"], no["id"])
        self.assertEqual(self.library.recommend("", tags=["抽象"], limit=1)[0]["id"], weird["id"])
        self.assertEqual([item["id"] for item in self.library.recommend("unmatched", limit=2)], [weird["id"], no["id"]])
        self.assertEqual([item["id"] for item in self.library.recommend("", limit=2)], [weird["id"], no["id"]])
        for kwargs in ({"limit": 0}, {"limit": True}, {"tags": "抽象"}, {"query": "x" * 2001}):
            arguments = {"query": "", **kwargs}
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.library.recommend(**arguments)

    def test_corrupt_index_is_preserved(self):
        self.directory.mkdir()
        index = self.directory / "index.json"
        content = b"{invalid json"
        index.write_bytes(content)
        with self.assertRaisesRegex(ValueError, "未覆盖"):
            MemeLibrary(self.directory)
        self.assertEqual(index.read_bytes(), content)

    def test_index_cannot_supply_arbitrary_file_paths_or_extensions(self):
        item = self.library.import_files([self.image()])[0]
        index = self.directory / "index.json"
        data = json.loads(index.read_text())
        data["items"][0]["extension"] = "/../../outside.png"
        index.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "未覆盖"):
            MemeLibrary(self.directory)
        with self.assertRaises(ValueError):
            self.library.get_path("../../outside.png")
        self.assertTrue((self.directory / (item["id"] + ".png")).exists())

    def symlink(self, target, link, directory=False):
        try:
            link.symlink_to(target, target_is_directory=directory)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")

    def test_source_and_storage_symlinks_are_rejected(self):
        source = self.image()
        link = self.root / "linked.png"
        self.symlink(source, link)
        with self.assertRaises(ValueError):
            self.library.import_files([link])
        target_directory = self.root / "actual-library"
        target_directory.mkdir()
        directory_link = self.root / "library-link"
        self.symlink(target_directory, directory_link, directory=True)
        with self.assertRaises(ValueError):
            MemeLibrary(directory_link)
        self.assertEqual(list(target_directory.iterdir()), [])

    def test_index_symlink_never_changes_target(self):
        self.directory.mkdir()
        target = self.root / "target.json"
        target.write_bytes(b"keep me")
        self.symlink(target, self.directory / "index.json")
        with self.assertRaises(ValueError):
            MemeLibrary(self.directory)
        with self.assertRaises(ValueError):
            self.library.import_files([self.image()])
        self.assertEqual(target.read_bytes(), b"keep me")

    def test_entry_symlink_never_reads_or_deletes_target(self):
        source = self.image()
        item = self.library.import_files([source])[0]
        owned = self.library.get_path(item["id"])
        owned.unlink()
        self.symlink(source, owned)
        for action in (lambda: self.library.get_path(item["id"]), lambda: self.library.remove(item["id"]), self.library.list, lambda: MemeLibrary(self.directory)):
            with self.assertRaises(ValueError):
                action()
        self.assertTrue(source.exists())
        self.assertTrue(owned.is_symlink())

    def test_removal_only_deletes_owned_copy(self):
        source = self.image()
        item = self.library.import_files([source])[0]
        owned = self.library.get_path(item["id"])
        self.assertTrue(self.library.remove(item["id"]))
        self.assertFalse(owned.exists())
        self.assertTrue(source.exists())
        self.assertEqual(self.library.list(), [])
        self.assertFalse(self.library.remove(item["id"]))
        self.assertEqual(MemeLibrary(self.directory).list(), [])

    def test_missing_copy_can_be_removed_from_metadata(self):
        item = self.library.import_files([self.image()])[0]
        self.library.get_path(item["id"]).unlink()
        with self.assertRaises(ValueError):
            self.library.get_path(item["id"])
        self.assertTrue(self.library.remove(item["id"]))
        self.assertEqual(self.library.list(), [])

    def test_modified_owned_copy_is_preserved_on_get_remove_or_import(self):
        source = self.image()
        item = self.library.import_files([source])[0]
        owned = self.library.get_path(item["id"])
        owned.write_bytes(b"changed by user")
        for action in (lambda: self.library.get_path(item["id"]), lambda: self.library.remove(item["id"]), lambda: self.library.import_files([source])):
            with self.assertRaises(ValueError):
                action()
        self.assertEqual(owned.read_bytes(), b"changed by user")
        self.assertTrue(source.exists())

    def test_index_save_failure_rolls_back_imported_copies(self):
        source = self.image()
        with patch("meme_library.os.replace", side_effect=OSError("disk error")):
            with self.assertRaises(ValueError):
                self.library.import_files([source])
        self.assertEqual(self.library.list(), [])
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertTrue(source.exists())

    def test_remove_restores_copy_when_index_save_fails(self):
        source = self.image()
        item = self.library.import_files([source])[0]
        owned = self.library.get_path(item["id"])
        with patch.object(self.library, "_save", side_effect=ValueError("save failed")):
            with self.assertRaisesRegex(ValueError, "save failed"):
                self.library.remove(item["id"])
        self.assertEqual(owned.read_bytes(), source.read_bytes())
        self.assertEqual(self.library.list(), [item])
        self.assertEqual(list(self.directory.glob(".meme-remove-*")), [])

    def test_concurrent_index_edits_are_not_overwritten(self):
        source = self.image()
        item = self.library.import_files([source])[0]
        other = MemeLibrary(self.directory)
        other.import_files([self.image("other.png", color="blue")])
        content = (self.directory / "index.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "其他操作"):
            self.library.remove(item["id"])
        self.assertEqual((self.directory / "index.json").read_bytes(), content)


if __name__ == "__main__":
    unittest.main()
