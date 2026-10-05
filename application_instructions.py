"""Interpret workflow instructions before constructing any browser commands."""
import os

from agent_field_memory import is_reusable_safe_field
from workday_qualifications import qualifications


SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'intent': {'type': 'string', 'enum': ['education_selection', 'answer', 'clarify']},
        'education_indexes': {'type': 'array', 'items': {'type': 'integer'}},
        'field_selector': {'type': 'string'}, 'value': {'type': 'string'},
        'message': {'type': 'string'},
    },
    'required': ['intent', 'education_indexes', 'field_selector', 'value', 'message'],
}


def interpret_instruction(text, snapshot, resume, provider=None):
    fields = [{k: f.get(k, '') for k in ('selector', 'label', 'type', 'options', 'resumeRow')}
              for f in snapshot.get('fields', []) if is_reusable_safe_field(f.get('label'), f.get('type'))]
    records = qualifications(resume)['education']
    if provider is None:
        from dotenv import load_dotenv
        load_dotenv()
        from openai_application_provider import OpenAIApplicationProvider
        provider = OpenAIApplicationProvider(model=os.getenv('OPENAI_INSTRUCTION_MODEL', 'gpt-5-mini'))
    context = {'errors': snapshot.get('errors', []), 'manual_fields': [f.get('label', '')
        for f in snapshot.get('fields', []) if not is_reusable_safe_field(f.get('label'), f.get('type'))
        and f.get('type') != 'password' and (f.get('required') or f.get('invalid')) and not f.get('value')]}
    result = provider.interpret_application_instruction(text=text, fields=fields, education=records, schema=SCHEMA, context=context)
    intent = result.get('intent')
    if intent == 'education_selection':
        indexes = result.get('education_indexes', [])
        if not indexes or any(type(i) is not int or not 0 <= i < len(records) for i in indexes) or len(set(indexes)) != len(indexes):
            raise ValueError('The instruction did not identify valid saved education records. No fields were changed.')
        return {'intent': intent, 'records': [records[i] for i in indexes]}
    if intent == 'answer':
        targets = [f for f in fields if f['selector'] == result.get('field_selector')]
        value = str(result.get('value', '')).strip()
        # A model must extract an answer the user actually supplied, not
        # invent a replacement or paste the entire workflow into a field.
        from agent_field_memory import parse_agent_instruction
        if len(targets) != 1 or not value or value.casefold() not in text.casefold() or parse_agent_instruction(value)[0] == 'workflow':
            raise ValueError('The instruction needs clarification before an answer can be entered.')
        return {'intent': intent, 'field': targets[0], 'value': value}
    if intent != 'clarify':
        raise ValueError('The instruction interpreter returned an unsupported action. No fields were changed.')
    raise ValueError(str(result.get('message') or 'Which saved record or field should I use?')[:500])
