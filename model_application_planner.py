"""Model decisions over bounded, locally validated browser capabilities."""
import json
import os
import threading

from agent_field_memory import is_manual_only_field


class ModelBudgetExceeded(ValueError):
    pass


def claim_model_call(run):
    with run.setdefault('_model_budget_lock', threading.Lock()):
        used = run.get('model_calls', 0)
        limit = run.get('model_call_limit', 20)
        if used >= limit:
            raise ModelBudgetExceeded('This run reached its model-request limit. No further model requests will be made in this run.')
        run['model_calls'] = used + 1


def decision_failure(error):
    """Return safe diagnostics without exposing API bodies, keys, or inputs."""
    name = type(error).__name__
    if isinstance(error, ModelBudgetExceeded):
        return 'request_limit', str(error), False
    status = getattr(error, 'status_code', None)
    if name == 'APITimeoutError':
        return 'timeout', 'The model request timed out.', True
    if name == 'APIConnectionError':
        return 'connection', 'The model service could not be reached.', True
    if status == 429:
        if getattr(error, 'code', None) == 'insufficient_quota':
            return 'quota', 'The API account has no available quota. Check its billing or usage limits.', False
        return 'rate_limit', 'The model service is rate limited.', True
    if status in {500, 502, 503, 504}:
        return 'service', 'The model service is temporarily unavailable.', True
    if status == 401:
        return 'authentication', 'The API key was rejected. Update the local API key and restart the companion.', False
    if status in {400, 403, 404}:
        return 'configuration', 'The API rejected the model or request configuration. Check model access and settings.', False
    if isinstance(error, ValueError):
        return 'invalid_decision', 'The model returned an invalid or unsupported action decision.', False
    return 'planner_error', 'The local model planner encountered an internal error.', False


DECISION_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'action_indexes': {'type': 'array', 'items': {'type': 'integer'}},
        'reason': {'type': 'string'},
        'needs_user_input': {'type': 'boolean'},
    },
    'required': ['action_indexes', 'reason', 'needs_user_input'],
}


def observation_key(snapshot):
    return json.dumps({k: snapshot.get(k) for k in ('url', 'documentKey', 'fields', 'actions', 'resumeSections',
        'errors', 'finalActions', 'accessReady', 'registrationActions')}, sort_keys=True)


class ModelApplicationPlanner:
    def __init__(self, provider=None):
        self._provider = provider

    @property
    def provider(self):
        if self._provider is None:
            from dotenv import load_dotenv
            from openai_application_provider import OpenAIApplicationProvider
            load_dotenv()
            self._provider = OpenAIApplicationProvider(
                model=os.getenv('OPENAI_AGENT_MODEL', os.getenv('OPENAI_INSTRUCTION_MODEL', 'gpt-5-mini')))
        return self._provider

    def choose(self, snapshot, candidates, run):
        # Never send generated passwords, file bytes, or arbitrary executor
        # metadata. The model refers to candidate indexes, not new selectors.
        safe_candidates = [{k: c[k] for k in ('kind', 'label', 'value', 'section', 'phase', 'source') if k in c}
                           for c in candidates]
        observation = {
            'title': snapshot.get('title', ''), 'step': snapshot.get('activeStep', ''),
            'page_text': snapshot.get('body', '')[:12000],
            'errors': snapshot.get('errors', []),
            'fields': [{k: f.get(k) for k in ('label', 'type', 'value', 'required', 'invalid', 'options', 'resumeSection', 'resumeRow')}
                       for f in snapshot.get('fields', []) if not is_manual_only_field(f.get('label'))],
            'actions': safe_candidates,
            'policy': {'auto_submit': bool(run.get('auto_submit')), 'auto_register': bool(run.get('auto_register'))},
            'selected_education': run.get('selected_education'),
            'recent_results': run.get('recent_action_results', [])[-6:],
        }
        claim_model_call(run)
        result = self.provider.decide_application_actions(observation=observation, schema=DECISION_SCHEMA)
        if not isinstance(result, dict) or type(result.get('needs_user_input')) is not bool:
            raise ValueError('The model returned an invalid decision.')
        indexes = result.get('action_indexes')
        reason = str(result.get('reason', '')).strip()[:600]
        if not isinstance(indexes, list) or any(type(i) is not int or not 0 <= i < len(candidates) for i in indexes) or len(set(indexes)) != len(indexes):
            raise ValueError('The model selected an action outside the available browser capabilities.')
        if result.get('needs_user_input') is True and indexes:
            raise ValueError('The model returned conflicting action and clarification decisions.')
        chosen = [dict(candidates[i]) for i in indexes]
        # A structural action must be observed before any following action.
        structural = {'click', 'resume-select', 'resume-add', 'check', 'upload', 'submit', 'register', 'authenticate'}
        if len(chosen) > 1 and any(c['kind'] in structural for c in chosen):
            # These are valid model-selected actions, but the executor must
            # observe structural changes. Execute only the first approved
            # action; discard the rest and plan again against the new page.
            chosen = chosen[:1]
        return chosen, reason or 'The model needs clarification before proceeding.'

    def draft(self, snapshot, profile, answers, resume, run=None):
        from application_narratives import resume_text, _kind
        fields = [{'index': i, 'field': f['label'], 'control': f['type'], 'action': 'review'}
                  for i, f in enumerate(snapshot.get('fields', []))
                  if not f.get('value') and (f.get('type') == 'textarea' or
                      (f.get('type') == 'text' and _kind(f.get('label', '')))) and not is_manual_only_field(f.get('label'))]
        if not fields:
            return {}
        facts = {'resume': resume_text(resume)}
        facts.update({f'profile.{k}': str(v) for k, v in profile.items()
                      if isinstance(v, (str, int, bool)) and not is_manual_only_field(k) and v})
        facts.update({f'answer.{i}': str(v) for i, (k, v) in enumerate(answers.items())
                      if not is_manual_only_field(k) and v and not k.startswith(('safe-field:', 'education-'))})
        facts = {k: v for k, v in facts.items() if v}
        if not facts:
            return {}
        if run is not None:
            claim_model_call(run)
        result = self.provider.draft_form_answers(fields=fields, verified_facts=facts,
            role=snapshot.get('title', ''), company='', job_description=snapshot.get('body', '')[:12000])
        return {snapshot['fields'][d['field_index']]['selector']: {'value': d['value'], 'source': 'Model draft from saved facts'}
                for d in result['drafts']}
