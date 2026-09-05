import unittest
from unittest.mock import Mock, patch

import pandas as pd

import cross_regime


UPLOADED_PDF = (
    "/mnt/user-data/uploads/"
    "_별표_3__총칭으로_지정된_제한물질의_구체적_목록_제3조제2항_관련__제한물질_금지물질의_지정_.pdf"
)

SEARCH_RESULT = {
    "AdmRulSearch": {
        "admrul": [
            {
                "행정규칙명": "제한물질·금지물질의 지정",
                "행정규칙일련번호": "999999",
                "발령번호": "2026-3",
                "발령일자": "20260101",
                "시행일자": "20260701",
                "소관부처명": "환경부",
            }
        ]
    }
}
BODY = {
    "AdmRulService": {
        "별표": {
            "별표단위": [
                {
                    "별표번호": "0003",
                    "별표제목": "[별표 3] 총칭으로 지정된 제한물질의 구체적 목록",
                    "별표서식PDF파일링크": "/LSW/flDownload.do?flSeq=1",
                }
            ]
        }
    }
}


def _fake_get(url, params=None, timeout=None, **kw):
    resp = Mock()
    resp.raise_for_status.return_value = None
    if "lawSearch.do" in url:
        resp.json.return_value = SEARCH_RESULT
    elif "lawService.do" in url:
        resp.json.return_value = BODY
    return resp


class AppendixFailureIsolationTest(unittest.TestCase):
    """A failure while extracting the appendix must never corrupt or hide the
    (unrelated) article-text extraction's own status, and must always surface
    a specific reason rather than the generic 'API 추출 실패' message."""

    def test_appendix_exception_does_not_overwrite_text_extraction_status(self):
        with patch("cross_regime.get_api_credential", return_value=("fake-oc", "test")):
            with patch("cross_regime.requests.get", side_effect=_fake_get):
                with patch(
                    "cross_regime.extract_candidates_from_payload",
                    side_effect=RuntimeError("simulated appendix parser crash"),
                ):
                    combined, statuses = cross_regime.fetch_cross_regime_candidates()

        row = statuses[statuses["regime"] == "RESTRICTED_PROHIBITED"].iloc[0]
        # The article-text extraction ran first and succeeded; that status
        # must survive even though the appendix step blew up afterward.
        self.assertEqual(row["candidate_status_ko"], "공식 원문 후보 추출·검토 대기")
        self.assertEqual(row["appendix_status"], "APPENDIX_EXTRACTION_ERROR")
        self.assertIn("simulated appendix parser crash", row["appendix_attachments"][0]["message"])

    def test_body_fetch_failure_still_marks_appendix_as_unattempted(self):
        def failing_get(url, params=None, timeout=None, **kw):
            if "lawSearch.do" in url:
                return _fake_get(url, params, timeout, **kw)
            raise ConnectionError("network down")

        with patch("cross_regime.get_api_credential", return_value=("fake-oc", "test")):
            with patch("cross_regime.requests.get", side_effect=failing_get):
                combined, statuses = cross_regime.fetch_cross_regime_candidates()

        row = statuses[statuses["regime"] == "RESTRICTED_PROHIBITED"].iloc[0]
        self.assertEqual(row["candidate_status_ko"], "공식 원문 후보 추출 실패")
        self.assertEqual(row["appendix_status"], "BODY_FETCH_FAILED")
        self.assertTrue(combined.empty)

    def test_a_real_successful_run_produces_matching_row_counts(self):
        with open(UPLOADED_PDF, "rb") as handle:
            real_pdf = handle.read()
        with patch("cross_regime.get_api_credential", return_value=("fake-oc", "test")):
            with patch("cross_regime.requests.get", side_effect=_fake_get):
                with patch("appendix_sync.download_attachment", return_value=real_pdf):
                    combined, statuses = cross_regime.fetch_cross_regime_candidates()

        row = statuses[statuses["regime"] == "RESTRICTED_PROHIBITED"].iloc[0]
        self.assertEqual(row["appendix_status"], "PARSED")
        self.assertEqual(row["appendix_rows"], 110)
        self.assertEqual(len(combined), 110)
        self.assertTrue((combined["approval_status"] == "CANDIDATE_REVIEW_REQUIRED").all())


if __name__ == "__main__":
    unittest.main()
