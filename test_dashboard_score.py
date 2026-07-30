import unittest

import pandas as pd

import app


class DashboardScoreTests(unittest.TestCase):
    def test_primary_score_uses_ai_buying_score_without_division(self):
        frame = app.normalise_leads(pd.DataFrame([{
            "lead_id": "lead_1", "company_name": "Company",
            "score": "3", "groq_score": "3", "ai_buying_score": "63",
        }]))
        self.assertEqual(frame.iloc[0]["ai_buying_score"], 63)
        self.assertEqual(frame.iloc[0]["groq_score"], 3)

    def test_displayed_qualification_comes_from_ai_buying_score(self):
        self.assertEqual(
            app.qualification_for_ai_buying_score(63),
            "Good Prospect",
        )
        self.assertEqual(
            app.qualification_for_ai_buying_score(85),
            "Excellent Prospect",
        )

    def test_groq_response_uses_separate_groq_score(self):
        result = app.parse_groq_analysis(
            '{"groq_score":3,"qualification":"low_priority",'
            '"recommendedService":"Service","whyGoodProspect":"Reason",'
            '"concreteProblem":"Problem","automationWorkflow":"Workflow",'
            '"practicalBenefit":"Benefit"}'
        )
        self.assertEqual(result["groq_score"], 3)
        self.assertNotIn("leadScore", result)


if __name__ == "__main__":
    unittest.main()
