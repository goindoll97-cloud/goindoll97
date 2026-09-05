import unittest
import pandas as pd

from company_engine import load_approved_master, latest_notice
from no_cas_scope import build_no_cas_scope_index, screen_no_cas_scopes, attach_no_cas_candidates


class NoCasScopeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.master, _ = load_approved_master()
        cls.notice = latest_notice(cls.master)
        cls.index = build_no_cas_scope_index(cls.master, cls.notice)

    def test_real_reaction_mixture_component_cas_is_found(self):
        inv = pd.DataFrame([{"material_id":"A1","chemical_name":"2-(1,1-dimethylpropyl)anthraquinone","cas":"32588-54-8"}])
        found = screen_no_cas_scopes(inv, self.index)
        self.assertIn("2024-1-1175", set(found["designation_id"]))
        self.assertIn("COMPONENT_CAS_CANDIDATE", set(found["candidate_match_type"]))

    def test_real_acrylate_component_cas_is_found(self):
        inv = pd.DataFrame([{"material_id":"A2","chemical_name":"acrylate component","cas":"3524-68-3"}])
        found = screen_no_cas_scopes(inv, self.index)
        self.assertIn("2021-1-1059", set(found["designation_id"]))

    def test_component_match_is_never_auto_confirmed(self):
        summary = pd.DataFrame([{"material_id":"A1","gate_decision":"보류(비해당 단정 불가)","review_regulatory_text":""}])
        candidate = pd.DataFrame([{
            "material_id":"A1","designation_id":"2024-1-1175","regulatory_name":"반응혼합물",
            "source_text":"공식 원문","candidate_reason":"구성성분 일치, 제품 동일성 확인 필요"
        }])
        out = attach_no_cas_candidates(summary, candidate)
        self.assertEqual(out.loc[0,"gate_decision"], "보류(CAS 없는 규제범위 후보)")
        self.assertEqual(out.loc[0,"final_decision"], "전문가 검토 필요")

    def test_english_acid_salt_name_variant_is_candidate(self):
        inv = pd.DataFrame([{
            "material_id": "A3", "chemical_name": "Sodium dichromate", "cas": "10588-01-9"
        }])
        found = screen_no_cas_scopes(inv, self.index)
        self.assertIn("97-1-271", set(found["designation_id"]))
        self.assertIn("NAME_SCOPE_CANDIDATE", set(found["candidate_match_type"]))

    def test_compound_name_with_attached_family_word_is_candidate(self):
        inv = pd.DataFrame([{
            "material_id": "A4", "chemical_name": "Ethyltrimethyllead", "cas": "1762-26-1"
        }])
        found = screen_no_cas_scopes(inv, self.index)
        self.assertIn("97-1-296", set(found["designation_id"]))


if __name__ == "__main__":
    unittest.main()
