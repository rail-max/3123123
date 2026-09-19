import importlib
import json
import sys
import tempfile
import threading
import types
import unittest
import zoneinfo
from datetime import timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch


sys.modules["database"] = types.ModuleType("database")
_real_zone_info = zoneinfo.ZoneInfo
zoneinfo.ZoneInfo = lambda _name: timezone.utc
bot = importlib.import_module("bot")
zoneinfo.ZoneInfo = _real_zone_info


class BotHandlerTests(unittest.TestCase):
    def setUp(self):
        self.db = Mock()
        self.db.get_user_settings.return_value = {
            "track_deleted": True,
            "track_edited": True,
            "support_mode": False,
            "support_active": False,
        }
        self.db.is_sub_active.return_value = True
        self.db.grant_channel_trial_once.return_value = False
        self.original_db = bot.db
        bot.db = self.db
        bot._BUSINESS_REPLY_MEDIA_SENT.clear()
        bot._PENDING_LOCKED_MESSAGES.clear()
        bot._REPLY_MEDIA_UPLOAD_ONLY.clear()

    def tearDown(self):
        bot.db = self.original_db

    def test_reply_photo_uses_file_id_without_download_when_accepted(self):
        reply = {"photo": [{"file_id": "small-photo"}, {"file_id": "big-photo"}]}

        with patch.object(bot, "api", return_value={"ok": True}) as api_mock, patch.object(
            bot, "send_downloaded_file"
        ) as download_mock:
            result = bot.send_reply_media(100, reply, "Photo", prefer_upload=True)

        self.assertTrue(result["ok"])
        api_mock.assert_called_once_with("sendPhoto", chat_id=100, photo="big-photo", caption="Photo", parse_mode="HTML")
        download_mock.assert_not_called()

    def test_reply_photo_downloads_only_after_file_id_rejection(self):
        reply = {"photo": [{"file_id": "photo-id"}]}

        with patch.object(bot, "api", return_value={"ok": False, "error_code": 400}) as api_mock, patch.object(
            bot, "send_downloaded_file", return_value={"ok": True}
        ) as download_mock:
            result = bot.send_reply_media(100, reply, prefer_upload=True)

        self.assertTrue(result["ok"])
        api_mock.assert_called_once()
        download_mock.assert_called_once_with(100, "photo-id", "photo", "")

    def test_reply_photo_does_not_retry_after_uncertain_transport_failure(self):
        reply = {"photo": [{"file_id": "photo-id"}]}

        with patch.object(bot, "api", return_value={"ok": False, "description": "timed out"}), patch.object(
            bot, "send_downloaded_file"
        ) as download_mock:
            result = bot.send_reply_media(100, reply, prefer_upload=True)

        self.assertFalse(result["ok"])
        download_mock.assert_not_called()

    def test_reply_video_voice_and_circle_use_file_id_first(self):
        for media_type, method in (
            ("video", "sendVideo"),
            ("voice", "sendVoice"),
            ("video_note", "sendVideoNote"),
        ):
            for prefer_upload in (False, True):
                with self.subTest(media_type=media_type, prefer_upload=prefer_upload):
                    reply = {media_type: {"file_id": "media-id"}, "is_view_once": True}
                    with patch.object(bot, "api", return_value={"ok": True}) as api_mock, patch.object(
                        bot, "send_downloaded_file"
                    ) as download_mock, patch.object(bot, "send") as notice_mock:
                        result = bot.send_reply_media(100, reply, "Notice", prefer_upload=prefer_upload)

                    self.assertTrue(result["ok"])
                    params = {"chat_id": 100, media_type: "media-id"}
                    if media_type == "video_note":
                        notice_mock.assert_called_once_with(100, "Notice")
                    else:
                        params.update(caption="Notice", parse_mode="HTML")
                        notice_mock.assert_not_called()
                    api_mock.assert_called_once_with(method, **params)
                    download_mock.assert_not_called()

    def test_reply_media_falls_back_once_on_explicit_rejection(self):
        for media_type in ("video", "voice", "video_note"):
            with self.subTest(media_type=media_type):
                reply = {media_type: {"file_id": "media-id"}}
                with patch.object(bot, "api", return_value={"ok": False, "error_code": 400}) as api_mock, patch.object(
                    bot, "send_downloaded_file", return_value={"ok": True}
                ) as download_mock, patch.object(bot, "send") as notice_mock:
                    result = bot.send_reply_media(100, reply, "Notice", prefer_upload=True)

                self.assertTrue(result["ok"])
                api_mock.assert_called_once()
                download_mock.assert_called_once_with(100, "media-id", media_type, "Notice")
                # The fallback sends its own notice only after the media succeeds.
                notice_mock.assert_not_called()

    def test_reply_media_does_not_reupload_after_timeout_or_other_api_errors(self):
        for media_type in ("photo", "video", "voice", "video_note"):
            for error in (
                {"ok": False, "description": "timed out"},
                {"ok": False, "error_code": 403},
                {"ok": False, "error_code": 429, "parameters": {"retry_after": 5}},
                {"ok": False, "error_code": 500},
            ):
                with self.subTest(media_type=media_type, error=error):
                    payload = {"file_id": "media-id"}
                    reply = {media_type: [payload] if media_type == "photo" else payload}
                    with patch.object(bot, "api", return_value=error) as api_mock, patch.object(
                        bot, "send_downloaded_file"
                    ) as download_mock, patch.object(bot, "send") as notice_mock:
                        result = bot.send_reply_media(100, reply, "Notice", prefer_upload=True)

                    self.assertEqual(result, error)
                    api_mock.assert_called_once()
                    download_mock.assert_not_called()
                    notice_mock.assert_not_called()

    def test_circle_notice_failure_does_not_resend_successful_media(self):
        reply = {"video_note": {"file_id": "circle-id"}}
        with patch.object(bot, "api", side_effect=[
            {"ok": True, "result": {"message_id": 42}},
            {"ok": False, "error_code": 400},
        ]) as api_mock, patch.object(bot, "send_downloaded_file") as download_mock:
            result = bot.send_reply_media(100, reply, "Notice", prefer_upload=True)

        self.assertTrue(result["ok"])
        self.assertEqual([call.args[0] for call in api_mock.call_args_list], ["sendVideoNote", "sendMessage"])
        download_mock.assert_not_called()

    def test_self_destructing_rejection_is_remembered_only_for_that_file(self):
        error = {"ok": False, "error_code": 400, "description": "can't use file of type SelfDestructingPhoto as Photo"}
        with patch.object(bot, "api", return_value=error) as api_mock, patch.object(
            bot, "send_downloaded_file", return_value={"ok": True}
        ) as download_mock:
            for file_id in ("photo-1", "photo-1", "photo-2"):
                self.assertTrue(bot.send_reply_media(100, {"photo": [{"file_id": file_id}]})["ok"])

        self.assertEqual(api_mock.call_count, 2)
        self.assertEqual(download_mock.call_count, 3)

    def test_upload_only_cache_expires_and_has_a_size_limit(self):
        error = {"ok": False, "error_code": 400, "description": "SelfDestructingPhoto"}
        with patch.object(bot, "REPLY_MEDIA_UPLOAD_ONLY_LIMIT", 2), patch.object(bot.time, "monotonic", return_value=10):
            for file_id in ("old", "middle", "new"):
                bot.remember_reply_media_upload_only("photo", file_id, error)
            self.assertFalse(bot.reply_media_needs_upload("photo", "old"))
            self.assertTrue(bot.reply_media_needs_upload("photo", "new"))
            self.assertFalse(bot.reply_media_needs_upload("video", "new"))
        with patch.object(bot.time, "monotonic", return_value=10 + bot.REPLY_MEDIA_UPLOAD_ONLY_TTL):
            self.assertFalse(bot.reply_media_needs_upload("photo", "new"))

    def test_other_errors_do_not_poison_upload_only_cache(self):
        for error in (
            {"ok": False, "error_code": 400, "description": "wrong file identifier"},
            {"ok": False, "error_code": 500, "description": "SelfDestructingPhoto"},
            {"ok": False, "description": "timed out"},
        ):
            bot.remember_reply_media_upload_only("photo", "id", error)
        self.assertFalse(bot.reply_media_needs_upload("photo", "id"))

    def test_escape_html_blocks_telegram_html_injection(self):
        self.assertEqual(bot.escape_html('<a href="x">&'), "&lt;a href=&quot;x&quot;&gt;&amp;")

    def test_escape_html_limit_applies_after_escaping(self):
        escaped = bot.escape_html("&" * 1000, 100)
        self.assertLessEqual(len(escaped), 100)
        self.assertFalse(escaped.rstrip().endswith("&"))

    def test_unknown_business_connection_is_ignored(self):
        self.db.get_owner_by_connection.return_value = None
        update = {
            "business_message": {
                "business_connection_id": "unknown",
                "message_id": 10,
                "date": 1,
                "chat": {"id": 200},
                "from": {"id": 300},
                "text": "secret",
            }
        }

        bot.handle_update(update)

        self.db.cache_message.assert_not_called()
        self.db.cache_media.assert_not_called()

    def test_start_requires_channel_subscription(self):
        update = {
            "message": {
                "chat": {"id": 100},
                "from": {"id": 100, "username": "user1", "first_name": "User"},
                "text": "/start",
            }
        }

        with patch.object(bot, "is_required_channel_member", return_value=False), patch.object(
            bot, "send_subscription_gate", return_value={"ok": True}
        ) as gate_mock, patch.object(bot, "send_start_flow", return_value={"ok": True}) as start_mock:
            bot.handle_update(update)

        gate_mock.assert_called_once_with(100)
        start_mock.assert_not_called()

    def test_menu_actions_require_channel_subscription(self):
        update = {
            "message": {
                "chat": {"id": 100},
                "from": {"id": 100, "username": "user1", "first_name": "User"},
                "text": "📊 Статус",
            }
        }

        with patch.object(bot, "is_required_channel_member", return_value=False), patch.object(
            bot, "send_subscription_gate", return_value={"ok": True}
        ) as gate_mock:
            bot.handle_update(update)

        gate_mock.assert_called_once_with(100)
        self.db.get_connections_count_for_user.assert_not_called()

    def test_menu_action_grants_channel_trial_when_subscribed(self):
        self.db.grant_channel_trial_once.return_value = True
        self.db.get_connections_count_for_user.return_value = 1
        self.db.get_user.return_value = {
            "sub_type": "trial",
            "sub_expires": None,
            "sub_remaining_seconds": 7 * 24 * 60 * 60,
        }
        update = {
            "message": {
                "chat": {"id": 100},
                "from": {"id": 100, "username": "user1", "first_name": "User"},
                "text": "📊 Статус",
            }
        }

        with patch.object(bot, "is_required_channel_member", return_value=True), patch.object(
            bot, "send", return_value={"ok": True}
        ) as send_mock:
            bot.handle_update(update)

        self.db.grant_channel_trial_once.assert_called_once_with(100, 7)
        self.assertIn("7 дней доступа", send_mock.call_args_list[0].args[1])

    def test_check_required_channel_starts_bot_when_subscribed(self):
        update = {
            "callback_query": {
                "id": "callback-1",
                "from": {"id": 100},
                "data": "check_required_channel",
                "message": {"chat": {"id": 100}, "message_id": 50},
            }
        }

        with patch.object(bot, "is_required_channel_member", return_value=True), patch.object(
            bot, "api", return_value={"ok": True}
        ) as api_mock, patch.object(bot, "unlock_start_after_channel", return_value={"ok": True}) as unlock_mock:
            bot.handle_update(update)

        unlock_mock.assert_called_once_with(100, 100)
        edit_call = [call for call in api_mock.call_args_list if call.args and call.args[0] == "editMessageText"][0]
        self.assertIn("Подписка найдена", edit_call.kwargs["text"])

    def test_unlock_start_grants_channel_trial_once(self):
        self.db.grant_channel_trial_once.return_value = True

        with patch.object(bot, "send", return_value={"ok": True}) as send_mock, patch.object(
            bot, "send_start_flow", return_value={"ok": True}
        ) as start_mock:
            bot.unlock_start_after_channel(100, 100)

        self.db.grant_channel_trial_once.assert_called_once_with(100, 7)
        self.assertIn("7 дней доступа", send_mock.call_args.args[1])
        start_mock.assert_called_once_with(100)

    def test_media_caption_and_file_are_both_cached(self):
        self.db.get_owner_by_connection.return_value = 100
        update = {
            "business_message": {
                "business_connection_id": "conn",
                "message_id": 10,
                "date": 1,
                "chat": {"id": 200, "first_name": "Chat"},
                "from": {"id": 300, "first_name": "Sender"},
                "caption": "caption",
                "video": {"file_id": "video-file"},
            }
        }

        bot.handle_update(update)

        self.db.cache_message.assert_called_once()
        self.db.cache_media.assert_called_once()
        self.assertEqual(self.db.cache_message.call_args.args[4], "caption")
        self.assertEqual(self.db.cache_media.call_args.args[5], "video-file")

    def test_incoming_business_reply_media_is_ignored(self):
        self.db.get_owner_by_connection.return_value = 100
        update = {
            "business_message": {
                "business_connection_id": "conn",
                "message_id": 11,
                "date": 1,
                "chat": {"id": 200, "first_name": "Chat"},
                "from": {"id": 300, "first_name": "Sender"},
                "text": "reply",
                "reply_to_message": {
                    "message_id": 10,
                    "photo": [{"file_id": "small-photo"}, {"file_id": "big-photo"}],
                },
            }
        }

        with patch.object(bot, "send_reply_media", return_value={"ok": True}) as send_reply_media:
            bot.handle_update(update)
            bot.handle_update(update)

        send_reply_media.assert_not_called()

    def test_business_owner_protected_reply_media_is_sent_once(self):
        self.db.get_owner_by_connection.return_value = 100
        update = {
            "business_message": {
                "business_connection_id": "conn",
                "message_id": 12,
                "date": 1,
                "chat": {"id": 200, "first_name": "Chat"},
                "from": {"id": 100, "first_name": "Owner"},
                "text": "reply",
                "reply_to_message": {
                    "message_id": 10,
                    "has_protected_content": True,
                    "photo": [{"file_id": "small-photo"}, {"file_id": "big-photo"}],
                },
            }
        }

        with patch.object(bot, "send_reply_media", return_value={"ok": True}) as send_reply_media:
            bot.handle_update(update)
            bot.handle_update(update)

        send_reply_media.assert_called_once()
        self.assertEqual(send_reply_media.call_args.args[0], 100)
        self.assertIn("Медиа из ответа", send_reply_media.call_args.args[2])
        self.assertTrue(send_reply_media.call_args.kwargs["prefer_upload"])
        self.db.cache_message.assert_not_called()

    def test_business_owner_own_protected_reply_media_is_ignored(self):
        self.db.get_owner_by_connection.return_value = 100
        update = {
            "business_message": {
                "business_connection_id": "conn",
                "message_id": 12,
                "date": 1,
                "chat": {"id": 200, "first_name": "Chat"},
                "from": {"id": 100, "first_name": "Owner"},
                "text": "reply",
                "reply_to_message": {
                    "message_id": 10,
                    "from": {"id": 100, "first_name": "Owner"},
                    "has_protected_content": True,
                    "photo": [{"file_id": "small-photo"}, {"file_id": "big-photo"}],
                },
            }
        }

        with patch.object(bot, "send_reply_media", return_value={"ok": True}) as send_reply_media, patch.object(
            bot, "send", return_value={"ok": True}
        ) as send_mock:
            bot.handle_update(update)

        send_reply_media.assert_not_called()
        send_mock.assert_not_called()
        self.db.cache_message.assert_not_called()

    def test_business_owner_plain_voice_reply_media_is_ignored(self):
        self.db.get_owner_by_connection.return_value = 100
        update = {
            "business_message": {
                "business_connection_id": "conn",
                "message_id": 12,
                "date": 1,
                "chat": {"id": 200, "first_name": "Chat"},
                "from": {"id": 100, "first_name": "Owner"},
                "text": "reply",
                "reply_to_message": {
                    "message_id": 10,
                    "voice": {"file_id": "voice-file"},
                },
            }
        }

        with patch.object(bot, "send_reply_media", return_value={"ok": True}) as send_reply_media, patch.object(
            bot, "send", return_value={"ok": True}
        ) as send_mock:
            bot.handle_update(update)

        send_reply_media.assert_not_called()
        send_mock.assert_not_called()
        self.db.cache_message.assert_not_called()

    def test_business_owner_protected_voice_reply_media_is_sent(self):
        self.db.get_owner_by_connection.return_value = 100
        update = {
            "business_message": {
                "business_connection_id": "conn",
                "message_id": 12,
                "date": 1,
                "chat": {"id": 200, "first_name": "Chat"},
                "from": {"id": 100, "first_name": "Owner"},
                "text": "reply",
                "reply_to_message": {
                    "message_id": 10,
                    "has_protected_content": True,
                    "voice": {"file_id": "voice-file"},
                },
            }
        }

        with patch.object(bot, "send_reply_media", return_value={"ok": True}) as send_reply_media:
            bot.handle_update(update)

        send_reply_media.assert_called_once()
        self.assertEqual(send_reply_media.call_args.args[0], 100)
        self.assertIn("Медиа из ответа", send_reply_media.call_args.args[2])
        self.assertTrue(send_reply_media.call_args.kwargs["prefer_upload"])

    def test_business_owner_plain_video_note_reply_media_is_ignored(self):
        self.db.get_owner_by_connection.return_value = 100
        update = {
            "business_message": {
                "business_connection_id": "conn",
                "message_id": 12,
                "date": 1,
                "chat": {"id": 200, "first_name": "Chat"},
                "from": {"id": 100, "first_name": "Owner"},
                "text": "reply",
                "reply_to_message": {
                    "message_id": 10,
                    "video_note": {"file_id": "video-note-file"},
                },
            }
        }

        with patch.object(bot, "send_reply_media", return_value={"ok": True}) as send_reply_media, patch.object(
            bot, "send", return_value={"ok": True}
        ) as send_mock:
            bot.handle_update(update)

        send_reply_media.assert_not_called()
        send_mock.assert_not_called()
        self.db.cache_message.assert_not_called()

    def test_business_reply_media_requires_active_subscription(self):
        self.db.get_owner_by_connection.return_value = 100
        self.db.is_sub_active.return_value = False
        self.db.get_referral_count.return_value = 0
        update = {
            "business_message": {
                "business_connection_id": "conn",
                "message_id": 12,
                "date": 1,
                "chat": {"id": 200, "first_name": "Chat"},
                "from": {"id": 100, "first_name": "Owner"},
                "text": "reply",
                "reply_to_message": {
                    "message_id": 10,
                    "has_protected_content": True,
                    "video": {"file_id": "video-file"},
                },
            }
        }

        with patch.object(bot, "send_reply_media", return_value={"ok": True}) as send_reply_media, patch.object(
            bot, "send", return_value={"ok": True}
        ) as send_mock:
            bot.handle_update(update)

        send_reply_media.assert_not_called()
        send_mock.assert_called_once()
        keyboard = send_mock.call_args.kwargs["keyboard"]
        self.assertTrue(keyboard["inline_keyboard"][0][0]["callback_data"].startswith("show_locked:"))

    def test_show_expired_deleted_callback_displays_payment_options(self):
        self.db.get_referral_count.return_value = 0
        update = {
            "callback_query": {
                "id": "callback-1",
                "from": {"id": 100},
                "data": "show_locked:missing",
                "message": {"chat": {"id": 100}, "message_id": 50},
            }
        }

        with patch.object(bot, "is_required_channel_member", return_value=True), patch.object(
            bot, "api", return_value={"ok": True}
        ) as api_mock:
            bot.handle_update(update)

        edit_call = [call for call in api_mock.call_args_list if call.args and call.args[0] == "editMessageText"][0]
        self.assertIn("Ваша подписка закончилась", edit_call.kwargs["text"])
        self.assertEqual(edit_call.kwargs["reply_markup"]["inline_keyboard"][0][0]["callback_data"], "buy_daily")

    def test_show_locked_callback_sends_saved_media_after_subscription_is_active(self):
        self.db.is_sub_active.return_value = True
        token = bot.create_pending_locked_message(
            100,
            "deleted",
            "чатом",
            {"message_id": 10, "voice": {"file_id": "voice-file"}},
        )
        update = {
            "callback_query": {
                "id": "callback-1",
                "from": {"id": 100},
                "data": f"show_locked:{token}",
                "message": {"chat": {"id": 100}, "message_id": 50},
            }
        }

        with patch.object(bot, "is_required_channel_member", return_value=True), patch.object(
            bot, "api", return_value={"ok": True}
        ) as api_mock, patch.object(
            bot, "send_reply_media", return_value={"ok": True}
        ) as send_reply_media:
            bot.handle_update(update)

        send_reply_media.assert_called_once()
        self.assertEqual(send_reply_media.call_args.args[0], 100)
        self.assertNotIn(token, bot._PENDING_LOCKED_MESSAGES)
        edit_call = [call for call in api_mock.call_args_list if call.args and call.args[0] == "editMessageText"][0]
        self.assertIn("Сообщение отправлено", edit_call.kwargs["text"])

    def test_incoming_business_reply_does_not_send_expired_notice(self):
        self.db.get_owner_by_connection.return_value = 100
        self.db.is_sub_active.return_value = False
        update = {
            "business_message": {
                "business_connection_id": "conn",
                "message_id": 12,
                "date": 1,
                "chat": {"id": 200, "first_name": "Chat"},
                "from": {"id": 300, "first_name": "Sender"},
                "text": "reply",
                "reply_to_message": {
                    "message_id": 10,
                    "voice": {"file_id": "voice-file"},
                },
            }
        }

        with patch.object(bot, "send_reply_media", return_value={"ok": True}) as send_reply_media, patch.object(
            bot, "send", return_value={"ok": True}
        ) as send_mock:
            bot.handle_update(update)

        send_reply_media.assert_not_called()
        send_mock.assert_not_called()

    def test_deleted_cache_is_kept_when_delivery_fails(self):
        self.db.get_owner_by_connection.return_value = 100
        self.db.get_cached_message.return_value = {
            "text": "<b>unsafe</b>",
            "date": "01.01.2026 10:00",
        }
        self.db.get_cached_media.return_value = None
        update = {
            "deleted_business_messages": {
                "business_connection_id": "conn",
                "chat": {"id": 200, "first_name": "Chat"},
                "message_ids": [10],
            }
        }

        with patch.object(bot, "allow_business_event", return_value=True), patch.object(
            bot, "send", return_value={"ok": False}
        ) as send_mock:
            bot.handle_update(update)

        self.assertIn("&lt;b&gt;unsafe&lt;/b&gt;", send_mock.call_args.args[1])
        self.db.delete_cached_message.assert_not_called()

    def test_edited_cache_advances_only_after_successful_notice(self):
        self.db.get_owner_by_connection.return_value = 100
        self.db.get_cached_message.return_value = {
            "text": "before",
            "date": "01.01.2026 10:00",
        }
        update = {
            "edited_business_message": {
                "business_connection_id": "conn",
                "message_id": 10,
                "chat": {"id": 200, "first_name": "Chat"},
                "text": "after",
            }
        }

        with patch.object(bot, "allow_business_event", return_value=True), patch.object(
            bot, "send", return_value={"ok": False}
        ):
            bot.handle_update(update)
        self.db.update_cached_text.assert_not_called()

        with patch.object(bot, "allow_business_event", return_value=True), patch.object(
            bot, "send", return_value={"ok": True}
        ):
            bot.handle_update(update)
        self.db.update_cached_text.assert_called_once_with("conn", 200, 10, "after")

    def test_successful_payment_is_granted_through_idempotent_db_operation(self):
        self.db.apply_stars_purchase.return_value = True
        update = {
            "message": {
                "chat": {"id": 100},
                "from": {"id": 100, "username": "buyer1", "first_name": "Buyer"},
                "successful_payment": {
                    "invoice_payload": "monthly",
                    "total_amount": bot.PRICE_MONTHLY,
                    "currency": "XTR",
                    "telegram_payment_charge_id": "charge-1",
                    "provider_payment_charge_id": "provider-1",
                },
            }
        }

        with patch.object(bot, "send", return_value={"ok": True}) as send_mock:
            bot.handle_update(update)

        self.db.apply_stars_purchase.assert_called_once()
        self.db.set_subscription.assert_not_called()
        send_mock.assert_called_once()

    def test_daily_subscription_invoice_uses_35_stars(self):
        update = {
            "callback_query": {
                "id": "callback-1",
                "from": {"id": 100},
                "data": "buy_daily",
                "message": {"chat": {"id": 100}, "message_id": 50},
            }
        }

        with patch.object(bot, "is_required_channel_member", return_value=True), patch.object(
            bot, "api", return_value={"ok": True}
        ) as api_mock:
            bot.handle_update(update)

        invoice_call = [call for call in api_mock.call_args_list if call.args and call.args[0] == "sendInvoice"][0]
        self.assertEqual(invoice_call.kwargs["payload"], "daily")
        self.assertEqual(invoice_call.kwargs["prices"][0]["amount"], 35)

    def test_daily_pre_checkout_is_accepted(self):
        update = {
            "pre_checkout_query": {
                "id": "pcq-1",
                "from": {"id": 100},
                "invoice_payload": "daily",
                "total_amount": 35,
                "currency": "XTR",
            }
        }

        with patch.object(bot, "api", return_value={"ok": True}) as api_mock:
            bot.handle_update(update)

        checkout_call = api_mock.call_args
        self.assertEqual(checkout_call.args[0], "answerPreCheckoutQuery")
        self.assertTrue(checkout_call.kwargs["ok"])

    def test_invalid_successful_payment_amount_is_rejected(self):
        update = {
            "message": {
                "chat": {"id": 100},
                "from": {"id": 100},
                "successful_payment": {
                    "invoice_payload": "monthly",
                    "total_amount": 1,
                    "currency": "XTR",
                    "telegram_payment_charge_id": "charge-1",
                },
            }
        }

        with patch.object(bot, "send", return_value={"ok": True}):
            bot.handle_update(update)

        self.db.apply_stars_purchase.assert_not_called()

    def test_referral_is_rewarded_only_when_business_connection_is_enabled(self):
        self.db.reward_referral_for_connection.return_value = 500
        update = {
            "business_connection": {
                "id": "conn",
                "user_chat_id": 100,
                "is_enabled": True,
                "user": {"id": 100, "username": "owner1", "first_name": "Owner"},
            }
        }

        with patch.object(bot, "send", return_value={"ok": True}) as send_mock:
            bot.handle_update(update)

        self.db.reward_referral_for_connection.assert_called_once_with(100)
        self.assertEqual(send_mock.call_count, 2)


