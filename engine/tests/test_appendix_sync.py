import unittest
from unittest.mock import Mock, patch

import pandas as pd

from appendix_sync import (
    build_candidate_rows,
    extract_candidates_from_payload,
    fetch_appendix_candidates,
    find_appendix_attachments,
    parse_appendix_pdf_bytes,
    parse_designation_cas_table,
)


UPLOADED_PDF = (
    "/mnt/user-data/uploads/"
    "_별표_3__총칭으로_지정된_제한물질의_구체적_목록_제3조제2항_관련__제한물질_금지물질의_지정_.pdf"
)

# Mirrors the documented admrul body schema (별표단위/별표서식PDF파일링크), taken
# from the National Law Information API's own usage example.
MOCK_ADMRUL_PAYLOAD = {
    "AdmRulService": {
        "행정규칙명": "제한물질·금지물질의 지정",
        "별표": {
            "별표단위": [
                {
                    "별표번호": "0003",
                    "별표가지번호": "00",
                    "별표구분": "별표",
                    "별표제목": "[별표 3] 총칭으로 지정된 제한물질의 구체적 목록",
                    "별표서식파일링크": "/LSW/flDownload.do?flSeq=100000001",
                    "별표서식PDF파일링크": "/LSW/flDownload.do?flSeq=100000002",
                },
                {
                    "별표번호": "0004",
                    "별표가지번호": "00",
                    "별표구분": "별표",
                    "별표제목": "[별표 4] 총칭으로 지정된 금지물질의 구체적 목록",
                    "별표서식파일링크": "/LSW/flDownload.do?flSeq=100000003",
                    # no PDF link for this one -- should be reported, not silently dropped
                },
            ]
        },
    }
}


class FindAttachmentsTest(unittest.TestCase):
    def test_finds_pdf_and_hwp_links_by_walking_the_payload(self):
        attachments = find_appendix_attachments(MOCK_ADMRUL_PAYLOAD)
        self.assertEqual(len(attachments), 2)
        with_pdf = [a for a in attachments if a["pdf_path"]]
        self.assertEqual(len(with_pdf), 1)
        self.assertEqual(with_pdf[0]["pdf_path"], "/LSW/flDownload.do?flSeq=100000002")
        without_pdf = [a for a in attachments if not a["pdf_path"]]
        self.assertEqual(len(without_pdf), 1)
        self.assertTrue(without_pdf[0]["hwp_path"])

    def test_no_attachments_returns_empty_list(self):
        self.assertEqual(find_appendix_attachments({"AdmRulService": {}}), [])


