from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.request
import zipfile

from PIL import Image

import public_meme_source as source
from public_meme_source import PublicMemeError, PublicMemeSource


def image_bytes(image_format="PNG", animated=False):
    stream = BytesIO()
    first = Image.new("RGB", (30, 20), "red")
    if animated:
        first.save(stream, format=image_format, save_all=True,
                   append_images=[Image.new("RGB", (30, 20), "blue")], duration=150, loop=0)
    else:
        first.save(stream, format=image_format)
    return stream.getvalue()


def pack_bytes(items):
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, raw in items:
            archive.writestr(name, raw)
    return stream.getvalue()


class PublicMemeTests(unittest.TestCase):
    def test_original_animation_and_metadata_are_returned_without_persisting(self):
        raw = image_bytes("GIF", animated=True)
        archive = pack_bytes([("开心.gif", raw)])
        with tempfile.TemporaryDirectory() as temp, patch.object(source, "_download", return_value=archive) as download:
            before = list(Path(temp).iterdir())
            results = PublicMemeSource().search("熊猫头开心", ["开心"])
            self.assertEqual(before, list(Path(temp).iterdir()))
            self.assertEqual(len(results), 1)
            item = results[0]
            self.assertEqual(item["data"], raw)
            self.assertTrue(item["animated"])
            self.assertEqual((item["width"], item["height"], item["extension"]), (30, 20, ".gif"))
            self.assertEqual(len(item["id"]), 64)
            self.assertEqual(item["source"], source.SOURCE)
            self.assertIn("文件名匹配", item["reason"])
            # The static download URL carries neither the query nor user context.
            self.assertEqual(download.call_args.args, (source._RELEASE_ROOT + source._PACKS[0]["asset"],))
            self.assertNotIn("开心", download.call_args.args[0])

    def test_named_category_and_filename_matching(self):
        archive = pack_bytes([("001哈哈.png", image_bytes()), ("002无语.png", image_bytes("JPEG"))])
        with patch.object(source, "_download", return_value=archive) as download:
            result = PublicMemeSource().search("无语", ["猫咪"], limit=1)[0]
        self.assertIn("猫咪日常", result["name"])
        self.assertIn("无语", result["name"])
        self.assertTrue(download.call_args.args[0].endswith(source._PACKS[3]["asset"]))

    def test_browse_candidates_rotate_and_pack_is_cached(self):
        archive = pack_bytes([(f"{index}.png", image_bytes("PNG" if index == 0 else "JPEG")) for index in range(2)])
        library = PublicMemeSource()
        with patch.object(source, "_download", return_value=archive) as download:
            first = library.search("熊猫头", limit=1)[0]
            second = library.search("熊猫头", limit=1)[0]
            self.assertNotEqual(first["id"], second["id"])
            download.assert_called_once()
            self.assertIn("分类匹配", first["reason"])

    def test_generic_abstract_query_rotates_public_categories(self):
        archive = pack_bytes([("reaction.png", image_bytes())])
        library = PublicMemeSource()
        with patch.object(source, "_download", return_value=archive) as download:
            first = library.search("抽象搞怪", limit=1)[0]
            second = library.search("抽象搞怪", limit=1)[0]
        self.assertIn("熊猫头", first["name"])
        self.assertIn("沙雕企鹅", second["name"])
        self.assertEqual(download.call_count, 2)

    def test_invalid_and_empty_requests_never_download(self):
        with patch.object(source, "_download") as download:
            self.assertEqual(PublicMemeSource().search("  "), [])
            for kwargs in ({"query": 1}, {"query": "x" * 501}, {"query": "x", "tags": "y"},
                           {"query": "x", "tags": [None]}, {"query": "x", "limit": True},
                           {"query": "x", "limit": 10}):
                with self.subTest(kwargs=kwargs), self.assertRaises(PublicMemeError):
                    PublicMemeSource().search(**kwargs)
            download.assert_not_called()

    def test_unsafe_zip_members_and_invalid_images_are_skipped(self):
        archive = pack_bytes([("../escape.png", image_bytes()), ("C:\\bad.png", image_bytes()),
                              ("not-an-image.gif", b"broken"), ("ok.png", image_bytes())])
        with patch.object(source, "_download", return_value=archive):
            results = PublicMemeSource().search("企鹅")
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["name"].endswith("ok.png"))

    def test_empty_pack_does_not_invent_candidate(self):
        with patch.object(source, "_download", return_value=pack_bytes([("readme.txt", b"empty")])):
            self.assertEqual(PublicMemeSource().search("搞怪"), [])

    def test_corrupt_or_oversized_pack_has_readable_failure(self):
        library = PublicMemeSource()
        with patch.object(source, "_download", return_value=b"not a zip"):
            with self.assertRaisesRegex(PublicMemeError, "无法读取"):
                library.search("企鹅")
        self.assertEqual(library._cache, {})
        with patch.object(source, "_download", return_value=b"0" * 10), patch.object(source, "_MAX_PACK_BYTES", 9):
            with self.assertRaisesRegex(PublicMemeError, "超过"):
                library.search("企鹅")

    def test_unpacked_pack_size_is_bounded_before_images_read(self):
        archive = pack_bytes([("large.png", image_bytes())])
        with patch.object(source, "_download", return_value=archive), patch.object(source, "_MAX_UNPACKED_BYTES", 1):
            with self.assertRaisesRegex(PublicMemeError, "内容过大"):
                PublicMemeSource().search("企鹅")

    def test_downloads_only_known_urls_and_checks_redirects(self):
        with patch("public_meme_source.urllib.request.build_opener") as opener:
            with self.assertRaises(PublicMemeError):
                source._download("https://example.com/evil.zip")
            opener.assert_not_called()
        redirect = source._SourceRedirect()
        req = urllib.request.Request(next(iter(source._PACK_URLS)))
        for url in ("http://release-assets.githubusercontent.com/github-production-release-asset/185535659/a",
                    "https://evil.example/asset.zip", "https://github.com/evil/project/raw/file.png",
                    "https://release-assets.githubusercontent.com/github-production-release-asset/999/a",
                    "https://user:secret@release-assets.githubusercontent.com/github-production-release-asset/185535659/a"):
            with self.subTest(url=url), self.assertRaises(PublicMemeError):
                redirect.redirect_request(req, None, 302, "", {}, url)
        approved = "https://release-assets.githubusercontent.com/github-production-release-asset/185535659/a?sig=value"
        self.assertEqual(redirect.redirect_request(req, None, 302, "", {}, approved).full_url, approved)


if __name__ == "__main__":
    unittest.main()