class TelegramTransportTests(unittest.TestCase):
    def setUp(self):
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def handle(self):
                try:
                    super().handle()
                except ConnectionResetError:
                    # Failed downloads deliberately close an unread response.
                    pass

            def respond(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.server.requests.append((self.client_address, self.command, self.path, body))
                answer = self.server.answers.pop(0)
                if answer is None:
                    self.close_connection = True
                    return
                status, payload, *declared_length = answer
                if isinstance(payload, dict):
                    payload = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(declared_length[0] if declared_length else len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                if declared_length:
                    self.close_connection = True

            do_POST = respond
            do_GET = respond

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.requests = []
        self.server.answers = []
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.pool = bot.urllib3.PoolManager(maxsize=2)
        self.directory = tempfile.TemporaryDirectory()
        self.root = f"http://127.0.0.1:{self.server.server_port}"
        self.patches = [
            patch.object(bot, "BASE", self.root + "/botTEST"),
            patch.object(bot, "TELEGRAM_FILE_BASE", self.root + "/file/botTEST"),
            patch.object(bot, "_TELEGRAM_HTTP", self.pool),
            patch.object(bot.tempfile, "tempdir", self.directory.name),
        ]
        for item in self.patches:
            item.start()
        bot._REPLY_MEDIA_UPLOAD_ONLY.clear()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.pool.clear()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.directory.cleanup()
        bot._REPLY_MEDIA_UPLOAD_ONLY.clear()

    def test_photo_fallback_reuses_one_connection_for_api_download_and_upload(self):
        payload = b"photo-content" * 50000
        self.server.answers = [
            (400, {"ok": False, "error_code": 400, "description": "SelfDestructingPhoto"}),
            (200, {"ok": True, "result": {"file_path": "photos/image.jpg"}}),
            (200, payload),
            (200, {"ok": True, "result": {"message_id": 42}}),
        ]
        with self.assertLogs(level="INFO") as logs:
            result = bot.send_reply_media(100, {"photo": [{"file_id": "photo-id"}]}, "Notice")

        self.assertTrue(result["ok"])
        requests = self.server.requests
        self.assertEqual([item[2] for item in requests], [
            "/botTEST/sendPhoto", "/botTEST/getFile", "/file/botTEST/photos/image.jpg", "/botTEST/sendPhoto",
        ])
        self.assertEqual(len({item[0] for item in requests}), 1)
        self.assertIn(payload, requests[-1][3])
        self.assertIn(b"Notice", requests[-1][3])
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])
        self.assertTrue(any("Telegram getFile" in line for line in logs.output))
        self.assertTrue(any("total_with_getFile=" in line for line in logs.output))

    def test_http_errors_keep_telegram_details(self):
        for status in (400, 403, 409, 429, 500):
            with self.subTest(status=status):
                response = {"ok": False, "error_code": status, "parameters": {"retry_after": 5}}
                self.server.answers = [(status, response)]
                self.assertEqual(bot.api("getUpdates", timeout=50), response)
        self.assertEqual(len(self.server.requests), 5)

    def test_connection_loss_after_post_does_not_replay_or_fallback(self):
        self.server.answers = [None]
        with patch.object(bot, "send_downloaded_file") as fallback:
            result = bot.send_reply_media(100, {"voice": {"file_id": "voice-id"}})
        self.assertFalse(result["ok"])
        self.assertEqual(len(self.server.requests), 1)
        fallback.assert_not_called()

    def test_malformed_response_does_not_trigger_fallback(self):
        self.server.answers = [(502, b"upstream unavailable")]
        with patch.object(bot, "send_downloaded_file") as fallback:
            result = bot.send_reply_media(100, {"video": {"file_id": "video-id"}})
        self.assertFalse(result["ok"])
        fallback.assert_not_called()

    def test_failed_download_leaves_no_temp_file(self):
        self.server.answers = [
            (200, {"ok": True, "result": {"file_path": "photos/expired.jpg"}}),
            (404, b"not found"),
        ]
        self.assertIsNone(bot.download_telegram_file("id", "photo"))
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    def test_upload_connection_loss_does_not_resend_photo_as_document(self):
        self.server.answers = [
            (200, {"ok": True, "result": {"file_path": "photos/image.jpg"}}),
            (200, b"photo-content"),
            None,
        ]
        self.assertFalse(bot.send_downloaded_file(100, "id", "photo")["ok"])
        self.assertEqual(len(self.server.requests), 3)
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    def test_truncated_download_is_removed_and_connection_is_replaced(self):
        self.server.answers = [
            (200, {"ok": True, "result": {"file_path": "photos/image.jpg"}}),
            (200, b"partial", 100),
            (200, {"ok": True}),
        ]
        self.assertIsNone(bot.download_telegram_file("id", "photo"))
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])
        self.assertTrue(bot.api("getMe")["ok"])
        self.assertNotEqual(self.server.requests[1][0], self.server.requests[2][0])


if __name__ == "__main__":
    unittest.main()
