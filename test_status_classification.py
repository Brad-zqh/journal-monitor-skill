"""Offline regression tests using synthetic manuscript fixtures."""

import unittest

from check_journals import apply_status_overrides, detect_status_change, is_closed


class StatusClassificationTests(unittest.TestCase):
    def test_required_reviews_completed_is_still_active(self):
        self.assertFalse(is_closed({'status': 'Required Reviews Completed'}))

    def test_other_workflow_states_are_still_active(self):
        for status in ('Under Review', 'Decision in Process', 'Major Revision'):
            with self.subTest(status=status):
                self.assertFalse(is_closed({'status': status}))

    def test_empty_manual_override_does_not_change_classification(self):
        self.assertFalse(is_closed({'status': 'Under Review', 'manual_active': None}))

    def test_decision_letter_does_not_make_a_revision_final(self):
        article = {
            'status': 'Major Revision',
            'disposition': 'Decision letter available',
        }
        self.assertFalse(is_closed(article))

    def test_terminal_statuses_are_closed(self):
        for status in ('Reject', 'Rejected', 'Accepted', 'Withdrawn'):
            with self.subTest(status=status):
                self.assertTrue(is_closed({'status': status}))

    def test_final_disposition_is_closed(self):
        self.assertTrue(is_closed({
            'status': 'Submission with a Decision',
            'disposition': 'Reject',
        }))

    def test_manual_resubmission_override_is_active(self):
        articles = [{
            'journal': 'TEST',
            'id': 'TEST-D-30-00001',
            'status': 'Completed - Reject',
            'status_date': 'Aug 14 2030',
            'disposition': 'Reject',
        }]
        config = {'status_overrides': {
            'TEST::TEST-D-30-00001': {
                'status': 'Reject and Resubmit',
                'status_date': '2030-08-15',
                'active': True,
            }
        }}

        apply_status_overrides(config, articles)

        self.assertEqual(articles[0]['status'], 'Reject and Resubmit')
        self.assertEqual(articles[0]['status_date'], '2030-08-15')
        self.assertFalse(is_closed(articles[0]))

    def test_decision_letter_formatting_is_not_a_status_change(self):
        old = {
            'status': 'Reject and Resubmit',
            'status_date': '2030-08-15',
            'disposition': 'Reject',
            'letter': 'Decision letter body\n5',
        }
        current = {
            'status': 'Reject and Resubmit',
            'status_date': '2030-08-15',
            'disposition': 'Reject',
            'letter': 'Decision letter body\nCookie settings Accept all cookies',
        }

        self.assertIsNone(detect_status_change('TEST', 'TEST-D-30-00001', old, current))

    def test_real_status_change_is_reported(self):
        old = {'status': 'With Editor', 'status_date': '2030-08-14'}
        current = {'status': 'Under Review', 'status_date': '2030-08-15'}

        change = detect_status_change('TEST', 'TEST-D-30-00001', old, current)

        self.assertEqual(change['old_status'], 'With Editor')
        self.assertEqual(change['new_status'], 'Under Review')


if __name__ == '__main__':
    unittest.main()
