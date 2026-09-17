import importlib
import sys
import types
import unittest
import zoneinfo
from datetime import timezone
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
        self.original_db = bot.db
        bot.db = self.db
        bot._BUSINESS_REPLY_MEDIA_SENT.clear()

    def tearDown(self):
        bot.db = self.original_db

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

    def test_business_owner_reply_media_is_not_ignored(self):
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

        with patch.object(bot, "send_reply_media", return_value={"ok": True}) as send_reply_media:
            bot.handle_update(update)
            bot.handle_update(update)

        send_reply_media.assert_called_once()
        self.assertEqual(send_reply_media.call_args.args[0], 100)
        self.assertIn("удалено сообщение", send_reply_media.call_args.args[2])
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
                    "voice": {"file_id": "voice-file"},
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
        self.assertEqual(keyboard["inline_keyboard"][0][0]["callback_data"], "show_expired_deleted")

    def test_show_expired_deleted_callback_displays_payment_options(self):
        self.db.get_referral_count.return_value = 0
        update = {
            "callback_query": {
                "id": "callback-1",
                "from": {"id": 100},
                "data": "show_expired_deleted",
                "message": {"chat": {"id": 100}, "message_id": 50},
            }
        }

        with patch.object(bot, "api", return_value={"ok": True}) as api_mock:
            bot.handle_update(update)

        edit_call = [call for call in api_mock.call_args_list if call.args and call.args[0] == "editMessageText"][0]
        self.assertIn("Ваша подписка закончилась", edit_call.kwargs["text"])
        self.assertEqual(edit_call.kwargs["reply_markup"]["inline_keyboard"][0][0]["callback_data"], "buy_weekly")

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


if __name__ == "__main__":
    unittest.main()
