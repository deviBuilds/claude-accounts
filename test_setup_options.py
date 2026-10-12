import curses
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import account_status
import setup_options as options
import accounts_tui as tui
import desktop_profiles as dp
import onboarding as ob
from test_accounts_tui import Screen
from test_onboarding import OnboardingTests


class SelectionTests(unittest.TestCase):
    def setUp(self):
        patch('curses.has_colors',return_value=False).start()
        patch('curses.curs_set').start()
        self.addCleanup(patch.stopall)

    def test_space_toggles_detected_apps_without_forcing_them_selected(self):
        screen=Screen();selector=options.Selector(screen,{'cli':True,'desktop':False},['cli','desktop'])
        selector.draw()
        self.assertIn('✓ Detected',screen.text)
        selector.key(32);selector.key(10)
        self.assertEqual(selector.result,['desktop'])

    def test_none_selected_cannot_continue_and_cancel_returns_none(self):
        selector=options.Selector(Screen(),{},['cli'])
        selector.key(32);selector.key(10)
        self.assertFalse(selector.finished)
        self.assertIn('at least one',selector.notice)
        selector.key(27)
        self.assertIsNone(selector.result)

    def test_cli_only_finishes_without_desktop_and_menu_does_not_offer_it(self):
        app=tui.App(Screen(),True);app.components=['cli']
        app.label='b';app.begin_setup()
        self.assertEqual(app.wizard['stage'],'complete')
        app.wizard['stage']='cli';app.after_status='verify_cli';app.status_ready()
        self.assertEqual(app.wizard['stage'],'complete')
        app.goto('account')
        self.assertFalse({'setup','save','switch'} & {item[0] for item in app.items()})

    def test_desktop_only_skips_cli_and_dispatches_desktop_helper(self):
        app=tui.App(Screen(),True);app.components=['desktop']
        app.label='a';app.begin_setup()
        self.assertEqual(app.wizard['stage'],'desktop')
        app.demo=False
        with patch.object(tui,'Worker') as worker:
            app.start_onboarding('prepare')
            self.assertEqual(worker.call_args.args[0][-2:],['prepare-desktop','a'])
        app.worker=None;app.goto('account')
        self.assertFalse({'cli','auth','usage'} & {item[0] for item in app.items()})

    def test_preferences_and_desktop_discovery_do_not_read_cli_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            home=Path(directory)
            options.save(['desktop'],home)
            self.assertEqual(options.load(home),['desktop'])
            store=home/'.claude-desktop-profiles';store.mkdir()
            (store/'onboarding-work.json').write_text('{"mode":"desktop"}')
            with patch.object(dp,'desktop_account',return_value=None),patch.object(dp,'app_running',return_value=False):
                state=account_status.collect(home,lambda _:self.fail('CLI credentials read'),lambda _:self.fail('CLI identity called'))
            self.assertIn('work',[a['label'] for a in state['accounts']])
            self.assertTrue(all(a['cli_state']=='not_selected' for a in state['accounts']))


class DesktopOnlyTests(OnboardingTests):
    # Reuse the isolated fake Desktop fixture, but only discover these new cases.
    def test_desktop_only_save_and_switch_without_cli_or_organization(self):
        self.manager.identity=lambda _:self.fail('CLI identity must not be required')
        ob.create_desktop(self.manager,'a')
        ob.prepare_desktop(self.manager,'a')
        ob.finish_desktop(self.manager,'a')
        first=self.manager.index()['profiles']['a']
        self.assertIsNone(first['organization'])
        self.assertTrue(first['desktopOnly'])
        self.assertFalse((self.home/'.claude-acct-d').exists())
        ob.create_desktop(self.manager,'d')
        ob.prepare_desktop(self.manager,'d')
        self.assertEqual(self.manager.index()['pendingLogin']['label'],'d')
        self.login('d');ob.finish_desktop(self.manager,'d')
        self.manager.switch('a')
        self.assertEqual((self.manager.root/'identity').read_text(),'a-id')
        self.manager.switch('d')
        self.assertEqual((self.manager.root/'identity').read_text(),'d-id')

    def test_desktop_only_wrong_account_and_missing_login_preserve_saved_profile(self):
        ob.finish_desktop(self.manager,'a')
        original=self.manager.index()
        self.login('d')
        with self.assertRaisesRegex(RuntimeError,'different account'):
            ob.finish_desktop(self.manager,'a')
        self.assertEqual(self.manager.index(),original)
        (self.manager.root/'identity').unlink()
        with self.assertRaisesRegex(RuntimeError,'not signed in'):
            ob.finish_desktop(self.manager,'d')
        self.assertEqual(self.manager.index(),original)

    def test_desktop_only_duplicate_login_cannot_be_saved_under_new_label(self):
        ob.finish_desktop(self.manager,'a')
        with self.assertRaisesRegex(RuntimeError,'already saved as a'):
            ob.finish_desktop(self.manager,'d')
        self.assertNotIn('d',self.manager.index()['profiles'])


# unittest would otherwise run the inherited base cases twice in this module.
for name in list(vars(OnboardingTests)):
    if name.startswith('test_'):
        setattr(DesktopOnlyTests,name,None)
del OnboardingTests

if __name__=='__main__':unittest.main()
