"""Chat transport and receipt regressions with local structural markers."""

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from persona_runtime.config import ConfigStore
from persona_runtime.desktop_client import TransportUnknown
from persona_runtime.models import RuntimeErrorBase, generic_persona, identifier
from persona_runtime.paths import RuntimePaths
from persona_runtime.service import RuntimeService
from persona_runtime.sessions import select
from persona_runtime.storage import Store
from persona_runtime.tk_notice import CONTRACT
from persona_runtime.workbench_conversation import WorkbenchConversation


class WorkbenchConversationTests(unittest.TestCase):
    def setUp(self):
        parent = RuntimePaths.from_environment().home / "tmp"
        parent.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(temp.cleanup)
        self.paths = RuntimePaths(Path(temp.name))
        self.config = ConfigStore(self.paths)
        self.config.initialize()
        self.config.update({"model": {"base_url": "https://api.deepseek.com", "name": "deepseek-v4-pro", "api_key": "0"}})
        self.store = Store(self.paths)
        self.store.initialize()
        service = RuntimeService(self.store)
        self.person = service.create_persona(replace(generic_persona(), name="1"))["id"]
        self.other = service.create_persona(replace(generic_persona(), name="2"))["id"]
        service.use_persona(self.person)
        with self.store.connection(write=True) as db:
            self.session = select(db, self.person, "desktop")
            self.other_session = select(db, self.other, "desktop")
        self.visible = True
        self.chat = WorkbenchConversation(self.paths, lambda: self.visible, lambda: True)

    def accepted(self):
        return {"channel": "desktop", "persona_id": self.person, "session_id": self.session, "receipt_id": "1"}

    def test_send_uses_captured_scope_and_preserves_the_next_draft(self):
        self.chat.save_draft(self.person, self.session, "11", 1)
        self.chat.save_draft(self.person, self.session, "12", 2)
        self.chat.save_draft(self.person, self.session, "10", 1)
        request_id = identifier()
        with patch.object(self.chat.client, "submit_input", return_value=self.accepted()) as submit:
            result = self.chat.send(self.person, self.session, "11", request_id, 1)
            self.assertEqual(result, {"state": "accepted"})
            submit.assert_called_once_with({"persona_id": self.person, "session_id": self.session,
                                           "channel": "desktop", "text": "11", "request_id": request_id,
                                           "desktop_presentation": CONTRACT})
            with self.assertRaises(RuntimeErrorBase):
                self.chat.send(self.person, self.other_session, "99", identifier(), 3)
            with self.assertRaises(RuntimeErrorBase):
                self.chat.send(self.other, self.other_session, "99", identifier(), 3)
            self.assertEqual(submit.call_count, 1)
        self.assertEqual(self.chat.get_draft(self.person, self.session), "12")
        reopened = WorkbenchConversation(self.paths, lambda: True, lambda: True)
        self.assertEqual(reopened.get_draft(self.person, self.session), "12")
        self.assertEqual(reopened.get_draft(self.other, self.other_session), "")

    def test_lost_submission_is_checked_without_resending_even_after_reopen(self):
        request_id = identifier()
        self.chat.save_draft(self.person, self.session, "21", 1)
        def losing_call(method, path, value=None):
            if path.endswith("/" + request_id):
                from persona_runtime.desktop_client import LocalAPIError
                raise LocalAPIError("submission_not_found", 404)
            if path == "/v1/health":
                return {"capabilities": ["durable_ingress_v2", "tk_notice_presented_v1"]}
            self.assertEqual((method, path), ("POST", "/v2/ingress"))
            raise TransportUnknown("1")
        with patch.object(self.chat.client, "call", side_effect=losing_call):
            self.assertEqual(self.chat.send(self.person, self.session, "21", request_id, 1)["state"],
                             "unconfirmed")
        reopened = WorkbenchConversation(self.paths, lambda: True, lambda: True)
        self.assertEqual(reopened.state(self.person, self.session)["pending"]["id"], request_id)
        with patch.object(reopened.client, "lookup", return_value=self.accepted()) as lookup:
            with patch.object(reopened.client, "submit_input") as submit:
                self.assertEqual(reopened.check_submission(self.person, self.session, None)["state"], "accepted")
                lookup.assert_called_once_with({"request_id": request_id})
                submit.assert_not_called()
        self.assertIsNone(reopened.state(self.person, self.session)["pending"])
        self.assertEqual(reopened.get_draft(self.person, self.session), "")

    def test_retry_reuses_original_identity_only_after_explicit_lookup(self):
        request_id = identifier()
        payload = {"request_id": request_id, "text": "31", "persona_id": self.person,
                   "session_id": self.session, "channel": "desktop", "desktop_presentation": CONTRACT}
        self.chat.client.journal.remember(payload)
        with patch.object(self.chat.client, "submit_input", return_value=self.accepted()) as submit:
            with self.assertRaises(RuntimeErrorBase):
                self.chat.retry_submission(self.person, self.session)
            with patch.object(self.chat.client, "lookup", return_value=None):
                self.assertEqual(self.chat.check_submission(self.person, self.session, request_id)["state"],
                                 "not_accepted")
            submit.assert_not_called()
            self.assertEqual(self.chat.retry_submission(self.person, self.session)["state"], "accepted")
            submit.assert_called_once_with(payload)

    def test_cancellation_rejects_a_turn_from_another_session(self):
        foreign = {"persona_id": self.other, "session_id": self.other_session,
                   "channel": "desktop", "id": "1", "status": "queued"}
        with patch.object(self.chat.client, "call", return_value=foreign):
            with patch.object(self.chat.client, "cancel_request") as cancel:
                with self.assertRaises(RuntimeErrorBase):
                    self.chat.cancel(self.person, self.session, "turn", "1")
                cancel.assert_not_called()

    def test_lost_presentation_ack_reuses_the_same_receipt_and_decision(self):
        target = ("scene", {"turn_id": "6", "utterance_id": "7", "publication_id": "8",
                            "revision": 1, "ordinal": 0})
        offer = {"state": "offered", "id": "9", "lease_id": "5", "body_hash": "4", "body": "41"}
        with patch.object(self.chat, "_next_presentation", return_value=target):
            with patch.object(self.chat.client, "manage", side_effect=[offer, TransportUnknown("1"), {}]) as manage:
                ticket = self.chat.offer(self.person, self.session)["ticket"]
                with self.assertRaises(TransportUnknown):
                    self.chat.acknowledge(ticket, True)
                self.visible = False
                self.chat.acknowledge(ticket, False)
                self.assertEqual(manage.call_args_list[1], manage.call_args_list[2])
                self.assertEqual(manage.call_args.kwargs["state"], "presented")
                self.assertEqual(manage.call_args.kwargs["receipt_id"], ticket)

    def test_unseen_notice_does_not_release_search(self):
        target = ("notice", {"turn_id": "6", "notice_id": "7"})
        with patch.object(self.chat, "_next_presentation", return_value=target):
            with patch.object(self.chat.client, "manage", return_value={"text": "51"}) as manage:
                ticket = self.chat.offer(self.person, self.session)["ticket"]
                self.visible = False
                self.chat.acknowledge(ticket, True)
                self.assertIsNone(self.chat.offer(self.person, self.session))
                manage.assert_called_once_with("chat.notice.preview", persona_id=self.person,
                                               session_id=self.session, turn_id="6", notice_id="7")


if __name__ == "__main__":
    unittest.main()
