"""Stable configured prompt construction and approved description budget."""
import unittest
from types import SimpleNamespace
from agent.skill_utils import extract_skill_description, is_skill_description_truncated_for_prompt
from agent.prompt_builder import execution_guidance_text
from agent.system_prompt import build_system_prompt_parts

class PromptContracts(unittest.TestCase):
    def test_skill_description_default_and_explicit_budget(self):
        description='a'*100+' skill selection context'
        self.assertEqual(extract_skill_description({'description':description}), description)
        self.assertFalse(is_skill_description_truncated_for_prompt({'description':description}))
        long='b'*1100
        self.assertEqual(len(extract_skill_description({'description':long})),1024)
        self.assertTrue(is_skill_description_truncated_for_prompt({'description':long}))
        self.assertEqual(extract_skill_description({'description':description},10),'a'*7+'...')
        self.assertEqual(extract_skill_description({'description':description},0),'')

    def test_exact_model_variants_keep_common_guidance_and_tool_filter(self):
        for model in ('gpt-5.6-sol','gpt-5.6-terra','openai/gpt-5.6-sol'):
            text=execution_guidance_text(['terminal'],model)
            self.assertIn('at most one direct verification',text)
            self.assertIn('<mandatory_tool_use>',text)
            self.assertIn('<literal_preservation>',text)
            self.assertNotIn('web_search',text)
        default=execution_guidance_text(['terminal'])
        for model in ('gpt-5.6-sol-preview','prefixgpt-5.6-sol','gpt-5.6-terra-extra','gpt-5.6','foo/bar/gpt-5.6-sol'):
            self.assertEqual(execution_guidance_text(['terminal'],model),default)

    def agent(self, model='gpt-5.6-sol', gate='auto', tools=None):
        return SimpleNamespace(load_soul_identity=False,skip_context_files=True,valid_tool_names=['terminal'] if tools is None else tools,_task_completion_guidance=False,_tool_use_enforcement=False,_environment_probe=False,_kanban_worker_guidance='',_memory_store=None,_memory_manager=None,model=model,provider='',platform='',pass_session_id=False,session_id='',_emit_status=lambda *a,**kw:None,_execution_guidance=gate)

    def test_real_stable_prompt_builder_honors_gates(self):
        for gate, expected in [('auto',True),(True,True),(False,False),(['sol'],True),(['terra'],False)]:
            with self.subTest(gate=gate):
                agent=self.agent(gate=gate)
                text=build_system_prompt_parts(agent)['stable']
                self.assertEqual('at most one direct verification' in text,expected)
                self.assertEqual(text,build_system_prompt_parts(agent)['stable'])
        self.assertNotIn('Execution discipline',build_system_prompt_parts(self.agent(tools=[]))['stable'])
