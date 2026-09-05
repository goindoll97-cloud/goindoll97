import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from structural_rule_drafting import (
    CROSS_NOTICE_EXCLUSION_RE,
    build_draft_review_hints,
    draft_and_cache,
    draft_structural_rule,
    draft_structural_rules,
    extract_no_cas_cross_regime_cases,
    extract_no_cas_master_cases,
    test_draft_against_smiles,
    to_extended_scope_rows,
)


TRIARYLTIN_TEXT = (
    "수산화 트리아릴주석 및 그 염류(산화트리아릴주석을 포함한다). "
    "다만, 본 고시에서 별도로 규정한 것은 제외"
)
SELENIUM_TEXT = "셀레늄 및 그 화합물. 다만, 본 고시에서 별도로 규정한 것은 제외"
PHOSPHINE_MIXTURE_TEXT = (
    "산화트리헥실포스핀, 산화 트리-n-옥틸포스핀, 산화모노옥틸디헥실포스핀의 혼합물"
)


def _mock_response(**overrides):
    base = {
        "scope_type": "STRUCTURAL_RANGE",
        "required_smarts": "",
        "excluded_smarts": "",
        "excluded_cas": "",
        "explanation_ko": "",
        "confidence": 0.5,
        "evidence_quotes": [],
    }
    base.update(overrides)
    return base


class CrossNoticeExclusionDetectionTest(unittest.TestCase):
    """The '다만, 본 고시에서 별도로 규정한 것은 제외' clause is fixed legal
    drafting -- detecting it must be rule-based (always correct), not left to
    the model's judgment on any given call."""

    def test_detects_the_exact_recurring_phrase(self):
        self.assertTrue(CROSS_NOTICE_EXCLUSION_RE.search(TRIARYLTIN_TEXT))
        self.assertTrue(CROSS_NOTICE_EXCLUSION_RE.search(SELENIUM_TEXT))

    def test_absent_when_the_clause_is_not_present(self):
        self.assertFalse(CROSS_NOTICE_EXCLUSION_RE.search(PHOSPHINE_MIXTURE_TEXT))

    def test_flag_is_set_independent_of_the_llm_call(self):
        with patch(
            "structural_rule_drafting._post_draft_chat",
            return_value=_mock_response(),
        ):
            audit = draft_structural_rule(
                {"case_id": "C1", "source_text": TRIARYLTIN_TEXT, "regime": "RESTRICTED"}
            )
        self.assertTrue(audit["llm_cross_notice_exclusion"])


class SmartsValidationTest(unittest.TestCase):
    """RDKit is the ground truth: an unparsable SMARTS must never survive
    into the draft, since a reviewer trusting a broken pattern is worse than
    a reviewer seeing an empty one."""

    def test_valid_organotin_smarts_is_kept(self):
        with patch(
            "structural_rule_drafting._post_draft_chat",
            return_value=_mock_response(
                required_smarts="[Sn](-c)(-c)(-c)[OX2,OX1-]",
                evidence_quotes=["수산화 트리아릴주석"],
            ),
        ):
            audit = draft_structural_rule(
                {"case_id": "C1", "source_text": TRIARYLTIN_TEXT, "regime": "RESTRICTED"}
            )
        self.assertEqual(audit["llm_required_smarts"], "[Sn](-c)(-c)(-c)[OX2,OX1-]")
        self.assertTrue(audit["llm_smarts_valid"])

    def test_syntactically_invalid_smarts_is_dropped_not_passed_through(self):
        with patch(
            "structural_rule_drafting._post_draft_chat",
            return_value=_mock_response(required_smarts="[Sn](((not valid"),
        ):
            audit = draft_structural_rule(
                {"case_id": "C1", "source_text": TRIARYLTIN_TEXT, "regime": "RESTRICTED"}
            )
        self.assertEqual(audit["llm_required_smarts"], "")
        self.assertFalse(audit["llm_smarts_valid"])

    def test_invalid_excluded_smarts_is_also_dropped(self):
        with patch(
            "structural_rule_drafting._post_draft_chat",
            return_value=_mock_response(
                required_smarts="[Se]", excluded_smarts="totally not smarts )))"
            ),
        ):
            audit = draft_structural_rule(
                {"case_id": "C2", "source_text": SELENIUM_TEXT, "regime": "PROHIBITED"}
            )
        self.assertEqual(audit["llm_required_smarts"], "[Se]")
        self.assertEqual(audit["llm_excluded_smarts"], "")
        self.assertFalse(audit["llm_smarts_valid"])

    def test_empty_smarts_is_not_treated_as_invalid(self):
        with patch(
            "structural_rule_drafting._post_draft_chat",
            return_value=_mock_response(required_smarts="", confidence=0.1),
        ):
            audit = draft_structural_rule(
                {"case_id": "C3", "source_text": PHOSPHINE_MIXTURE_TEXT, "regime": "RESTRICTED"}
            )
        self.assertTrue(audit["llm_smarts_valid"])


