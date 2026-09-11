"""Policy is enforced on real subscription surfaces and watcher delivery."""
import argparse
import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from hermes_cli import kanban, kanban_db as kb
from hermes_cli import kanban_notifications as policy
from gateway.config import Platform
from gateway.run import GatewayRunner
from plugins.kanban.dashboard import plugin_api as api
from tools.kanban_tools import _maybe_auto_subscribe


class Adapter:
    def __init__(self, home, fail=False):
        self.runtime_profile_home=home
        self.sent=[]
        self.fail=fail
    async def send(self, chat_id, text, metadata=None):
        if self.fail: raise RuntimeError('temporary send failure')
        self.sent.append((chat_id,metadata))


class PolicyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.env=patch.dict(os.environ, {'HOME':str(self.root),'HERMES_HOME':str(self.root),'HERMES_KANBAN_HOME':str(self.root),'HERMES_KANBAN_DB':str(self.root/'kanban.db'),'HERMES_PROFILE':'default'})
        self.env.start(); self.addCleanup(self.env.stop)
        self.conn=kb.connect(); self.addCleanup(self.conn.close)
        self.tid=kb.create_task(self.conn,title='policy task')
        self.config('origin')

    def config(self, mode):
        (self.root/'config.yaml').write_text(json.dumps({'kanban':{'notification_policy':mode,'done_sub_retention_days':0},'platforms':{'telegram':{'home_channel':{'platform':'telegram','chat_id':'home','thread_id':'99'}},'discord':{'home_channel':{'platform':'discord','chat_id':'discord-home'}}}}))

    def add(self, **kwargs):
        kb.add_notify_sub(self.conn,task_id=self.tid,platform='discord',chat_id='origin',thread_id='42',notifier_profile='default',delivery_mode='notify+wake',delivery_metadata={'reply_to':'old'},**kwargs)

    def runner(self, fail=False):
        r=GatewayRunner.__new__(GatewayRunner); r._running=True
        r._launch_profile_home=self.root; r._launch_profile_name='default'
        r._kanban_dispatcher_lock_handle=object(); r._profile_adapters={}
        r.adapters={Platform.TELEGRAM:Adapter(self.root,fail),Platform.DISCORD:Adapter(self.root,fail)}
        return r

    async def tick(self,r,between=None):
        real_sleep=asyncio.sleep
        async def sleep(delay):
            if delay==5: return
            r._running=False
            await real_sleep(0)
        original=r._kanban_policy_subscription
        calls=0
        def resolve(row):
            nonlocal calls
            calls+=1
            if calls==2 and between: between()
            return original(row)
        with patch('asyncio.sleep',sleep), patch.object(r,'_kanban_policy_subscription',side_effect=resolve):
            await r._kanban_notifier_watcher(interval=1)

    def test_malformed_policy_denies_external_and_preserves_tui_explicitly(self):
        for raw in [42,[],None,{'mode':'typo'},{'mode':False},{'mode':''}]:
            self.config(raw)
            self.assertIsNone(policy.resolve_notify_target(platform='discord',chat_id='origin'))
            self.assertEqual(policy.resolve_notify_target(platform='tui',chat_id='s').platform,'tui')
        self.config({'mode':'deny','allowed_platforms':['discord'],'preserve_tui':False})
        self.assertIsNotNone(policy.resolve_notify_target(platform='discord',chat_id='origin'))
        self.assertIsNone(policy.resolve_notify_target(platform='tui',chat_id='s'))
        (self.root/'config.yaml').write_text('kanban: [unterminated')
        self.assertIsNone(policy.resolve_notify_target(platform='discord',chat_id='origin'))

    def test_owner_missing_fails_closed_and_scope_resets(self):
        from hermes_constants import get_hermes_home
        from agent.secret_scope import current_secret_scope
        before=(get_hermes_home(),current_secret_scope(),dict(os.environ))
        self.assertIsNone(policy.resolve_notify_target(platform='discord',chat_id='origin',notifier_profile='missing'))
        self.assertEqual(before,(get_hermes_home(),current_secret_scope(),dict(os.environ)))

    def test_foreign_owner_home_does_not_inherit_process_destination(self):
        other=self.root/'mounted-owner'; other.mkdir()
        (other/'config.yaml').write_text(json.dumps({'kanban':{'notification_policy':'telegram_home_only'},'platforms':{'telegram':{'home_channel':{'platform':'telegram','chat_id':'owned-home'}}}}))
        before=dict(os.environ)
        with patch.dict(os.environ,{'TELEGRAM_HOME_CHANNEL':'ambient-wrong'}):
            target=policy.resolve_notify_target(platform='discord',chat_id='origin',notifier_profile='mounted',profile_home=other)
            self.assertEqual(target.chat_id,'owned-home')
        self.assertEqual(dict(os.environ),before)

    def test_redirect_subscription_sanitizes_origin_metadata(self):
        self.config('telegram_home_only')
        target=policy.subscribe_notify(self.conn,task_id=self.tid,platform='discord',chat_id='origin',thread_id='42',user_id='origin-user',user_id_alt='alt',notifier_profile='default',delivery_mode='notify+wake',delivery_metadata={'reply_to':'old'})
        self.assertEqual((target['platform'],target['chat_id'],target['thread_id']),('telegram','home','99'))
        row=kb.list_notify_subs(self.conn,self.tid)[0]
        self.assertEqual(row['delivery_mode'],'notify'); self.assertFalse(row['delivery_metadata']); self.assertFalse(row['user_id_alt'])

    async def test_delivery_rechecks_policy_preserves_existing_row_and_cursor(self):
        self.add(); kb.complete_task(self.conn,self.tid,summary='done')
        before=kb.list_notify_subs(self.conn,self.tid)[0]
        self.config('telegram_home_only'); r=self.runner(); await self.tick(r)
        self.assertEqual(r.adapters[Platform.DISCORD].sent,[])
        self.assertEqual(r.adapters[Platform.TELEGRAM].sent,[('home',{'thread_id':'99'})])
        after=kb.list_notify_subs(self.conn,self.tid)[0]
        self.assertGreater(after['last_event_id'],before['last_event_id'])
        self.assertEqual({k:v for k,v in before.items() if k!='last_event_id'}, {k:v for k,v in after.items() if k!='last_event_id'})

    async def test_denied_between_claim_and_send_rewinds_original_row(self):
        self.add(); kb.complete_task(self.conn,self.tid,summary='done')
        before=kb.list_notify_subs(self.conn,self.tid)[0]
        r=self.runner(); await self.tick(r,between=lambda:self.config('deny'))
        self.assertFalse(any(a.sent for a in r.adapters.values()))
        self.assertEqual(kb.list_notify_subs(self.conn,self.tid)[0],before)

    async def test_redirect_send_failure_rewinds_original_row(self):
        self.add(); kb.complete_task(self.conn,self.tid,summary='done')
        before=kb.list_notify_subs(self.conn,self.tid)[0]; self.config('telegram_home_only')
        await self.tick(self.runner(fail=True))
        self.assertEqual(kb.list_notify_subs(self.conn,self.tid)[0],before)

    def test_cli_denial_audit_is_readonly_and_no_false_success(self):
        self.add(); before=kb.list_notify_subs(self.conn,self.tid); self.config('deny')
        parser=argparse.ArgumentParser(); kanban.build_parser(parser.add_subparsers())
        args=parser.parse_args(['kanban','notify-subscribe',self.tid,'--platform','discord','--chat-id','new'])
        with contextlib.redirect_stderr(io.StringIO()): self.assertEqual(kanban.kanban_command(args),1)
        out=io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(kanban.kanban_command(parser.parse_args(['kanban','notify-audit','--json'])),0)
        self.assertIsNone(json.loads(out.getvalue())[0]['policy_target'])
        self.assertEqual(kb.list_notify_subs(self.conn,self.tid),before)

    def test_tool_subscription_uses_policy_and_tui_preservation(self):
        self.config('deny')
        with patch.dict(os.environ,{'HERMES_SESSION_PLATFORM':'discord','HERMES_SESSION_CHAT_ID':'origin'}):
            self.assertFalse(_maybe_auto_subscribe(self.conn,self.tid))
        with patch.dict(os.environ,{'HERMES_SESSION_PLATFORM':'','HERMES_SESSION_CHAT_ID':'','HERMES_SESSION_KEY':'tui-session'}):
            self.assertTrue(_maybe_auto_subscribe(self.conn,self.tid))
        self.assertEqual(kb.list_notify_subs(self.conn,self.tid)[0]['platform'],'tui')

    def test_dashboard_filters_homes_but_allows_removing_denied_existing_sub(self):
        self.config('telegram_home_only')
        self.assertEqual([h['platform'] for h in api.get_home_channels(task_id=None,board='default')['home_channels']],['telegram'])
        with self.assertRaises(api.HTTPException): api.subscribe_home(self.tid,'discord',board='default')
        api.subscribe_home(self.tid,'telegram',board='default')
        self.config('deny')
        api.unsubscribe_home(self.tid,'telegram',board='default')
        self.assertEqual(kb.list_notify_subs(self.conn,self.tid),[])

    async def test_slash_create_denied_subscription_keeps_created_task(self):
        self.config('deny'); r=self.runner()
        r._thread_metadata_for_source=lambda *a: {'reply_to':'old'}
        r._reply_anchor_for_event=lambda *a:None
        event=SimpleNamespace(text='/kanban create slash-task',source=SimpleNamespace(platform=Platform.DISCORD,chat_id='origin',thread_id='42',user_id='u',chat_type='channel'))
        output=await r._handle_kanban_command(event)
        self.assertIn('Created t_',output)
        tid=self.conn.execute("SELECT id FROM tasks WHERE title='slash-task'").fetchone()[0]
        self.assertEqual(kb.list_notify_subs(self.conn,tid),[])
