import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from consent_governance.texts import GovernanceError  # noqa: E402
from support import (  # noqa: E402
    FIELDS, PURPOSE, PURPOSE_NEW, RECIPIENTS, TEXT, build_basic_scenario, make_service,
)


class TextVersionTests(unittest.TestCase):
    def setUp(self):
        self.svc, self.clock, _ = make_service()
        self.svc.texts.register_text(TEXT, "授权书")

    def test_revisions_are_immutable_snapshots(self):
        r1 = self.svc.texts.publish(TEXT, [PURPOSE], FIELDS, 365, RECIPIENTS)
        snap = self.svc.texts.get_snapshot(TEXT, r1)
        snap["purposes"].append(PURPOSE_NEW)  # 拿到的是副本，不影响存储
        self.assertEqual(self.svc.texts.get_snapshot(TEXT, r1)["purposes"], [PURPOSE])

    def test_purpose_expansion_requires_new_revision(self):
        r1 = self.svc.texts.publish(TEXT, [PURPOSE], FIELDS, 365, RECIPIENTS)
        r2 = self.svc.texts.publish(TEXT, [PURPOSE, PURPOSE_NEW], FIELDS, 365, RECIPIENTS)
        self.assertEqual(r2, r1 + 1)
        self.assertEqual(self.svc.texts.current_revision(TEXT), r2)

    def test_purpose_shrink_is_rejected(self):
        self.svc.texts.publish(TEXT, [PURPOSE, PURPOSE_NEW], FIELDS, 365, RECIPIENTS)
        with self.assertRaises(GovernanceError):
            self.svc.texts.publish(TEXT, [PURPOSE], FIELDS, 365, RECIPIENTS)

    def test_identical_republish_rejected(self):
        self.svc.texts.publish(TEXT, [PURPOSE], FIELDS, 365, RECIPIENTS)
        with self.assertRaises(GovernanceError):
            self.svc.texts.publish(TEXT, [PURPOSE], FIELDS, 365, RECIPIENTS)

    def test_invalid_snapshots_rejected(self):
        with self.assertRaises(GovernanceError):
            self.svc.texts.publish(TEXT, [], FIELDS, 365, RECIPIENTS)
        with self.assertRaises(GovernanceError):
            self.svc.texts.publish(TEXT, [PURPOSE], FIELDS, 0, RECIPIENTS)
        with self.assertRaises(GovernanceError):
            self.svc.texts.publish(TEXT, [PURPOSE], FIELDS, 365, [])


class ConsentLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.svc, self.clock, _ = make_service()
        self.ids = build_basic_scenario(self.svc)
        self.rev = self.ids["rev"]

    def test_old_consent_does_not_cover_new_purpose(self):
        """用途扩大后，旧版本同意不自动覆盖新用途。"""
        self.assertTrue(self.svc.texts.covers_purpose(TEXT, self.rev, PURPOSE))
        self.assertFalse(self.svc.texts.covers_purpose(TEXT, self.rev, PURPOSE_NEW))
        with self.assertRaises(GovernanceError):
            self.svc.consent.agree("S-001", PURPOSE_NEW, TEXT, self.rev)

    def test_new_purpose_consent_requires_new_version(self):
        r2 = self.svc.texts.publish(TEXT, [PURPOSE, PURPOSE_NEW], FIELDS, 365, RECIPIENTS)
        ev = self.svc.consent.agree("S-001", PURPOSE_NEW, TEXT, r2)
        self.assertEqual(ev["text_revision"], r2)

    def test_refuse_withdraw_reagree_lifecycle(self):
        # 拒绝先行
        svc2, _, _ = make_service()
        svc2.texts.register_text(TEXT, "授权书")
        svc2.texts.publish(TEXT, [PURPOSE], FIELDS, 365, RECIPIENTS)
        svc2.consent.refuse("S-009", PURPOSE, TEXT, 1)
        self.assertIsNone(svc2.consent.effective_basis("S-009", PURPOSE))
        # 拒绝之后可同意
        svc2.consent.agree("S-009", PURPOSE, TEXT, 1)
        self.assertIsNotNone(svc2.consent.effective_basis("S-009", PURPOSE))

        # 撤回
        self.svc.consent.withdraw("S-001", PURPOSE, TEXT, self.rev)
        self.assertIsNone(self.svc.consent.effective_basis("S-001", PURPOSE))
        # 撤回后不能用 agree，必须 reagree
        with self.assertRaises(GovernanceError):
            self.svc.consent.agree("S-001", PURPOSE, TEXT, self.rev)
        self.svc.consent.reagree("S-001", PURPOSE, TEXT, self.rev)
        basis = self.svc.consent.effective_basis("S-001", PURPOSE)
        self.assertIsNotNone(basis)
        self.assertEqual(basis["event"]["kind"], "reagree")

    def test_retention_expiry(self):
        self.assertIsNotNone(self.svc.consent.effective_basis("S-001", PURPOSE))
        self.clock.advance(days=366)
        self.assertIsNone(self.svc.consent.effective_basis("S-001", PURPOSE))

    def test_repeated_withdraw_is_natural_debounce(self):
        first = self.svc.consent.withdraw("S-001", PURPOSE, TEXT, self.rev)
        second = self.svc.consent.withdraw("S-001", PURPOSE, TEXT, self.rev)
        self.assertTrue(second.get("replayed"))
        self.assertEqual(first["event_id"], second["event_id"])
        # 账本只有一条撤回事件
        kinds = [e["kind"] for e in self.svc.consent.events("S-001")]
        self.assertEqual(kinds.count("withdraw"), 1)

    def test_idempotency_key_dedups_across_calls(self):
        a = self.svc.consent.withdraw("S-002", PURPOSE, TEXT, self.rev, request_id="REQ-W-1")
        b = self.svc.consent.withdraw("S-002", PURPOSE, TEXT, self.rev, request_id="REQ-W-1")
        self.assertEqual(a["event_id"], b["event_id"])


if __name__ == "__main__":
    unittest.main()
