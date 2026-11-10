"""The usaspending-gov quick start and x-blink-display (offline; no network or browser)."""
import copy
import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from blink import bundled, dynamic_cli  # noqa: E402
from blink.errors import BlinkError  # noqa: E402
from blink.profile import compile_manifest  # noqa: E402

RESULT = {'status': 'ok', 'data': {'results': [
    {'agency_name': 'Department of the Treasury', 'budget_authority_amount': 3982061714337.72,
     'percentage_of_total_budget_authority': 0.2460918865898895, 'obligated_amount': 1979847631401.85, 'active_fy': '2026'},
    {'agency_name': 'A very long agency name that keeps going and going on', 'budget_authority_amount': -5,
     'percentage_of_total_budget_authority': None, 'obligated_amount': 'n/a', 'active_fy': '2026'}]}}


def document():
    return bundled.load('usaspending-gov', ROOT)['document']


class BudgetBlinkTests(unittest.TestCase):
    def test_bundled_public_read_only_and_learned_from_a_recording(self):
        found = bundled.load('usaspending-gov', ROOT)
        self.assertTrue(bundled.public_read_only(found['document']))
        self.assertIn('recording', found['document']['x-blink-service']['history'][0]['note'])
        operation = compile_manifest(found['document'])['operations']['agencyBudgets']
        self.assertEqual((operation['method'], operation['origin']), ('GET', 'https://api.usaspending.gov'))
        self.assertEqual(operation['inputSchema']['required'], [])  # the page's sort order is the default
        self.assertEqual(operation['display']['rows'], '/results')

    def test_display_is_carried_only_when_declared_so_existing_digests_stay_the_same(self):
        nasa = compile_manifest(bundled.load('nasa-gov', ROOT)['document'])['operations']['latestNews']
        self.assertNotIn('display', nasa)
        doc = document()
        without = copy.deepcopy(doc)
        del without['paths']['/api/v2/references/toptier_agencies/']['get']['x-blink-display']
        self.assertNotEqual(compile_manifest(doc)['operations']['agencyBudgets']['digest'],
                            compile_manifest(without)['operations']['agencyBudgets']['digest'])

    def test_invalid_display_hints_are_rejected(self):
        for bad in ({'columns': []}, {'columns': [{'path': 'no-slash', 'label': 'X'}]},
                    {'columns': [{'path': '/a', 'label': 'X', 'format': 'html'}]}, {'rows': '/r', 'columns': [{'path': '/a'}]},
                    {'columns': [{'path': '/a', 'label': 'X'}], 'script': 'x'}):
            doc = document()
            doc['paths']['/api/v2/references/toptier_agencies/']['get']['x-blink-display'] = bad
            with self.subTest(bad=bad), self.assertRaises(BlinkError):
                compile_manifest(doc)

    def test_table_uses_labels_formats_and_the_websites_numbers(self):
        display = compile_manifest(document())['operations']['agencyBudgets']['display']
        out = io.StringIO()
        with mock.patch('sys.stdout', out):
            dynamic_cli.render(RESULT, 'table', display)
        header, first, second = out.getvalue().splitlines()
        self.assertEqual(header.split('  ')[0], 'Agency')
        for text in ('Budgetary resources', 'Share', 'Obligated', 'Fiscal year'):
            self.assertIn(text, header)
        self.assertIn('$3,982,061,714,338', first)  # exactly as usaspending.gov shows it
        self.assertIn('24.61%', first)
        self.assertIn('-$5', second)
        self.assertIn('n/a', second)
        self.assertIn('…', second)  # long names are cut, numbers never

    def test_json_output_ignores_the_display(self):
        out = io.StringIO()
        with mock.patch('sys.stdout', out):
            dynamic_cli.render(RESULT, 'json', {'columns': [{'path': '/x', 'label': 'X'}]})
        self.assertEqual(json.loads(out.getvalue())['data'], RESULT['data'])

    def test_an_unexpected_shape_falls_back_to_the_generic_table(self):
        out = io.StringIO()
        with mock.patch('sys.stdout', out):
            dynamic_cli.render({'status': 'ok', 'data': {'results': 5}}, 'table', {'rows': '/results/x/y', 'columns': [{'path': '/a', 'label': 'A'}]})
        self.assertTrue(out.getvalue())

    def test_routine_run_on_a_terminal_prints_the_last_step_as_its_table(self):
        display = compile_manifest(document())['operations']['agencyBudgets']['display']
        routine = {'status': 'ok', 'summary': 'Routine completed and verified', 'warnings': [],
                   'data': {'runId': 'r', 'steps': [RESULT], 'lastStep': {'service': 'bundled:usaspending-gov', 'operation': 'agencyBudgets'}}}
        out = io.StringIO()
        with mock.patch.object(dynamic_cli, '_legacy', return_value=(routine, {'output': 'json'})), \
                mock.patch.object(dynamic_cli, '_operation', return_value={'status': 'ok', 'data': {'display': display}}), \
                mock.patch('sys.stdout', out), mock.patch.object(out, 'isatty', return_value=True), \
                mock.patch.dict('os.environ', {'BLINK_CONFIG_DIR': str(Path(self.id().replace('.', '_')).resolve().parent / '.unused')}):
            with mock.patch.object(dynamic_cli.ConfigStore, 'load', return_value={'defaults': {}, 'blinks': {}}):
                code = dynamic_cli.route(['routine', 'run', 'budget'])
        lines = out.getvalue().splitlines()
        self.assertEqual(code, 0)
        self.assertEqual(lines[0], 'Routine completed and verified (1 step)')
        self.assertIn('$3,982,061,714,338', lines[2])

    def test_dry_runs_keep_their_plan_visible(self):
        out = io.StringIO()
        with mock.patch('sys.stdout', out):
            dynamic_cli.render({'status': 'ok', 'data': {'dryRun': True, 'dispatch': False}}, 'table',
                               {'rows': '/results', 'columns': [{'path': '/a', 'label': 'A'}]})
        self.assertIn('dryRun', out.getvalue())

    def test_a_changed_response_shape_falls_back_instead_of_an_empty_table(self):
        display = {'rows': '/results', 'columns': [{'path': '/agency_name', 'label': 'Agency'}]}
        for data in ({'items': [{'agency_name': 'X'}]}, {'results': [{'other': 1}]}):
            out = io.StringIO()
            with mock.patch('sys.stdout', out):
                dynamic_cli.render({'status': 'ok', 'data': data}, 'table', display)
            self.assertNotIn('Agency', out.getvalue().splitlines()[0])
        out = io.StringIO()
        with mock.patch('sys.stdout', out):  # a legitimately empty list is still the reviewed table
            dynamic_cli.render({'status': 'ok', 'data': {'results': []}}, 'table', display)
        self.assertEqual(out.getvalue().strip(), 'Agency')

    def test_numbers_keep_every_digit(self):
        self.assertEqual(dynamic_cli._formatted(9007199254740993, 'number'), '9,007,199,254,740,993')
        self.assertEqual(dynamic_cli._formatted('9007199254740993', 'usd'), '$9,007,199,254,740,993')
        self.assertEqual(dynamic_cli._formatted(0.2460918865898895, 'percent'), '24.61%')
        self.assertEqual(dynamic_cli._formatted('NaN', 'usd'), 'NaN')
        self.assertEqual(dynamic_cli._formatted('123456789012345678901234567.49', 'usd'), '$123,456,789,012,345,678,901,234,567')
        self.assertEqual(dynamic_cli._formatted('0.123449999999999999999999999999', 'percent'), '12.34%')
        self.assertEqual(dynamic_cli._formatted('12345678901234567890123456789', 'usd'), '$12,345,678,901,234,567,890,123,456,789')
        self.assertEqual(dynamic_cli._formatted('0.12344' + '9' * 116, 'percent'), '12.34%')
        self.assertEqual(dynamic_cli._formatted('0.12344' + '9' * 190, 'percent'), '12.34%')
        long_text = '1.' + '1' * 300
        self.assertEqual(dynamic_cli._formatted(long_text, 'percent'), long_text)

    def test_huge_exponents_are_shown_as_text_at_once(self):
        import time
        started = time.monotonic()
        self.assertEqual(dynamic_cli._formatted('1e1000000', 'number'), '1e1000000')
        self.assertLess(time.monotonic() - started, 0.5)

    def test_pointers_follow_rfc_6901(self):
        self.assertEqual(dynamic_cli._pointer({'': {'name': 'EXPECTED'}, 'name': 'WRONG'}, '//name'), 'EXPECTED')
        self.assertIs(dynamic_cli._pointer({'a': 1}, '/b'), dynamic_cli.MISSING)

    def test_labels_and_values_cannot_drive_the_terminal(self):
        out = io.StringIO()
        with mock.patch('sys.stdout', out):
            dynamic_cli.render({'status': 'ok', 'data': [{'a': 'x\x1b[2Jy'}]}, 'table',
                               {'columns': [{'path': '/a', 'label': 'L\x1b[H\nforged'}]})
        self.assertNotIn('\x1b', out.getvalue())
        self.assertEqual(len(out.getvalue().splitlines()), 2)

    def test_readme_shows_both_examples_for_this_release(self):
        from blink import __version__
        readme = (ROOT / 'README.md').read_text()
        self.assertIn(f'archive/refs/tags/v{__version__}.zip blink usaspending-gov agency-budgets', readme)
        self.assertIn('docs/media/budget.gif', readme)
        self.assertTrue((ROOT / 'docs/media/budget.gif').is_file())


if __name__ == '__main__':
    unittest.main()