class EvidenceQuoteValidationTest(unittest.TestCase):
    """A quote that doesn't actually appear in the source text is a
    hallucination and must not count as supporting evidence, mirroring the
    same check local_llm.py already applies to review explanations."""

    def test_verbatim_quote_is_kept(self):
        with patch(
            "structural_rule_drafting._post_draft_chat",
            return_value=_mock_response(
                required_smarts="[Se]",
                confidence=0.8,
                evidence_quotes=["셀레늄 및 그 화합물"],
            ),
        ):
            audit = draft_structural_rule(
                {"case_id": "C2", "source_text": SELENIUM_TEXT, "regime": "PROHIBITED"}
            )
        self.assertIn("셀레늄 및 그 화합물", audit["llm_evidence_quotes"])
        self.assertEqual(audit["llm_confidence"], 0.8)

    def test_fabricated_quote_is_dropped_and_confidence_capped(self):
        with patch(
            "structural_rule_drafting._post_draft_chat",
            return_value=_mock_response(
                required_smarts="[Se]",
                confidence=0.9,
                evidence_quotes=["이 문구는 원문에 없습니다"],
            ),
        ):
            audit = draft_structural_rule(
                {"case_id": "C2", "source_text": SELENIUM_TEXT, "regime": "PROHIBITED"}
            )
        self.assertEqual(audit["llm_evidence_quotes"], "")
        self.assertLessEqual(audit["llm_confidence"], 0.3)


class DraftNeverAutoApprovesTest(unittest.TestCase):
    def test_every_draft_is_review_required_regardless_of_confidence(self):
        with patch(
            "structural_rule_drafting._post_draft_chat",
            return_value=_mock_response(
                required_smarts="[Sn](-c)(-c)(-c)[OX2,OX1-]",
                confidence=0.99,
                evidence_quotes=["수산화 트리아릴주석"],
            ),
        ):
            audit = draft_structural_rule(
                {"case_id": "C1", "source_text": TRIARYLTIN_TEXT, "regime": "RESTRICTED"}
            )
        self.assertEqual(audit["approval_status"], "CANDIDATE_REVIEW_REQUIRED")

    def test_llm_error_never_raises_and_is_reported(self):
        with patch(
            "structural_rule_drafting._post_draft_chat",
            side_effect=ConnectionError("ollama down"),
        ):
            audit = draft_structural_rule(
                {"case_id": "C1", "source_text": TRIARYLTIN_TEXT, "regime": "RESTRICTED"}
            )
        self.assertIn("LLM_ERROR", audit["llm_status"])
        self.assertEqual(audit["approval_status"], "CANDIDATE_REVIEW_REQUIRED")

    def test_missing_source_text_is_reported_without_calling_the_model(self):
        with patch("structural_rule_drafting._post_draft_chat") as mocked:
            audit = draft_structural_rule({"case_id": "C9", "source_text": ""})
        mocked.assert_not_called()
        self.assertEqual(audit["llm_status"], "NO_SOURCE_TEXT")


