"""Exercise fresh prompt builds against real external skill files."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent import prompt_builder as pb
from agent.skill_utils import extract_skill_description


class ExternalRefreshTests(unittest.TestCase):
    def test_fresh_build_observes_edit_add_delete_and_category(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            local = home / 'skills'
            local.mkdir()
            external = home / 'external'
            skill = external / 'demo' / 'SKILL.md'
            skill.parent.mkdir(parents=True)
            skill.write_text('---\nname: demo\ndescription: original description\n---\nBody\n')
            (home / 'config.yaml').write_text(f'skills:\n  external_dirs:\n    - {external}\n')
            with patch.dict(os.environ, {'HERMES_HOME': str(home)}):
                pb.clear_skills_system_prompt_cache()
                first = pb.build_skills_system_prompt(skills_dir_override=local)
                self.assertIn('original description', first)
                skill.write_text('---\nname: demo\ndescription: refreshed description\n---\nBody\n')
                second = pb.build_skills_system_prompt(skills_dir_override=local)
                self.assertIn('refreshed description', second)
                self.assertIn('original description', first)  # returned conversation prefix stays frozen
                extra = external / 'extra' / 'SKILL.md'
                extra.parent.mkdir()
                extra.write_text('---\nname: extra\ndescription: extra description\n---\nBody\n')
                self.assertIn('extra description', pb.build_skills_system_prompt(skills_dir_override=local))
                category = extra.parent / 'DESCRIPTION.md'
                category.write_text('---\ndescription: category refreshed\n---\n')
                self.assertIn('category refreshed', pb.build_skills_system_prompt(skills_dir_override=local))
                skill.unlink()
                self.assertNotIn('refreshed description', pb.build_skills_system_prompt(skills_dir_override=local))
                pb.clear_skills_system_prompt_cache()

    def test_explicit_description_caps(self):
        fm = {'description': 'a' * 1100}
        for cap in (-1, 0, 1, 2, 3, 4, 60, 1024, 2000):
            result = extract_skill_description(fm, max_chars=cap)
            self.assertLessEqual(len(result), max(0, cap))
        self.assertEqual(extract_skill_description(fm, max_chars=2000), fm['description'])
