import json
import unittest

from openai_application_provider import (
    OpenAIApplicationProvider,
    is_protected_field,
    merge_openai_drafts,
)


class FakeResponse:
    def __init__(self, payload):
        self.output_text = json.dumps(payload)


class FakeResponses:
    def __init__(self, payload):
        self.payload = payload
        self.request = None

    def create(self, **kwargs):
        self.request = kwargs
        return FakeResponse(self.payload)


class FakeClient:
    def __init__(self, payload):
        self.responses = FakeResponses(payload)

    def with_options(self, **kwargs):
        return self


class OpenAIApplicationProviderTests(unittest.TestCase):
    def test_instruction_interpretation_uses_structured_output_without_storage(self):
        from application_instructions import SCHEMA
        client = FakeClient(dict(intent='clarify', education_indexes=[], field_selector='', value='', message='Which record?'))
        provider = OpenAIApplicationProvider(model='test-model', client=client)
        result = provider.interpret_application_instruction(text='Use my degree', fields=[], education=[], schema=SCHEMA)
        self.assertEqual(result['intent'], 'clarify')
        request = client.responses.request
        self.assertFalse(request['store'])
        self.assertTrue(request['text']['format']['strict'])

    def test_drafts_only_eligible_textarea_with_cited_facts(self):
        client = FakeClient({
            "drafts": [
                {"field_index": 3, "value": "SQL and Power BI experience.", "source_fact_ids": ["answer_library.skills"], "reason": "Relevant experience."},
                {"field_index": 4, "value": "Unsupported", "source_fact_ids": ["not.a.fact"], "reason": "Ignored."},
            ],
            "unresolved_field_indexes": [4],
        })
        provider = OpenAIApplicationProvider(model="test-model", client=client)
        result = provider.draft_form_answers(
            fields=[
                {"index": 3, "field": "Why are you a fit?", "control": "textarea", "action": "review"},
                {"index": 4, "field": "Right to work in the UK?", "control": "textarea", "action": "review"},
            ],
            verified_facts={"answer_library.skills": "SQL and Power BI"},
            role="BI Analyst", company="Example", job_description="Use data.",
        )
        self.assertEqual(result["drafts"][0]["field_index"], 3)
        self.assertEqual(result["unresolved_field_indexes"], [])
        self.assertEqual(client.responses.request["text"]["format"]["type"], "json_schema")

    def test_merge_never_overrides_protected_or_non_review_fields(self):
        plan = {"fields": [
            {"index": 1, "field": "Why are you a fit?", "required": True, "action": "review", "current_value": "", "proposed_value": None, "source": "Needs your input"},
            {"index": 2, "field": "Visa sponsorship required?", "required": True, "action": "review", "current_value": "", "proposed_value": None, "source": "Needs your input"},
        ], "required_missing": ["Why are you a fit?", "Visa sponsorship required?"]}
        merged = merge_openai_drafts(plan, {"drafts": [
            {"field_index": 1, "value": "Grounded answer", "source_fact_ids": ["answer_library.skills"], "reason": ""},
            {"field_index": 2, "value": "Yes", "source_fact_ids": ["answer_library.skills"], "reason": ""},
        ], "unresolved_field_indexes": []})
        self.assertEqual(merged["fields"][0]["action"], "fill")
        self.assertEqual(merged["fields"][1]["action"], "review")
        self.assertTrue(is_protected_field("Work permit eligibility"))


if __name__ == "__main__":
    unittest.main()