class ToExtendedScopeRowsTest(unittest.TestCase):
    def test_output_columns_match_the_real_approved_file_schema(self):
        with open(
            "data/extended_scope_rules_TEMPLATE.csv", encoding="utf-8"
        ) as handle:
            real_columns = handle.readline().strip().split(",")
        with patch(
            "structural_rule_drafting._post_draft_chat",
            return_value=_mock_response(
                required_smarts="[Sn](-c)(-c)(-c)[OX2,OX1-]",
                evidence_quotes=["수산화 트리아릴주석"],
            ),
        ):
            audit = draft_structural_rules(
                [{"case_id": "C1", "source_text": TRIARYLTIN_TEXT, "regime": "RESTRICTED",
                  "designation_id": "06-5-99"}]
            )
        rows = to_extended_scope_rows(audit, "제한물질·금지물질의 지정")
        self.assertEqual(list(rows.columns), real_columns)

    def test_unverified_flags_are_always_false_never_auto_approved(self):
        with patch(
            "structural_rule_drafting._post_draft_chat",
            return_value=_mock_response(required_smarts="[Se]", confidence=0.95),
        ):
            audit = draft_structural_rules(
                [{"case_id": "C2", "source_text": SELENIUM_TEXT, "regime": "PROHIBITED"}]
            )
        rows = to_extended_scope_rows(audit, "제한물질·금지물질의 지정")
        self.assertFalse(rows.iloc[0]["automation_status"])
        self.assertFalse(rows.iloc[0]["source_verified"])

    def test_cross_notice_exclusion_becomes_a_composition_condition_note(self):
        with patch(
            "structural_rule_drafting._post_draft_chat",
            return_value=_mock_response(required_smarts="[Se]"),
        ):
            audit = draft_structural_rules(
                [{"case_id": "C2", "source_text": SELENIUM_TEXT, "regime": "PROHIBITED"}]
            )
        rows = to_extended_scope_rows(audit, "제한물질·금지물질의 지정")
        self.assertIn("우선함", rows.iloc[0]["composition_condition"])

    def test_empty_audit_yields_empty_frame_with_correct_columns(self):
        rows = to_extended_scope_rows(pd.DataFrame(), "x")
        self.assertTrue(rows.empty)


class DraftHintMatchingTest(unittest.TestCase):
    """A material's structure is tested against an unapproved draft only to
    surface a review hint -- it must never claim a confirmed result and must
    correctly distinguish chemically different structures."""

    DRAFT = {
        "llm_required_smarts": "[Sn](-c)(-c)(-c)[OX2,OX1-]",
        "llm_excluded_smarts": "",
        "designation_id": "06-5-99",
        "regime": "RESTRICTED",
        "source_text": TRIARYLTIN_TEXT,
        "llm_explanation_ko": "트리아릴주석 골격",
        "llm_confidence": 0.6,
    }

    def test_triphenyltin_hydroxide_matches_the_aryl_pattern(self):
        result = test_draft_against_smiles(
            "c1ccccc1[Sn](O)(c1ccccc1)c1ccccc1", self.DRAFT
        )
        self.assertTrue(result["matched"])
        self.assertIn("미검증", result["reason"])

    def test_tributyltin_alkyl_form_does_not_match_the_aryl_only_pattern(self):
        result = test_draft_against_smiles("CCCC[Sn](O)(CCCC)CCCC", self.DRAFT)
        self.assertFalse(result["matched"])

    def test_unrelated_molecule_does_not_match(self):
        result = test_draft_against_smiles("CCO", self.DRAFT)
        self.assertFalse(result["matched"])

    def test_missing_smiles_is_handled_without_crashing(self):
        result = test_draft_against_smiles("", self.DRAFT)
        self.assertFalse(result["matched"])

    def test_excluded_pattern_overrides_a_required_match(self):
        draft_with_exclusion = dict(self.DRAFT, llm_excluded_smarts="[Sn](-c)(-c)(-c)[OX2,OX1-]")
        result = test_draft_against_smiles(
            "c1ccccc1[Sn](O)(c1ccccc1)c1ccccc1", draft_with_exclusion
        )
        self.assertFalse(result["matched"])
        self.assertIn("제외", result["reason"])


