from io import BytesIO
import unittest
from zipfile import ZipFile

from application_narratives import draft_application_narratives, resume_text


def snapshot(question="Why are you interested in working at Kraken?", **updates):
    return {
        "title": "Data Analyst - Kraken",
        "body": "Data Analyst: SQL, Power BI, and stakeholder reporting.",
        "fields": [{"selector": "#motivation", "label": question, "type": "textarea", "value": ""}],
        **updates,
    }


def docx_resume():
    buffer = BytesIO()
    with ZipFile(buffer, "w") as document:
        document.writestr("word/document.xml", (
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:body><w:p><w:r><w:t>Professional skills</w:t></w:r></w:p>'
            '<w:p><w:r><w:t>SQL, Power </w:t></w:r><w:r><w:t>BI, Python and data quality.</w:t></w:r></w:p>'
            '</w:body></w:document>'
        ))
    return {"name": "resume.docx", "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "data": buffer.getvalue()}


class ApplicationNarrativeTests(unittest.TestCase):
    def test_actual_kraken_prompt_uses_saved_docx_without_api_or_career_profile(self):
        drafts = draft_application_narratives(snapshot(), {"first_name": "Candidate"}, {}, docx_resume())
        draft = drafts["#motivation"]
        self.assertIn("the Data Analyst role at Kraken", draft["value"])
        self.assertIn("SQL", draft["value"])
        self.assertIn("Power BI", draft["value"])
        self.assertIn("resume", draft["source"])
        self.assertNotIn("crypto", draft["value"].lower())
        self.assertNotIn("years", draft["value"])

    def test_question_employer_overrides_old_application_context_and_motivation(self):
        draft = draft_application_narratives(snapshot(), {}, {
            "company": "RHP", "role": "Housing Analyst",
            "motivation": "I am passionate about RHP and its housing mission.",
            "skills": "My practical skills include SQL, Tableau and Power BI.",
        })["#motivation"]
        self.assertIn("Kraken", draft["value"])
        self.assertNotIn("RHP", draft["value"])
        self.assertNotIn("Housing", draft["value"])
        self.assertIn("answer_library.skills", draft["source"])

    def test_motivation_variants_use_current_company(self):
        for question in (
            "Why do you want to join Kraken?", "Why Kraken?",
            "What attracts you to Kraken?", "Why would you like to work for Kraken?",
            "Why are you applying for this position at Kraken?",
            "What excites you about this opportunity?",
        ):
            with self.subTest(question=question):
                draft = draft_application_narratives(snapshot(question), {"skills": ["SQL", "Power BI"]}, {})
                self.assertIn("Kraken", draft["#motivation"]["value"])

    def test_skills_named_on_page_are_prioritized_without_inventing_other_requirements(self):
        draft = draft_application_narratives(snapshot(body="Python and AWS experience"), {
            "skills": ["SQL", "Tableau", "Power BI", "Python", "Excel", "AWS", "Rust"],
        }, {})["#motivation"]["value"]
        self.assertIn("Python, AWS", draft)
        self.assertNotIn("Kubernetes", draft)

    def test_saved_employment_can_support_motivation_without_recognized_skill_terms(self):
        draft = draft_application_narratives(snapshot(), {"employment_history": [
            {"title": "Service Coordinator", "employer": "Example Ltd", "current": True},
        ]}, {})["#motivation"]
        self.assertIn("work as Service Coordinator at Example Ltd", draft["value"])
        self.assertIn("profile.employment_history", draft["source"])

    def test_fit_supporting_statement_and_cover_letter_use_evidence(self):
        for question in ("Why are you a good fit?", "Relevant skills and experience", "Supporting statement", "Cover letter"):
            with self.subTest(question=question):
                result = draft_application_narratives(snapshot(question), {"skills": ["Python"]}, {})
                self.assertIn("Python", result["#motivation"]["value"])

    def test_personal_anecdotes_and_specific_factual_screening_stay_unresolved(self):
        for question in (
            "Tell us about a time you delivered amazing service.",
            "Why are you interested in Kraken? Give an example of a challenge you overcame.",
            "What is your salary expectation?", "Why do you need visa sponsorship?",
            "How many years of experience do you have?", "Do you have Rust experience?",
            "Describe your biggest achievement.", "Why are you eligible to work here?",
            "Why did you leave your previous job?", "Why were you fired?",
            "Why do you want to leave your current role?",
        ):
            with self.subTest(question=question):
                self.assertEqual(draft_application_narratives(snapshot(question), {"skills": ["SQL"]}, {}), {})

    def test_contact_profile_and_prior_company_motivation_are_not_career_evidence(self):
        self.assertEqual(draft_application_narratives(snapshot(), {"first_name": "Candidate"}, {
            "motivation": "I have always wanted to work at RHP.",
        }), {})

    def test_reviewed_library_record_supported_but_unreviewed_record_ignored(self):
        result = draft_application_narratives(snapshot(), {}, {"skills": {"response": "Python and SQL", "reviewed_by_user": True}})
        self.assertIn("SQL", result["#motivation"]["value"])
        self.assertEqual(draft_application_narratives(snapshot(), {}, {"skills": {"response": "Python and SQL", "reviewed_by_user": False}}), {})

    def test_does_not_overwrite_existing_answer_or_fill_select(self):
        fields = [
            {"selector": "#answered", "label": "Why Kraken?", "type": "textarea", "value": "My own answer"},
            {"selector": "#choice", "label": "Why Kraken?", "type": "select-one", "value": ""},
        ]
        self.assertEqual(draft_application_narratives(snapshot(fields=fields), {"skills": ["SQL"]}, {}), {})

    def test_plain_resume_and_negated_skill(self):
        result = draft_application_narratives(snapshot(), {}, {}, "My skills include SQL. I have no experience in Python.")
        self.assertIn("SQL", result["#motivation"]["value"])
        self.assertNotIn("Python", result["#motivation"]["value"])

    def test_page_title_supplies_company_and_role_for_generic_question(self):
        result = draft_application_narratives(snapshot("Why are you interested in this role?", title="Senior Analyst at Kraken"), {"skills": ["SQL"]}, {})
        self.assertIn("the Senior Analyst role at Kraken", result["#motivation"]["value"])

    def test_question_company_not_replaced_by_stale_role_when_title_missing(self):
        result = draft_application_narratives(snapshot(title="Application"), {"skills": ["SQL"]}, {"company": "RHP", "role": "Housing Analyst"})
        self.assertIn("this role at Kraken", result["#motivation"]["value"])
        self.assertNotIn("Housing", result["#motivation"]["value"])

    def test_character_limit_keeps_complete_sentences(self):
        page = snapshot()
        page["fields"][0]["maxLength"] = 200
        result = draft_application_narratives(page, {"skills": ["SQL"]}, {})["#motivation"]["value"]
        self.assertLessEqual(len(result), 200)
        self.assertTrue(result.endswith("."))
        page["fields"][0]["maxLength"] = 10
        self.assertEqual(draft_application_narratives(page, {"skills": ["SQL"]}, {}), {})

    def test_resume_parser_joins_word_runs_and_fails_closed_for_bad_files(self):
        self.assertIn("Power BI", resume_text(docx_resume()))
        self.assertEqual(resume_text({"name": "bad.docx", "data": b"not a document"}), "")
        self.assertEqual(resume_text({"name": "bad.pdf", "data": b"not a document"}), "")
        self.assertEqual(resume_text({"name": "binary.dat", "data": b"Python"}), "")
        self.assertEqual(resume_text({"name": "cv.txt", "data": b"Python"}), "Python")


if __name__ == "__main__":
    unittest.main()