class ParseAppendixTableTest(unittest.TestCase):
    """Exercises the parser against the real uploaded appendix PDF."""

    @classmethod
    def setUpClass(cls):
        with open(UPLOADED_PDF, "rb") as handle:
            cls.parsed = parse_appendix_pdf_bytes(handle.read())

    def test_recovers_every_designation_group_with_correct_row_counts(self):
        counts = self.parsed.groupby("designation_id").size().to_dict()
        self.assertEqual(
            counts,
            {"06-5-1": 8, "06-5-4": 14, "06-5-6": 15, "06-5-8": 22, "06-5-10": 51},
        )

    def test_cas_only_rows_match_the_source_document(self):
        cas_counts = (
            self.parsed[self.parsed["cas"].ne("")]
            .groupby("designation_id")
            .size()
            .to_dict()
        )
        self.assertEqual(
            cas_counts,
            {"06-5-1": 6, "06-5-4": 12, "06-5-6": 14, "06-5-8": 20, "06-5-10": 49},
        )

    def test_thresholds_and_operators_are_recovered_from_the_group_title(self):
        thresholds = self.parsed.drop_duplicates("designation_id").set_index(
            "designation_id"
        )[["concentration_threshold_pct", "concentration_operator"]]
        self.assertEqual(thresholds.loc["06-5-1", "concentration_operator"], "이상")
        self.assertEqual(thresholds.loc["06-5-1", "concentration_threshold_pct"], 0.1)
        # Lead is the one group with a different operator and threshold.
        self.assertEqual(thresholds.loc["06-5-8", "concentration_operator"], "초과")
        self.assertEqual(thresholds.loc["06-5-8", "concentration_threshold_pct"], 0.009)

    def test_no_designation_code_had_to_be_inferred(self):
        # A fully successful parse should not need the UNKNOWN-N fallback.
        self.assertFalse(self.parsed["designation_id_inferred"].any())

    def test_catch_all_rows_are_flagged_without_a_cas(self):
        catch_all = self.parsed[self.parsed["is_catch_all"]]
        self.assertTrue(len(catch_all) >= 1)
        self.assertTrue((catch_all["cas"] == "").all())

    def test_a_known_cas_lands_on_the_right_designation(self):
        row = self.parsed[self.parsed["cas"] == "301-04-2"]  # Lead acetate
        self.assertEqual(len(row), 1)
        self.assertEqual(row.iloc[0]["designation_id"], "06-5-8")

    def test_inline_designation_code_glued_to_an_item_line_is_still_recovered(self):
        # "06-5-8 7 Lead 2-ethylhexanoate 301-08-6" is one physical PDF line;
        # the corresponding CAS must still be parsed out.
        row = self.parsed[self.parsed["cas"] == "301-08-6"]
        self.assertEqual(len(row), 1)
        self.assertEqual(row.iloc[0]["designation_id"], "06-5-8")

    def test_unrecognized_layout_yields_an_empty_frame_not_an_error(self):
        empty = parse_designation_cas_table("이것은 표 형식이 아닌 임의의 텍스트입니다.")
        self.assertTrue(empty.empty)


class BuildCandidateRowsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(UPLOADED_PDF, "rb") as handle:
            cls.parsed = parse_appendix_pdf_bytes(handle.read())
        cls.attachment = {
            "appendix_title": "[별표 3] 총칭으로 지정된 제한물질의 구체적 목록",
            "appendix_no": "0003",
            "appendix_kind": "별표",
        }
        cls.notice_info = {
            "official_title": "제한물질·금지물질의 지정",
            "official_notice": "2026-3",
            "official_serial": "999999",
            "issue_date": "20260101",
            "effective_date": "20260701",
        }

    def test_candidates_are_never_pre_approved(self):
        candidates = build_candidate_rows(
            self.parsed, "RESTRICTED", self.attachment, self.notice_info
        )
        self.assertTrue(candidates["approval_status"].eq("CANDIDATE_REVIEW_REQUIRED").all())
        self.assertTrue(
            candidates["extraction_status"].eq("OFFICIAL_APPENDIX_PDF_AUTO_EXTRACTED").all()
        )

    def test_threshold_and_source_citation_are_carried_through(self):
        candidates = build_candidate_rows(
            self.parsed, "RESTRICTED", self.attachment, self.notice_info
        )
        lead = candidates[candidates["direct_cas"] == "301-04-2"].iloc[0]
        self.assertEqual(lead["concentration_threshold_pct"], 0.009)
        self.assertEqual(lead["concentration_operator"], "초과")
        self.assertEqual(lead["source_reference"], "2026-3")
        self.assertIn("06-5-8", lead["source_locator"])

    def test_catch_all_rows_get_manual_review_scope_type(self):
        candidates = build_candidate_rows(
            self.parsed, "RESTRICTED", self.attachment, self.notice_info
        )
        catch_all = candidates[candidates["direct_cas"].eq("")]
        self.assertTrue(len(catch_all) > 0)
        self.assertTrue((catch_all["scope_type"] == "MANUAL_REVIEW").all())

    def test_empty_parsed_frame_yields_empty_candidates(self):
        candidates = build_candidate_rows(
            pd.DataFrame(), "RESTRICTED", self.attachment, self.notice_info
        )
        self.assertTrue(candidates.empty)


