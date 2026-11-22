"""Isolated direct-response flow checks; no network or background workers."""
import ast
import copy
import os
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

os.environ['TELEGRAM_BOT_TOKEN'] = 'test:test'
os.environ['GITHUB_TOKEN'] = ''
os.environ['ONEC_WEBHOOK'] = ''

# Remove only module-level worker starts before executing the actual functions.
tree = ast.parse(Path(__file__).with_name('bot.py').read_text(encoding='utf-8-sig'))
tree.body = [n for n in tree.body if not (isinstance(n, ast.Expr) and
    isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Attribute) and
    n.value.func.attr == 'start')]
bot = types.ModuleType('isolated_bot')
bot.__file__ = str(Path(__file__).with_name('bot.py'))
with patch('requests.sessions.Session.request', side_effect=AssertionError('Network prohibited')):
    exec(compile(tree, bot.__file__, 'exec'), bot.__dict__)

class DirectResponseTests(unittest.TestCase):
    def setUp(self):
        self.store = {}
        self.sent = []
        bot.STATE.clear()
        bot.api = lambda method, **kw: self.sent.append((method, kw)) or {'ok': True, 'result': {'message_id': 3}}
        bot.gh_read_json = lambda path, default=None: copy.deepcopy(self.store.get(path, default))
        def update(path, mutate, message, **kwargs):
            data = copy.deepcopy(self.store.get(path, {}))
            mutate(data)
            self.store[path] = data
            return copy.deepcopy(data)
        bot.gh_update_json = update
        bot.lookup_phone = lambda chat: None
        bot.is_live_test = lambda chat: False
        bot._segment = lambda chat: bot.STATE.get(chat, {}).get('segment')
        bot.save_subscriber = lambda *a, **k: None
        bot.schedule_url = lambda: 'https://example.invalid/schedule'
        bot.send_to_1c = lambda *a, **k: None
        bot.log_lead_event = lambda *a, **k: None
        bot.too_soon = lambda uid: False
        bot.is_quiet = lambda: False
        bot._now = lambda now=None: now or datetime(2026, 11, 22, 15, 30, tzinfo=bot.MSK)
        self.user = {'id': 888, 'first_name': 'Тест'}
    def state(self, **kwargs):
        bot.STATE[888] = dict(direct_flow=True, segment='new', phone='+79991234567', **kwargs)
        return bot.STATE[888]
    def test_gate_two_buttons(self):
        bot.step_gate(888, 'Тест')
        keys = self.sent[-1][1]['reply_markup']['inline_keyboard']
        self.assertEqual([r[0]['callback_data'] for r in keys], ['seg:new', 'seg:member'])
    def test_offer_contact_and_both_formats(self):
        bot.step_quick_offer(888)
        msg = self.sent[-1][1]
        self.assertIn('экскурсию', msg['text'])
        self.assertIn('500 м²', msg['text'])
        self.assertTrue(msg['reply_markup']['keyboard'][0][0]['request_contact'])
    def test_phone_opens_format_not_group(self):
        self.state(direct_stage='offer')
        bot.finish(888, self.user, '+79991234567')
        text = str(self.sent[-1][1])
        self.assertIn('dr:training', text)
        self.assertIn('dr:tour', text)
        self.assertNotIn('d2:group', text)
    def test_training_goal_then_direction(self):
        self.state()
        bot.direct_button(888, 2, self.user, 'dr:training')
        self.assertEqual(bot.STATE[888]['direct_stage'], 'goal')
        bot.direct_button(888, 2, self.user, 'drg:shape')
        self.assertEqual(bot.STATE[888]['goal'], 'shape')
        self.assertEqual(bot.STATE[888]['direct_stage'], 'direction')
        self.assertNotIn('group', str(self.sent[-1][1]))
    def test_tour_multi_select_and_skip(self):
        self.state()
        bot.direct_button(888, 2, self.user, 'dr:tour')
        bot.direct_button(888, 2, self.user, 'dri:gym')
        bot.direct_button(888, 2, self.user, 'dri:sauna')
        self.assertEqual(bot.STATE[888]['interests'], ['gym', 'sauna'])
        bot.direct_button(888, 2, self.user, 'dri:skip')
        self.assertEqual(bot.STATE[888]['interests'], [])
        self.assertEqual(bot.STATE[888]['direct_stage'], 'date')
    def test_real_hours(self):
        self.assertEqual(bot.SLOTS_TRAINING, [f'{h:02d}:00' for h in range(10,21)])
        self.assertEqual(bot.slots_on('2026-11-22', 'training', 888), ['16:00','17:00','18:00','19:00','20:00'])
        self.assertEqual(bot.SLOTS_TOUR, ['10:00','12:00','14:00','17:00','19:00','21:00'])
    def test_goal_filter_and_boxing_exception(self):
        with patch.object(bot, '_настройка_подбора', return_value={'направления': {'gym':['gym'], 'fight':['boxing']}, 'цели': {'shape':['weight']}}), patch.object(bot, 'нужное_по_здоровью', return_value=([],[])), patch.object(bot, 'очередь', return_value=['A','B','C']), patch.object(bot, 'профили', return_value={'A':{'компетенции':['gym']},'B':{'компетенции':['gym','weight']},'C':{'компетенции':['boxing']}}):
            self.assertEqual(bot.разбор_подбора('shape','gym','',strict_goal=True)[0], ['B'])
            self.assertEqual(bot.разбор_подбора('shape','fight','',strict_goal=True)[0], ['C'])
            self.assertEqual(bot.разбор_подбора('shape','gym','')[0], ['A','B'])
    def test_special_goal_skips_direction(self):
        self.state(fmt='training')
        bot.direct_button(888, 2, self.user, 'drg:back')
        self.assertEqual(bot.STATE[888]['dir'], 'back')
        self.assertEqual(bot.STATE[888]['direct_stage'], 'date')
    def test_restart_restores_goal(self):
        self.state(fmt='training', goal='shape', strict_goal=True, direct_stage='date')
        bot.direct_save(888, 'goal', 'shape')
        bot.STATE.clear()
        bot.lookup_phone = lambda chat: '+79991234567'
        restored = bot.direct_restore(888)
        self.assertEqual(restored['goal'], 'shape')
        self.assertTrue(restored['strict_goal'])
        self.assertEqual(restored['phone'], '+79991234567')
    def test_member_stale_button_cannot_restore_guest(self):
        self.state(fmt='training')
        bot.direct_save(888, 'offer')
        bot.STATE[888] = {'segment': 'member'}
        bot.direct_button(888, 2, self.user, 'dr:training')
        self.assertEqual(bot.STATE[888]['segment'], 'member')
        self.assertNotIn('drg:', str(self.sent[-1]))
    def test_stopped_cannot_resume_with_old_button(self):
        self.state(direct_stage='stopped')
        bot.direct_button(888, 2, self.user, 'dr:training')
        self.assertEqual(bot.STATE[888]['direct_stage'], 'stopped')
    def test_stale_and_invalid_hours_do_not_book(self):
        self.state(fmt='training', goal='shape', direct_stage='time')
        for hour in ('10:00', '18:30', '21:00'):
            with patch.object(bot, 'slot_chosen', side_effect=AssertionError('Invalid hour booked')):
                bot.on_button({'id':'cb', 'data':'sl:training:2026-11-22:'+hour,
                    'message':{'chat':{'id':888,'type':'private'},'message_id':2}, 'from':self.user})
        self.assertEqual(bot.STATE[888]['direct_stage'], 'date')
    def test_followup_once_and_not_historic(self):
        from datetime import timedelta
        self.state(fmt='training', direct_stage='goal')
        bot.direct_save(888, 'offer')
        rec = self.store[bot.FUNNEL_PATH]['888']
        rec['updated_at'] = (datetime.now(bot.MSK) - timedelta(hours=3)).isoformat()
        self.store[bot.GH_PATH] = {'888': {'segment':'new','phone':'+79991234567'}, '999':{'segment':'new','first_seen':'2020-01-01 00:00'}}
        bot.send_24h_followups()
        first = len(self.sent)
        self.assertEqual(first, 1)
        self.assertIn('dr:resume', str(self.sent))
        bot.send_24h_followups()
        self.assertEqual(len(self.sent), first)
    def test_followup_not_for_start_only_or_pending(self):
        from datetime import timedelta
        self.state(direct_stage='gate')
        bot.direct_save(888, 'start')
        self.store[bot.FUNNEL_PATH]['888']['updated_at'] = (datetime.now(bot.MSK)-timedelta(hours=3)).isoformat()
        bot.send_24h_followups()
        self.assertFalse(self.sent)
        bot.STATE[888]['booking_mid'] = 2
        bot.direct_save(888, 'offer')
        self.store[bot.FUNNEL_PATH]['888']['updated_at'] = (datetime.now(bot.MSK)-timedelta(hours=3)).isoformat()
        bot.send_24h_followups()
        self.assertFalse(self.sent)
    def test_complete_new_training_confirms_and_transmits_goal(self):
        self.state(fmt='training', goal='shape', dir='gym', strict_goal=True, direct_stage='time', client_name='Тест')
        name = 'Дмитрий Анников'
        with patch.object(bot, '_настройка_подбора', return_value={'направления':{'gym':['gym']},'цели':{'shape':['weight']}}), patch.object(bot, 'нужное_по_здоровью', return_value=([],[])), patch.object(bot, 'очередь', return_value=[name]), patch.object(bot, 'профили', return_value={name:{'компетенции':['gym','weight']}}), patch.object(bot, '_free_trainers_for', return_value=[name]), patch.object(bot, 'trainer_chat_id_for', return_value=666), patch.object(bot, 'coordinator_chat_id', return_value=None), patch.object(bot, 'active_manager_name', return_value='Тест'), patch.object(bot, 'manager_for_visit', return_value='Тест'), patch.object(bot, 'push_1c_followup', return_value=None), patch.object(bot, '_ход_отдан', return_value=None), patch.object(bot, 'отчёт_координатору', return_value=None):
            bot.on_button({'id':'cb','data':'sl:training:2026-11-23:18:00',
                'message':{'chat':{'id':888,'type':'private'},'message_id':2},'from':self.user})
        self.assertEqual(bot.STATE[888]['direct_stage'],'confirmed')
        trainer_messages = [kw['text'] for method,kw in self.sent if kw.get('chat_id')==666]
        self.assertTrue(any('Снизить вес' in text for text in trainer_messages))
        events = self.store[bot.FUNNEL_PATH]['888']['events']
        self.assertTrue(any(e['event']=='confirmed' for e in events))
    def test_terminal_contact_and_calendar_do_not_restart(self):
        for stage in ('pending','confirmed','stopped'):
            self.state(fmt='training', direct_stage=stage)
            with patch.object(bot, 'send_to_1c', side_effect=AssertionError('Duplicate lead')):
                bot.finish(888, self.user, '+79991234567')
            bot.on_button({'id':'cb','data':'tp:2026-11-23:training',
                'message':{'chat':{'id':888,'type':'private'},'message_id':2},'from':self.user})
            self.assertEqual(bot.STATE[888]['direct_stage'],stage)
    def test_member_stale_calendar_cannot_book_guest(self):
        bot.STATE[888] = {'segment':'member'}
        with patch.object(bot, 'slot_chosen', side_effect=AssertionError('Guest booking for member')):
            bot.on_button({'id':'cb','data':'sl:training:2026-11-23:18:00',
                'message':{'chat':{'id':888,'type':'private'},'message_id':2},'from':self.user})
    def test_test_phone_retires_prior_offer(self):
        self.state()
        bot.direct_save(888,'offer')
        bot.STATE[888]['phone'] = '+79000000001'
        bot.direct_save(888,'phone')
        self.assertTrue(self.store[bot.FUNNEL_PATH]['888']['test'])
        self.assertEqual(self.store[bot.FUNNEL_PATH]['888']['closed'],'test')
    def test_events_exclude_live_test(self):
        self.state()
        bot.is_live_test = lambda chat: True
        bot.direct_save(888, 'phone')
        self.assertFalse(self.store.get(bot.FUNNEL_PATH))

if __name__ == '__main__':
    with patch('requests.sessions.Session.request', side_effect=AssertionError('Network prohibited')):
        unittest.main()
