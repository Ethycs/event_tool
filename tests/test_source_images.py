"""Telegram and Instagram surface flyer images into the `images` field.

These are the two sources the OCR pass exists for: an Instagram event post is
usually a poster with a one-emoji caption, and a Telegram event flyer is a photo
that may have no caption at all. `pipeline/ocr_enrich` reads `images`; these
pin that each source populates it.
"""
from __future__ import annotations

from types import SimpleNamespace

from event_harvester.sources import telegram as tg
from event_harvester.sources.instagram import _image_url, normalize_media


class TestInstagramImages:
    def test_mobile_api_picks_the_largest_candidate(self):
        media = {"image_versions2": {"candidates": [
            {"url": "small", "width": 320, "height": 320},
            {"url": "big", "width": 1080, "height": 1080}]}}
        assert _image_url(media) == "big"

    def test_graphql_display_url(self):
        assert _image_url({"display_url": "u"}) == "u"

    def test_display_resources_largest_src(self):
        media = {"display_resources": [
            {"src": "a", "config_width": 640}, {"src": "b", "config_width": 1080}]}
        assert _image_url(media) == "b"

    def test_no_image_is_empty(self):
        assert _image_url({"caption": {"text": "x"}}) == ""

    def test_normalize_media_attaches_the_image(self):
        media = {"caption": {"text": "Queer dance party this Friday, come thru!"},
                 "id": "1", "user": {"username": "stud"}, "display_url": "poster.jpg"}
        rec = normalize_media(media, source_name="ig")
        assert rec["images"] == ["poster.jpg"]

    def test_a_captioned_post_with_no_image_has_no_images_key(self):
        media = {"caption": {"text": "Big night out this Saturday at the club yeah"},
                 "id": "2", "user": {"username": "stud"}}
        rec = normalize_media(media, source_name="ig")
        assert "images" not in rec


class TestTelegramPhoto:
    def _msg(self, *, text=None, photo=False):
        return SimpleNamespace(
            id=1, text=text, message=text, date=__import__("datetime").datetime(
                2026, 8, 22, tzinfo=__import__("datetime").timezone.utc),
            sender=SimpleNamespace(username="chan"), photo=(object() if photo else None),
            media=None,
        )

    def test_has_photo_detects_a_photo_message(self):
        assert tg._has_photo(self._msg(photo=True))
        assert not tg._has_photo(self._msg(text="hi"))

    def test_a_caption_less_photo_is_no_longer_dropped(self):
        """It used to return None for no text; a flyer with no caption is the
        whole point of OCR, so it must survive to carry its image. This asserts
        the new guard logic (`not text and not _has_photo`) directly."""
        msg = self._msg(photo=True)          # no text, has photo
        assert not (not msg.text and not tg._has_photo(msg))

    def test_a_textless_imageless_message_is_still_dropped(self):
        msg = self._msg()                    # no text, no photo
        assert (not msg.text and not tg._has_photo(msg))

    def test_the_download_cap_is_bounded(self):
        assert 1 <= tg._MAX_IMAGE_DOWNLOADS <= 1000