class BuildDraftReviewHintsTest(unittest.TestCase):
    def test_matched_hint_is_produced_for_a_held_material_only(self):
        plain_summary = pd.DataFrame(
            [
                {"물질명": "트리페닐주석 화합물", "CAS번호": "76-87-9", "판정결과": "보류(추가 확인 필요)"},
                {"물질명": "에탄올", "CAS번호": "64-17-5", "판정결과": "비해당"},
            ]
        )
        enriched = pd.DataFrame(
            [
                {"input_cas": "76-87-9", "smiles": "c1ccccc1[Sn](O)(c1ccccc1)c1ccccc1"},
                {"input_cas": "64-17-5", "smiles": "CCO"},
            ]
        )
        with patch(
            "structural_rule_drafting._post_draft_chat",
            return_value=_mock_response(
                required_smarts="[Sn](-c)(-c)(-c)[OX2,OX1-]",
                evidence_quotes=["수산화 트리아릴주석"],
            ),
        ):
            audit = draft_structural_rules(
                [{"case_id": "S1", "source_text": TRIARYLTIN_TEXT, "regime": "RESTRICTED",
                  "designation_id": "06-5-99"}]
            )
        hints = build_draft_review_hints(plain_summary, enriched, audit)
        self.assertEqual(len(hints), 1)
        self.assertEqual(hints.iloc[0]["cas"], "76-87-9")

    def test_a_confirmed_regulated_material_is_not_offered_a_hint(self):
        plain_summary = pd.DataFrame(
            [{"물질명": "트리페닐주석 화합물", "CAS번호": "76-87-9", "판정결과": "규제 해당"}]
        )
        enriched = pd.DataFrame(
            [{"input_cas": "76-87-9", "smiles": "c1ccccc1[Sn](O)(c1ccccc1)c1ccccc1"}]
        )
        with patch(
            "structural_rule_drafting._post_draft_chat",
            return_value=_mock_response(required_smarts="[Sn](-c)(-c)(-c)[OX2,OX1-]"),
        ):
            audit = draft_structural_rules(
                [{"case_id": "S1", "source_text": TRIARYLTIN_TEXT, "regime": "RESTRICTED"}]
            )
        hints = build_draft_review_hints(plain_summary, enriched, audit)
        self.assertTrue(hints.empty)

    def test_no_drafts_yields_empty_hints(self):
        plain_summary = pd.DataFrame(
            [{"물질명": "X", "CAS번호": "1-1-1", "판정결과": "보류(추가 확인 필요)"}]
        )
        hints = build_draft_review_hints(plain_summary, pd.DataFrame(), pd.DataFrame())
        self.assertTrue(hints.empty)


class ExtractNoCasMasterCasesTest(unittest.TestCase):
    """This is the scale-up mechanism: instead of a person hand-picking one
    compound family at a time, every no-CAS row in the whole notice is found
    in one pass."""

    def _master_row(self, **overrides):
        row = {
            "notice": "2026-5",
            "designation_id": "9999-1-1",
            "rule_id": "R1",
            "direct_cas": "",
            "substance_name_ko": "테스트 화합물군",
            "main_rule_text": "테스트 화합물군 및 그 염류",
            "source_text": "",
        }
        row.update(overrides)
        return row

    def test_finds_a_genuine_no_cas_row(self):
        master = pd.DataFrame([self._master_row()])
        cases = extract_no_cas_master_cases(master, "2026-5")
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]["designation_id"], "9999-1-1")

    def test_skips_rows_that_have_a_direct_cas(self):
        master = pd.DataFrame([self._master_row(direct_cas="50-00-0")])
        self.assertEqual(extract_no_cas_master_cases(master, "2026-5"), [])

    def test_skips_abolished_designations(self):
        master = pd.DataFrame([self._master_row(substance_name_ko="삭제", main_rule_text="")])
        self.assertEqual(extract_no_cas_master_cases(master, "2026-5"), [])

    def test_skips_rows_with_no_text_to_draft_from(self):
        master = pd.DataFrame([self._master_row(main_rule_text="", source_text="")])
        self.assertEqual(extract_no_cas_master_cases(master, "2026-5"), [])

    def test_only_the_requested_notice_is_scanned(self):
        master = pd.DataFrame(
            [self._master_row(notice="2025-2", designation_id="OLD-1")]
        )
        self.assertEqual(extract_no_cas_master_cases(master, "2026-5"), [])

    def test_real_master_file_yields_the_expected_count(self):
        master = pd.read_csv(
            "data/regulatory_rules_master_APPROVED.csv", low_memory=False
        )
        master.columns = [c.strip().lstrip("\ufeff") for c in master.columns]
        cases = extract_no_cas_master_cases(master, "2026-5")
        # matches the count verified directly against the real file
        self.assertEqual(len(cases), 96)
        self.assertTrue(all(c["case_id"].startswith("MASTER-") for c in cases))


