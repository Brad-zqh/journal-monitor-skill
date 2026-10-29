"""Offline regression tests using synthetic manuscript fixtures."""

import asyncio
import unittest

from playwright.async_api import async_playwright

from check_journals import PARSE_ROWS_JS, SCHOLARONE_ROWS_JS


async def parse_rows(html, parser=SCHOLARONE_ROWS_JS):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content(html)
        rows = await page.evaluate(parser)
        await browser.close()
        return rows


class ScholarOneParserTests(unittest.TestCase):
    def test_returned_draft_uses_message_in_id_cell(self):
        rows = asyncio.run(parse_rows("""
            <table>
              <tr><th>CONTINUE</th><th>ID</th><th>TITLE</th><th>CREATED</th><th>DELETE</th></tr>
              <tr>
                <td>Continue</td>
                <td>Draft (TEST-01-2030-0001)<br>Returned by Editorial Assistant on 23-Jul-2030</td>
                <td>A synthetic manuscript about research methods<br>View Submission</td>
                <td>04-Jul-2030</td>
                <td>Delete</td>
              </tr>
            </table>
        """))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['status'], 'Returned by Editorial Assistant')
        self.assertEqual(rows[0]['status_date'], '23-Jul-2030')
        self.assertEqual(
            rows[0]['title'],
            'A synthetic manuscript about research methods',
        )
        self.assertEqual(rows[0]['created'], '04-Jul-2030')
        self.assertEqual(rows[0]['submitted'], '')

    def test_submitted_layout_uses_named_columns(self):
        rows = asyncio.run(parse_rows("""
            <table>
              <tr><th>STATUS</th><th>ID</th><th>TITLE</th><th>CREATED</th><th>SUBMITTED</th></tr>
              <tr>
                <td>Contact Journal &bull; AE: Not Assigned &bull; EIC: Editor, Example &bull; Awaiting Admin Processing</td>
                <td>TEST-01-2030-0001</td>
                <td>A complete manuscript title</td>
                <td>04-Jul-2030</td>
                <td>18-Jul-2030</td>
              </tr>
            </table>
        """))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['status'], 'Awaiting Admin Processing')
        self.assertEqual(rows[0]['status_date'], '18-Jul-2030')
        self.assertEqual(rows[0]['ae'], 'Not Assigned')
        self.assertEqual(rows[0]['eic'], 'Editor, Example')


class EditorialManagerParserTests(unittest.TestCase):
    def test_ingest_submission_without_manuscript_number_is_tracked(self):
        rows = asyncio.run(parse_rows("""
            <table>
              <tr>
                <th>Action</th><th>Manuscript Number</th><th>Title</th>
                <th>Initial Date Submitted</th><th>Status Date</th><th>Current Status</th>
              </tr>
              <tr>
                <td>View Submission</td><td></td>
                <td>A synthetic manuscript about research methods</td>
                <td>Jul 30, 2030</td><td>Jul 30, 2030</td><td>Submission Received via Ingest</td>
              </tr>
            </table>
        """, PARSE_ROWS_JS))

        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]['id'].startswith('PENDING-'))
        self.assertEqual(rows[0]['status'], 'Submission Received via Ingest')
        self.assertEqual(rows[0]['status_date'], 'Jul 30, 2030')
        self.assertEqual(
            rows[0]['title'],
            'A synthetic manuscript about research methods',
        )

    def test_transfer_decision_overrides_generic_submitted_queue(self):
        rows = asyncio.run(parse_rows("""
            <table>
              <tr><th>STATUS</th><th>ID</th><th>TITLE</th></tr>
              <tr>
                <td>Contact Journal &bull; AE: Not Assigned &bull; EIC: Editor, Example &bull; Approval To Transfer requested on 29-Jul-2030 &bull; View Decision Letter</td>
                <td>TEST-01-2030-0001</td>
                <td>Approval To Transfer requested on 29-Jul-2030</td>
              </tr>
            </table>
        """))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['status'], 'Rejected - Transfer approval requested')
        self.assertEqual(rows[0]['status_date'], '29-Jul-2030')
        self.assertEqual(rows[0]['disposition'], 'Decision letter available')
        self.assertEqual(rows[0]['title'], '')
        self.assertEqual(rows[0]['eic'], 'Editor, Example')


if __name__ == '__main__':
    unittest.main()