class ExtractFromPayloadTest(unittest.TestCase):
    def test_missing_pdf_link_is_reported_not_silently_skipped(self):
        with patch("appendix_sync.download_attachment") as mocked_download:
            mocked_download.return_value = b"irrelevant"
            with patch("appendix_sync.parse_appendix_pdf_bytes") as mocked_parse:
                mocked_parse.return_value = pd.DataFrame(
                    [
                        {
                            "designation_id": "06-5-1",
                            "designation_id_inferred": False,
                            "item_no": 1,
                            "substance_name": "Test",
                            "cas": "100-00-1",
                            "is_catch_all": False,
                            "group_title": "테스트 물질군 0.1% 이상",
                            "concentration_threshold_pct": 0.1,
                            "concentration_operator": "이상",
                        }
                    ]
                )
                candidates, status = extract_candidates_from_payload(
                    MOCK_ADMRUL_PAYLOAD, "RESTRICTED", {"official_notice": "2026-3"}
                )
        self.assertEqual(status["attachment_count"], 2)
        self.assertEqual(status["parsed_attachment_count"], 1)
        statuses = [item["status"] for item in status["attachments"]]
        self.assertIn("PDF_LINK_MISSING", statuses)
        self.assertIn("PARSED", statuses)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates.iloc[0]["direct_cas"], "100-00-1")

    def test_no_attachments_returns_clear_status(self):
        candidates, status = extract_candidates_from_payload(
            {"AdmRulService": {}}, "RESTRICTED", {}
        )
        self.assertTrue(candidates.empty)
        self.assertEqual(status["status"], "NO_ATTACHMENT")

    def test_download_failure_on_one_attachment_does_not_block_others(self):
        payload = {
            "별표": {
                "별표단위": [
                    {
                        "별표번호": "1",
                        "별표제목": "실패할 별표",
                        "별표서식PDF파일링크": "/LSW/flDownload.do?flSeq=1",
                    },
                    {
                        "별표번호": "2",
                        "별표제목": "성공할 별표",
                        "별표서식PDF파일링크": "/LSW/flDownload.do?flSeq=2",
                    },
                ]
            }
        }
        with patch("appendix_sync.download_attachment") as mocked_download:
            def side_effect(path, timeout=60):
                if "flSeq=1" in path:
                    raise ConnectionError("network down")
                return b"ok"
            mocked_download.side_effect = side_effect
            with patch("appendix_sync.parse_appendix_pdf_bytes") as mocked_parse:
                mocked_parse.return_value = pd.DataFrame(
                    [
                        {
                            "designation_id": "01-1-1",
                            "designation_id_inferred": False,
                            "item_no": 1,
                            "substance_name": "OK",
                            "cas": "50-00-0",
                            "is_catch_all": False,
                            "group_title": "성공 물질군 1% 이상",
                            "concentration_threshold_pct": 1.0,
                            "concentration_operator": "이상",
                        }
                    ]
                )
                candidates, status = extract_candidates_from_payload(
                    payload, "PROHIBITED", {"official_notice": "2026-1"}
                )
        self.assertEqual(len(candidates), 1)
        failed = [a for a in status["attachments"] if a["status"] == "DOWNLOAD_OR_PARSE_ERROR"]
        self.assertEqual(len(failed), 1)
        self.assertIn("network down", failed[0]["message"])