class DraftAndCacheTest(unittest.TestCase):
    def test_second_call_reuses_the_cache_without_calling_the_model(self):
        cases = [
            {"case_id": "M1", "source_text": "화합물군 1", "regime": "HUMAN_HAZARD"},
            {"case_id": "M2", "source_text": "화합물군 2", "regime": "HUMAN_HAZARD"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "drafts.csv"
            with patch(
                "structural_rule_drafting._post_draft_chat",
                return_value=_mock_response(),
            ) as mocked:
                first = draft_and_cache(cases, cache_path)
                self.assertEqual(mocked.call_count, 2)
                second = draft_and_cache(cases, cache_path)
                self.assertEqual(mocked.call_count, 2)  # unchanged -- cache hit
            self.assertEqual(len(first), len(second) == len(first) and len(first) or 0)
            self.assertTrue(cache_path.exists())

    def test_unchanged_cases_are_reused_when_a_new_case_is_added(self):
        # Adding M2 must not force M1 (whose text is unchanged) to be
        # redrafted -- only the genuinely new case should cost a model call.
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "drafts.csv"
            with patch(
                "structural_rule_drafting._post_draft_chat",
                return_value=_mock_response(),
            ) as mocked:
                draft_and_cache(
                    [{"case_id": "M1", "source_text": "화합물군 1", "regime": "HUMAN_HAZARD"}],
                    cache_path,
                )
                self.assertEqual(mocked.call_count, 1)
                draft_and_cache(
                    [
                        {"case_id": "M1", "source_text": "화합물군 1", "regime": "HUMAN_HAZARD"},
                        {"case_id": "M2", "source_text": "화합물군 2", "regime": "HUMAN_HAZARD"},
                    ],
                    cache_path,
                )
                self.assertEqual(mocked.call_count, 2)  # only M2 cost a fresh call

    def test_a_law_revision_changing_existing_wording_is_redrafted(self):
        # Same case_id, but the notice text was actually amended -- this must
        # NOT silently keep serving the pre-amendment draft.
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "drafts.csv"

            def fake_post(source_text, timeout=180):
                return _mock_response(explanation_ko=source_text)

            with patch(
                "structural_rule_drafting._post_draft_chat", side_effect=fake_post
            ) as mocked:
                before = draft_and_cache(
                    [{"case_id": "M1", "source_text": "개정 전 조문", "regime": "PROHIBITED"}],
                    cache_path,
                )
                self.assertEqual(mocked.call_count, 1)
                self.assertEqual(before.iloc[0]["llm_explanation_ko"], "개정 전 조문")

                after = draft_and_cache(
                    [{"case_id": "M1", "source_text": "개정 후 새 조문", "regime": "PROHIBITED"}],
                    cache_path,
                )
        self.assertEqual(mocked.call_count, 2)  # the changed case was redrafted
        self.assertEqual(after.iloc[0]["llm_explanation_ko"], "개정 후 새 조문")

    def test_new_cases_not_in_the_cache_trigger_a_fresh_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "drafts.csv"
            with patch(
                "structural_rule_drafting._post_draft_chat",
                return_value=_mock_response(),
            ) as mocked:
                draft_and_cache(
                    [{"case_id": "M1", "source_text": "화합물군 1", "regime": "HUMAN_HAZARD"}],
                    cache_path,
                )
                self.assertEqual(mocked.call_count, 1)
                draft_and_cache(
                    [
                        {"case_id": "M1", "source_text": "화합물군 1", "regime": "HUMAN_HAZARD"},
                        {"case_id": "M2", "source_text": "화합물군 2", "regime": "HUMAN_HAZARD"},
                    ],
                    cache_path,
                )
                self.assertEqual(mocked.call_count, 2)  # only the new case M2 is drafted

    def test_force_refresh_always_redrafts(self):
        cases = [{"case_id": "M1", "source_text": "화합물군 1", "regime": "HUMAN_HAZARD"}]
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "drafts.csv"
            with patch(
                "structural_rule_drafting._post_draft_chat",
                return_value=_mock_response(),
            ) as mocked:
                draft_and_cache(cases, cache_path)
                draft_and_cache(cases, cache_path, force_refresh=True)
                self.assertEqual(mocked.call_count, 2)


class ExtractNoCasCrossRegimeCasesTest(unittest.TestCase):
    def _row(self, **overrides):
        row = {
            "regime": "PROHIBITED",
            "designation_id": "06-4-13",
            "substance_name": "기타 벤지딘의 염류",
            "direct_cas": "",
            "source_text": "벤지딘과 그 염류 / 구체적 목록 제6호 기타 벤지딘의 염류",
        }
        row.update(overrides)
        return row

    def test_finds_a_genuine_no_cas_row(self):
        rules = pd.DataFrame([self._row()])
        cases = extract_no_cas_cross_regime_cases(rules)
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]["regime"], "PROHIBITED")

    def test_skips_rows_that_have_a_direct_cas(self):
        rules = pd.DataFrame([self._row(direct_cas="92-87-5")])
        self.assertEqual(extract_no_cas_cross_regime_cases(rules), [])

    def test_deduplicates_identical_rows(self):
        rules = pd.DataFrame([self._row(), self._row()])
        cases = extract_no_cas_cross_regime_cases(rules)
        self.assertEqual(len(cases), 1)

    def test_empty_input_yields_empty_list(self):
        self.assertEqual(extract_no_cas_cross_regime_cases(pd.DataFrame()), [])

    def test_real_approved_file_finds_the_known_prohibited_catchalls(self):
        rules = pd.read_csv("data/cross_regime_rules_APPROVED.csv")
        cases = extract_no_cas_cross_regime_cases(rules)
        self.assertEqual(len(cases), 12)
        self.assertTrue(all(c["regime"] == "PROHIBITED" for c in cases))


    def test_partial_progress_survives_a_simulated_interruption(self):
        # Case 2 raises mid-batch (simulating the user stopping the app or a
        # crash); case 1's result must already be on disk, not lost.
        def flaky_post(source_text, timeout=180):
            if source_text == "화합물군 2":
                raise KeyboardInterrupt("user stopped the run")
            return _mock_response(explanation_ko=source_text)

        cases = [
            {"case_id": "M1", "source_text": "화합물군 1", "regime": "HUMAN_HAZARD"},
            {"case_id": "M2", "source_text": "화합물군 2", "regime": "HUMAN_HAZARD"},
            {"case_id": "M3", "source_text": "화합물군 3", "regime": "HUMAN_HAZARD"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "drafts.csv"
            with patch("structural_rule_drafting._post_draft_chat", side_effect=flaky_post):
                with self.assertRaises(KeyboardInterrupt):
                    draft_and_cache(cases, cache_path)
            # M1 must already be saved to disk even though M2 blew up.
            self.assertTrue(cache_path.exists())
            saved = pd.read_csv(cache_path, dtype=str)
            self.assertIn("M1", set(saved["case_id"]))
            self.assertNotIn("M3", set(saved["case_id"]))

    def test_on_progress_is_called_once_per_drafted_case_in_order(self):
        cases = [
            {"case_id": "M1", "source_text": "화합물군 1", "regime": "HUMAN_HAZARD"},
            {"case_id": "M2", "source_text": "화합물군 2", "regime": "HUMAN_HAZARD"},
        ]
        seen = []
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "drafts.csv"
            with patch(
                "structural_rule_drafting._post_draft_chat",
                return_value=_mock_response(),
            ):
                draft_and_cache(
                    cases, cache_path, on_progress=lambda done, total, row: seen.append((done, total))
                )
        self.assertEqual(seen, [(1, 2), (2, 2)])

    def test_on_progress_is_not_called_for_cases_served_from_cache(self):
        cases = [{"case_id": "M1", "source_text": "화합물군 1", "regime": "HUMAN_HAZARD"}]
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "drafts.csv"
            with patch(
                "structural_rule_drafting._post_draft_chat",
                return_value=_mock_response(),
            ):
                draft_and_cache(cases, cache_path)
                seen = []
                draft_and_cache(
                    cases, cache_path, on_progress=lambda done, total, row: seen.append(done)
                )
        self.assertEqual(seen, [])  # fully served from cache -- nothing to report


if __name__ == "__main__":
    unittest.main()
