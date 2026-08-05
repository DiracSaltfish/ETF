from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import ib_flex


SEND_SUCCESS = b"""<?xml version="1.0"?>
<FlexStatementResponse>
  <Status>Success</Status>
  <ReferenceCode>123456789</ReferenceCode>
</FlexStatementResponse>
"""

GET_PENDING = b"""<?xml version="1.0"?>
<FlexStatementResponse>
  <Status>Fail</Status>
  <ErrorCode>1019</ErrorCode>
  <ErrorMessage>Statement generation in progress.</ErrorMessage>
</FlexStatementResponse>
"""

REPORT_XML = b"""<?xml version="1.0"?>
<FlexQueryResponse>
  <FlexStatements count="1">
    <FlexStatement fromDate="20260701" toDate="20260702">
      <Trades>
        <Trade symbol="XOP" quantity="-100" tradePrice="140.25" />
        <Trade symbol="XOP" quantity="100" tradePrice="139.80" />
      </Trades>
      <BorrowFeeDetails>
        <BorrowFeeDetail symbol="XOP" quantity="100" borrowFee="1.23" />
      </BorrowFeeDetails>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>
"""

REPORT_XML_ORDER = b"""<?xml version="1.0"?>
<FlexQueryResponse>
  <FlexStatements count="1">
    <FlexStatement fromDate="20260701" toDate="20260702">
      <Trades>
        <Order symbol="XOP" quantity="-100" tradePrice="140.25" />
      </Trades>
      <HardToBorrowDetails>
        <HardToBorrowDetail symbol="XOP" quantity="100" borrowFee="1.23" />
      </HardToBorrowDetails>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>
"""


class FlexDownloadTests(unittest.TestCase):
    def test_load_token_prefers_environment_without_printing_value(self) -> None:
        with mock.patch.dict("os.environ", {"TEST_FLEX_TOKEN": "secret-value"}, clear=False):
            token, source = ib_flex.load_token(
                "TEST_FLEX_TOKEN",
                ib_flex.DEFAULT_KEYCHAIN_SERVICE,
                "tester",
            )
        self.assertEqual(token, "secret-value")
        self.assertIn("TEST_FLEX_TOKEN", source)
        self.assertNotIn(token, source)

    def test_download_uses_date_override_and_retries_generation(self) -> None:
        responses = iter((SEND_SUCCESS, GET_PENDING, REPORT_XML))
        calls: list[tuple[str, dict[str, str]]] = []
        sleeps: list[float] = []

        def request(endpoint, params, _user_agent, _timeout):
            calls.append((endpoint, dict(params)))
            return next(responses)

        payload = ib_flex.download_statement(
            "secret-token",
            "987654",
            date(2026, 7, 1),
            date(2026, 7, 2),
            poll_seconds=0.01,
            request_fn=request,
            sleep_fn=sleeps.append,
        )

        self.assertEqual(payload, REPORT_XML)
        self.assertEqual(calls[0][0], "SendRequest")
        self.assertEqual(calls[0][1]["fd"], "20260701")
        self.assertEqual(calls[0][1]["td"], "20260702")
        self.assertEqual(calls[1][1]["q"], "123456789")
        self.assertGreaterEqual(len(sleeps), 2)

    def test_summarize_and_save_xml_statement(self) -> None:
        summary = ib_flex.summarize_statement(REPORT_XML)
        self.assertEqual(summary.format, "xml")
        self.assertEqual(summary.from_date, "20260701")
        self.assertEqual(summary.to_date, "20260702")
        self.assertEqual(summary.trade_count, 2)
        self.assertEqual(summary.borrow_fee_count, 1)

        with tempfile.TemporaryDirectory() as directory:
            destination = ib_flex.save_statement(
                REPORT_XML,
                Path(directory),
                date(2026, 7, 1),
                date(2026, 7, 2),
            )
            self.assertEqual(destination.name, "ib_activity_20260701_20260702.xml")
            self.assertEqual(destination.read_bytes(), REPORT_XML)

    def test_summarize_current_order_and_hard_to_borrow_tags(self) -> None:
        summary = ib_flex.summarize_statement(REPORT_XML_ORDER)
        self.assertEqual(summary.trade_count, 1)
        self.assertEqual(summary.borrow_fee_count, 1)

    def test_rejects_more_than_365_days(self) -> None:
        with self.assertRaisesRegex(ValueError, "365"):
            ib_flex.validate_date_range(date(2025, 1, 1), date(2026, 1, 1))

    def test_non_retryable_service_error_is_sanitized(self) -> None:
        invalid_token = b"""<FlexStatementResponse>
          <Status>Fail</Status><ErrorCode>1015</ErrorCode><ErrorMessage>Token is invalid.</ErrorMessage>
        </FlexStatementResponse>"""

        with self.assertRaises(ib_flex.FlexError) as caught:
            ib_flex.download_statement(
                "do-not-leak-this-token",
                "987654",
                date(2026, 7, 1),
                date(2026, 7, 2),
                poll_seconds=0,
                request_fn=lambda *_args: invalid_token,
            )
        self.assertNotIn("do-not-leak-this-token", str(caught.exception))
        self.assertIn("1015", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