class FetchAppendixCandidatesTest(unittest.TestCase):
    def test_missing_api_key_short_circuits_before_any_request(self):
        candidates, status = fetch_appendix_candidates(
            "", "RESTRICTED", {"official_serial": "123"}
        )
        self.assertTrue(candidates.empty)
        self.assertEqual(status["status"], "API_KEY_MISSING")

    def test_missing_notice_serial_short_circuits(self):
        candidates, status = fetch_appendix_candidates("some-key", "RESTRICTED", {})
        self.assertTrue(candidates.empty)
        self.assertEqual(status["status"], "NO_NOTICE")

    @patch("appendix_sync.requests.get")
    def test_body_fetch_failure_is_reported_and_never_raises(self, mocked_get):
        mocked_get.side_effect = ConnectionError("boom")
        candidates, status = fetch_appendix_candidates(
            "secret-oc", "RESTRICTED", {"official_serial": "123"}
        )
        self.assertTrue(candidates.empty)
        self.assertEqual(status["status"], "BODY_FETCH_ERROR")
        self.assertNotIn("secret-oc", status["message"])  # credential must not leak

    @patch("appendix_sync.requests.get")
    def test_successful_body_fetch_delegates_to_payload_extraction(self, mocked_get):
        mocked_response = Mock()
        mocked_response.json.return_value = MOCK_ADMRUL_PAYLOAD
        mocked_response.raise_for_status.return_value = None
        mocked_get.return_value = mocked_response
        with patch("appendix_sync.download_attachment", return_value=b"pdf-bytes"):
            with open(UPLOADED_PDF, "rb") as handle:
                real_bytes = handle.read()
            with patch("appendix_sync.download_attachment", return_value=real_bytes):
                candidates, status = fetch_appendix_candidates(
                    "secret-oc", "RESTRICTED", {"official_serial": "123", "official_notice": "2026-3"}
                )
        self.assertEqual(status["parsed_attachment_count"], 1)
        self.assertEqual(len(candidates), 110)
        self.assertTrue((candidates["approval_status"] == "CANDIDATE_REVIEW_REQUIRED").all())




class AbolishedAppendixTest(unittest.TestCase):
    """A 별표 slot titled '삭제' (or '<삭 제>') means the appendix number was
    abolished in a past amendment -- there is no table to read, and this must
    be distinguished from a genuine unrecognized-format failure."""

    def test_abolished_title_is_skipped_without_downloading(self):
        payload = {
            "별표": {
                "별표단위": [
                    {
                        "별표번호": "0002",
                        "별표제목": "삭제",
                        "별표서식PDF파일링크": "/LSW/flDownload.do?flSeq=1",
                    }
                ]
            }
        }
        with patch("appendix_sync.download_attachment") as mocked_download:
            candidates, status = extract_candidates_from_payload(
                payload, "RESTRICTED", {"official_notice": "2026-3"}
            )
        mocked_download.assert_not_called()
        self.assertEqual(status["attachments"][0]["status"], "ABOLISHED_APPENDIX")
        self.assertTrue(candidates.empty)

    def test_abolished_title_with_surrounding_bracket_and_spaces(self):
        payload = {
            "별표": {
                "별표단위": [
                    {
                        "별표번호": "0006",
                        "별표제목": "<삭 제>",
                        "별표서식PDF파일링크": "/LSW/flDownload.do?flSeq=2",
                    }
                ]
            }
        }
        _, status = extract_candidates_from_payload(payload, "PROHIBITED", {})
        self.assertEqual(status["attachments"][0]["status"], "ABOLISHED_APPENDIX")

    def test_a_real_substance_title_is_not_mistaken_for_abolished(self):
        payload = {
            "별표": {
                "별표단위": [
                    {
                        "별표번호": "0003",
                        "별표제목": "총칭으로 지정된 제한물질의 구체적 목록",
                        "별표서식PDF파일링크": "/LSW/flDownload.do?flSeq=3",
                    }
                ]
            }
        }
        with patch("appendix_sync.download_attachment", side_effect=ConnectionError("x")):
            _, status = extract_candidates_from_payload(payload, "RESTRICTED", {})
        # a real title still attempts the download (and fails here only
        # because we forced a connection error, not because it was skipped).
        self.assertEqual(status["attachments"][0]["status"], "DOWNLOAD_OR_PARSE_ERROR")


if __name__ == "__main__":
    unittest.main()
